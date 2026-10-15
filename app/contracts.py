"""Versioned cross-boundary contracts for the first workflow/plugin slice.

These models define data ownership and validation only. They do not execute
workflows, invoke plugins, call providers, or send messages.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "v1"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["v1"]


class ContractAccessError(ValueError):
    """Raised when a caller attempts to cross a data-isolation boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class PrincipalKind(StrEnum):
    USER = "user"
    BOT = "bot"
    ADMIN = "admin"
    SYSTEM = "system"


class Principal(ContractModel):
    kind: PrincipalKind
    id: str = Field(min_length=1, max_length=160)


class ConversationKind(StrEnum):
    PRIVATE = "private"
    GROUP = "group"


class ConversationRef(ContractModel):
    id: str = Field(min_length=1, max_length=160)
    kind: ConversationKind
    user_id: str | None = None
    group_id: str | None = None

    @model_validator(mode="after")
    def validate_owner(self) -> ConversationRef:
        if self.kind is ConversationKind.PRIVATE and (not self.user_id or self.group_id):
            raise ValueError("private conversation requires user_id and forbids group_id")
        if self.kind is ConversationKind.GROUP and (not self.group_id or self.user_id):
            raise ValueError("group conversation requires group_id and forbids user_id")
        return self


class DataScopeKind(StrEnum):
    PRIVATE = "private"
    GROUP = "group"
    GLOBAL = "global"


class DataScope(ContractModel):
    kind: DataScopeKind
    user_id: str | None = None
    group_id: str | None = None

    @model_validator(mode="after")
    def validate_owner(self) -> DataScope:
        if self.kind is DataScopeKind.PRIVATE and (not self.user_id or self.group_id):
            raise ValueError("private scope requires user_id and forbids group_id")
        if self.kind is DataScopeKind.GROUP and (not self.group_id or self.user_id):
            raise ValueError("group scope requires group_id and forbids user_id")
        if self.kind is DataScopeKind.GLOBAL and (self.user_id or self.group_id):
            raise ValueError("global scope cannot have user_id or group_id")
        return self


class MediaRef(ContractModel):
    id: str = Field(min_length=1, max_length=160)
    content_type: str = Field(pattern=r"^[^/\\s]+/[^/\\s]+$")
    size_bytes: int = Field(ge=0, le=10 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_url: str | None = None


class TextSegment(ContractModel):
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=100_000)


class ImageSegment(ContractModel):
    type: Literal["image"]
    media: MediaRef


class MentionSegment(ContractModel):
    type: Literal["mention"]
    target: Principal


class ReplySegment(ContractModel):
    type: Literal["reply"]
    message_id: str = Field(min_length=1, max_length=160)


MessageSegment = TextSegment | ImageSegment | MentionSegment | ReplySegment


class MessageEnvelope(ContractModel):
    id: str = Field(min_length=1, max_length=160)
    occurred_at: datetime
    conversation: ConversationRef
    sender: Principal
    segments: list[MessageSegment] = Field(min_length=1, max_length=100)
    trace_id: str = Field(min_length=1, max_length=160)


class SessionRef(ContractModel):
    id: str = Field(min_length=1, max_length=160)
    conversation: ConversationRef
    persona_version_id: str = Field(min_length=1, max_length=160)


class PersonaRef(ContractModel):
    id: str = Field(min_length=1, max_length=160)
    version_id: str = Field(min_length=1, max_length=160)


class ToolCall(ContractModel):
    id: str = Field(min_length=1, max_length=160)
    tool_id: str = Field(min_length=1, max_length=160)
    arguments: dict[str, Any]
    actor: Principal
    scope: DataScope


class ToolResult(ContractModel):
    call_id: str = Field(min_length=1, max_length=160)
    ok: bool
    output: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    retryable: bool = False

    @model_validator(mode="after")
    def validate_error(self) -> ToolResult:
        if not self.ok and not self.error_code:
            raise ValueError("failed tool result requires error_code")
        if self.ok and self.error_code:
            raise ValueError("successful tool result cannot have error_code")
        return self


class MemoryRecord(ContractModel):
    id: str = Field(min_length=1, max_length=160)
    scope: DataScope
    content: str = Field(min_length=1, max_length=100_000)
    source_session_id: str = Field(min_length=1, max_length=160)
    embedding_version: str | None = None


class NodeInput(ContractModel):
    name: str = Field(min_length=1, max_length=160)
    value: Any


class NodeOutput(ContractModel):
    name: str = Field(min_length=1, max_length=160)
    value: Any
    secret: bool = False


class PluginPermission(StrEnum):
    WEB_SEARCH = "web.search"
    READ_MESSAGE = "message.read"
    WRITE_MESSAGE = "message.write"
    READ_MEMORY = "memory.read"
    WRITE_MEMORY = "memory.write"


class PluginComponent(ContractModel):
    id: str = Field(min_length=1, max_length=160)
    kind: Literal["workflow_node"]
    input_schema_ref: str = Field(min_length=1, max_length=300)
    output_schema_ref: str = Field(min_length=1, max_length=300)


class PluginManifest(ContractModel):
    plugin_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,63}$")
    version: str = Field(pattern=r"^0\.[0-9]+\.[0-9]+$")
    api_version: Literal["v1"]
    permissions: list[PluginPermission] = Field(default_factory=list, max_length=32)
    components: list[PluginComponent] = Field(min_length=1, max_length=64)


class WorkflowNode(ContractModel):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    component_ref: str = Field(min_length=1, max_length=300)
    config: dict[str, Any] = Field(default_factory=dict)


class WorkflowEdge(ContractModel):
    source: str = Field(min_length=1, max_length=64)
    target: str = Field(min_length=1, max_length=64)


class WorkflowGraph(ContractModel):
    nodes: list[WorkflowNode] = Field(min_length=1, max_length=256)
    edges: list[WorkflowEdge] = Field(default_factory=list, max_length=1024)

    @model_validator(mode="after")
    def validate_edges(self) -> WorkflowGraph:
        node_ids = {node.id for node in self.nodes}
        if len(node_ids) != len(self.nodes):
            raise ValueError("workflow node ids must be unique")
        if any(edge.source not in node_ids or edge.target not in node_ids for edge in self.edges):
            raise ValueError("workflow edge must reference existing nodes")
        return self


class WorkflowDraft(ContractModel):
    workflow_id: str = Field(min_length=1, max_length=160)
    owner_id: str = Field(min_length=1, max_length=160)
    revision: int = Field(ge=1)
    graph: WorkflowGraph


class WorkflowVersion(ContractModel):
    workflow_id: str = Field(min_length=1, max_length=160)
    version_id: str = Field(min_length=1, max_length=160)
    owner_id: str = Field(min_length=1, max_length=160)
    graph: WorkflowGraph
    published_at: datetime


class WorkflowRun(ContractModel):
    run_id: str = Field(min_length=1, max_length=160)
    workflow_version_id: str = Field(min_length=1, max_length=160)
    trigger_message_id: str | None = None
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]


class NodeRun(ContractModel):
    run_id: str = Field(min_length=1, max_length=160)
    node_id: str = Field(min_length=1, max_length=64)
    status: Literal["queued", "running", "succeeded", "failed", "skipped"]
    input: list[NodeInput] = Field(default_factory=list)
    output: list[NodeOutput] = Field(default_factory=list)
    error_code: str | None = None


class SecretRef(ContractModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,159}$")
    owner: Literal["configuration_service"]
    exposed_to: list[str] = Field(default_factory=list, max_length=32)


class AuditRecord(ContractModel):
    id: str = Field(min_length=1, max_length=160)
    actor: Principal
    action: str = Field(min_length=1, max_length=160)
    resource_type: str = Field(min_length=1, max_length=80)
    resource_id: str = Field(min_length=1, max_length=160)
    outcome: Literal["allowed", "denied", "failed"]
    error_code: str | None = None


class ContractCatalog(ContractModel):
    """Schema root containing every v1 resource contract as optional entries."""

    message: MessageEnvelope | None = None
    media: MediaRef | None = None
    session: SessionRef | None = None
    persona: PersonaRef | None = None
    tool_call: ToolCall | None = None
    tool_result: ToolResult | None = None
    memory: MemoryRecord | None = None
    plugin_manifest: PluginManifest | None = None
    workflow_graph: WorkflowGraph | None = None
    workflow_draft: WorkflowDraft | None = None
    workflow_version: WorkflowVersion | None = None
    workflow_run: WorkflowRun | None = None
    node_run: NodeRun | None = None
    secret_ref: SecretRef | None = None
    audit_record: AuditRecord | None = None


def assert_scope_access(scope: DataScope, *, actor_user_id: str | None, group_id: str | None) -> None:
    """Enforce the only permitted access to user/group-owned data.

    Global data is reserved for system-owned resources; callers must pass an
    explicit system actor by using ``actor_user_id=None`` and ``group_id=None``.
    """

    if scope.kind is DataScopeKind.PRIVATE and scope.user_id != actor_user_id:
        raise ContractAccessError("scope.private_forbidden", "private resource belongs to another user")
    if scope.kind is DataScopeKind.GROUP and scope.group_id != group_id:
        raise ContractAccessError("scope.group_forbidden", "group resource belongs to another group")
    if scope.kind is DataScopeKind.GLOBAL and (actor_user_id is not None or group_id is not None):
        raise ContractAccessError("scope.global_forbidden", "global resource requires a system context")
