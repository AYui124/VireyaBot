import asyncio
import json

import httpx
import pytest

from app.providers import (
    OpenAIResponsesProvider,
    ProviderCapability,
    ProviderConfig,
    ProviderErrorCode,
    ProviderFailure,
    ResponseRequest,
    redact_provider_payload,
)


def _provider(handler, *, search=False):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAIResponsesProvider(
        ProviderConfig(
            base_url="https://provider.invalid/v1",
            api_key="synthetic-key",
            responses_model="synthetic-responses",
            embedding_model="synthetic-embedding",
            native_search_enabled=search,
        ),
        client=client,
    ), client


def test_responses_image_structured_tool_and_embedding_protocols() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer synthetic-key"
        payload = json.loads(request.content)
        if request.url.path.endswith("/embeddings"):
            assert payload["input"] == ["alpha", "beta"]
            return httpx.Response(200, json={"model": "synthetic-embedding", "data": [{"index": 0, "embedding": [0.1, 0.2]}, {"index": 1, "embedding": [0.3, 0.4]}], "usage": {"total_tokens": 2}})
        assert payload["model"] == "synthetic-responses"
        assert payload["text"]["format"]["type"] == "json_schema"
        assert payload["tools"][0]["type"] == "function"
        assert payload["input"][0]["content"][1]["type"] == "input_image"
        return httpx.Response(200, json={"id": "resp-1", "status": "completed", "output_text": '{"answer":"ok"}', "output": [{"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": "{}"}]})

    async def run() -> None:
        provider, client = _provider(handler)
        try:
            result = await provider.responses(ResponseRequest(input=[{"role": "user", "content": [{"type": "input_text", "text": "describe"}, {"type": "input_image", "image_url": "data:image/png;base64,AA=="}]}], response_format={"type": "json_schema", "name": "answer", "schema": {"type": "object"}}, tools=[{"type": "function", "name": "lookup", "parameters": {"type": "object"}}]), trace_id="trace-1")
            assert result.schema_version == "v1"
            assert result.trace_id == "trace-1"
            assert result.output_text == '{"answer":"ok"}'
            assert result.tool_calls[0].name == "lookup"
            embedding = await provider.embeddings(["alpha", "beta"], trace_id="trace-2")
            assert embedding.schema_version == "v1"
            assert embedding.trace_id == "trace-2"
            assert embedding.dimensions == 2
            assert len(embedding.data) == 2
        finally:
            await client.aclose()

    asyncio.run(run())


def test_streaming_native_search_and_nested_url_citations() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload.get("stream"):
            body = 'data: {"type":"response.created"}\n\ndata: {"type":"response.output_text.delta","delta":"hi"}\n\ndata: {"type":"response.completed","response":{"status":"completed"}}\n\ndata: [DONE]\n\n'
            return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})
        assert payload["tools"] == [{"type": "web_search_preview"}]
        return httpx.Response(200, json={"id": "search-1", "status": "completed", "output_text": "answer", "output": [{"type": "message", "content": [{"type": "output_text", "text": "answer", "annotations": [{"type": "url_citation", "url": "https://example.test/result", "title": "Result"}]}]}]})

    async def run() -> None:
        provider, client = _provider(handler, search=True)
        try:
            events = [event async for event in provider.stream_responses(ResponseRequest(input="hello"), trace_id="trace-stream")]
            assert events[0].schema_version == "v1"
            assert events[0].trace_id == "trace-stream"
            assert events[0].delta == "hi"
            result = await provider.search("query", trace_id="trace-search")
            assert result.citations == ["https://example.test/result"]
            matrix = {item.capability: item for item in provider.capability_matrix()}
            assert matrix[ProviderCapability.NATIVE_WEB_SEARCH].enabled is True
            assert all(item.endpoint_verified is False for item in matrix.values())
            assert ProviderCapability.NATIVE_WEB_SEARCH in provider.capabilities
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("status_code", [401, 429, 504])
def test_provider_http_errors_are_stable_and_do_not_echo_upstream_body(status_code) -> None:
    expected = {401: (ProviderErrorCode.AUTHENTICATION, False), 429: (ProviderErrorCode.RATE_LIMITED, True), 504: (ProviderErrorCode.TIMEOUT, True)}[status_code]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text='{"error":"secret upstream detail"}')

    async def run() -> None:
        provider, client = _provider(handler)
        try:
            with pytest.raises(ProviderFailure) as raised:
                await provider.responses(ResponseRequest(input="hello"), trace_id="trace-error")
            assert raised.value.failure.error_code == expected[0]
            assert raised.value.failure.retryable is expected[1]
            assert raised.value.failure.trace_id == "trace-error"
            assert "secret upstream" not in str(raised.value)
        finally:
            await client.aclose()

    asyncio.run(run())


def test_422_classification_requires_filter_signal() -> None:
    bodies = [("invalid", {"error": {"code": "invalid_request"}}, ProviderErrorCode.INVALID_REQUEST), ("filtered", {"error": {"type": "content_filter"}}, ProviderErrorCode.CONTENT_FILTERED)]

    async def run() -> None:
        for _, body, expected in bodies:
            async def handler(request: httpx.Request, body=body) -> httpx.Response:
                return httpx.Response(422, json=body)
            provider, client = _provider(handler)
            try:
                with pytest.raises(ProviderFailure) as raised:
                    await provider.responses(ResponseRequest(input="hello"), trace_id="trace-422")
                assert raised.value.failure.error_code == expected
            finally:
                await client.aclose()

    asyncio.run(run())


def test_actual_transport_timeout_maps_to_timeout_failure() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic network timeout", request=request)

    async def run() -> None:
        provider, client = _provider(handler)
        try:
            with pytest.raises(ProviderFailure) as raised:
                await provider.responses(ResponseRequest(input="hello"), trace_id="trace-timeout")
            assert raised.value.failure.error_code == ProviderErrorCode.TIMEOUT
            assert raised.value.failure.trace_id == "trace-timeout"
        finally:
            await client.aclose()

    asyncio.run(run())


def test_failed_responses_and_failed_sse_are_redacted_failures() -> None:
    async def response_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "resp-failed", "status": "failed", "error": {"message": "private upstream error"}})

    async def stream_handler(request: httpx.Request) -> httpx.Response:
        body = 'data: {"type":"response.failed","response":{"status":"failed","error":{"message":"private stream error"}}}\n\n'
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    async def run() -> None:
        provider, client = _provider(response_handler)
        try:
            with pytest.raises(ProviderFailure) as raised:
                await provider.responses(ResponseRequest(input="hello"), trace_id="trace-response-failed")
            assert raised.value.failure.error_code == ProviderErrorCode.UNAVAILABLE
            assert "private upstream" not in str(raised.value)
        finally:
            await client.aclose()
        provider, client = _provider(stream_handler)
        try:
            with pytest.raises(ProviderFailure) as raised:
                _ = [event async for event in provider.stream_responses(ResponseRequest(input="hello"), trace_id="trace-stream-failed")]
            assert raised.value.failure.trace_id == "trace-stream-failed"
            assert "private stream" not in str(raised.value)
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("data", [[{"index": 0, "embedding": [0.1]}, {"index": 0, "embedding": [0.2]}], [{"index": 0, "embedding": [0.1, 0.2]}, {"index": 1, "embedding": [0.3]}], [{"index": 0, "embedding": [float("nan")]}, {"index": 1, "embedding": [0.2]}]])
def test_malformed_embedding_batches_fail_closed(data) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps({"model": "synthetic-embedding", "data": data}, allow_nan=True).encode())

    async def run() -> None:
        provider, client = _provider(handler)
        try:
            with pytest.raises(ProviderFailure) as raised:
                await provider.embeddings(["alpha", "beta"], trace_id="trace-embedding-invalid")
            assert raised.value.failure.error_code == ProviderErrorCode.UNAVAILABLE
            assert raised.value.failure.trace_id == "trace-embedding-invalid"
        finally:
            await client.aclose()

    asyncio.run(run())


def test_provider_evidence_redacts_credentials() -> None:
    payload = redact_provider_payload({"Authorization": "Bearer secret", "api_key": "secret", "nested": {"token": "secret"}, "value": "kept"})
    assert payload == {"Authorization": "[REDACTED]", "api_key": "[REDACTED]", "nested": {"token": "[REDACTED]"}, "value": "kept"}
