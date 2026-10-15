import asyncio
import json

import httpx
import websockets

from app.integrations.onebot import (
    OneBotProtocolError,
    SnowLumaConfig,
    SnowLumaGateway,
)


def test_config_rejects_mismatched_mode_addresses() -> None:
    try:
        SnowLumaConfig("http", websocket_url="ws://127.0.0.1:1", http_url="http://127.0.0.1:2", token="x").validate()
    except OneBotProtocolError as exc:
        assert "websocket_url" in str(exc)
    else:
        raise AssertionError("mismatched HTTP configuration was accepted")


def test_http_action_uses_action_path_and_bearer_auth() -> None:
    asyncio.run(_exercise_http_gateway())


async def _exercise_http_gateway() -> None:
    requests: list[tuple[str, dict, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((str(request.url), json.loads(request.content), request.headers["Authorization"]))
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {"message_id": 9}})

    gateway = SnowLumaGateway(SnowLumaConfig("http", http_url="http://snowluma.test/api", token="test-token"))
    await gateway.start()
    await gateway._http.aclose()
    gateway._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    response = await gateway.call("send_private_msg", {"user_id": 42, "message": "ok"})
    assert response.data["message_id"] == 9
    assert requests == [("http://snowluma.test/api/send_private_msg", {"user_id": 42, "message": "ok"}, "Bearer test-token")]
    await gateway.stop()


def test_forward_websocket_correlates_concurrent_actions_and_events() -> None:
    asyncio.run(_exercise_forward_gateway())


def test_reverse_websocket_rejects_wrong_token() -> None:
    asyncio.run(_exercise_reverse_auth())


async def _exercise_reverse_auth() -> None:
    gateway = SnowLumaGateway(SnowLumaConfig("reverse_ws", websocket_url="ws://127.0.0.1:0/onebot/v11/ws", token="right"))
    await gateway.start()
    port = gateway._server.sockets[0].getsockname()[1]
    try:
        async with websockets.asyncio.client.connect(
            f"ws://127.0.0.1:{port}/onebot/v11/ws",
            additional_headers={"Authorization": "Bearer wrong"},
        ) as client:
            await asyncio.wait_for(client.wait_closed(), timeout=1)
        assert not gateway.connected
    finally:
        await gateway.stop()

async def _exercise_forward_gateway() -> None:
    received: list[dict] = []
    connected = asyncio.Event()

    async def server(connection) -> None:
        assert connection.request.headers["Authorization"] == "Bearer test-token"
        connected.set()
        await connection.send(json.dumps({
            "time": 1,
            "self_id": 100,
            "post_type": "message",
            "message_type": "private",
            "user_id": 42,
            "message": [{"type": "at", "data": {"qq": "100"}}, {"type": "reply", "data": {"id": 7}}, {"type": "image", "data": {"file": "x"}}],
        }))
        async for raw in connection:
            request = json.loads(raw)
            received.append(request)
            await connection.send(json.dumps({
                "status": "ok",
                "retcode": 0,
                "data": {"action": request["action"]},
                "echo": request["echo"],
            }))
            if len(received) == 2:
                return

    async with websockets.asyncio.server.serve(server, "127.0.0.1", 0) as server_socket:
        port = server_socket.sockets[0].getsockname()[1]
        gateway = SnowLumaGateway(SnowLumaConfig("forward_ws", websocket_url=f"ws://127.0.0.1:{port}", token="test-token"))
        await gateway.start()
        await gateway.wait_connected()
        event = await gateway.next_event(timeout=1)
        assert event.message_type == "private"
        assert event.segments[0]["type"] == "at"
        first, second = await asyncio.gather(
            gateway.call("get_login_info"),
            gateway.call("send_private_msg", {"user_id": 42, "message": "ok"}),
        )
        assert {first.data["action"], second.data["action"]} == {"get_login_info", "send_private_msg"}
        assert len({request["echo"] for request in received}) == 2
        await gateway.stop()

    assert not gateway.connected
