"""Public Provider fields shared by the management API and form metadata."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

ProtocolType = Literal["openai_responses", "openai_chat_completions", "anthropic_messages"]
COMMON_FIELDS = ["name", "protocol_type", "base_url", "timeout_seconds", "enabled", "api_key", "responses_model", "default_stream", "temperature", "max_output_tokens", "extra_headers"]
OPENAI_FIELDS = ["organization", "project", "embedding_model", "embedding_dimensions"]
PROVIDER_PROTOCOLS = {
    "openai_responses": {
        "label": "OpenAI Responses", "fields": COMMON_FIELDS + OPENAI_FIELDS + ["native_search_enabled"],
        "temperature_max": 2,
        "defaults": {"responses_model": "gpt-4.1-mini", "embedding_model": "text-embedding-3-small", "base_url": "https://api.openai.com/v1", "max_output_tokens": 4096},
        "capabilities": ["text", "image_input", "streaming", "structured_output", "tool_calling", "native_web_search", "embedding", "embedding_batch"],
    },
    "openai_chat_completions": {
        "label": "OpenAI Chat Completions", "fields": COMMON_FIELDS + OPENAI_FIELDS,
        "temperature_max": 2,
        "defaults": {"responses_model": "gpt-4.1-mini", "embedding_model": "text-embedding-3-small", "base_url": "https://api.openai.com/v1", "max_output_tokens": 4096},
        "capabilities": ["text", "streaming", "structured_output", "tool_calling", "embedding", "embedding_batch"],
    },
    "anthropic_messages": {
        "label": "Anthropic Messages", "fields": COMMON_FIELDS,
        "temperature_max": 1,
        "defaults": {"responses_model": "claude-sonnet-4-5", "embedding_model": "", "base_url": "https://api.anthropic.com", "max_output_tokens": 4096},
        "capabilities": ["text", "streaming", "tool_calling"],
    },
}
RESERVED_HEADERS = {"authorization", "proxy-authorization", "x-api-key", "api-key", "host", "content-type", "content-length", "transfer-encoding", "connection", "cookie", "set-cookie", "anthropic-version", "anthropic-beta", "openai-organization", "openai-project", "openai-beta", "accept", "accept-encoding", "user-agent"}


class HeaderRow(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: StrictStr = Field(min_length=1, max_length=160)
    value: StrictStr = Field(default="", max_length=4096, repr=False)
    previous_name: StrictStr | None = Field(default=None, min_length=1, max_length=160)

    @field_validator("name", "previous_name")
    @classmethod
    def valid_name(cls, value: str | None) -> str | None:
        if value is not None and (not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", value) or value.lower() in RESERVED_HEADERS or value.lower().startswith("x-stainless-")):
            raise ValueError("Header 名称无效或属于保留字段")
        return value

    @field_validator("value")
    @classmethod
    def valid_value(cls, value: str) -> str:
        if any(ord(char) < 32 and char != "\t" or ord(char) > 126 for char in value):
            raise ValueError("Header 值必须是无换行的 ASCII 文本")
        return value


class ProviderUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, allow_inf_nan=False)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    protocol_type: ProtocolType | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    timeout_seconds: float | None = Field(default=None, gt=0, le=600)
    organization: str | None = Field(default=None, max_length=160)
    project: str | None = Field(default=None, max_length=160)
    enabled: bool | None = None
    api_key: str | None = Field(default=None, max_length=4096, repr=False)
    responses_model: str | None = Field(default=None, min_length=1, max_length=160)
    embedding_model: str | None = Field(default=None, max_length=160)
    embedding_dimensions: int | None = Field(default=None, gt=0, le=2147483647)
    native_search_enabled: bool | None = None
    default_stream: bool | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, ge=1, le=32768)
    extra_headers: list[HeaderRow] | None = Field(default=None, max_length=32)
    confirm_protocol_change: bool = False

    @field_validator("api_key", "organization", "project")
    @classmethod
    def valid_sdk_header(cls, value: str | None) -> str | None:
        if value is not None and any(ord(char) < 32 or ord(char) > 126 for char in value):
            raise ValueError("SDK Header 字段必须是无换行的 ASCII 文本")
        return value


class ProviderCreateRequest(ProviderUpdateRequest):
    name: str = Field(min_length=1, max_length=120)
    protocol_type: ProtocolType
    base_url: str = Field(min_length=1, max_length=500)
    timeout_seconds: float = Field(gt=0, le=600)
    responses_model: str = Field(min_length=1, max_length=160)
