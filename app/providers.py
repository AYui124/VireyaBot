"""Provider boundary backed by the official OpenAI and Anthropic SDKs."""

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol

import anthropic
import httpx
import openai
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
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
    default_stream: bool = False
    temperature: float | None = None
    max_output_tokens: int = 4096
    extra_headers: dict[str, str] = field(default_factory=dict)

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

    async def stream_responses(self, request: ResponseRequest, *, trace_id: str) -> AsyncIterator[StreamEvent]: ...

    async def embeddings(self, inputs: list[str], *, trace_id: str) -> EmbeddingResult: ...


class SearchProvider(Protocol):
    async def search(self, query: str, *, trace_id: str) -> ResponseResult: ...


def _failure(trace_id: str, code: ProviderErrorCode, message: str, *, retryable: bool) -> ProviderFailure:
    return ProviderFailure(Failure(schema_version=SCHEMA_VERSION, trace_id=trace_id, error_code=code, retryable=retryable, message=message))


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dump(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    return {}


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


def _contains_refusal(items: list[Any]) -> bool:
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "refusal":
            return True
        if any(isinstance(content, dict) and content.get("type") == "refusal" for content in item.get("content", [])):
            return True
    return False


def _extract_item_text(item: dict[str, Any]) -> str | None:
    if isinstance(item.get("text"), str):
        return item["text"]
    parts = [content["text"] for content in item.get("content", []) if isinstance(content, dict) and isinstance(content.get("text"), str)]
    return "".join(parts) or None


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


def _extract_text(items: list[ProviderOutput]) -> str:
    return "".join(item.text for item in items if item.text is not None)


def _response_result(value: dict[str, Any], *, trace_id: str) -> ResponseResult:
    status = value.get("status")
    if status not in {None, "completed"}:
        raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider response did not complete", retryable=False)
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
    output_text = value.get("output_text") if isinstance(value.get("output_text"), str) else _extract_text(output)
    return ResponseResult(trace_id=trace_id, id=str(value.get("id", "")), output_text=output_text, output=output, usage=_dict(value.get("usage")), tool_calls=tool_calls, citations=citations)


def _map_sdk_failure(exc: Exception, trace_id: str) -> ProviderFailure:
    body = getattr(exc, "body", None)
    marker = json.dumps(body, ensure_ascii=False).lower() if isinstance(body, (dict, list, str)) else str(exc).lower()
    if any(term in marker for term in ("content", "safety", "filter")):
        return _failure(trace_id, ProviderErrorCode.CONTENT_FILTERED, "provider filtered the content", retryable=False)
    status = getattr(exc, "status_code", None)
    if isinstance(exc, (openai.AuthenticationError, anthropic.AuthenticationError)) or status in {401, 403}:
        return _failure(trace_id, ProviderErrorCode.AUTHENTICATION, "provider authentication failed", retryable=False)
    if isinstance(exc, (openai.RateLimitError, anthropic.RateLimitError)) or status == 429:
        return _failure(trace_id, ProviderErrorCode.RATE_LIMITED, "provider rate limit reached", retryable=True)
    if isinstance(exc, (openai.APITimeoutError, anthropic.APITimeoutError)) or status in {408, 504}:
        return _failure(trace_id, ProviderErrorCode.TIMEOUT, "provider request timed out", retryable=True)
    if isinstance(exc, (openai.BadRequestError, anthropic.BadRequestError)) or status in {400, 422}:
        return _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "provider rejected the request", retryable=False)
    if isinstance(exc, (openai.APIConnectionError, anthropic.APIConnectionError)):
        return _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider transport unavailable", retryable=True)
    return _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider request failed", retryable=True)


def _chat_messages(request: ResponseRequest) -> list[dict[str, Any]]:
    if isinstance(request.input, str):
        messages: list[dict[str, Any]] = [{"role": "user", "content": request.input}]
    else:
        messages = []
        for item in request.input:
            role = item.get("role", "user")
            content = item.get("content", "")
            if isinstance(content, list):
                converted: list[dict[str, Any]] = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "input_text":
                        converted.append({"type": "text", "text": part.get("text", "")})
                    elif part.get("type") == "input_image":
                        raise ValueError("image input is not supported by this Provider")
                    else:
                        converted.append(part)
                content = converted
            if role == "function_call_output":
                messages.append({"role": "tool", "tool_call_id": item.get("call_id", ""), "content": content})
            else:
                messages.append({"role": role, "content": content})
    if request.instructions:
        messages.insert(0, {"role": "system", "content": request.instructions})
    return messages


def _chat_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") != "function":
            raise ValueError("only function tools are supported by Chat Completions")
        function = {key: tool[key] for key in ("name", "description", "parameters") if key in tool}
        result.append({"type": "function", "function": function})
    return result


def _anthropic_messages(request: ResponseRequest) -> tuple[str | None, list[dict[str, Any]]]:
    if isinstance(request.input, str):
        return request.instructions, [{"role": "user", "content": request.input}]
    messages: list[dict[str, Any]] = []
    for item in request.input:
        role = item.get("role", "user")
        content = item.get("content", "")
        if role == "system":
            continue
        if isinstance(content, list):
            converted: list[dict[str, Any]] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "input_text":
                    converted.append({"type": "text", "text": part.get("text", "")})
                elif part.get("type") == "input_image":
                    raise ValueError("image input is not supported by this Provider")
                elif part.get("type") == "function_call_output":
                    converted.append({"type": "tool_result", "tool_use_id": part.get("call_id", ""), "content": part.get("output", "")})
                else:
                    converted.append(part)
            content = converted
        messages.append({"role": "assistant" if role == "assistant" else "user", "content": content})
    return request.instructions, messages


def _anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") != "function":
            raise ValueError("only function tools are supported by Anthropic Messages")
        result.append({key: tool[key] for key in ("name", "description") if key in tool} | {"input_schema": tool.get("parameters", {"type": "object"})})
    return result


class OpenAIResponsesProvider:
    capabilities = frozenset({ProviderCapability.TEXT, ProviderCapability.IMAGE_INPUT, ProviderCapability.STREAMING, ProviderCapability.STRUCTURED_OUTPUT, ProviderCapability.TOOL_CALLING, ProviderCapability.EMBEDDING, ProviderCapability.EMBEDDING_BATCH})

    def __init__(self, config: ProviderConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._http_client = client
        self._owns_http_client = client is None
        self._client = AsyncOpenAI(api_key=config.api_key, organization=config.organization, base_url=config.base_url, timeout=config.timeout_seconds, max_retries=0, default_headers=config.extra_headers or None, http_client=client)
        if config.native_search_enabled:
            self.capabilities = self.capabilities | {ProviderCapability.NATIVE_WEB_SEARCH}

    def capability_matrix(self) -> list[ProviderCapabilityStatus]:
        return [ProviderCapabilityStatus(capability=capability, adapter_supported=capability in self.capabilities, endpoint_verified=False, enabled=capability in self.capabilities and (capability != ProviderCapability.NATIVE_WEB_SEARCH or self.config.native_search_enabled)) for capability in ProviderCapability]

    async def _close(self) -> None:
        if self._owns_http_client:
            await self._client.close()

    def _request_kwargs(self, request: ResponseRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": request.model or self.config.responses_model, "input": request.input}
        if request.instructions is not None:
            kwargs["instructions"] = request.instructions
        if request.response_format is not None:
            kwargs["text"] = {"format": request.response_format}
        if request.tools:
            kwargs["tools"] = request.tools
        if self.config.temperature is not None:
            kwargs["temperature"] = self.config.temperature
        kwargs["max_output_tokens"] = self.config.max_output_tokens
        return kwargs

    async def responses(self, request: ResponseRequest, *, trace_id: str) -> ResponseResult:
        _validate_request(request, trace_id)
        if self.config.default_stream:
            events = [event async for event in self.stream_responses(request, trace_id=trace_id)]
            text = "".join(event.delta or "" for event in events)
            usage_event = next((event for event in events if event.event_type == "usage" and event.output), None)
            usage = json.loads(usage_event.output.text) if usage_event and usage_event.output and usage_event.output.text else {}
            return ResponseResult(trace_id=trace_id, id="", output_text=text, output=[ProviderOutput(type="message", text=text)], usage=usage)
        try:
            value = _dump(await self._client.responses.create(**self._request_kwargs(request)))
            return _response_result(value, trace_id=trace_id)
        except ProviderFailure:
            raise
        except Exception as exc:
            raise _map_sdk_failure(exc, trace_id) from exc

    async def stream_responses(self, request: ResponseRequest, *, trace_id: str) -> AsyncIterator[StreamEvent]:
        _validate_request(request, trace_id)
        stream: Any = None
        completed = False
        try:
            stream_kwargs = self._request_kwargs(request)
            stream_kwargs["stream"] = True
            stream = await self._client.responses.create(**stream_kwargs)
            async for raw_event in stream:
                data = _dump(raw_event)
                event_type = str(data.get("type", "message"))
                if event_type in {"response.failed", "response.error", "error", "response.incomplete"}:
                    raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider stream returned an error", retryable=False)
                if event_type == "response.completed":
                    response_data = _dict(data.get("response"))
                    usage = response_data.get("usage", data.get("usage"))
                    if isinstance(usage, dict):
                        yield StreamEvent(trace_id=trace_id, event_type="usage", output=ProviderOutput(type="usage", text=json.dumps(usage)))
                    completed = True
                    yield StreamEvent(trace_id=trace_id, event_type="response.completed")
                    continue
                delta = data.get("delta") if isinstance(data.get("delta"), str) else None
                item = _dump(data.get("item"))
                normalized = _normalize_output_item(item) if item else None
                if normalized and normalized.type in {"function_call", "tool_call"} and normalized.name is not None:
                    yield StreamEvent(trace_id=trace_id, event_type="response.tool_call.delta", output=normalized)
                elif delta is not None:
                    yield StreamEvent(trace_id=trace_id, event_type=event_type, delta=delta)
            if not completed:
                raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider stream ended unexpectedly", retryable=True)
        except ProviderFailure:
            raise
        except Exception as exc:
            raise _map_sdk_failure(exc, trace_id) from exc
        finally:
            if stream is not None and hasattr(stream, "close"):
                await stream.close()

    async def embeddings(self, inputs: list[str], *, trace_id: str) -> EmbeddingResult:
        if not inputs:
            raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "embedding input cannot be empty", retryable=False)
        try:
            kwargs: dict[str, Any] = {"model": self.config.embedding_model, "input": inputs}
            if self.config.embedding_dimensions is not None:
                kwargs["dimensions"] = self.config.embedding_dimensions
            value = _dump(await self._client.embeddings.create(**kwargs))
            items = value.get("data")
            if not isinstance(items, list) or len(items) != len(inputs):
                raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned mismatched embeddings", retryable=True)
            result: list[EmbeddingItem] = []
            dimensions: int | None = None
            for expected_index, item in enumerate(items):
                embedding = _dump(item).get("embedding")
                index = _dump(item).get("index")
                if index != expected_index or not isinstance(embedding, list):
                    raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned invalid embeddings", retryable=True)
                vector = [float(value) for value in embedding if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)]
                if len(vector) != len(embedding) or not vector or (dimensions is not None and len(vector) != dimensions):
                    raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned invalid embeddings", retryable=True)
                dimensions = len(vector)
                result.append(EmbeddingItem(index=expected_index, vector=vector))
            assert dimensions is not None
            if self.config.embedding_dimensions is not None and dimensions != self.config.embedding_dimensions:
                raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned configured-incompatible embedding dimensions", retryable=True)
            return EmbeddingResult(trace_id=trace_id, model=str(value.get("model", self.config.embedding_model)), data=result, dimensions=dimensions, usage=_dict(value.get("usage")))
        except ProviderFailure:
            raise
        except Exception as exc:
            raise _map_sdk_failure(exc, trace_id) from exc

    async def search(self, query: str, *, trace_id: str) -> ResponseResult:
        if not self.config.native_search_enabled:
            raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "native web search is not enabled", retryable=False)
        return await self.responses(ResponseRequest(input=query, tools=[{"type": "web_search_preview"}]), trace_id=trace_id)


class OpenAIChatCompletionsProvider(OpenAIResponsesProvider):
    capabilities = frozenset({ProviderCapability.TEXT, ProviderCapability.STREAMING, ProviderCapability.STRUCTURED_OUTPUT, ProviderCapability.TOOL_CALLING, ProviderCapability.EMBEDDING, ProviderCapability.EMBEDDING_BATCH})

    def __init__(self, config: ProviderConfig, *, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(config, client=client)
        self.capabilities = frozenset(self.capabilities - {ProviderCapability.NATIVE_WEB_SEARCH})

    async def responses(self, request: ResponseRequest, *, trace_id: str) -> ResponseResult:
        _validate_request(request, trace_id)
        if self.config.default_stream:
            events = [event async for event in self.stream_responses(request, trace_id=trace_id)]
            text = "".join(event.delta or "" for event in events)
            usage_event = next((event for event in events if event.event_type == "usage" and event.output), None)
            usage = json.loads(usage_event.output.text) if usage_event and usage_event.output and usage_event.output.text else {}
            return ResponseResult(trace_id=trace_id, id="", output_text=text, output=[ProviderOutput(type="message", text=text)], usage=usage)
        if _has_image_input(request.input):
            raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "image input is not supported by this Provider", retryable=False)
        try:
            kwargs: dict[str, Any] = {"model": request.model or self.config.responses_model, "messages": _chat_messages(request), "max_completion_tokens": self.config.max_output_tokens}
            if request.response_format is not None:
                kwargs["response_format"] = request.response_format
            if self.config.temperature is not None:
                kwargs["temperature"] = self.config.temperature
            if request.tools:
                kwargs["tools"] = _chat_tools(request.tools)
            value = _dump(await self._client.chat.completions.create(**kwargs))
            choices = value.get("choices")
            if not isinstance(choices, list) or not choices:
                raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned an invalid response", retryable=True)
            message = _dict(_dump(choices[0]).get("message"))
            text = message.get("content") if isinstance(message.get("content"), str) else ""
            output = [ProviderOutput(type="message", id=str(value.get("id", "")), text=text)]
            calls: list[ToolCallResult] = []
            for call in message.get("tool_calls", []):
                function = _dict(_dump(call).get("function"))
                if isinstance(function.get("name"), str):
                    arguments = str(function.get("arguments", ""))
                    call_id = str(_dump(call).get("id", ""))
                    calls.append(ToolCallResult(id=call_id, name=function["name"], arguments=arguments))
                    output.append(ProviderOutput(type="tool_call", id=call_id, call_id=call_id, name=function["name"], arguments=arguments))
            return ResponseResult(trace_id=trace_id, id=str(value.get("id", "")), output_text=text, output=output, usage=_dict(value.get("usage")), tool_calls=calls)
        except ProviderFailure:
            raise
        except ValueError as exc:
            raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, str(exc), retryable=False) from exc
        except Exception as exc:
            raise _map_sdk_failure(exc, trace_id) from exc

    async def stream_responses(self, request: ResponseRequest, *, trace_id: str) -> AsyncIterator[StreamEvent]:
        _validate_request(request, trace_id)
        if _has_image_input(request.input):
            raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "image input is not supported by this Provider", retryable=False)
        kwargs: dict[str, Any] = {"model": request.model or self.config.responses_model, "messages": _chat_messages(request), "stream": True, "stream_options": {"include_usage": True}}
        if request.response_format is not None:
            kwargs["response_format"] = request.response_format
        if request.tools:
            kwargs["tools"] = _chat_tools(request.tools)
        if self.config.temperature is not None:
            kwargs["temperature"] = self.config.temperature
        stream: Any = None
        finished = False
        tool_calls: dict[int, dict[str, str]] = {}
        try:
            stream = await self._client.chat.completions.create(**kwargs)
            async for raw_chunk in stream:
                chunk = _dump(raw_chunk)
                usage = chunk.get("usage")
                if isinstance(usage, dict):
                    usage_seen = True
                    yield StreamEvent(trace_id=trace_id, event_type="usage", output=ProviderOutput(type="usage", text=json.dumps(usage)))
                choices = chunk.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                choice = _dump(choices[0])
                delta = _dict(_dump(choice.get("delta")))
                text = delta.get("content")
                if isinstance(text, str) and text:
                    yield StreamEvent(trace_id=trace_id, event_type="response.output_text.delta", delta=text)
                for call in delta.get("tool_calls", []):
                    call_dict = _dump(call)
                    function = _dict(call_dict.get("function"))
                    if function:
                        index = int(call_dict.get("index", len(tool_calls)))
                        current = tool_calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        if call_dict.get("id"):
                            current["id"] = str(call_dict["id"])
                        if function.get("name"):
                            current["name"] = str(function["name"])
                        current["arguments"] += str(function.get("arguments", ""))
                        yield StreamEvent(trace_id=trace_id, event_type="response.tool_call.delta", output=ProviderOutput(type="tool_call", id=current["id"], call_id=current["id"], name=current["name"] or None, arguments=str(function.get("arguments", ""))))
                if choice.get("finish_reason"):
                    finished = True
            if not finished:
                raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider stream ended unexpectedly", retryable=True)
            for current in tool_calls.values():
                yield StreamEvent(trace_id=trace_id, event_type="response.tool_call.completed", output=ProviderOutput(type="tool_call", id=current["id"], call_id=current["id"], name=current["name"] or None, arguments=current["arguments"]))
            if not usage_seen:
                yield StreamEvent(trace_id=trace_id, event_type="usage", output=ProviderOutput(type="usage", text="{}"))
            yield StreamEvent(trace_id=trace_id, event_type="response.completed")
        except ProviderFailure:
            raise
        except Exception as exc:
            raise _map_sdk_failure(exc, trace_id) from exc
        finally:
            if stream is not None and hasattr(stream, "close"):
                await stream.close()

    async def search(self, query: str, *, trace_id: str) -> ResponseResult:
        raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "native web search is not supported by this Provider", retryable=False)


class AnthropicMessagesProvider(OpenAIChatCompletionsProvider):
    capabilities = frozenset({ProviderCapability.TEXT, ProviderCapability.STREAMING, ProviderCapability.TOOL_CALLING})

    def __init__(self, config: ProviderConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        anthropic_base_url = config.base_url.removesuffix("/v1")
        self._client = AsyncAnthropic(api_key=config.api_key, base_url=anthropic_base_url, timeout=config.timeout_seconds, max_retries=0, default_headers=config.extra_headers or None, http_client=client)
        self._http_client = client
        self._owns_http_client = client is None

    async def _close(self) -> None:
        if self._owns_http_client:
            await self._client.close()

    async def responses(self, request: ResponseRequest, *, trace_id: str) -> ResponseResult:
        _validate_request(request, trace_id)
        if self.config.default_stream:
            events = [event async for event in self.stream_responses(request, trace_id=trace_id)]
            text = "".join(event.delta or "" for event in events)
            usage_event = next((event for event in events if event.event_type == "usage" and event.output), None)
            usage = json.loads(usage_event.output.text) if usage_event and usage_event.output and usage_event.output.text else {}
            return ResponseResult(trace_id=trace_id, id="", output_text=text, output=[ProviderOutput(type="message", text=text)], usage=usage)
        if request.response_format is not None or _has_image_input(request.input):
            raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "requested capability is not supported by this Provider", retryable=False)
        system, messages = _anthropic_messages(request)
        try:
            kwargs: dict[str, Any] = {"model": request.model or self.config.responses_model, "max_tokens": self.config.max_output_tokens, "messages": messages}
            if system:
                kwargs["system"] = system
            if self.config.temperature is not None:
                kwargs["temperature"] = self.config.temperature
            if request.tools:
                kwargs["tools"] = _anthropic_tools(request.tools)
            value = _dump(await self._client.messages.create(**kwargs))
            output: list[ProviderOutput] = []
            calls: list[ToolCallResult] = []
            for item in value.get("content", []):
                item_dict = _dump(item)
                if item_dict.get("type") == "text":
                    output.append(ProviderOutput(type="message", text=item_dict.get("text", "")))
                elif item_dict.get("type") == "tool_use":
                    call_id = str(item_dict.get("id", ""))
                    name = str(item_dict.get("name", ""))
                    arguments = json.dumps(item_dict.get("input", {}), ensure_ascii=False)
                    calls.append(ToolCallResult(id=call_id, name=name, arguments=arguments))
                    output.append(ProviderOutput(type="tool_call", id=call_id, call_id=call_id, name=name, arguments=arguments))
            return ResponseResult(trace_id=trace_id, id=str(value.get("id", "")), output_text=_extract_text(output), output=output, usage=_dict(value.get("usage")), tool_calls=calls)
        except ProviderFailure:
            raise
        except Exception as exc:
            raise _map_sdk_failure(exc, trace_id) from exc

    async def stream_responses(self, request: ResponseRequest, *, trace_id: str) -> AsyncIterator[StreamEvent]:
        _validate_request(request, trace_id)
        if request.response_format is not None or _has_image_input(request.input):
            raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "requested capability is not supported by this Provider", retryable=False)
        system, messages = _anthropic_messages(request)
        kwargs: dict[str, Any] = {"model": request.model or self.config.responses_model, "max_tokens": self.config.max_output_tokens, "messages": messages, "stream": True}
        if system:
            kwargs["system"] = system
        if self.config.temperature is not None:
            kwargs["temperature"] = self.config.temperature
        if request.tools:
            kwargs["tools"] = _anthropic_tools(request.tools)
        stream: Any = None
        usage: dict[str, Any] = {}
        tools: dict[int, dict[str, str]] = {}
        stopped = False
        try:
            stream = await self._client.messages.create(**kwargs)
            async for raw_event in stream:
                data = _dump(raw_event)
                event_type = data.get("type")
                if event_type == "message_start":
                    usage.update(_dict(_dump(data.get("message")).get("usage")))
                elif event_type == "content_block_start":
                    block = _dump(data.get("content_block"))
                    if block.get("type") == "tool_use":
                        index = int(data.get("index", len(tools)))
                        call_id = str(block.get("id", ""))
                        tools[index] = {"id": call_id, "name": str(block.get("name", "")), "arguments": ""}
                        yield StreamEvent(trace_id=trace_id, event_type="response.tool_call.delta", output=ProviderOutput(type="tool_call", id=call_id, call_id=call_id, name=tools[index]["name"], arguments=""))
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
            if not stopped:
                raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider stream ended unexpectedly", retryable=True)
            yield StreamEvent(trace_id=trace_id, event_type="usage", output=ProviderOutput(type="usage", text=json.dumps(usage)))
            for tool in tools.values():
                yield StreamEvent(trace_id=trace_id, event_type="response.tool_call.completed", output=ProviderOutput(type="tool_call", id=tool["id"], call_id=tool["id"], name=tool["name"] or None, arguments=tool["arguments"]))
            yield StreamEvent(trace_id=trace_id, event_type="response.completed")
        except ProviderFailure:
            raise
        except Exception as exc:
            raise _map_sdk_failure(exc, trace_id) from exc
        finally:
            if stream is not None and hasattr(stream, "close"):
                await stream.close()

    async def embeddings(self, inputs: list[str], *, trace_id: str) -> EmbeddingResult:
        raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "embedding is not supported by this Provider", retryable=False)

    async def search(self, query: str, *, trace_id: str) -> ResponseResult:
        raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "native web search is not supported by this Provider", retryable=False)


def redact_provider_payload(value: Any) -> Any:
    """Return evidence-safe JSON with credentials and sensitive headers removed."""
    secret_names = {"api_key", "authorization", "cookie", "password", "token", "secret"}
    if isinstance(value, Mapping):
        return {str(key): "[REDACTED]" if str(key).lower() in secret_names else redact_provider_payload(item) for key, item in value.items()}
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
