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
        return httpx.Response(200, json={"id": "resp-1", "output_text": '{"answer":"ok"}', "output": [{"type": "function_call", "name": "lookup", "arguments": "{}"}]})

    async def run() -> None:
        provider, client = _provider(handler)
        try:
            result = await provider.responses(
                ResponseRequest(
                    input=[{"role": "user", "content": [{"type": "input_text", "text": "describe"}, {"type": "input_image", "image_url": "data:image/png;base64,AA=="}]}],
                    response_format={"type": "json_schema", "name": "answer", "schema": {"type": "object"}},
                    tools=[{"type": "function", "name": "lookup", "parameters": {"type": "object"}}],
                ),
                trace_id="trace-1",
            )
            assert result.output_text == '{"answer":"ok"}'
            assert result.tool_calls[0]["name"] == "lookup"
            embedding = await provider.embeddings(["alpha", "beta"], trace_id="trace-2")
            assert embedding.dimensions == 2
            assert len(embedding.data) == 2
        finally:
            await client.aclose()

    asyncio.run(run())


def test_streaming_native_search_and_capability_matrix() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload.get("stream"):
            return httpx.Response(200, content=b'data: {"type":"response.output_text.delta","delta":"hi"}\n\ndata: [DONE]\n\n', headers={"content-type": "text/event-stream"})
        assert payload["tools"] == [{"type": "web_search_preview"}]
        return httpx.Response(200, json={"id": "search-1", "output_text": "answer", "output": [{"type": "web_search_call", "annotations": [{"url": "https://example.test/result"}]}]})

    async def run() -> None:
        provider, client = _provider(handler, search=True)
        try:
            events = [event async for event in provider.stream_responses(ResponseRequest(input="hello"), trace_id="trace-stream")]
            assert events[0].event_type == "response.output_text.delta"
            result = await provider.search("query", trace_id="trace-search")
            assert result.citations == ["https://example.test/result"]
            assert ProviderCapability.NATIVE_WEB_SEARCH in provider.capabilities
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [(401, ProviderErrorCode.AUTHENTICATION, False), (429, ProviderErrorCode.RATE_LIMITED, True), (504, ProviderErrorCode.TIMEOUT, True)],
)
def test_provider_errors_are_stable_and_do_not_echo_upstream_body(status, code, retryable) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text='{"error":"secret upstream detail"}')

    async def run() -> None:
        provider, client = _provider(handler)
        try:
            with pytest.raises(ProviderFailure) as raised:
                await provider.responses(ResponseRequest(input="hello"), trace_id="trace-error")
            assert raised.value.failure.error_code == code
            assert raised.value.failure.retryable is retryable
            assert "secret upstream" not in str(raised.value)
        finally:
            await client.aclose()

    asyncio.run(run())


def test_provider_evidence_redacts_credentials() -> None:
    payload = redact_provider_payload({"Authorization": "Bearer secret", "api_key": "secret", "nested": {"token": "secret"}, "value": "kept"})
    assert payload == {"Authorization": "[REDACTED]", "api_key": "[REDACTED]", "nested": {"token": "[REDACTED]"}, "value": "kept"}
