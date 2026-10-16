"""Provider boundary backed by the official OpenAI and Anthropic SDKs."""

from __future__ import annotations

import json
import logging
import math
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
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


class _SdkDiagnosticFilter(logging.Filter):
    """Keep diagnostic events/status codes, not SDK or HTTP wire payloads."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith("httpcore."):
            # httpcore.Trace embeds repr(headers/errors) into msg, not args.
            # Its first token is the transport-owned event name and phase.
            event, _, detail = str(record.msg).partition(" ")
            record.msg = event + (" [REDACTED]" if detail else "")
            record.args = ()
        elif isinstance(record.args, tuple):
            record.args = tuple(value if type(value) in {int, float} else "[REDACTED]" for value in record.args)
        elif record.args:
            record.args = {"redacted": True}
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


_SDK_DIAGNOSTIC_FILTER = _SdkDiagnosticFilter()


def _sanitize_sdk_logging() -> None:
    names = [
        f"{package}.{module}"
        for package in ("openai", "anthropic")
        for module in ("_base_client", "_response", "_legacy_response")
    ]
    # Logger filters do not run on ancestor loggers during propagation.
    names.extend(("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy", "httpcore.socks"))
    for name in names:
        logger = logging.getLogger(name)
        if _SDK_DIAGNOSTIC_FILTER not in logger.filters:
            logger.addFilter(_SDK_DIAGNOSTIC_FILTER)


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
    project: str | None = None
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
    stream: bool | None = None

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
    response_id: str | None = None
    usage: dict[str, Any] | None = None


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
        return value.model_dump(exclude_none=True, warnings="error")
    return {}


def _validate_request(request: ResponseRequest, trace_id: str) -> None:
    if request.schema_version != SCHEMA_VERSION:
        raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "unsupported provider schema version", retryable=False)
    if not trace_id or (request.trace_id and request.trace_id != trace_id):
        raise _failure(trace_id or request.trace_id, ProviderErrorCode.INVALID_REQUEST, "provider trace_id conflict", retryable=False)


def _has_image_input(value: Any) -> bool:
    if isinstance(value, list):
        return any(_has_image_input(item) for item in value)
    if isinstance(value, dict):
        return value.get("type") in {"input_image", "image_url", "image"} or _has_image_input(value.get("content")) or _has_image_input(value.get("output"))
    return False


def _valid_content(content: Any, *, images: bool) -> bool:
    if isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    for part in content:
        if not isinstance(part, dict):
            return False
        if part.get("type") in {"input_text", "output_text", "text"}:
            if not isinstance(part.get("text"), str):
                return False
        elif images and part.get("type") == "input_image":
            if not isinstance(part.get("image_url"), str) and not isinstance(part.get("file_id"), str):
                return False
        else:
            return False
    return True


def _message_content(content: str | list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    return [{"type": "text", "text": part["text"]} if part.get("type") in {"input_text", "output_text"} else part for part in content]


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
    if item.get("type") in {"text", "output_text"} and isinstance(item.get("text"), str):
        return item["text"]
    parts = [content["text"] for content in item.get("content", []) if isinstance(content, dict) and content.get("type") in {"text", "output_text"} and isinstance(content.get("text"), str)]
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


def _response_identity(value: Any, trace_id: str, previous: str | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商响应缺少有效标识", retryable=True)
    if previous is not None and value != previous:
        raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商响应标识冲突", retryable=True)
    return value


def _response_result(value: dict[str, Any], *, trace_id: str) -> ResponseResult:
    status = value.get("status")
    if status not in {None, "completed"}:
        raise _upstream_failure(value.get("error") or value.get("incomplete_details"), trace_id)
    response_id = _response_identity(value.get("id"), trace_id)
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
        if normalized.type in {"function_call", "tool_call"}:
            tool_calls.append(_tool_result(normalized, trace_id))
    output_text = value.get("output_text") if isinstance(value.get("output_text"), str) else _extract_text(output)
    return ResponseResult(trace_id=trace_id, id=response_id, output_text=output_text, output=output, usage=_dict(value.get("usage")), tool_calls=tool_calls, citations=citations)


def _error_markers(value: Any) -> set[str]:
    if not isinstance(value, dict):
        return set()
    markers = {value[key].lower() for key in ("type", "code", "reason") if isinstance(value.get(key), str)}
    return markers | _error_markers(value.get("error"))


def _upstream_failure(value: Any, trace_id: str) -> ProviderFailure:
    markers = _error_markers(value)
    if markers & {"content_filter", "content_filtered", "content_policy_violation", "safety_violation"}:
        return _failure(trace_id, ProviderErrorCode.CONTENT_FILTERED, "服务商过滤了请求内容", retryable=False)
    if markers & {"authentication_error", "invalid_api_key", "permission_error"}:
        return _failure(trace_id, ProviderErrorCode.AUTHENTICATION, "服务商身份验证失败", retryable=False)
    if markers & {"rate_limit_error", "rate_limit_exceeded"}:
        return _failure(trace_id, ProviderErrorCode.RATE_LIMITED, "服务商请求频率已达上限", retryable=True)
    if markers & {"timeout", "timeout_error", "request_timeout"}:
        return _failure(trace_id, ProviderErrorCode.TIMEOUT, "服务商请求超时", retryable=True)
    if markers & {"invalid_request_error", "invalid_request"}:
        return _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "服务商拒绝了请求参数", retryable=False)
    return _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商响应未完成", retryable=True)


def _map_sdk_failure(exc: Exception, trace_id: str) -> ProviderFailure:
    status = getattr(exc, "status_code", None)
    if isinstance(exc, (openai.AuthenticationError, anthropic.AuthenticationError)) or status in {401, 403}:
        return _failure(trace_id, ProviderErrorCode.AUTHENTICATION, "服务商身份验证失败", retryable=False)
    if isinstance(exc, (openai.RateLimitError, anthropic.RateLimitError)) or status == 429:
        return _failure(trace_id, ProviderErrorCode.RATE_LIMITED, "服务商请求频率已达上限", retryable=True)
    if isinstance(exc, (openai.APITimeoutError, anthropic.APITimeoutError, httpx.TimeoutException, TimeoutError)) or status in {408, 504}:
        return _failure(trace_id, ProviderErrorCode.TIMEOUT, "服务商请求超时", retryable=True)
    mapped = _upstream_failure(getattr(exc, "body", None), trace_id)
    if mapped.failure.error_code != ProviderErrorCode.UNAVAILABLE:
        return mapped
    if isinstance(exc, (openai.BadRequestError, anthropic.BadRequestError)) or status in {400, 422}:
        return _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "服务商拒绝了请求参数", retryable=False)
    return _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商暂时不可用", retryable=True)


@asynccontextmanager
async def _sdk_boundary(trace_id: str) -> AsyncIterator[None]:
    """Keep SDK exception bodies out of logger.exception's rendered traceback."""
    try:
        yield
    except ProviderFailure:
        raise
    except (openai.OpenAIError, anthropic.AnthropicError, httpx.HTTPError, TimeoutError, ValueError, TypeError, KeyError, AttributeError) as exc:
        failure = _map_sdk_failure(exc, trace_id)
    else:
        return
    raise failure from None


@asynccontextmanager
async def _sdk_stream(create: Any, trace_id: str) -> AsyncIterator[Any]:
    async with _sdk_boundary(trace_id):
        stream = await create
        try:
            yield stream
        finally:
            await stream.close()


def _chat_messages(request: ResponseRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if request.instructions:
        messages.append({"role": "system", "content": request.instructions})
    items = [{"role": "user", "content": request.input}] if isinstance(request.input, str) else request.input
    for item in items:
        if item.get("type") == "function_call":
            call = {"id": item["call_id"], "type": "function", "function": {"name": item["name"], "arguments": item["arguments"]}}
            if messages and messages[-1].get("tool_calls"):
                messages[-1]["tool_calls"].append(call)
            else:
                messages.append({"role": "assistant", "tool_calls": [call]})
        elif item.get("type") == "function_call_output":
            messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": _message_content(item["output"])})
        else:
            messages.append({"role": item.get("role", "user"), "content": _message_content(item.get("content", ""))})
    return messages


def _chat_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {key: tool[key] for key in ("name", "description", "parameters", "strict") if key in tool}} for tool in tools]


def _anthropic_messages(request: ResponseRequest) -> tuple[str | None, list[dict[str, Any]]]:
    system = [request.instructions] if request.instructions else []
    messages: list[dict[str, Any]] = []
    items = [{"role": "user", "content": request.input}] if isinstance(request.input, str) else request.input
    for item in items:
        role = item.get("role", "user")
        if item.get("type") == "function_call":
            role = "assistant"
            content = [{"type": "tool_use", "id": item["call_id"], "name": item["name"], "input": json.loads(item["arguments"])}]
        elif item.get("type") == "function_call_output":
            role = "user"
            content = [{"type": "tool_result", "tool_use_id": item["call_id"], "content": _message_content(item["output"])}]
        else:
            raw = item.get("content", "")
            content = [{"type": "text", "text": raw}] if isinstance(raw, str) else _message_content(raw)
        if role in {"system", "developer"}:
            system.extend(part["text"] for part in content if part.get("type") == "text")
        elif messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(content)
        else:
            messages.append({"role": role, "content": content})
    return "\n\n".join(system) or None, messages


def _anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: tool[key] for key in ("name", "description") if key in tool} | {"input_schema": tool.get("parameters", {"type": "object"})} for tool in tools]


def _tool_result(output: ProviderOutput, trace_id: str) -> ToolCallResult:
    if not output.call_id or not output.name or output.arguments is None:
        raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商返回了不完整的工具调用", retryable=True)
    try:
        arguments = json.loads(output.arguments)
    except (ValueError, TypeError):
        raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商返回了无效的工具参数", retryable=True) from None
    if not isinstance(arguments, dict):
        raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商返回了无效的工具参数", retryable=True)
    return ToolCallResult(id=output.call_id, name=output.name, arguments=output.arguments)


def _check_finish(reason: Any, trace_id: str, *, anthropic_protocol: bool = False) -> None:
    if reason == "content_filter":
        raise _failure(trace_id, ProviderErrorCode.CONTENT_FILTERED, "服务商过滤了请求内容", retryable=False)
    if reason == "refusal":
        raise _failure(trace_id, ProviderErrorCode.REFUSED, "服务商拒绝了请求", retryable=False)
    legal = {"end_turn", "tool_use", "stop_sequence"} if anthropic_protocol else {"stop", "tool_calls"}
    if reason not in legal:
        raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商响应未完成", retryable=False)


async def _aggregate(events: AsyncIterator[StreamEvent], trace_id: str) -> ResponseResult:
    response_id = ""
    text: list[str] = []
    output: list[ProviderOutput] = []
    calls: dict[str, ToolCallResult] = {}
    usage: dict[str, Any] = {}
    citations: list[str] = []
    completed = False
    try:
        async for event in events:
            if event.response_id:
                response_id = event.response_id
            if event.usage is not None:
                usage.update(event.usage)
            if event.event_type == "response.output_text.delta" and event.delta is not None:
                text.append(event.delta)
            if event.output is not None and event.event_type in {"response.output_item.done", "response.tool_call.completed"}:
                item = event.output
                if item.type in {"function_call", "tool_call"}:
                    call = _tool_result(item, trace_id)
                    if call.id not in calls:
                        output.append(item)
                    calls[call.id] = call
                else:
                    output.append(item)
                citations.extend(url for url in item.annotations if url not in citations)
            completed = completed or event.event_type == "response.completed"
    finally:
        await events.aclose()
    if not completed:
        raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商流式响应意外结束", retryable=True)
    output_text = "".join(text)
    if not any(item.type == "message" for item in output) and output_text:
        output.insert(0, ProviderOutput(type="message", text=output_text))
    return ResponseResult(trace_id=trace_id, id=response_id, output_text=output_text, output=output, usage=usage, tool_calls=list(calls.values()), citations=citations)


class OpenAIResponsesProvider:
    capabilities = frozenset({ProviderCapability.TEXT, ProviderCapability.IMAGE_INPUT, ProviderCapability.STREAMING, ProviderCapability.STRUCTURED_OUTPUT, ProviderCapability.TOOL_CALLING, ProviderCapability.EMBEDDING, ProviderCapability.EMBEDDING_BATCH})

    def __init__(self, config: ProviderConfig, *, client: httpx.AsyncClient | None = None) -> None:
        _sanitize_sdk_logging()
        self.config = config
        self._http_client = client
        self._owns_http_client = client is None
        self._client = AsyncOpenAI(api_key=config.api_key, organization=config.organization, project=config.project, base_url=config.base_url, timeout=config.timeout_seconds, max_retries=0, default_headers=config.extra_headers or None, http_client=client)
        if config.native_search_enabled:
            self.capabilities = self.capabilities | {ProviderCapability.NATIVE_WEB_SEARCH}

    def capability_matrix(self) -> list[ProviderCapabilityStatus]:
        return [ProviderCapabilityStatus(capability=capability, adapter_supported=capability in self.capabilities, endpoint_verified=False, enabled=capability in self.capabilities and (capability != ProviderCapability.NATIVE_WEB_SEARCH or self.config.native_search_enabled)) for capability in ProviderCapability]

    async def _close(self) -> None:
        if self._owns_http_client:
            await self._client.close()

    def _validate(self, request: ResponseRequest, trace_id: str, *, streaming: bool = False) -> None:
        _validate_request(request, trace_id)
        invalid = _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "请求参数或能力不受此服务商支持", retryable=False)
        if request.stream is not None and not isinstance(request.stream, bool):
            raise invalid
        if (streaming or self._use_stream(request)) and ProviderCapability.STREAMING not in self.capabilities:
            raise invalid
        if request.tools and ProviderCapability.TOOL_CALLING not in self.capabilities:
            raise invalid
        if _has_image_input(request.input) and ProviderCapability.IMAGE_INPUT not in self.capabilities:
            raise invalid
        if request.response_format is not None:
            if ProviderCapability.STRUCTURED_OUTPUT not in self.capabilities or not isinstance(request.response_format, dict):
                raise invalid
            if request.response_format.get("type") not in {"text", "json_object", "json_schema"}:
                raise invalid
            if request.response_format.get("type") == "json_schema":
                schema = request.response_format.get("json_schema", request.response_format)
                if not isinstance(schema, dict) or not isinstance(schema.get("name"), str) or not schema["name"] or not isinstance(schema.get("schema"), dict):
                    raise invalid
        if not isinstance(request.tools, list):
            raise invalid
        for tool in request.tools:
            if not isinstance(tool, dict):
                raise invalid
            if tool.get("type") == "function":
                if not isinstance(tool.get("name"), str) or not tool["name"] or not isinstance(tool.get("parameters", {}), dict):
                    raise invalid
            elif tool.get("type") in {"web_search", "web_search_preview"}:
                if ProviderCapability.NATIVE_WEB_SEARCH not in self.capabilities:
                    raise invalid
            else:
                raise invalid
        if not isinstance(request.input, (str, list)):
            raise invalid
        if isinstance(request.input, list):
            for item in request.input:
                if not isinstance(item, dict) or item.get("type") not in {None, "message", "function_call", "function_call_output"}:
                    raise invalid
                if item.get("type") in {"function_call", "function_call_output"}:
                    if not isinstance(item.get("call_id"), str) or not item["call_id"]:
                        raise invalid
                    if item["type"] == "function_call":
                        if not isinstance(item.get("name"), str) or not item["name"] or not isinstance(item.get("arguments"), str):
                            raise invalid
                        try:
                            arguments = json.loads(item["arguments"])
                        except ValueError:
                            raise invalid from None
                        if not isinstance(arguments, dict):
                            raise invalid
                    elif not _valid_content(item.get("output"), images=ProviderCapability.IMAGE_INPUT in self.capabilities):
                        raise invalid
                else:
                    content = item.get("content", "")
                    if not _valid_content(content, images=ProviderCapability.IMAGE_INPUT in self.capabilities) or item.get("role", "user") not in {"user", "assistant", "system", "developer"}:
                        raise invalid

    def _use_stream(self, request: ResponseRequest) -> bool:
        return self.config.default_stream if request.stream is None else request.stream

    def _request_kwargs(self, request: ResponseRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": request.model or self.config.responses_model, "input": request.input}
        if request.instructions is not None:
            kwargs["instructions"] = request.instructions
        if request.response_format is not None:
            response_format = request.response_format
            if response_format.get("type") == "json_schema" and "json_schema" in response_format:
                response_format = {"type": "json_schema"} | response_format["json_schema"]
            kwargs["text"] = {"format": response_format}
        if request.tools:
            kwargs["tools"] = request.tools
        if self.config.temperature is not None:
            kwargs["temperature"] = self.config.temperature
        kwargs["max_output_tokens"] = self.config.max_output_tokens
        return kwargs

    async def responses(self, request: ResponseRequest, *, trace_id: str) -> ResponseResult:
        self._validate(request, trace_id)
        if self._use_stream(request):
            return await _aggregate(self.stream_responses(request, trace_id=trace_id), trace_id)
        async with _sdk_boundary(trace_id):
            value = _dump(await self._client.responses.create(**self._request_kwargs(request), stream=False))
            return _response_result(value, trace_id=trace_id)

    async def stream_responses(self, request: ResponseRequest, *, trace_id: str) -> AsyncIterator[StreamEvent]:
        self._validate(request, trace_id, streaming=True)
        response_id: str | None = None
        tools: dict[str, ProviderOutput] = {}
        done_items: set[str] = set()
        completed_tools: set[str] = set()
        completed: dict[str, Any] | None = None
        async with _sdk_stream(self._client.responses.create(**self._request_kwargs(request), stream=True), trace_id) as stream:
            async for raw_event in stream:
                data = _dump(raw_event)
                event_type = data.get("type")
                response = _dict(data.get("response"))
                if "id" in response or event_type in {"response.created", "response.in_progress", "response.completed"}:
                    response_id = _response_identity(response.get("id"), trace_id, response_id)
                if event_type in {"response.failed", "response.error", "error", "response.incomplete"}:
                    raise _upstream_failure(response.get("error") or response.get("incomplete_details") or data, trace_id)
                if event_type in {"response.refusal.delta", "response.refusal.done"}:
                    raise _failure(trace_id, ProviderErrorCode.REFUSED, "服务商拒绝了请求", retryable=False)
                if event_type == "response.created":
                    yield StreamEvent(trace_id=trace_id, event_type="response.created", response_id=response_id)
                elif event_type == "response.output_text.delta":
                    if isinstance(data.get("delta"), str):
                        yield StreamEvent(trace_id=trace_id, event_type=event_type, response_id=response_id, delta=data["delta"])
                elif event_type in {"response.output_item.added", "response.output_item.done"}:
                    item = _dict(data.get("item"))
                    if _contains_refusal([item]):
                        raise _failure(trace_id, ProviderErrorCode.REFUSED, "服务商拒绝了请求", retryable=False)
                    normalized = _normalize_output_item(item)
                    if normalized.type == "function_call":
                        if not normalized.id or not normalized.call_id or not normalized.name:
                            raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商返回了不完整的工具调用", retryable=True)
                        previous = tools.get(normalized.id)
                        if previous is not None and (previous.call_id != normalized.call_id or previous.name != normalized.name):
                            raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商工具调用标识冲突", retryable=True)
                        tools[normalized.id] = normalized
                        if event_type == "response.output_item.done":
                            _tool_result(normalized, trace_id)
                            if normalized.id not in completed_tools:
                                completed_tools.add(normalized.id)
                                yield StreamEvent(trace_id=trace_id, event_type="response.tool_call.completed", response_id=response_id, output=normalized)
                        else:
                            yield StreamEvent(trace_id=trace_id, event_type="response.tool_call.delta", response_id=response_id, output=normalized)
                    elif event_type == "response.output_item.done":
                        yield StreamEvent(trace_id=trace_id, event_type=event_type, response_id=response_id, output=normalized)
                    if event_type == "response.output_item.done" and normalized.id:
                        done_items.add(normalized.id)
                elif event_type in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
                    item_id = data.get("item_id")
                    if item_id not in tools:
                        raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商工具调用缺少标识", retryable=True)
                    current = tools[item_id]
                    if event_type.endswith(".delta"):
                        delta = data.get("delta")
                        if not isinstance(delta, str):
                            raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商工具参数无效", retryable=True)
                        tools[item_id] = current.model_copy(update={"arguments": (current.arguments or "") + delta})
                        yield StreamEvent(trace_id=trace_id, event_type="response.tool_call.delta", response_id=response_id, output=current.model_copy(update={"arguments": delta}))
                    else:
                        arguments = data.get("arguments")
                        if not isinstance(arguments, str):
                            raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商工具参数无效", retryable=True)
                        current = current.model_copy(update={"arguments": arguments})
                        tools[item_id] = current
                        _tool_result(current, trace_id)
                        completed_tools.add(item_id)
                        yield StreamEvent(trace_id=trace_id, event_type="response.tool_call.completed", response_id=response_id, output=current.model_copy())
                elif event_type == "response.completed":
                    result = _response_result(response, trace_id=trace_id)
                    response_id = result.id
                    for item in result.output:
                        if item.type == "function_call":
                            if item.id not in completed_tools:
                                _tool_result(item, trace_id)
                                if item.id:
                                    completed_tools.add(item.id)
                                yield StreamEvent(trace_id=trace_id, event_type="response.tool_call.completed", response_id=response_id, output=item)
                        elif not item.id or item.id not in done_items:
                            yield StreamEvent(trace_id=trace_id, event_type="response.output_item.done", response_id=response_id, output=item)
                    if set(tools) - completed_tools:
                        raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商工具调用未完成", retryable=True)
                    completed = response
                    break
        if completed is None:
            raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商流式响应意外结束", retryable=True)
        yield StreamEvent(trace_id=trace_id, event_type="usage", response_id=response_id, usage=_dict(completed.get("usage")))
        yield StreamEvent(trace_id=trace_id, event_type="response.completed", response_id=response_id)

    async def embeddings(self, inputs: list[str], *, trace_id: str) -> EmbeddingResult:
        if not trace_id or not inputs or any(not isinstance(item, str) for item in inputs):
            raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "向量输入和追踪标识不能为空", retryable=False)
        async with _sdk_boundary(trace_id):
            kwargs: dict[str, Any] = {"model": self.config.embedding_model, "input": inputs, "encoding_format": "float"}
            if self.config.embedding_dimensions is not None:
                kwargs["dimensions"] = self.config.embedding_dimensions
            # SDK model construction coerces boolean vector entries to floats.
            # Read public raw content before the SDK's typed embedding post-parser.
            response = await self._client.embeddings.with_raw_response.create(**kwargs)
            value = _dict(json.loads(response.content))
            items = value.get("data")
            if not isinstance(items, list) or len(items) != len(inputs):
                raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "provider returned mismatched embeddings", retryable=True)
            result: list[EmbeddingItem] = []
            dimensions: int | None = None
            for expected_index, item in enumerate(items):
                embedding = _dump(item).get("embedding")
                index = _dump(item).get("index")
                if type(index) is not int or index != expected_index or not isinstance(embedding, list):
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

    async def search(self, query: str, *, trace_id: str) -> ResponseResult:
        if ProviderCapability.NATIVE_WEB_SEARCH not in self.capabilities:
            raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "此服务商未启用原生网络搜索", retryable=False)
        return await self.responses(ResponseRequest(input=query, tools=[{"type": "web_search_preview"}]), trace_id=trace_id)


class OpenAIChatCompletionsProvider(OpenAIResponsesProvider):
    capabilities = frozenset({ProviderCapability.TEXT, ProviderCapability.STREAMING, ProviderCapability.STRUCTURED_OUTPUT, ProviderCapability.TOOL_CALLING, ProviderCapability.EMBEDDING, ProviderCapability.EMBEDDING_BATCH})

    def __init__(self, config: ProviderConfig, *, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(config, client=client)
        self.capabilities = frozenset(self.capabilities - {ProviderCapability.NATIVE_WEB_SEARCH})

    def _request_kwargs(self, request: ResponseRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": request.model or self.config.responses_model, "messages": _chat_messages(request), "max_completion_tokens": self.config.max_output_tokens}
        if request.response_format is not None:
            response_format = request.response_format
            if response_format.get("type") == "json_schema" and "json_schema" not in response_format:
                response_format = {"type": "json_schema", "json_schema": {key: value for key, value in response_format.items() if key != "type"}}
            kwargs["response_format"] = response_format
        if self.config.temperature is not None:
            kwargs["temperature"] = self.config.temperature
        if request.tools:
            kwargs["tools"] = _chat_tools(request.tools)
        return kwargs

    async def responses(self, request: ResponseRequest, *, trace_id: str) -> ResponseResult:
        self._validate(request, trace_id)
        if self._use_stream(request):
            return await _aggregate(self.stream_responses(request, trace_id=trace_id), trace_id)
        async with _sdk_boundary(trace_id):
            value = _dump(await self._client.chat.completions.create(**self._request_kwargs(request), stream=False))
            choices = value.get("choices")
            if not isinstance(choices, list) or len(choices) != 1:
                raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商返回了无效响应", retryable=True)
            choice = _dump(choices[0])
            _check_finish(choice.get("finish_reason"), trace_id)
            response_id = _response_identity(value.get("id"), trace_id)
            message = _dict(choice.get("message"))
            if message.get("refusal"):
                raise _failure(trace_id, ProviderErrorCode.REFUSED, "服务商拒绝了请求", retryable=False)
            text = message.get("content") if isinstance(message.get("content"), str) else ""
            output = [ProviderOutput(type="message", id=response_id, text=text)]
            calls: list[ToolCallResult] = []
            for call in message.get("tool_calls") or []:
                function = _dict(call.get("function"))
                item = ProviderOutput(type="tool_call", id=call.get("id"), call_id=call.get("id"), name=function.get("name"), arguments=function.get("arguments"))
                calls.append(_tool_result(item, trace_id))
                output.append(item)
            return ResponseResult(trace_id=trace_id, id=response_id, output_text=text, output=output, usage=_dict(value.get("usage")), tool_calls=calls)

    async def stream_responses(self, request: ResponseRequest, *, trace_id: str) -> AsyncIterator[StreamEvent]:
        self._validate(request, trace_id, streaming=True)
        kwargs = self._request_kwargs(request) | {"stream": True, "stream_options": {"include_usage": True}}
        finished = False
        response_id: str | None = None
        tool_calls: dict[int, ProviderOutput] = {}
        usage: dict[str, Any] = {}
        async with _sdk_stream(self._client.chat.completions.create(**kwargs), trace_id) as stream:
            async for raw_chunk in stream:
                chunk = _dump(raw_chunk)
                if chunk.get("error"):
                    raise _upstream_failure(chunk["error"], trace_id)
                response_id = _response_identity(chunk.get("id"), trace_id, response_id)
                if isinstance(chunk.get("usage"), dict):
                    usage.update(chunk["usage"])
                choices = chunk.get("choices")
                if not isinstance(choices, list):
                    raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商返回了无效流式响应", retryable=True)
                if not choices:
                    continue
                if finished or len(choices) != 1:
                    raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商流式响应顺序无效", retryable=True)
                choice = _dump(choices[0])
                delta = _dict(choice.get("delta"))
                if delta.get("refusal"):
                    raise _failure(trace_id, ProviderErrorCode.REFUSED, "服务商拒绝了请求", retryable=False)
                text = delta.get("content")
                if isinstance(text, str) and text:
                    yield StreamEvent(trace_id=trace_id, event_type="response.output_text.delta", response_id=response_id, delta=text)
                for call in delta.get("tool_calls") or []:
                    index = call.get("index")
                    if type(index) is not int or index < 0:
                        raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商工具调用索引无效", retryable=True)
                    current = tool_calls.setdefault(index, ProviderOutput(type="tool_call", id="", call_id="", name="", arguments=""))
                    function = _dict(call.get("function"))
                    call_id = (current.id or "") + (call.get("id") or "")
                    arguments = function.get("arguments") or ""
                    current = current.model_copy(update={"id": call_id, "call_id": call_id, "name": (current.name or "") + (function.get("name") or ""), "arguments": (current.arguments or "") + arguments})
                    tool_calls[index] = current
                    yield StreamEvent(trace_id=trace_id, event_type="response.tool_call.delta", response_id=response_id, output=current.model_copy(update={"arguments": arguments}))
                if choice.get("finish_reason") is not None:
                    _check_finish(choice["finish_reason"], trace_id)
                    finished = True
            if not finished:
                raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商流式响应意外结束", retryable=True)
            _response_identity(response_id, trace_id)
            for current in tool_calls.values():
                _tool_result(current, trace_id)
                yield StreamEvent(trace_id=trace_id, event_type="response.tool_call.completed", response_id=response_id, output=current)
        yield StreamEvent(trace_id=trace_id, event_type="usage", response_id=response_id, usage=usage)
        yield StreamEvent(trace_id=trace_id, event_type="response.completed", response_id=response_id)

    async def search(self, query: str, *, trace_id: str) -> ResponseResult:
        raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "此服务商不支持原生网络搜索", retryable=False)


class AnthropicMessagesProvider(OpenAIChatCompletionsProvider):
    capabilities = frozenset({ProviderCapability.TEXT, ProviderCapability.STREAMING, ProviderCapability.TOOL_CALLING})

    def __init__(self, config: ProviderConfig, *, client: httpx.AsyncClient | None = None) -> None:
        _sanitize_sdk_logging()
        self.config = config
        anthropic_base_url = config.base_url.rstrip("/").removesuffix("/v1")
        self._client = AsyncAnthropic(api_key=config.api_key, base_url=anthropic_base_url, timeout=config.timeout_seconds, max_retries=0, default_headers=config.extra_headers or None, http_client=client)
        self._http_client = client
        self._owns_http_client = client is None

    def _request_kwargs(self, request: ResponseRequest) -> dict[str, Any]:
        system, messages = _anthropic_messages(request)
        kwargs: dict[str, Any] = {"model": request.model or self.config.responses_model, "max_tokens": self.config.max_output_tokens, "messages": messages}
        if system:
            kwargs["system"] = system
        if self.config.temperature is not None:
            kwargs["temperature"] = self.config.temperature
        if request.tools:
            kwargs["tools"] = _anthropic_tools(request.tools)
        return kwargs

    async def responses(self, request: ResponseRequest, *, trace_id: str) -> ResponseResult:
        self._validate(request, trace_id)
        if self._use_stream(request):
            return await _aggregate(self.stream_responses(request, trace_id=trace_id), trace_id)
        async with _sdk_boundary(trace_id):
            value = _dump(await self._client.messages.create(**self._request_kwargs(request), stream=False))
            _check_finish(value.get("stop_reason"), trace_id, anthropic_protocol=True)
            response_id = _response_identity(value.get("id"), trace_id)
            output: list[ProviderOutput] = []
            calls: list[ToolCallResult] = []
            content = value.get("content")
            if not isinstance(content, list):
                raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商返回了无效响应", retryable=True)
            for block in content:
                if block.get("type") == "text":
                    output.append(ProviderOutput(type="message", text=block.get("text", "")))
                elif block.get("type") == "tool_use":
                    item = ProviderOutput(type="tool_call", id=block.get("id"), call_id=block.get("id"), name=block.get("name"), arguments=json.dumps(block.get("input"), ensure_ascii=False))
                    calls.append(_tool_result(item, trace_id))
                    output.append(item)
                elif block.get("type") == "refusal":
                    raise _failure(trace_id, ProviderErrorCode.REFUSED, "服务商拒绝了请求", retryable=False)
            return ResponseResult(trace_id=trace_id, id=response_id, output_text=_extract_text(output), output=output, usage=_dict(value.get("usage")), tool_calls=calls)

    async def stream_responses(self, request: ResponseRequest, *, trace_id: str) -> AsyncIterator[StreamEvent]:
        self._validate(request, trace_id, streaming=True)
        usage: dict[str, Any] = {}
        tools: dict[int, ProviderOutput] = {}
        blocks: dict[int, str] = {}
        stopped = False
        stop_reason: str | None = None
        response_id: str | None = None
        async with _sdk_stream(self._client.messages.create(**self._request_kwargs(request), stream=True), trace_id) as stream:
            async for raw_event in stream:
                data = _dump(raw_event)
                event_type = data.get("type")
                if event_type == "error":
                    raise _upstream_failure(data, trace_id)
                if event_type == "message_start":
                    message = _dict(data.get("message"))
                    response_id = _response_identity(message.get("id"), trace_id, response_id)
                    usage.update(_dict(message.get("usage")))
                    yield StreamEvent(trace_id=trace_id, event_type="response.created", response_id=response_id)
                elif event_type == "content_block_start":
                    index = data.get("index")
                    if type(index) is not int or index < 0 or index in blocks:
                        raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商内容块索引无效", retryable=True)
                    block = _dict(data.get("content_block"))
                    blocks[index] = block.get("type", "")
                    if block.get("type") == "tool_use":
                        item = ProviderOutput(type="tool_call", id=block.get("id"), call_id=block.get("id"), name=block.get("name"), arguments=json.dumps(block["input"], ensure_ascii=False) if block.get("input") else "")
                        if not item.call_id or not item.name:
                            raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商工具调用缺少标识", retryable=True)
                        tools[index] = item
                        yield StreamEvent(trace_id=trace_id, event_type="response.tool_call.delta", response_id=response_id, output=item.model_copy())
                    elif block.get("type") == "text" and block.get("text"):
                        yield StreamEvent(trace_id=trace_id, event_type="response.output_text.delta", response_id=response_id, delta=block["text"])
                elif event_type == "content_block_delta":
                    index = data.get("index")
                    if index not in blocks:
                        raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商内容块缺少起始事件", retryable=True)
                    delta = _dict(data.get("delta"))
                    if delta.get("type") == "text_delta" and blocks[index] == "text":
                        yield StreamEvent(trace_id=trace_id, event_type="response.output_text.delta", response_id=response_id, delta=delta["text"])
                    elif delta.get("type") == "input_json_delta" and index in tools:
                        tool = tools[index]
                        partial = delta["partial_json"]
                        tools[index] = tool.model_copy(update={"arguments": (tool.arguments or "") + partial})
                        yield StreamEvent(trace_id=trace_id, event_type="response.tool_call.delta", response_id=response_id, output=tool.model_copy(update={"arguments": partial}))
                    elif delta.get("type") in {"text_delta", "input_json_delta"}:
                        raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商内容块类型冲突", retryable=True)
                elif event_type == "content_block_stop":
                    index = data.get("index")
                    if index not in blocks:
                        raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商内容块结束事件无效", retryable=True)
                    blocks.pop(index)
                    if index in tools:
                        tool = tools.pop(index)
                        if not tool.arguments:
                            tool = tool.model_copy(update={"arguments": "{}"})
                        _tool_result(tool, trace_id)
                        yield StreamEvent(trace_id=trace_id, event_type="response.tool_call.completed", response_id=response_id, output=tool)
                elif event_type == "message_delta":
                    usage.update(_dict(data.get("usage")))
                    reason = _dict(data.get("delta")).get("stop_reason")
                    if reason is not None:
                        _check_finish(reason, trace_id, anthropic_protocol=True)
                        stop_reason = reason
                elif event_type == "message_stop":
                    if blocks:
                        raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商内容块未完成", retryable=True)
                    _check_finish(stop_reason, trace_id, anthropic_protocol=True)
                    _response_identity(response_id, trace_id)
                    stopped = True
                    break
        if not stopped:
            raise _failure(trace_id, ProviderErrorCode.UNAVAILABLE, "服务商流式响应意外结束", retryable=True)
        yield StreamEvent(trace_id=trace_id, event_type="usage", response_id=response_id, usage=usage)
        yield StreamEvent(trace_id=trace_id, event_type="response.completed", response_id=response_id)

    async def embeddings(self, inputs: list[str], *, trace_id: str) -> EmbeddingResult:
        raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "此服务商不支持向量嵌入", retryable=False)

    async def search(self, query: str, *, trace_id: str) -> ResponseResult:
        raise _failure(trace_id, ProviderErrorCode.INVALID_REQUEST, "此服务商不支持原生网络搜索", retryable=False)


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
