import asyncio
import io
import json
import logging
import secrets
import traceback

import httpx
import pytest

from app.providers import (
    AnthropicMessagesProvider,
    OpenAIChatCompletionsProvider,
    OpenAIResponsesProvider,
    ProviderCapability,
    ProviderConfig,
    ProviderErrorCode,
    ProviderFailure,
    ResponseRequest,
    redact_provider_payload,
)


def _provider(handler, *, search=False, embedding_dimensions=None):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAIResponsesProvider(
        ProviderConfig(
            base_url="https://provider.invalid/v1",
            api_key="synthetic-key",
            responses_model="synthetic-responses",
            embedding_model="synthetic-embedding",
            embedding_dimensions=embedding_dimensions,
            native_search_enabled=search,
        ),
        client=client,
    ), client

def _provider_for(provider_type, handler, **settings):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = {"base_url": "https://provider.invalid/v1", "api_key": "synthetic-key", "responses_model": "synthetic-responses", "embedding_model": "synthetic-embedding"} | settings
    return provider_type(ProviderConfig(**config), client=client), client


def test_responses_image_structured_tool_and_embedding_protocols() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer synthetic-key"
        payload = json.loads(request.content)
        if request.url.path.endswith("/embeddings"):
            assert payload["input"] == ["alpha", "beta"]
            return httpx.Response(200, json={"model": "synthetic-embedding", "data": [{"index": 0, "embedding": [0.1, 0.2]}, {"index": 1, "embedding": [0.3, 0.4]}], "usage": {"total_tokens": 2}})
        assert payload["model"] == "synthetic-responses"
        if isinstance(payload["input"], list) and payload["input"] and payload["input"][0].get("type") == "function_call_output":
            assert payload["input"][0] == {"type": "function_call_output", "call_id": "call-1", "output": "tool result"}
            return httpx.Response(200, json={"id": "resp-tool", "status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "tool answer"}]}]})
        assert payload["text"]["format"]["type"] == "json_schema"
        assert payload["tools"][0]["type"] == "function"
        assert payload["input"][0]["content"][1]["type"] == "input_image"
        assert payload["input"][0]["content"][1]["image_url"].startswith("data:image/png;base64,iVBOR")
        return httpx.Response(200, json={"id": "resp-1", "status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "standard answer"}]}, {"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": "{}"}]})

    async def run() -> None:
        provider, client = _provider(handler)
        try:
            result = await provider.responses(ResponseRequest(input=[{"role": "user", "content": [{"type": "input_text", "text": "describe"}, {"type": "input_image", "image_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="}]}], response_format={"type": "json_schema", "name": "answer", "schema": {"type": "object"}}, tools=[{"type": "function", "name": "lookup", "parameters": {"type": "object"}}]), trace_id="trace-1")
            assert result.schema_version == "v1"
            assert result.trace_id == "trace-1"
            assert result.output_text == "standard answer"
            assert result.tool_calls[0].name == "lookup"
            tool_result = await provider.responses(ResponseRequest(input=[{"type": "function_call_output", "call_id": "call-1", "output": "tool result"}]), trace_id="trace-tool-output")
            assert tool_result.output_text == "tool answer"
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
            return _sse_response(OpenAIResponsesProvider, _stream_events(OpenAIResponsesProvider, text="hi", call_id="call-stream-1"))
        assert payload["tools"] == [{"type": "web_search_preview"}]
        return httpx.Response(200, json={"id": "search-1", "status": "completed", "output_text": "answer", "output": [{"type": "message", "content": [{"type": "output_text", "text": "answer", "annotations": [{"type": "url_citation", "url": "https://example.test/result", "title": "Result"}]}]}]})

    async def run() -> None:
        provider, client = _provider(handler, search=True)
        try:
            events = [event async for event in provider.stream_responses(ResponseRequest(input="hello"), trace_id="trace-stream")]
            assert events[0].schema_version == "v1"
            assert events[0].trace_id == "trace-stream"
            assert any(event.delta == "hi" for event in events)
            assert any(event.output is not None and event.output.call_id == "call-stream-1" for event in events)
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


def test_completed_refusal_is_a_stable_failure() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "refused", "status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "private reason"}]}]})

    async def run() -> None:
        provider, client = _provider(handler)
        try:
            with pytest.raises(ProviderFailure) as raised:
                await provider.responses(ResponseRequest(input="hello"), trace_id="trace-refusal")
            assert raised.value.failure.error_code == ProviderErrorCode.REFUSED
            assert raised.value.failure.trace_id == "trace-refusal"
            assert "private reason" not in str(raised.value)
        finally:
            await client.aclose()

    asyncio.run(run())


def test_unknown_request_version_and_conflicting_trace_do_not_call_upstream() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"status": "completed", "output": []})

    async def run() -> None:
        nonlocal calls
        with pytest.raises(ValueError, match="unsupported provider schema version"):
            ResponseRequest(input="hello", schema_version="v999")
        provider, client = _provider(handler)
        try:
            with pytest.raises(ProviderFailure) as raised:
                await provider.responses(ResponseRequest(input="hello", trace_id="conflicting-trace"), trace_id="call-trace")
            assert raised.value.failure.error_code == ProviderErrorCode.INVALID_REQUEST
            assert calls == 0
        finally:
            await client.aclose()

    asyncio.run(run())


def test_embedding_dimensions_must_match_configuration() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "synthetic-embedding", "data": [{"index": 0, "embedding": [0.1, 0.2]}]})

    async def run() -> None:
        provider, client = _provider(handler, embedding_dimensions=3)
        try:
            with pytest.raises(ProviderFailure) as raised:
                await provider.embeddings(["alpha"], trace_id="trace-dimension")
            assert raised.value.failure.error_code == ProviderErrorCode.UNAVAILABLE
            assert raised.value.failure.trace_id == "trace-dimension"
        finally:
            await client.aclose()

    asyncio.run(run())


def test_streaming_422_is_classified_without_response_not_read() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error": {"code": "invalid_request"}})

    async def run() -> None:
        provider, client = _provider(handler)
        try:
            with pytest.raises(ProviderFailure) as raised:
                _ = [event async for event in provider.stream_responses(ResponseRequest(input="hello"), trace_id="trace-stream-422")]
            assert raised.value.failure.error_code == ProviderErrorCode.INVALID_REQUEST
            assert raised.value.failure.trace_id == "trace-stream-422"
        finally:
            await client.aclose()

    asyncio.run(run())


def test_provider_evidence_redacts_credentials() -> None:
    payload = redact_provider_payload({"Authorization": "Bearer secret", "api_key": "secret", "nested": {"token": "secret"}, "value": "kept"})
    assert payload == {"Authorization": "[REDACTED]", "api_key": "[REDACTED]", "nested": {"token": "[REDACTED]"}, "value": "kept"}

def test_chat_stream_orders_usage_before_completion() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(OpenAIChatCompletionsProvider, _stream_events(OpenAIChatCompletionsProvider, text="A"))

    async def run() -> None:
        provider, client = _provider_for(OpenAIChatCompletionsProvider, handler)
        try:
            events = [event async for event in provider.stream_responses(ResponseRequest(input="hello"), trace_id="chat-stream")]
            types = [event.event_type for event in events]
            assert types.index("usage") < types.index("response.completed")
            assert [event.delta for event in events if event.delta] == ["A"]
            assert events[-1].response_id == "response-1"
        finally:
            await client.aclose()

    asyncio.run(run())


def test_anthropic_stream_keeps_input_usage_and_tool_identity() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(AnthropicMessagesProvider, _stream_events(AnthropicMessagesProvider, call_id="tool-1"))

    async def run() -> None:
        provider, client = _provider_for(AnthropicMessagesProvider, handler)
        try:
            events = [event async for event in provider.stream_responses(ResponseRequest(input="hello"), trace_id="anthropic-stream")]
            usage = next(event for event in events if event.event_type == "usage")
            assert usage.usage == {"input_tokens": 3, "output_tokens": 1}
            completed = next(event for event in events if event.event_type == "response.tool_call.completed")
            assert completed.output.call_id == "tool-1"
            assert events[-1].event_type == "response.completed"
        finally:
            await client.aclose()

    asyncio.run(run())


PROVIDERS = [OpenAIResponsesProvider, OpenAIChatCompletionsProvider, AnthropicMessagesProvider]
ARGUMENTS = '{"q":"hello"}'


def _response_body(provider_type, *, text="answer", call_id="call-1"):
    if provider_type is OpenAIResponsesProvider:
        return {"id": "response-1", "object": "response", "status": "completed", "model": "synthetic-responses", "output": [{"type": "message", "id": "message-1", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": text, "annotations": []}]}, {"type": "function_call", "id": "item-1", "call_id": call_id, "name": "lookup", "arguments": ARGUMENTS, "status": "completed"}], "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4}}
    if provider_type is OpenAIChatCompletionsProvider:
        return {"id": "response-1", "object": "chat.completion", "created": 1, "model": "synthetic-responses", "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "content": text, "tool_calls": [{"type": "function", "id": call_id, "function": {"name": "lookup", "arguments": ARGUMENTS}}]}}], "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}
    return {"id": "response-1", "type": "message", "role": "assistant", "model": "synthetic-responses", "content": [{"type": "text", "text": text}, {"type": "tool_use", "id": call_id, "name": "lookup", "input": {"q": "hello"}}], "stop_reason": "tool_use", "stop_sequence": None, "usage": {"input_tokens": 3, "output_tokens": 1}}


def _chat_chunk(delta, *, finish_reason=None, usage=None):
    value = {"id": "response-1", "object": "chat.completion.chunk", "created": 1, "model": "synthetic-responses", "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
    if usage is not None:
        value.update(choices=[], usage=usage)
    return value


def _stream_events(provider_type, *, text="answer", call_id="call-1"):
    response = _response_body(provider_type, text=text, call_id=call_id)
    if provider_type is OpenAIResponsesProvider:
        message, tool = response["output"]
        events = [
            {"type": "response.created", "response": response | {"status": "in_progress", "output": [], "usage": None}},
            {"type": "response.output_item.added", "output_index": 0, "item": message | {"status": "in_progress", "content": []}},
            {"type": "response.output_text.delta", "item_id": "message-1", "output_index": 0, "content_index": 0, "delta": text},
            {"type": "response.output_item.done", "output_index": 0, "item": message},
            {"type": "response.output_item.added", "output_index": 1, "item": tool | {"arguments": "", "status": "in_progress"}},
            {"type": "response.function_call_arguments.delta", "item_id": "item-1", "output_index": 1, "delta": '{"q":'},
            {"type": "response.function_call_arguments.delta", "item_id": "item-1", "output_index": 1, "delta": '"hello"}'},
            {"type": "response.function_call_arguments.done", "item_id": "item-1", "output_index": 1, "arguments": ARGUMENTS},
            {"type": "response.output_item.done", "output_index": 1, "item": tool},
            {"type": "response.completed", "response": response},
        ]
        return [event | {"sequence_number": index} for index, event in enumerate(events)]
    if provider_type is OpenAIChatCompletionsProvider:
        return [
            _chat_chunk({"role": "assistant", "content": text}),
            _chat_chunk({"tool_calls": [{"index": 0, "id": call_id, "type": "function", "function": {"name": "lookup", "arguments": ""}}]}),
            _chat_chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"q":'}}]}),
            _chat_chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"hello"}'}}]}),
            _chat_chunk({}, finish_reason="tool_calls"),
            _chat_chunk({}, usage=response["usage"]),
        ]
    return [
        {"type": "message_start", "message": response | {"content": [], "stop_reason": None, "usage": {"input_tokens": 3, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": call_id, "name": "lookup", "input": {}}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"q":'}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '"hello"}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]


def _sse_bytes(provider_type, events, *, done=True):
    body = "".join((f'event: {event["type"]}\n' if provider_type is AnthropicMessagesProvider else "") + "data: " + json.dumps(event) + "\n\n" for event in events)
    if done and provider_type is OpenAIChatCompletionsProvider:
        body += "data: [DONE]\n\n"
    return body.encode()


def _sse_response(provider_type, events):
    return httpx.Response(200, content=_sse_bytes(provider_type, events), headers={"content-type": "text/event-stream"})


@pytest.mark.parametrize("provider_type", PROVIDERS)
@pytest.mark.parametrize("default_stream", [False, True])
@pytest.mark.parametrize("override", [None, False, True])
def test_stream_override_and_aggregation_keep_the_full_contract(provider_type, default_stream, override):
    streamed = default_stream if override is None else override

    async def handler(request):
        payload = json.loads(request.content)
        assert payload["stream"] is streamed
        assert payload.get("max_completion_tokens", payload.get("max_tokens", payload.get("max_output_tokens"))) == 123
        assert payload["temperature"] == 0.4
        if provider_type is not AnthropicMessagesProvider:
            assert request.headers["OpenAI-Project"] == "project-1"
        if streamed:
            return _sse_response(provider_type, _stream_events(provider_type))
        return httpx.Response(200, json=_response_body(provider_type))

    async def run():
        provider, client = _provider_for(provider_type, handler, default_stream=default_stream, project="project-1", max_output_tokens=123, temperature=0.4)
        try:
            result = await provider.responses(ResponseRequest(input="hello", stream=override, trace_id="aggregate"), trace_id="aggregate")
            assert result.schema_version == "v1" and result.trace_id == "aggregate"
            assert result.id == "response-1"
            assert result.output_text == "answer"
            assert len(result.tool_calls) == 1
            assert result.tool_calls[0].id == "call-1"
            assert result.tool_calls[0].name == "lookup"
            assert json.loads(result.tool_calls[0].arguments) == {"q": "hello"}
            assert any(item.call_id == "call-1" for item in result.output)
            if provider_type is OpenAIChatCompletionsProvider:
                assert result.usage["prompt_tokens"] == 3 and result.usage["completion_tokens"] == 1
            else:
                assert result.usage["input_tokens"] == 3 and result.usage["output_tokens"] == 1
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("provider_type", PROVIDERS)
@pytest.mark.parametrize("streamed", [False, True])
def test_standard_function_history_is_translated_without_loss(provider_type, streamed):
    history = [{"role": "system", "content": "history system"}, {"role": "user", "content": [{"type": "input_text", "text": "question"}]}, {"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": ARGUMENTS}, {"type": "function_call", "call_id": "call-2", "name": "lookup", "arguments": "{}"}, {"type": "function_call_output", "call_id": "call-1", "output": "first result"}, {"type": "function_call_output", "call_id": "call-2", "output": "second result"}]

    async def handler(request):
        payload = json.loads(request.content)
        if provider_type is OpenAIResponsesProvider:
            assert payload["input"] == history
            assert payload["instructions"] == "instructions"
            assert payload["tools"][0]["parameters"] == {"type": "object"}
        elif provider_type is OpenAIChatCompletionsProvider:
            messages = payload["messages"]
            assert messages[:2] == [{"role": "system", "content": "instructions"}, {"role": "system", "content": "history system"}]
            assert messages[2]["content"] == [{"type": "text", "text": "question"}]
            assert [call["id"] for call in messages[3]["tool_calls"]] == ["call-1", "call-2"]
            assert messages[3]["tool_calls"][0]["function"] == {"name": "lookup", "arguments": ARGUMENTS}
            assert messages[4:] == [{"role": "tool", "tool_call_id": "call-1", "content": "first result"}, {"role": "tool", "tool_call_id": "call-2", "content": "second result"}]
            assert payload["tools"][0]["function"]["parameters"] == {"type": "object"}
        else:
            assert payload["system"] == "instructions\n\nhistory system"
            messages = payload["messages"]
            assert messages[0] == {"role": "user", "content": [{"type": "text", "text": "question"}]}
            assert messages[1] == {"role": "assistant", "content": [{"type": "tool_use", "id": "call-1", "name": "lookup", "input": {"q": "hello"}}, {"type": "tool_use", "id": "call-2", "name": "lookup", "input": {}}]}
            assert messages[2] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "first result"}, {"type": "tool_result", "tool_use_id": "call-2", "content": "second result"}]}
            assert payload["tools"][0]["input_schema"] == {"type": "object"}
        return _sse_response(provider_type, _stream_events(provider_type)) if streamed else httpx.Response(200, json=_response_body(provider_type))

    async def run():
        provider, client = _provider_for(provider_type, handler)
        try:
            result = await provider.responses(ResponseRequest(input=history, instructions="instructions", tools=[{"type": "function", "name": "lookup", "parameters": {"type": "object"}}], stream=streamed), trace_id="history")
            assert result.output_text == "answer"
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("provider_type", [OpenAIResponsesProvider, OpenAIChatCompletionsProvider])
@pytest.mark.parametrize("streamed", [False, True])
def test_structured_format_uses_the_selected_protocol(provider_type, streamed):
    schema = {"type": "json_schema", "name": "answer", "schema": {"type": "object", "properties": {"answer": {"type": "string"}}}, "strict": True}

    async def handler(request):
        payload = json.loads(request.content)
        if provider_type is OpenAIResponsesProvider:
            assert payload["text"]["format"] == schema
        else:
            assert payload["response_format"] == {"type": "json_schema", "json_schema": {key: value for key, value in schema.items() if key != "type"}}
        text = '{"answer":"ok"}'
        return _sse_response(provider_type, _stream_events(provider_type, text=text)) if streamed else httpx.Response(200, json=_response_body(provider_type, text=text))

    async def run():
        provider, client = _provider_for(provider_type, handler)
        try:
            await provider.responses(ResponseRequest(input="hello", response_format=schema, stream=streamed), trace_id="structured")
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("provider_type", PROVIDERS)
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("status,body,code,retryable", [
    (401, {"error": {"message": "secret content filter request", "type": "content_filter"}}, ProviderErrorCode.AUTHENTICATION, False),
    (403, {"error": {"message": "secret upstream"}}, ProviderErrorCode.AUTHENTICATION, False),
    (429, {"error": {"message": "secret content filter request"}}, ProviderErrorCode.RATE_LIMITED, True),
    (408, {"error": {"message": "secret upstream"}}, ProviderErrorCode.TIMEOUT, True),
    (504, {"error": {"message": "secret upstream"}}, ProviderErrorCode.TIMEOUT, True),
    (422, {"error": {"message": "content field is missing; secret upstream", "type": "invalid_request_error"}}, ProviderErrorCode.INVALID_REQUEST, False),
    (400, {"error": {"message": "secret upstream", "code": "content_filter"}}, ProviderErrorCode.CONTENT_FILTERED, False),
    (500, {"error": {"message": "secret upstream"}}, ProviderErrorCode.UNAVAILABLE, True),
])
def test_official_sdk_failure_chains_are_safe_for_logger_exception(provider_type, streamed, status, body, code, retryable, caplog):
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        return httpx.Response(status, json=body)

    async def run():
        provider, client = _provider_for(provider_type, handler)
        try:
            try:
                await provider.responses(ResponseRequest(input="hello", stream=streamed), trace_id="failure-chain")
            except ProviderFailure as exc:
                logging.getLogger("provider-regression").exception("Provider request failed")
                assert exc.failure.error_code == code
                assert exc.failure.retryable is retryable
                assert exc.failure.schema_version == "v1" and exc.failure.trace_id == "failure-chain"
                assert exc.__cause__ is None and exc.__suppress_context__ is True
            else:
                pytest.fail("provider failure was lost")
            assert len(calls) == 1
        finally:
            await client.aclose()

    asyncio.run(run())
    assert "secret upstream" not in caplog.text
    assert "secret content" not in caplog.text
    assert "synthetic-key" not in caplog.text


@pytest.mark.parametrize("provider_type", PROVIDERS)
@pytest.mark.parametrize("transport_error,code", [(httpx.ReadTimeout, ProviderErrorCode.TIMEOUT), (httpx.ConnectError, ProviderErrorCode.UNAVAILABLE)])
def test_transport_failure_has_no_retry_or_protocol_fallback(provider_type, transport_error, code, caplog):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        raise transport_error("secret transport credential", request=request)

    async def run():
        provider, client = _provider_for(provider_type, handler)
        try:
            try:
                await provider.responses(ResponseRequest(input="hello", stream=True), trace_id="transport")
            except ProviderFailure as exc:
                logging.getLogger("provider-regression").exception("Provider request failed")
                assert exc.failure.error_code == code
            else:
                pytest.fail("transport error was ignored")
            assert calls == 1
        finally:
            await client.aclose()

    asyncio.run(run())
    assert "secret transport credential" not in caplog.text


@pytest.mark.parametrize("provider_type", PROVIDERS)
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("invalid_request", [
    ResponseRequest(input="hello", tools=[{"type": "web_search_preview"}]),
    ResponseRequest(input="hello", tools=[{"type": "computer_use"}]),
    ResponseRequest(input="hello", tools=[{"type": "function"}]),
    ResponseRequest(input="hello", response_format={"type": "unsupported"}),
    ResponseRequest(input="hello", response_format={"type": "json_schema"}),
    ResponseRequest(input=[{"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": "not-json"}]),
    ResponseRequest(input=[{"type": "function_call_output", "output": "missing identity"}]),
    ResponseRequest(input=[{"role": "user", "content": [{"type": "input_text"}]}]),
    ResponseRequest(input="hello", stream="true"),
])
def test_invalid_capabilities_and_history_fail_before_network(provider_type, streamed, invalid_request):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        pytest.fail("invalid request reached upstream")

    async def run():
        provider, client = _provider_for(provider_type, handler, default_stream=streamed)
        try:
            with pytest.raises(ProviderFailure) as raised:
                await provider.responses(invalid_request, trace_id="invalid")
            assert raised.value.failure.error_code == ProviderErrorCode.INVALID_REQUEST
            assert raised.value.failure.trace_id == "invalid"
            with pytest.raises(ProviderFailure):
                _ = [event async for event in provider.stream_responses(invalid_request, trace_id="invalid")]
            assert calls == 0
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("provider_type", [OpenAIChatCompletionsProvider, AnthropicMessagesProvider])
def test_unsupported_images_search_and_anthropic_embeddings_are_not_advertised(provider_type):
    async def handler(request):
        pytest.fail("unsupported capability reached upstream")

    async def run():
        provider, client = _provider_for(provider_type, handler, native_search_enabled=True)
        try:
            assert ProviderCapability.IMAGE_INPUT not in provider.capabilities
            assert ProviderCapability.NATIVE_WEB_SEARCH not in provider.capabilities
            for stream in (False, True):
                with pytest.raises(ProviderFailure):
                    await provider.responses(ResponseRequest(input=[{"role": "user", "content": [{"type": "input_image", "image_url": "https://example.test/image.png"}]}], stream=stream), trace_id="image")
            with pytest.raises(ProviderFailure):
                await provider.search("hello", trace_id="search")
            if provider_type is AnthropicMessagesProvider:
                assert ProviderCapability.EMBEDDING not in provider.capabilities
                assert ProviderCapability.STRUCTURED_OUTPUT not in provider.capabilities
                with pytest.raises(ProviderFailure):
                    await provider.embeddings(["hello"], trace_id="embedding")
                with pytest.raises(ProviderFailure):
                    await provider.responses(ResponseRequest(input="hello", response_format={"type": "json_object"}), trace_id="structured")
        finally:
            await client.aclose()

    asyncio.run(run())


class _ControlledStream(httpx.AsyncByteStream):
    def __init__(self, prefix, suffix, *, timeout=False):
        self.prefix = prefix
        self.suffix = suffix
        self.timeout = timeout
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        yield self.prefix
        self.waiting.set()
        if self.timeout:
            raise httpx.ReadTimeout("secret midstream timeout")
        await self.release.wait()
        yield self.suffix

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("provider_type", PROVIDERS)
@pytest.mark.parametrize("ending", ["aclose", "cancel", "timeout", "complete"])
def test_stream_is_incremental_and_closes_on_every_exit(provider_type, ending, caplog):
    async def run():
        events = _stream_events(provider_type)
        split = 1 if provider_type is OpenAIChatCompletionsProvider else 3
        body = _ControlledStream(_sse_bytes(provider_type, events[:split], done=False), _sse_bytes(provider_type, events[split:]), timeout=ending == "timeout")

        async def handler(request):
            assert json.loads(request.content)["stream"] is True
            return httpx.Response(200, stream=body, headers={"content-type": "text/event-stream"})

        provider, client = _provider_for(provider_type, handler)
        stream = provider.stream_responses(ResponseRequest(input="hello", stream=False), trace_id="lifecycle")
        try:
            async def first_text():
                async for event in stream:
                    if event.event_type == "response.output_text.delta":
                        return event
                pytest.fail("stream did not expose text incrementally")

            first = await asyncio.wait_for(first_text(), timeout=1)
            assert first.delta == "answer" and first.response_id == "response-1"
            assert not body.waiting.is_set(), "adapter buffered past the first text event"
            assert not body.closed
            if ending == "aclose":
                await stream.aclose()
            elif ending == "cancel":
                task = asyncio.create_task(anext(stream))
                await asyncio.wait_for(body.waiting.wait(), timeout=1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif ending == "timeout":
                try:
                    _ = [event async for event in stream]
                except ProviderFailure as exc:
                    logging.getLogger("provider-regression").exception("Provider request failed")
                    assert exc.failure.error_code == ProviderErrorCode.TIMEOUT
                else:
                    pytest.fail("midstream timeout was ignored")
            else:
                body.release.set()
                remaining = [event async for event in stream]
                assert remaining[-1].event_type == "response.completed"
            assert body.closed
            assert not client.is_closed, "closing a response must not close the injected client"
        finally:
            await stream.aclose()
            await client.aclose()

    asyncio.run(run())
    assert "secret midstream timeout" not in caplog.text


@pytest.mark.parametrize("provider_type", PROVIDERS)
@pytest.mark.parametrize("ending", ["truncated", "error"])
def test_midstream_failure_never_emits_success(provider_type, ending, caplog):
    original = _stream_events(provider_type)
    if ending == "truncated":
        events = original[:3] if provider_type is OpenAIChatCompletionsProvider else original[:-1]
    else:
        if provider_type is AnthropicMessagesProvider:
            error = {"type": "error", "error": {"type": "rate_limit_error", "message": "secret stream upstream"}}
        elif provider_type is OpenAIResponsesProvider:
            error = {"type": "error", "code": "rate_limit_exceeded", "message": "secret stream upstream", "sequence_number": 99}
        else:
            error = {"error": {"type": "rate_limit_error", "message": "secret stream upstream"}}
        split = 1 if provider_type is OpenAIChatCompletionsProvider else 3
        events = original[:split] + [error] + original[split:]

    async def handler(request):
        return _sse_response(provider_type, events)

    async def run():
        provider, client = _provider_for(provider_type, handler)
        observed = []
        try:
            try:
                async for event in provider.stream_responses(ResponseRequest(input="hello"), trace_id="midstream"):
                    observed.append(event)
            except ProviderFailure as exc:
                logging.getLogger("provider-regression").exception("Provider request failed")
                expected = ProviderErrorCode.RATE_LIMITED if ending == "error" else ProviderErrorCode.UNAVAILABLE
                assert exc.failure.error_code == expected
            else:
                pytest.fail("failed stream succeeded")
            assert any(event.event_type == "response.output_text.delta" for event in observed)
            assert not any(event.event_type == "response.completed" for event in observed)
            with pytest.raises(ProviderFailure):
                await provider.responses(ResponseRequest(input="hello", stream=True), trace_id="aggregate-failure")
        finally:
            await client.aclose()

    asyncio.run(run())
    assert "secret stream upstream" not in caplog.text


def test_chat_legal_finish_without_usage_and_fragmented_parallel_tools():
    events = [
        _chat_chunk({"content": "answer"}),
        _chat_chunk({"tool_calls": [{"index": 1, "id": "call-", "type": "function", "function": {"name": "look", "arguments": "{"}}, {"index": 0, "id": "other", "type": "function", "function": {"name": "other", "arguments": "{}"}}]}),
        _chat_chunk({"tool_calls": [{"index": 1, "id": "1", "function": {"name": "up", "arguments": '"q":"hello"}'}}]}),
        _chat_chunk({}, finish_reason="tool_calls"),
    ]

    async def handler(request):
        payload = json.loads(request.content)
        assert payload["max_completion_tokens"] == 4096
        assert payload["stream_options"] == {"include_usage": True}
        return _sse_response(OpenAIChatCompletionsProvider, events)

    async def run():
        provider, client = _provider_for(OpenAIChatCompletionsProvider, handler)
        try:
            result = await provider.responses(ResponseRequest(input="hello", stream=True), trace_id="chat-tools")
            assert result.id == "response-1" and result.output_text == "answer" and result.usage == {}
            assert {call.id: (call.name, json.loads(call.arguments)) for call in result.tool_calls} == {"call-1": ("lookup", {"q": "hello"}), "other": ("other", {})}
        finally:
            await client.aclose()

    asyncio.run(run())


def test_responses_arguments_are_not_text_and_keep_stable_call_identity():
    events = _stream_events(OpenAIResponsesProvider)
    events.insert(3, {"type": "response.reasoning_summary_text.delta", "item_id": "reasoning-1", "output_index": 2, "summary_index": 0, "delta": "private reasoning", "sequence_number": 100})

    async def handler(request):
        return _sse_response(OpenAIResponsesProvider, events)

    async def run():
        provider, client = _provider_for(OpenAIResponsesProvider, handler)
        try:
            observed = [event async for event in provider.stream_responses(ResponseRequest(input="hello"), trace_id="identities")]
            tool_events = [event for event in observed if event.event_type.startswith("response.tool_call.")]
            assert all(event.output.id == "item-1" and event.output.call_id == "call-1" and event.output.name == "lookup" for event in tool_events)
            assert [event.output.arguments for event in tool_events if event.event_type == "response.tool_call.delta"] == ["", '{"q":', '"hello"}']
            assert sum(event.event_type == "response.tool_call.completed" for event in tool_events) == 1
            assert "".join(event.delta or "" for event in observed) == "answer"
            result = await provider.responses(ResponseRequest(input="hello", stream=True), trace_id="aggregate-identities")
            assert result.output_text == "answer"
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("provider_type", [OpenAIResponsesProvider, AnthropicMessagesProvider])
def test_native_terminal_event_is_a_stop_boundary(provider_type):
    async def run():
        body = _ControlledStream(_sse_bytes(provider_type, _stream_events(provider_type)), b"data: should-not-be-read\n\n")

        async def handler(request):
            return httpx.Response(200, stream=body, headers={"content-type": "text/event-stream"})

        provider, client = _provider_for(provider_type, handler)
        try:
            result = await asyncio.wait_for(provider.responses(ResponseRequest(input="hello", stream=True), trace_id="boundary"), timeout=1)
            assert result.id == "response-1"
            assert body.closed and not body.waiting.is_set()
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("provider_type,reason,code", [
    (OpenAIChatCompletionsProvider, "length", ProviderErrorCode.UNAVAILABLE),
    (OpenAIChatCompletionsProvider, "content_filter", ProviderErrorCode.CONTENT_FILTERED),
    (AnthropicMessagesProvider, "max_tokens", ProviderErrorCode.UNAVAILABLE),
    (AnthropicMessagesProvider, "refusal", ProviderErrorCode.REFUSED),
])
@pytest.mark.parametrize("streamed", [False, True])
def test_incomplete_and_filtered_finish_reasons_fail_closed(provider_type, reason, code, streamed):
    async def handler(request):
        if streamed:
            events = _stream_events(provider_type)
            if provider_type is OpenAIChatCompletionsProvider:
                events[-2]["choices"][0]["finish_reason"] = reason
            else:
                events[-2]["delta"]["stop_reason"] = reason
            return _sse_response(provider_type, events)
        response = _response_body(provider_type)
        if provider_type is OpenAIChatCompletionsProvider:
            response["choices"][0]["finish_reason"] = reason
        else:
            response["stop_reason"] = reason
        return httpx.Response(200, json=response)

    async def run():
        provider, client = _provider_for(provider_type, handler)
        try:
            with pytest.raises(ProviderFailure) as raised:
                await provider.responses(ResponseRequest(input="hello", stream=streamed), trace_id="finish")
            assert raised.value.failure.error_code == code
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("index", [True, 0.0, "0", -1, None])
@pytest.mark.parametrize("provider_type", [OpenAIResponsesProvider, OpenAIChatCompletionsProvider])
def test_embedding_indices_are_strict_integers(provider_type, index):
    async def handler(request):
        assert request.url.path == "/v1/embeddings"
        assert json.loads(request.content)["encoding_format"] == "float"
        return httpx.Response(200, json={"object": "list", "model": "synthetic-embedding", "data": [{"object": "embedding", "index": index, "embedding": [0.1, 0.2]}], "usage": {"prompt_tokens": 1, "total_tokens": 1}})

    async def run():
        provider, client = _provider_for(provider_type, handler)
        try:
            with pytest.raises(ProviderFailure) as raised:
                await provider.embeddings(["hello"], trace_id="index")
            assert raised.value.failure.error_code == ProviderErrorCode.UNAVAILABLE
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("streamed", [False, True])
def test_search_citations_only_include_returned_annotations(streamed):
    async def handler(request):
        payload = json.loads(request.content)
        assert payload["tools"] == [{"type": "web_search_preview"}]
        response = _response_body(OpenAIResponsesProvider, text="Do not cite https://unreturned.test or the query URL")
        message = response["output"][0]
        message["content"][0]["annotations"] = [{"type": "url_citation", "url": "https://returned.test/source", "title": "source", "start_index": 0, "end_index": 3}]
        if streamed:
            events = _stream_events(OpenAIResponsesProvider, text=response["output"][0]["content"][0]["text"])
            events[3]["item"] = message
            events[-1]["response"] = response
            return _sse_response(OpenAIResponsesProvider, events)
        return httpx.Response(200, json=response)

    async def run():
        provider, client = _provider_for(OpenAIResponsesProvider, handler, native_search_enabled=True, default_stream=streamed)
        try:
            result = await provider.search("https://query.test", trace_id="citations")
            assert result.citations == ["https://returned.test/source"]
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("provider_type", PROVIDERS)
def test_sdk_debug_logging_redacts_custom_headers_and_raw_errors(provider_type, caplog):
    secret = "configured-custom-header-secret"
    key = "configured-sdk-auth-secret"

    async def handler(request):
        assert request.headers["X-Private-Credential"] == secret
        return httpx.Response(401, json={"error": {"type": "authentication_error", "message": secret + key}})

    async def run():
        provider, client = _provider_for(provider_type, handler, api_key=key, extra_headers={"X-Private-Credential": secret, "Authorization": "Bearer " + key})
        try:
            with caplog.at_level(logging.DEBUG):
                try:
                    await provider.responses(ResponseRequest(input="hello"), trace_id="debug-logging")
                except ProviderFailure as exc:
                    logging.getLogger("provider-regression").exception("Provider request failed")
                    assert exc.failure.error_code == ProviderErrorCode.AUTHENTICATION
                else:
                    pytest.fail("authentication error was ignored")
            assert any(record.name in {"openai._base_client", "anthropic._base_client"} for record in caplog.records)
            assert "[REDACTED]" in caplog.text or "redacted" in caplog.text
            assert secret not in caplog.text and key not in caplog.text
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("provider_type", PROVIDERS)
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("status", [200, 401, 429, 500])
def test_transport_debug_logs_do_not_expose_upstream_secrets(provider_type, streamed, status, caplog):
    key, header, body_secret = (secrets.token_hex(24) for _ in range(3))
    direct_logs = io.StringIO()
    direct_handler = logging.StreamHandler(direct_logs)
    transport_logger = logging.getLogger("httpcore.http11")
    transport_logger.addHandler(direct_handler)

    async def run():
        served = asyncio.get_running_loop().create_future()

        async def serve(reader, writer):
            try:
                raw = await reader.readuntil(b"\r\n\r\n")
                length = next(int(line.split(b":", 1)[1]) for line in raw.split(b"\r\n") if line.lower().startswith(b"content-length:"))
                await reader.readexactly(length)
                if status == 200 and streamed:
                    body = _sse_response(provider_type, _stream_events(provider_type, text=body_secret)).content
                    content_type = "text/event-stream"
                else:
                    payload = _response_body(provider_type, text=body_secret) if status == 200 else {"error": {"message": key + header + body_secret}}
                    body = json.dumps(payload).encode()
                    content_type = "application/json"
                # Reason phrases and arbitrary response headers are untrusted too.
                writer.write(f"HTTP/1.1 {status} {body_secret}\r\nContent-Type: {content_type}\r\nX-Upstream-Key: {key}\r\nX-Upstream-Header: {header}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                served.set_result(None)

        listener = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = listener.sockets[0].getsockname()[1]
        async with listener:
            provider = provider_type(ProviderConfig(base_url=f"http://127.0.0.1:{port}/v1", api_key=key, responses_model="model", embedding_model="embedding", extra_headers={"X-Private": header}))
            try:
                if status == 200:
                    result = await provider.responses(ResponseRequest(input="hello", stream=streamed), trace_id="transport-log")
                    assert result.output_text == body_secret
                else:
                    try:
                        await provider.responses(ResponseRequest(input="hello", stream=streamed), trace_id="transport-log")
                    except ProviderFailure as exc:
                        expected = {401: ProviderErrorCode.AUTHENTICATION, 429: ProviderErrorCode.RATE_LIMITED, 500: ProviderErrorCode.UNAVAILABLE}[status]
                        assert exc.failure.error_code == expected
                        formatted_chain = "".join(traceback.format_exception(exc))
                        assert all(value not in formatted_chain for value in (key, header, body_secret))
                        logging.getLogger("provider-regression").exception("trace_id=%s code=%s", exc.failure.trace_id, exc.failure.error_code)
                    else:
                        pytest.fail("upstream failure was ignored")
            finally:
                await provider._close()
                await asyncio.wait_for(served, timeout=3)

    try:
        with caplog.at_level(logging.DEBUG):
            asyncio.run(run())
        # Assert both originating handlers and root propagation, not just Failure.
        assert "receive_response_headers.complete" in direct_logs.getvalue()
        assert any(record.name == "httpcore.http11" for record in caplog.records)
        assert any(record.name == "httpx" and str(status) in record.getMessage() for record in caplog.records)
        if status != 200:
            assert "transport-log" in caplog.text
            assert str({401: ProviderErrorCode.AUTHENTICATION, 429: ProviderErrorCode.RATE_LIMITED, 500: ProviderErrorCode.UNAVAILABLE}[status]) in caplog.text
        for secret in (key, header, body_secret):
            assert secret not in caplog.text
            assert secret not in direct_logs.getvalue()
    finally:
        transport_logger.removeHandler(direct_handler)
        direct_handler.close()


@pytest.mark.parametrize("provider_type", [OpenAIResponsesProvider, OpenAIChatCompletionsProvider])
@pytest.mark.parametrize("vector", [[True, 0.1], ["0.1", 0.2], [], [float("inf")], [None]])
def test_embedding_values_are_not_coerced_into_valid_vectors(provider_type, vector):
    async def handler(request):
        return httpx.Response(200, content=json.dumps({"model": "synthetic-embedding", "data": [{"index": 0, "embedding": vector}]}, allow_nan=True).encode())

    async def run():
        provider, client = _provider_for(provider_type, handler)
        try:
            with pytest.raises(ProviderFailure) as raised:
                await provider.embeddings(["hello"], trace_id="vector")
            assert raised.value.failure.error_code == ProviderErrorCode.UNAVAILABLE
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("provider_type", PROVIDERS)
def test_schema_and_trace_conflicts_reject_both_response_entrypoints(provider_type):
    async def handler(request):
        pytest.fail("conflicting contract reached upstream")

    async def run():
        provider, client = _provider_for(provider_type, handler)
        wrong_version = ResponseRequest(input="hello")
        object.__setattr__(wrong_version, "schema_version", "v999")
        try:
            for request in (ResponseRequest(input="hello", trace_id="other"), wrong_version):
                with pytest.raises(ProviderFailure) as raised:
                    await provider.responses(request, trace_id="conflict")
                assert raised.value.failure.error_code == ProviderErrorCode.INVALID_REQUEST
                with pytest.raises(ProviderFailure):
                    _ = [event async for event in provider.stream_responses(request, trace_id="conflict")]
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("provider_type,broken", [
    (OpenAIResponsesProvider, "unknown_item"),
    (OpenAIResponsesProvider, "conflicting_identity"),
    (OpenAIChatCompletionsProvider, "missing_tool_id"),
    (AnthropicMessagesProvider, "missing_block_stop"),
    (AnthropicMessagesProvider, "unknown_block"),
])
def test_malformed_stream_tool_lifecycles_fail_closed(provider_type, broken):
    events = _stream_events(provider_type)
    if broken == "unknown_item":
        events[5]["item_id"] = "unknown"
    elif broken == "conflicting_identity":
        events[8]["item"] = events[8]["item"] | {"call_id": "conflicting"}
    elif broken == "missing_tool_id":
        events[1]["choices"][0]["delta"]["tool_calls"][0].pop("id")
    elif broken == "missing_block_stop":
        events.pop(7)
    else:
        events[5]["index"] = 99

    async def handler(request):
        return _sse_response(provider_type, events)

    async def run():
        provider, client = _provider_for(provider_type, handler)
        try:
            with pytest.raises(ProviderFailure) as raised:
                await provider.responses(ResponseRequest(input="hello", stream=True), trace_id="malformed-tools")
            assert raised.value.failure.error_code == ProviderErrorCode.UNAVAILABLE
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("provider_type", PROVIDERS)
@pytest.mark.parametrize("capability", [ProviderCapability.STREAMING, ProviderCapability.TOOL_CALLING])
def test_capability_declarations_are_enforced_before_outbound(provider_type, capability):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_response_body(provider_type))

    async def run():
        provider, client = _provider_for(provider_type, handler, default_stream=True)
        provider.capabilities = provider.capabilities - {capability}
        request = ResponseRequest(input="hello", tools=[{"type": "function", "name": "lookup"}] if capability == ProviderCapability.TOOL_CALLING else [])
        try:
            with pytest.raises(ProviderFailure) as raised:
                await provider.responses(request, trace_id="capability")
            assert raised.value.failure.error_code == ProviderErrorCode.INVALID_REQUEST
            with pytest.raises(ProviderFailure):
                _ = [event async for event in provider.stream_responses(request, trace_id="capability")]
            assert calls == 0
            if capability == ProviderCapability.STREAMING:
                with pytest.raises(ProviderFailure):
                    await provider.responses(ResponseRequest(input="hello", stream=True), trace_id="explicit-stream")
                result = await provider.responses(ResponseRequest(input="hello", stream=False), trace_id="explicit-nonstream")
                assert result.output_text == "answer" and calls == 1
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("provider_type", PROVIDERS)
def test_standard_tool_output_content_parts_use_target_protocol(provider_type):
    history = [{"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": "{}"}, {"type": "function_call_output", "call_id": "call-1", "output": [{"type": "input_text", "text": "result"}]}]

    async def handler(request):
        payload = json.loads(request.content)
        if provider_type is OpenAIResponsesProvider:
            assert payload["input"] == history
        elif provider_type is OpenAIChatCompletionsProvider:
            assert payload["messages"][-1] == {"role": "tool", "tool_call_id": "call-1", "content": [{"type": "text", "text": "result"}]}
        else:
            assert payload["messages"][-1] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": [{"type": "text", "text": "result"}]}]}
        return httpx.Response(200, json=_response_body(provider_type))

    async def run():
        provider, client = _provider_for(provider_type, handler)
        try:
            result = await provider.responses(ResponseRequest(input=history), trace_id="tool-output-parts")
            assert result.output_text == "answer"
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("provider_type", PROVIDERS)
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("invalid_id", ["missing", None, "", 123])
def test_successful_response_requires_a_nonempty_string_id(provider_type, streamed, invalid_id):
    async def handler(request):
        if streamed:
            events = _stream_events(provider_type)
            if provider_type is OpenAIResponsesProvider:
                identities = [events[0]["response"], events[-1]["response"]]
            elif provider_type is OpenAIChatCompletionsProvider:
                identities = events
            else:
                identities = [events[0]["message"]]
        else:
            response = _response_body(provider_type)
            identities = [response]
        for value in identities:
            if invalid_id == "missing":
                value.pop("id", None)
            else:
                value["id"] = invalid_id
        return _sse_response(provider_type, events) if streamed else httpx.Response(200, json=response)

    async def run():
        provider, client = _provider_for(provider_type, handler)
        try:
            with pytest.raises(ProviderFailure) as raised:
                await provider.responses(ResponseRequest(input="hello", stream=streamed), trace_id="invalid-response-id")
            assert raised.value.failure.error_code == ProviderErrorCode.UNAVAILABLE
            assert raised.value.failure.trace_id == "invalid-response-id"
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("provider_type", PROVIDERS)
def test_response_id_cannot_change_midstream(provider_type):
    events = _stream_events(provider_type)
    if provider_type is OpenAIResponsesProvider:
        events[-1]["response"]["id"] = "different-response"
    elif provider_type is OpenAIChatCompletionsProvider:
        events[1]["id"] = "different-response"
    else:
        events.insert(3, {"type": "message_start", "message": events[0]["message"] | {"id": "different-response"}})

    async def handler(request):
        return _sse_response(provider_type, events)

    async def run():
        provider, client = _provider_for(provider_type, handler)
        observed = []
        try:
            with pytest.raises(ProviderFailure) as raised:
                async for event in provider.stream_responses(ResponseRequest(input="hello"), trace_id="response-id-conflict"):
                    observed.append(event)
            assert raised.value.failure.error_code == ProviderErrorCode.UNAVAILABLE
            assert not any(event.event_type == "response.completed" for event in observed)
        finally:
            await client.aclose()

    asyncio.run(run())
