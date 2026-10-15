"""Provider boundary for Responses-compatible models, search, and embeddings.

The core talks to this module through typed requests and results. Upstream JSON is
never exposed as a contract object, and provider failures are mapped to stable
codes without copying response bodies into logs or error messages.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

import httpx

from app.contracts import Failure


class ProviderCapability(StrEnum):
    TEXT = "text"
    IMAGE_INPUT = "image_input"
    STREAMING = "streaming"
    STRUCTURED_OUTPUT = "structured_output"
    TOOL_CALLING = "tool_calling"
    NATIVE_WEB_SEARCH = "native_web_search"
    EMBEDDING = "embedding"
    EMBEDDING_BATCH = "embedding_batch"


class ProviderErrorCode(StrEnum):
    AUTHENTICATION = "provider.auth_failed"
    RATE_LIMITED = "provider.rate_limited"
    TIMEOUT = "provider.timeout"
    CONTENT_FILTERED = "provider.content_filtered"
    INVALID_REQUEST = "provider.invalid_request"
    UNAVAILABLE = "provider.unavailable"


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    base_url: str
    api_key: str
    responses_model: str
    embedding_model: str
    timeout_seconds: float = 30.0
    organization: str | None = None
    native_search_enabled: bool = False
    embedding_dimensions: int | None = None

    def __post_init__(self) -> None:
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("provider.base_url must use http:// or https://")
        if not self.api_key:
            raise ValueError("provider.api_key is required")
        if self.timeout_seconds <= 0:
            raise ValueError("provider.timeout_seconds must be positive")
        if self.embedding_dimensions is not None and self.embedding_dimensions <= 0:
            raise ValueError("provider.embedding_dimensions must be positive")


@dataclass(frozen=True, slots=True)
class ResponseRequest:
    input: str | list[dict[str, Any]]
    instructions: str | None = None
    model: str | None = None
    response_format: dict[str, Any] | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ResponseResult:
    id: str
    output_text: str
    output: list[dict[str, Any]]
    usage: dict[str, Any]
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class StreamEvent:
    event_type: str
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class EmbeddingItem:
    index: int
    vector: list[float]


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    model: str
    data: list[EmbeddingItem]
    dimensions: int
    usage: dict[str, Any]


class ProviderFailure(RuntimeError):
    """A provider failure safe to expose across the application boundary."""

    def __init__(self, failure: Failure) -> None:
        self.failure = failure
        super().__init__(failure.message or failure.error_code)


class ModelProvider(Protocol):
    capabilities: frozenset[ProviderCapability]

    async def responses(self, request: ResponseRequest, *, trace_id: str) -> ResponseResult: ...

    async def stream_responses(
        self, request: ResponseRequest, *, trace_id: str
    ) -> AsyncIterator[StreamEvent]: ...

    async def embeddings(self, inputs: list[str], *, trace_id: str) -> EmbeddingResult: ...


class SearchProvider(Protocol):
    async def search(self, query: str, *, trace_id: str) -> ResponseResult: ...


def _failure(trace_id: str, code: ProviderErrorCode, message: str, *, retryable: bool) -> ProviderFailure:
    return ProviderFailure(
        Failure(
            schema_version="v1",
            trace_id=trace_id,
            error_code=code,
            retryable=retryable,
            message=message,
        )
    )


class OpenAIResponsesProvider:
    """Minimal Responses-compatible adapter; no provider-specific fields cross it."""

    capabilities = frozenset(
        {
            ProviderCapability.TEXT,
            ProviderCapability.IMAGE_INPUT,
            ProviderCapability.STREAMING,
            ProviderCapability.STRUCTURED_OUTPUT,
            ProviderCapability.TOOL_CALLING,
            ProviderCapability.EMBEDDING,
            ProviderCapability.EMBEDDING_BATCH,
        }
    )

    def __init__(self, config: ProviderConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._client = client
        self._owns_client = client is None
        if config.native_search_enabled:
            self.capabilities = self.capabilities | {ProviderCapability.NATIVE_WEB_SEARCH}

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        if self.config.organization:
            headers["OpenAI-Organization"] = self.config.organization
        return headers

    def _url(self, path: str) -> str:
        return f"{self.config.base_url.rstrip('/')}/{path.lstrip('/')}"

    async def _request(self, method: str, path: str, payload: Mapping[str, Any], *, trace_id: str) -> dict[str, Any]:
        client = self._client or httpx.AsyncClient(timeout=self.config.timeout_seconds)
        try:
            try:
                response = await client.request(method, self._url(path), headers=self._headers(), json=payload)
            except httpx.TimeoutException as exc:
                raise _failure(trace_id, ProviderErrorCode.TIMEOUT, "provider request timed out", retryable=True) from exc
            except httpx.HTTPError as exc:
                raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider transport unavailable", retryable=True) from exc
            if response.status_code in {401, 403}:
                raise _failure(trace_id, ProviderErrorCode.AUTHENTICATION, "provider authentication failed", retryable=False)
            if response.status_code == 429:
                raise _failure(trace_id, ProviderErrorCode.RATE_LIMITED, "provider rate limit reached", retryable=True)
            if response.status_code in {408, 504}:
                raise _failure(trace_id, ProviderErrorCode.TIMEOUT, "provider request timed out", retryable=True)
            if response.status_code == 400:
                raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "provider rejected the request", retryable=False)
            if response.status_code == 422:
                raise _failure(trace_id, ProviderErrorCode.CONTENT_FILTERED, "provider filtered the content", retryable=False)
            if response.is_error:
                raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned an error", retryable=True)
            try:
                value = response.json()
            except ValueError as exc:
                raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned invalid JSON", retryable=True) from exc
            if not isinstance(value, dict):
                raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned an invalid response", retryable=True)
            return value
        finally:
            if self._owns_client:
                await client.aclose()

    async def responses(self, request: ResponseRequest, *, trace_id: str) -> ResponseResult:
        payload: dict[str, Any] = {"model": request.model or self.config.responses_model, "input": request.input}
        if request.instructions is not None:
            payload["instructions"] = request.instructions
        if request.response_format is not None:
            payload["text"] = {"format": request.response_format}
        if request.tools:
            payload["tools"] = request.tools
        value = await self._request("POST", "/responses", payload, trace_id=trace_id)
        return _parse_response(value)

    async def stream_responses(
        self, request: ResponseRequest, *, trace_id: str
    ) -> AsyncIterator[StreamEvent]:
        payload: dict[str, Any] = {
            "model": request.model or self.config.responses_model,
            "input": request.input,
            "stream": True,
        }
        if request.instructions is not None:
            payload["instructions"] = request.instructions
        if request.response_format is not None:
            payload["text"] = {"format": request.response_format}
        if request.tools:
            payload["tools"] = request.tools
        client = self._client or httpx.AsyncClient(timeout=self.config.timeout_seconds)
        try:
            try:
                async with client.stream("POST", self._url("/responses"), headers=self._headers(), json=payload) as response:
                    await _raise_for_status(response, trace_id)
                    async for line in response.aiter_lines():
                        if not line or line.startswith(":"):
                            continue
                        if line.startswith("data:"):
                            raw = line[5:].strip()
                            if raw == "[DONE]":
                                return
                            try:
                                data = json.loads(raw)
                            except json.JSONDecodeError as exc:
                                raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned invalid stream data", retryable=True) from exc
                            if isinstance(data, dict):
                                yield StreamEvent(str(data.get("type", "message")), data)
            except httpx.TimeoutException as exc:
                raise _failure(trace_id, ProviderErrorCode.TIMEOUT, "provider request timed out", retryable=True) from exc
            except httpx.HTTPError as exc:
                raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider transport unavailable", retryable=True) from exc
        finally:
            if self._owns_client:
                await client.aclose()

    async def embeddings(self, inputs: list[str], *, trace_id: str) -> EmbeddingResult:
        if not inputs:
            raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "embedding input cannot be empty", retryable=False)
        payload: dict[str, Any] = {"model": self.config.embedding_model, "input": inputs}
        if self.config.embedding_dimensions is not None:
            payload["dimensions"] = self.config.embedding_dimensions
        value = await self._request("POST", "/embeddings", payload, trace_id=trace_id)
        items = value.get("data")
        if not isinstance(items, list):
            raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned invalid embeddings", retryable=True)
        result: list[EmbeddingItem] = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
                raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned invalid embeddings", retryable=True)
            result.append(EmbeddingItem(int(item.get("index", len(result))), [float(x) for x in item["embedding"]]))
        dimensions = len(result[0].vector) if result else 0
        return EmbeddingResult(str(value.get("model", self.config.embedding_model)), result, dimensions, _dict(value.get("usage")))

    async def search(self, query: str, *, trace_id: str) -> ResponseResult:
        if not self.config.native_search_enabled:
            raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "native web search is not enabled", retryable=False)
        request = ResponseRequest(input=query, tools=[{"type": "web_search_preview"}])
        return await self.responses(request, trace_id=trace_id)


async def _raise_for_status(response: httpx.Response, trace_id: str) -> None:
    if response.status_code in {401, 403}:
        raise _failure(trace_id, ProviderErrorCode.AUTHENTICATION, "provider authentication failed", retryable=False)
    if response.status_code == 429:
        raise _failure(trace_id, ProviderErrorCode.RATE_LIMITED, "provider rate limit reached", retryable=True)
    if response.status_code in {408, 504}:
        raise _failure(trace_id, ProviderErrorCode.TIMEOUT, "provider request timed out", retryable=True)
    if response.status_code == 400:
        raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "provider rejected the request", retryable=False)
    if response.status_code == 422:
        raise _failure(trace_id, ProviderErrorCode.CONTENT_FILTERED, "provider filtered the content", retryable=False)
    if response.is_error:
        raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned an error", retryable=True)




def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _parse_response(value: dict[str, Any]) -> ResponseResult:
    output = value.get("output")
    output_items = output if isinstance(output, list) else []
    tool_calls = [item for item in output_items if isinstance(item, dict) and item.get("type") in {"function_call", "tool_call"}]
    citations: list[str] = []
    for item in output_items:
        if not isinstance(item, dict):
            continue
        for annotation in item.get("annotations", []):
            if isinstance(annotation, dict) and isinstance(annotation.get("url"), str):
                citations.append(annotation["url"])
    return ResponseResult(
        id=str(value.get("id", "")),
        output_text=str(value.get("output_text", _extract_text(output_items))),
        output=[item for item in output_items if isinstance(item, dict)],
        usage=_dict(value.get("usage")),
        tool_calls=tool_calls,
        citations=citations,
    )


def _extract_text(items: list[Any]) -> str:
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "".join(parts)


def redact_provider_payload(value: Any) -> Any:
    """Return evidence-safe JSON with credentials and sensitive headers removed."""
    secret_names = {"api_key", "authorization", "cookie", "password", "token", "secret"}
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if str(key).lower() in secret_names else redact_provider_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_provider_payload(item) for item in value]
    return value


async def build_provider(session: Any) -> OpenAIResponsesProvider:
    """Construct a provider from PostgreSQL configuration, never from environment secrets."""
    from app.repository import read_provider_runtime_settings

    values = await read_provider_runtime_settings(session)
    return OpenAIResponsesProvider(ProviderConfig(**values))
