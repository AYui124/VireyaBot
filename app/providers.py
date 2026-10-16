"""Provider boundary for Responses-compatible models, search, and embeddings."""

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol

import httpx
from pydantic import Field

from app.contracts import ContractModel, Failure

SCHEMA_VERSION = "v1"


class ProviderCapability(StrEnum):
    TEXT = "text"
    IMAGE_INPUT = "image_input"
    STREAMING = "streaming"
    STRUCTURED_OUTPUT = "structured_output"
    TOOL_CALLING = "tool_calling"
    NATIVE_WEB_SEARCH = "native_web_search"
    EMBEDDING = "embedding"
    EMBEDDING_BATCH = "embedding_batch"


class ProviderCapabilityStatus(ContractModel):
    schema_version: Literal["v1"] = SCHEMA_VERSION
    capability: ProviderCapability
    adapter_supported: bool
    endpoint_verified: bool
    enabled: bool


class ProviderErrorCode(StrEnum):
    AUTHENTICATION = "provider.auth_failed"
    RATE_LIMITED = "provider.rate_limited"
    TIMEOUT = "provider.timeout"
    CONTENT_FILTERED = "provider.content_filtered"
    REFUSED = "provider.refused"
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
    protocol_type: str = "openai_responses"

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
    schema_version: Literal["v1"] = SCHEMA_VERSION
    trace_id: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported provider schema version: {self.schema_version}")


class ProviderOutput(ContractModel):
    """Normalized output item; upstream response dictionaries do not cross the boundary."""

    schema_version: Literal["v1"] = SCHEMA_VERSION
    type: str
    id: str | None = None
    call_id: str | None = None
    text: str | None = None
    name: str | None = None
    arguments: str | None = None
    annotations: list[str] = Field(default_factory=list)


class ToolCallResult(ContractModel):
    schema_version: Literal["v1"] = SCHEMA_VERSION
    id: str
    name: str
    arguments: str


class ResponseResult(ContractModel):
    schema_version: Literal["v1"] = SCHEMA_VERSION
    trace_id: str
    id: str
    output_text: str
    output: list[ProviderOutput]
    usage: dict[str, Any]
    tool_calls: list[ToolCallResult] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)


class StreamEvent(ContractModel):
    schema_version: Literal["v1"] = SCHEMA_VERSION
    trace_id: str
    event_type: str
    delta: str | None = None
    output: ProviderOutput | None = None


class EmbeddingItem(ContractModel):
    schema_version: Literal["v1"] = SCHEMA_VERSION
    index: int
    vector: list[float]


class EmbeddingResult(ContractModel):
    schema_version: Literal["v1"] = SCHEMA_VERSION
    trace_id: str
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
            schema_version=SCHEMA_VERSION,
            trace_id=trace_id,
            error_code=code,
            retryable=retryable,
            message=message,
        )
    )


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}

def _validate_request(request: ResponseRequest, trace_id: str) -> None:
    if request.schema_version != SCHEMA_VERSION:
        raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "unsupported provider schema version", retryable=False)
    if not trace_id or (request.trace_id and request.trace_id != trace_id):
        raise _failure(trace_id or request.trace_id, ProviderErrorCode.INVALID_REQUEST, "provider trace_id conflict", retryable=False)

def _has_image_input(value: str | list[dict[str, Any]]) -> bool:
    return isinstance(value, list) and any(
        isinstance(part, dict)
        and any(isinstance(content, dict) and content.get("type") == "input_image" for content in part.get("content", []))
        for part in value
    )



class OpenAIResponsesProvider:
    """Minimal Responses-compatible adapter; only normalized contracts leave this class."""

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
        if config.native_search_enabled and self.__class__ is OpenAIResponsesProvider:
            self.capabilities = self.capabilities | {ProviderCapability.NATIVE_WEB_SEARCH}
    def capability_matrix(self) -> list[ProviderCapabilityStatus]:
        return [
            ProviderCapabilityStatus(
                capability=capability,
                adapter_supported=capability in self.capabilities,
                endpoint_verified=False,
                enabled=capability in self.capabilities and (capability != ProviderCapability.NATIVE_WEB_SEARCH or self.config.native_search_enabled),
            )
            for capability in ProviderCapability
        ]

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.config.api_key}"
        }
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
            await _raise_for_status(response, trace_id)
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
        _validate_request(request, trace_id)
        payload: dict[str, Any] = {"model": request.model or self.config.responses_model, "input": request.input}
        if request.instructions is not None:
            payload["instructions"] = request.instructions
        if request.response_format is not None:
            payload["text"] = {"format": request.response_format}
        if request.tools:
            payload["tools"] = request.tools
        value = await self._request("POST", "/responses", payload, trace_id=trace_id)
        return _parse_response(value, trace_id=trace_id)

    async def stream_responses(
        self, request: ResponseRequest, *, trace_id: str
    ) -> AsyncIterator[StreamEvent]:
        _validate_request(request, trace_id)
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
        completed = False
        try:
            try:
                async with client.stream("POST", self._url("/responses"), headers=self._headers(), json=payload) as response:
                    await _raise_for_status(response, trace_id)
                    async for line in response.aiter_lines():
                        if not line or line.startswith(":"):
                            continue
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError as exc:
                            raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned invalid stream data", retryable=True) from exc
                        if not isinstance(data, dict):
                            raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned invalid stream data", retryable=True)
                        event_type = str(data.get("type", "message"))
                        if event_type in {"response.failed", "response.error", "error", "response.incomplete"}:
                            raise _failure_from_event(trace_id, event_type, data)
                        if event_type == "response.completed":
                            response_data = _dict(data.get("response"))
                            if response_data.get("status") not in {None, "completed"}:
                                raise _failure_from_status(trace_id, response_data.get("status"), response_data.get("error"))
                            usage = response_data.get("usage", data.get("usage"))
                            if isinstance(usage, dict):
                                yield StreamEvent(trace_id=trace_id, event_type="usage", output=ProviderOutput(type="usage", text=json.dumps(usage)))
                            completed = True
                            yield StreamEvent(trace_id=trace_id, event_type="response.completed")
                            continue
                        event = _parse_stream_event(data, trace_id=trace_id)
                        if event is not None:
                            yield event
            except httpx.TimeoutException as exc:
                raise _failure(trace_id, ProviderErrorCode.TIMEOUT, "provider request timed out", retryable=True) from exc
            except httpx.HTTPError as exc:
                raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider transport unavailable", retryable=True) from exc
            if not completed:
                raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider stream ended unexpectedly", retryable=True)
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
        if not isinstance(items, list) or len(items) != len(inputs):
            raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned mismatched embeddings", retryable=True)
        result: list[EmbeddingItem] = []
        dimensions: int | None = None
        for expected_index, item in enumerate(items):
            if not isinstance(item, dict) or item.get("index") != expected_index or not isinstance(item.get("embedding"), list):
                raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned invalid embeddings", retryable=True)
            vector: list[float] = []
            for value_item in item["embedding"]:
                if isinstance(value_item, bool) or not isinstance(value_item, (int, float)) or not math.isfinite(value_item):
                    raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned invalid embeddings", retryable=True)
                vector.append(float(value_item))
            if not vector or (dimensions is not None and len(vector) != dimensions):
                raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned inconsistent embedding dimensions", retryable=True)
            dimensions = len(vector)
            result.append(EmbeddingItem(index=expected_index, vector=vector))
        assert dimensions is not None
        if self.config.embedding_dimensions is not None and dimensions != self.config.embedding_dimensions:
            raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned configured-incompatible embedding dimensions", retryable=True)
        return EmbeddingResult(
            trace_id=trace_id,
            model=str(value.get("model", self.config.embedding_model)),
            data=result,
            dimensions=dimensions,
            usage=_dict(value.get("usage")),
        )

    async def search(self, query: str, *, trace_id: str) -> ResponseResult:
        if not self.config.native_search_enabled:
            raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "native web search is not enabled", retryable=False)
        return await self.responses(ResponseRequest(input=query, tools=[{"type": "web_search_preview"}]), trace_id=trace_id)


class OpenAIChatCompletionsProvider(OpenAIResponsesProvider):
    """OpenAI Chat Completions-compatible adapter with normalized outputs."""

    capabilities = frozenset({ProviderCapability.TEXT, ProviderCapability.STREAMING, ProviderCapability.STRUCTURED_OUTPUT, ProviderCapability.TOOL_CALLING, ProviderCapability.EMBEDDING, ProviderCapability.EMBEDDING_BATCH})

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        headers["Content-Type"] = "application/json"
        return headers

    @staticmethod
    def _messages(request: ResponseRequest) -> list[dict[str, Any]]:
        if isinstance(request.input, str):
            messages: list[dict[str, Any]] = [{"role": "user", "content": request.input}]
        else:
            messages = list(request.input)
        if request.instructions:
            messages.insert(0, {"role": "system", "content": request.instructions})
        return messages

    async def responses(self, request: ResponseRequest, *, trace_id: str) -> ResponseResult:
        _validate_request(request, trace_id)
        if _has_image_input(request.input):
            raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "image input is not supported by this Provider", retryable=False)
        payload: dict[str, Any] = {"model": request.model or self.config.responses_model, "messages": self._messages(request)}
        if request.response_format is not None:
            payload["response_format"] = request.response_format
        if request.tools:
            payload["tools"] = request.tools
        value = await self._request("POST", "/chat/completions", payload, trace_id=trace_id)
        choices = value.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned an invalid response", retryable=True)
        message = _dict(choices[0].get("message"))
        text = message.get("content") if isinstance(message.get("content"), str) else ""
        output = [ProviderOutput(type="message", id=str(value.get("id", "")), text=text)]
        calls: list[ToolCallResult] = []
        for call in message.get("tool_calls", []):
            if isinstance(call, dict):
                function = _dict(call.get("function"))
                name, arguments = function.get("name"), function.get("arguments")
                if isinstance(name, str) and isinstance(arguments, str):
                    calls.append(ToolCallResult(id=str(call.get("id", "")), name=name, arguments=arguments))
                    output.append(ProviderOutput(type="tool_call", id=str(call.get("id", "")), call_id=str(call.get("id", "")), name=name, arguments=arguments))
        return ResponseResult(trace_id=trace_id, id=str(value.get("id", "")), output_text=text, output=output, usage=_dict(value.get("usage")), tool_calls=calls)

    async def stream_responses(self, request: ResponseRequest, *, trace_id: str) -> AsyncIterator[StreamEvent]:
        _validate_request(request, trace_id)
        if _has_image_input(request.input):
            raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "image input is not supported by this Provider", retryable=False)
        payload: dict[str, Any] = {"model": request.model or self.config.responses_model, "messages": self._messages(request), "stream": True, "stream_options": {"include_usage": True}}
        if request.response_format is not None:
            payload["response_format"] = request.response_format
        if request.tools:
            payload["tools"] = request.tools
        finished = False
        done = False
        usage_seen = False
        async for event in self._stream_sse("/chat/completions", payload, trace_id=trace_id):
            if event.get("__stream_done__"):
                done = True
                continue
            choices = event.get("choices")
            if not isinstance(choices, list) or not choices:
                usage = event.get("usage")
                if isinstance(usage, dict):
                    usage_seen = True
                    yield StreamEvent(trace_id=trace_id, event_type="usage", output=ProviderOutput(type="usage", text=json.dumps(usage)))
                continue
            choice = _dict(choices[0])
            delta = _dict(choice.get("delta"))
            text = delta.get("content") if isinstance(delta.get("content"), str) else None
            if text:
                yield StreamEvent(trace_id=trace_id, event_type="response.output_text.delta", delta=text)
            for call in delta.get("tool_calls", []):
                if isinstance(call, dict):
                    function = _dict(call.get("function"))
                    if isinstance(function.get("name"), str) or isinstance(function.get("arguments"), str):
                        yield StreamEvent(trace_id=trace_id, event_type="response.tool_call.delta", output=ProviderOutput(type="tool_call", id=str(call.get("id", "")), call_id=str(call.get("id", "")), name=function.get("name"), arguments=function.get("arguments")))
            if choice.get("finish_reason"):
                finished = True
        if not done or not finished:
            raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider stream ended unexpectedly", retryable=True)
        if not usage_seen:
            yield StreamEvent(trace_id=trace_id, event_type="usage", output=ProviderOutput(type="usage", text="{}"))
        yield StreamEvent(trace_id=trace_id, event_type="response.completed")

    async def search(self, query: str, *, trace_id: str) -> ResponseResult:
        raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "native web search is not supported by this Provider", retryable=False)
    async def _stream_sse(self, path: str, payload: Mapping[str, Any], *, trace_id: str) -> AsyncIterator[dict[str, Any]]:
        client = self._client or httpx.AsyncClient(timeout=self.config.timeout_seconds)
        try:
            try:
                async with client.stream("POST", self._url(path), headers=self._headers(), json=payload) as response:
                    await _raise_for_status(response, trace_id)
                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            yield {"__stream_done__": True}
                            return
                        try:
                            value = json.loads(raw)
                        except json.JSONDecodeError as exc:
                            raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned invalid stream data", retryable=True) from exc
                        if not isinstance(value, dict):
                            raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned invalid stream data", retryable=True)
                        if value.get("type") == "error" or "error" in value:
                            raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider stream returned an error", retryable=False)
                        yield value
            except httpx.TimeoutException as exc:
                raise _failure(trace_id, ProviderErrorCode.TIMEOUT, "provider request timed out", retryable=True) from exc
            except httpx.HTTPError as exc:
                raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider transport unavailable", retryable=True) from exc
        finally:
            if self._owns_client:
                await client.aclose()


class AnthropicMessagesProvider(OpenAIChatCompletionsProvider):
    """Anthropic Messages adapter; embeddings and native search are unavailable."""

    capabilities = frozenset({ProviderCapability.TEXT, ProviderCapability.STREAMING, ProviderCapability.TOOL_CALLING})

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.config.api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}

    @staticmethod
    def _anthropic_messages(request: ResponseRequest) -> tuple[str | None, list[dict[str, Any]]]:
        if isinstance(request.input, str):
            messages = [{"role": "user", "content": request.input}]
        else:
            messages = list(request.input)
        return request.instructions, messages

    async def responses(self, request: ResponseRequest, *, trace_id: str) -> ResponseResult:
        _validate_request(request, trace_id)
        if request.response_format is not None:
            raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "structured output is not supported by this Provider", retryable=False)
        if _has_image_input(request.input):
            raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "image input is not supported by this Provider", retryable=False)
        system, messages = self._anthropic_messages(request)
        payload: dict[str, Any] = {"model": request.model or self.config.responses_model, "max_tokens": 4096, "messages": messages}
        if system:
            payload["system"] = system
        if request.tools:
            payload["tools"] = request.tools
        value = await self._request("POST", "/messages", payload, trace_id=trace_id)
        content = value.get("content")
        if not isinstance(content, list):
            raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned an invalid response", retryable=True)
        output: list[ProviderOutput] = []
        calls: list[ToolCallResult] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                output.append(ProviderOutput(type="message", text=item["text"]))
            elif item.get("type") == "tool_use" and isinstance(item.get("name"), str):
                arguments = json.dumps(item.get("input", {}), ensure_ascii=False)
                call = ToolCallResult(id=str(item.get("id", "")), name=item["name"], arguments=arguments)
                calls.append(call)
                output.append(ProviderOutput(type="tool_call", id=call.id, call_id=call.id, name=call.name, arguments=call.arguments))
        return ResponseResult(trace_id=trace_id, id=str(value.get("id", "")), output_text=_extract_text(output), output=output, usage=_dict(value.get("usage")), tool_calls=calls)

    async def stream_responses(self, request: ResponseRequest, *, trace_id: str) -> AsyncIterator[StreamEvent]:
        _validate_request(request, trace_id)
        if request.response_format is not None:
            raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "structured output is not supported by this Provider", retryable=False)
        if _has_image_input(request.input):
            raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "image input is not supported by this Provider", retryable=False)
        system, messages = self._anthropic_messages(request)
        payload: dict[str, Any] = {"model": request.model or self.config.responses_model, "max_tokens": 4096, "messages": messages, "stream": True}
        if system:
            payload["system"] = system
        if request.tools:
            payload["tools"] = request.tools
        usage: dict[str, Any] = {}
        tools: dict[int, dict[str, str]] = {}
        done = False
        stopped = False
        async for data in self._stream_sse("/messages", payload, trace_id=trace_id):
            if data.get("__stream_done__"):
                done = True
                continue
            event_type = data.get("type")
            if event_type == "message_start":
                message = _dict(data.get("message"))
                start_usage = message.get("usage")
                if isinstance(start_usage, dict):
                    usage.update(start_usage)
            elif event_type == "content_block_start":
                block = _dict(data.get("content_block"))
                if block.get("type") == "tool_use":
                    index = int(data.get("index", len(tools)))
                    tool_id = str(block.get("id", ""))
                    tool_name = str(block.get("name", ""))
                    tools[index] = {"id": tool_id, "name": tool_name, "arguments": ""}
                    yield StreamEvent(trace_id=trace_id, event_type="response.tool_call.delta", output=ProviderOutput(type="tool_call", id=tool_id, call_id=tool_id, name=tool_name, arguments=""))
            elif event_type == "content_block_delta":
                delta = _dict(data.get("delta"))
                if isinstance(delta.get("text"), str):
                    yield StreamEvent(trace_id=trace_id, event_type="response.output_text.delta", delta=delta["text"])
                elif isinstance(delta.get("partial_json"), str):
                    index = int(data.get("index", 0))
                    tool = tools.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    tool["arguments"] += delta["partial_json"]
                    yield StreamEvent(trace_id=trace_id, event_type="response.tool_call.delta", output=ProviderOutput(type="tool_call", id=tool["id"], call_id=tool["id"], name=tool["name"] or None, arguments=delta["partial_json"]))
            elif event_type == "message_delta":
                usage.update(_dict(data.get("usage")))
            elif event_type == "message_stop":
                stopped = True
        if not done or not stopped:
            raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider stream ended unexpectedly", retryable=True)
        yield StreamEvent(trace_id=trace_id, event_type="usage", output=ProviderOutput(type="usage", text=json.dumps(usage)))
        for tool in tools.values():
            yield StreamEvent(trace_id=trace_id, event_type="response.tool_call.completed", output=ProviderOutput(type="tool_call", id=tool["id"], call_id=tool["id"], name=tool["name"] or None, arguments=tool["arguments"]))
        yield StreamEvent(trace_id=trace_id, event_type="response.completed")

    async def embeddings(self, inputs: list[str], *, trace_id: str) -> EmbeddingResult:
        raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "embedding is not supported by this Provider", retryable=False)
    async def search(self, query: str, *, trace_id: str) -> ResponseResult:
        raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "native web search is not supported by this Provider", retryable=False)

async def _raise_for_status(response: httpx.Response, trace_id: str) -> None:
    if response.status_code in {401, 403}:
        raise _failure(trace_id, ProviderErrorCode.AUTHENTICATION, "provider authentication failed", retryable=False)
    if response.status_code == 429:
        raise _failure(trace_id, ProviderErrorCode.RATE_LIMITED, "provider rate limit reached", retryable=True)
    if response.status_code in {408, 504}:
        raise _failure(trace_id, ProviderErrorCode.TIMEOUT, "provider request timed out", retryable=True)
    if response.status_code == 422:
        try:
            await response.aread()
            body = response.json()
        except (ValueError, httpx.HTTPError):
            body = {}
        error = _dict(body.get("error")) if isinstance(body, dict) else {}
        marker = f"{error.get('code', '')} {error.get('type', '')}".lower()
        if any(term in marker for term in ("content", "safety", "filter")):
            raise _failure(trace_id, ProviderErrorCode.CONTENT_FILTERED, "provider filtered the content", retryable=False)
        raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "provider rejected the request", retryable=False)
    if response.status_code == 400:
        raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "provider rejected the request", retryable=False)
    if response.is_error:
        raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned an error", retryable=True)


def _failure_from_status(trace_id: str, status: Any, error: Any) -> ProviderFailure:
    marker = json.dumps(error, ensure_ascii=False).lower() if isinstance(error, (dict, list, str)) else ""
    if any(term in marker for term in ("content", "safety", "filter")):
        return _failure(trace_id, ProviderErrorCode.CONTENT_FILTERED, "provider filtered the content", retryable=False)
    if status in {"failed", "error", "cancelled", "incomplete"}:
        return _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider response did not complete", retryable=False)
    return _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned an invalid response", retryable=True)


def _failure_from_event(trace_id: str, event_type: str, data: dict[str, Any]) -> ProviderFailure:
    response = _dict(data.get("response"))
    return _failure_from_status(trace_id, response.get("status", event_type), response.get("error", data.get("error")))


def _parse_response(value: dict[str, Any], *, trace_id: str) -> ResponseResult:
    status = value.get("status")
    if status != "completed":
        raise _failure_from_status(trace_id, status, value.get("error"))
    output_items = value.get("output")
    if not isinstance(output_items, list):
        raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned an invalid response", retryable=True)
    if _contains_refusal(output_items):
        raise _failure(trace_id, ProviderErrorCode.REFUSED, "provider refused the request", retryable=False)
    output: list[ProviderOutput] = []
    tool_calls: list[ToolCallResult] = []
    citations: list[str] = []
    for item in output_items:
        if not isinstance(item, dict):
            raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned an invalid response", retryable=True)
        normalized = _normalize_output_item(item)
        output.append(normalized)
        citations.extend(normalized.annotations)
        if normalized.type in {"function_call", "tool_call"} and normalized.name is not None and normalized.arguments is not None:
            tool_calls.append(ToolCallResult(id=normalized.call_id or normalized.id or "", name=normalized.name, arguments=normalized.arguments))
    output_text = value.get("output_text")
    if not isinstance(output_text, str) or not output_text:
        output_text = _extract_text(output)
    return ResponseResult(
        trace_id=trace_id,
        id=str(value.get("id", "")),
        output_text=output_text,
        output=output,
        usage=_dict(value.get("usage")),
        tool_calls=tool_calls,
        citations=citations,
    )


def _contains_refusal(items: list[Any]) -> bool:
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "refusal":
            return True
        if any(isinstance(content, dict) and content.get("type") == "refusal" for content in item.get("content", [])):
            return True
    return False


def _normalize_output_item(item: dict[str, Any]) -> ProviderOutput:
    annotations: list[str] = []
    for content in item.get("content", []):
        if not isinstance(content, dict):
            continue
        for annotation in content.get("annotations", []):
            if isinstance(annotation, dict) and annotation.get("type") == "url_citation" and isinstance(annotation.get("url"), str):
                annotations.append(annotation["url"])
    for annotation in item.get("annotations", []):
        if isinstance(annotation, dict) and annotation.get("type") == "url_citation" and isinstance(annotation.get("url"), str):
            annotations.append(annotation["url"])
    return ProviderOutput(
        type=str(item.get("type", "unknown")),
        id=str(item["id"]) if isinstance(item.get("id"), str) else None,
        call_id=str(item["call_id"]) if isinstance(item.get("call_id"), str) else None,
        text=_extract_item_text(item),
        name=str(item["name"]) if isinstance(item.get("name"), str) else None,
        arguments=str(item["arguments"]) if isinstance(item.get("arguments"), str) else None,
        annotations=annotations,
    )


def _extract_item_text(item: dict[str, Any]) -> str | None:
    if isinstance(item.get("text"), str):
        return item["text"]
    parts = [content["text"] for content in item.get("content", []) if isinstance(content, dict) and isinstance(content.get("text"), str)]
    return "".join(parts) or None


def _parse_stream_event(data: dict[str, Any], *, trace_id: str) -> StreamEvent | None:
    event_type = str(data.get("type", "message"))
    delta = data.get("delta") if isinstance(data.get("delta"), str) else None
    output = data.get("item")
    if isinstance(output, dict) and _contains_refusal([output]):
        raise _failure(trace_id, ProviderErrorCode.REFUSED, "provider refused the request", retryable=False)
    normalized = _normalize_output_item(output) if isinstance(output, dict) else None
    if event_type in {"response.completed", "response.created", "response.in_progress"} and delta is None and normalized is None:
        return None
    return StreamEvent(schema_version=SCHEMA_VERSION, trace_id=trace_id, event_type=event_type, delta=delta, output=normalized)


def _extract_text(items: list[ProviderOutput]) -> str:
    return "".join(item.text for item in items if item.text is not None)


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


async def build_provider(session: Any, provider_id: str) -> ModelProvider:
    """Construct exactly the enabled protocol selected by stable ID."""
    from app.repository import read_provider_runtime_settings

    values = await read_provider_runtime_settings(session, provider_id)
    config = ProviderConfig(**values)
    if config.protocol_type == "openai_chat_completions":
        return OpenAIChatCompletionsProvider(config)
    if config.protocol_type == "anthropic_messages":
        return AnthropicMessagesProvider(config)
    if config.protocol_type == "openai_responses":
        return OpenAIResponsesProvider(config)
    raise ValueError("unsupported provider protocol")
