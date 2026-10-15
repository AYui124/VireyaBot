"""Narrow OneBot v11 gateway for an externally managed SnowLuma instance.

The gateway owns transport, authentication, action correlation, reconnects, and
untrusted media checks. Agent/business code should consume ``OneBotEvent`` and
call ``call``; it must not receive the raw transport object.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
import websockets
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import WebSocketException

MASKED_TOKEN = "••••••••"
DEFAULT_MEDIA_LIMIT = 10 * 1024 * 1024


class OneBotError(RuntimeError):
    """Base class for gateway failures."""


class OneBotNotConnected(OneBotError):
    """Raised when an action is attempted before the gateway is connected."""


class OneBotProtocolError(OneBotError):
    """Raised for invalid configuration or malformed action responses."""


@dataclass(frozen=True, slots=True)
class SnowLumaConfig:
    connection_mode: str
    websocket_url: str = ""
    http_url: str = ""
    token: str = ""

    @classmethod
    def from_settings(cls, values: Mapping[str, Any]) -> SnowLumaConfig:
        config = cls(
            connection_mode=str(values.get("connection_mode", "")),
            websocket_url=str(values.get("websocket_url", "")),
            http_url=str(values.get("http_url", "")),
            token="" if values.get("token") in (None, MASKED_TOKEN) else str(values.get("token", "")),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.connection_mode not in {"reverse_ws", "forward_ws", "http"}:
            raise OneBotProtocolError("connection_mode must be reverse_ws, forward_ws, or http")
        if self.connection_mode == "http":
            _validate_url(self.http_url, {"http", "https"}, "http_url")
            if self.websocket_url:
                raise OneBotProtocolError("websocket_url must be empty in HTTP mode")
        else:
            _validate_url(self.websocket_url, {"ws", "wss"}, "websocket_url")
            if self.http_url:
                raise OneBotProtocolError("http_url must be empty in WebSocket mode")
        if not self.token:
            raise OneBotProtocolError("SnowLuma token is required for a connection")


@dataclass(frozen=True, slots=True)
class OneBotEvent:
    post_type: str
    raw: Mapping[str, Any]

    @property
    def message_type(self) -> str | None:
        value = self.raw.get("message_type")
        return value if isinstance(value, str) else None

    @property
    def user_id(self) -> str | int | None:
        return self.raw.get("user_id")

    @property
    def group_id(self) -> str | int | None:
        return self.raw.get("group_id")

    @property
    def segments(self) -> tuple[Mapping[str, Any], ...]:
        message = self.raw.get("message")
        if not isinstance(message, list):
            return ()
        return tuple(segment for segment in message if isinstance(segment, Mapping))


@dataclass(frozen=True, slots=True)
class OneBotResponse:
    status: str
    retcode: int
    data: Mapping[str, Any]
    echo: str


class SnowLumaGateway:
    """Explicit-lifecycle OneBot v11 client/server boundary.

    ``start`` is never called by configuration persistence or application
    startup. WebSocket modes reconnect after an established connection drops;
    ``stop`` cancels that loop and fails all pending actions.
    """

    def __init__(
        self,
        config: SnowLumaConfig,
        *,
        media_limit: int = DEFAULT_MEDIA_LIMIT,
        request_timeout: float = 10.0,
    ) -> None:
        config.validate()
        if media_limit <= 0:
            raise ValueError("media_limit must be positive")
        self.config = config
        self.media_limit = media_limit
        self.request_timeout = request_timeout
        self._stop = asyncio.Event()
        self._connected = asyncio.Event()
        self._events: asyncio.Queue[OneBotEvent] = asyncio.Queue()
        self._pending: dict[str, asyncio.Future[OneBotResponse]] = {}
        self._counter = 0
        self._runner: asyncio.Task[None] | None = None
        self._started = False
        self._server: websockets.asyncio.server.Server | None = None
        self._connection: ClientConnection | ServerConnection | None = None
        self._http: httpx.AsyncClient | None = None

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop.clear()
        if self.config.connection_mode == "http":
            self._http = httpx.AsyncClient(timeout=self.request_timeout, follow_redirects=False)
            self._connected.set()
            return
        if self.config.connection_mode == "reverse_ws":
            parsed = urlparse(self.config.websocket_url)
            self._server = await websockets.asyncio.server.serve(
                self._accept_reverse,
                parsed.hostname,
                parsed.port,
                max_size=self.media_limit,
            )
            return
        self._runner = asyncio.create_task(self._run_websocket(), name="snowluma-onebot")

    async def stop(self) -> None:
        self._stop.set()
        if self._runner:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
            self._runner = None
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await self._close_connection(OneBotError("gateway stopped"))
        if self._http:
            await self._http.aclose()
            self._http = None
        self._connected.clear()
        self._started = False

    async def wait_connected(self, timeout: float = 10.0) -> None:
        await asyncio.wait_for(self._connected.wait(), timeout)

    async def next_event(self, timeout: float | None = None) -> OneBotEvent:
        if timeout is None:
            return await self._events.get()
        return await asyncio.wait_for(self._events.get(), timeout)

    async def events(self) -> AsyncIterator[OneBotEvent]:
        while not self._stop.is_set():
            yield await self._events.get()

    async def call(self, action: str, params: Mapping[str, Any] | None = None) -> OneBotResponse:
        if not action:
            raise ValueError("action must not be empty")
        payload = {"action": action, "params": dict(params or {})}
        if self.config.connection_mode == "http":
            if not self._http:
                raise OneBotNotConnected("gateway has not been started")
            response = await self._http.post(
                f"{self.config.http_url.rstrip('/')}/{action}",
                json=dict(params or {}),
                headers=self._auth_headers(),
            )
            response.raise_for_status()
            return self._parse_response(response.json(), action)
        if not self._connected.is_set() or self._connection is None:
            raise OneBotNotConnected("WebSocket is not connected")
        self._counter += 1
        echo = f"vireyabot-{self._counter}-{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[OneBotResponse] = loop.create_future()
        self._pending[echo] = future
        try:
            await self._connection.send(json.dumps({**payload, "echo": echo}, separators=(",", ":")))
            return await future
        finally:
            self._pending.pop(echo, None)

    async def get_image(self, file: str) -> OneBotResponse:
        return await self.call("get_image", {"file": file})

    async def fetch_media(self, url: str) -> bytes:
        """Fetch a returned image URL without redirects or private-network access."""
        _validate_remote_url(url)
        async with httpx.AsyncClient(timeout=self.request_timeout, follow_redirects=False) as client:
            response = await client.get(url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if not content_type.startswith("image/"):
                raise OneBotProtocolError("remote media is not an image")
            if len(response.content) > self.media_limit:
                raise OneBotProtocolError("remote media exceeds configured size limit")
            return response.content

    async def _run_websocket(self) -> None:
        delay = 0.25
        while not self._stop.is_set():
            try:
                if self.config.connection_mode == "forward_ws":
                    parsed = urlparse(self.config.websocket_url)
                    async with websockets.asyncio.client.connect(
                        self.config.websocket_url,
                        additional_headers=self._auth_headers(),
                        max_size=self.media_limit,
                    ) as connection:
                        await self._serve_connection(connection, parsed.path or "/")
                else:
                    await self._connected.wait()
                    await self._serve_connection(self._connection, urlparse(self.config.websocket_url).path or "/")
                delay = 0.25
            except asyncio.CancelledError:
                raise
            except (WebSocketException, OSError, TimeoutError, OneBotError) as exc:
                await self._close_connection(exc)
                if not self._stop.is_set():
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 5.0)

    async def _serve_connection(self, connection: ClientConnection | ServerConnection | None, _: str) -> None:
        if connection is None:
            return
        self._connection = connection
        self._connected.set()
        try:
            async for raw in connection:
                message = json.loads(raw)
                if not isinstance(message, Mapping):
                    raise OneBotProtocolError("OneBot frame must be an object")
                echo = message.get("echo")
                if echo is not None and echo in self._pending:
                    self._pending[echo].set_result(self._parse_response(message, str(echo)))
                elif isinstance(message.get("post_type"), str):
                    await self._events.put(OneBotEvent(message["post_type"], message))
        finally:
            await self._close_connection(OneBotError("WebSocket disconnected"))

    async def _accept_reverse(self, connection: ServerConnection) -> None:
        auth = connection.request.headers.get("Authorization", "")
        if auth != self._auth_headers()["Authorization"]:
            await connection.close(code=1008, reason="invalid token")
            return
        if self._connection is not None:
            await self._close_connection(OneBotError("replaced by a new connection"))
        await self._serve_connection(connection, urlparse(self.config.websocket_url).path or "/")

    async def _close_connection(self, error: BaseException) -> None:
        self._connected.clear()
        connection, self._connection = self._connection, None
        if connection is not None:
            await connection.close()
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.token}"}

    @staticmethod
    def _parse_response(payload: Any, echo: str) -> OneBotResponse:
        if not isinstance(payload, Mapping):
            raise OneBotProtocolError("OneBot response must be an object")
        status = payload.get("status")
        retcode = payload.get("retcode")
        data = payload.get("data", {})
        if not isinstance(status, str) or not isinstance(retcode, int) or not isinstance(data, Mapping):
            raise OneBotProtocolError("invalid OneBot response shape")
        return OneBotResponse(status, retcode, data, echo)


def _validate_url(value: str, schemes: set[str], field: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password:
        raise OneBotProtocolError(f"{field} must be a valid {'/'.join(sorted(schemes))} URL")
    if parsed.fragment:
        raise OneBotProtocolError(f"{field} must not contain a fragment")


def _validate_remote_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise OneBotProtocolError("media URL must use authenticated-free HTTP(S)")
    for address in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM):
        ip = ipaddress.ip_address(address[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            raise OneBotProtocolError("media URL resolves to a private or reserved address")
