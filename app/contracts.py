"""Versioned cross-boundary contracts for the first workflow/plugin slice.

These models are deliberately transport-neutral. They define ownership,
version pinning, capability grants, and safe failure semantics without
implementing a workflow engine or plugin runner.
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


class Failure(ContractModel):
    """Safe, machine-readable failure shared by runner-facing contracts."""

    trace_id: str = Field(min_length=1, max_length=160)
    error_code: str = Field(min_length=1, max_length=160)
    retryable: bool
    message: str | None = Field(default=None, max_length=500)


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


class AccessContext(ContractModel):
    """Trusted identity and current conversation supplied by the caller."""

    actor: Principal
    conversation: ConversationRef | None = None


class ExecutionContext(ContractModel):
    """Engine-supplied context pinned for one component invocation."""

    actor: Principal
    conversation: ConversationRef | None = None
    scope: DataScope
    workflow_version_id: str = Field(min_length=1, max_length=160)
    component_ref: str = Field(
        pattern=r"^[a-z][a-z0-9_.-]{1,63}@0\.[0-9]+\.[0-9]+/[a-zA-Z][a-zA-Z0-9_.-]{0,63}$"
    )
    capabilities: list[str] = Field(default_factory=list, max_length=32)


class MediaRef(ContractModel):
    id: str = Field(min_length=1, max_length=160)
    # Syntax acceptance is separate from the Gateway's download policy; MIME has no parameters.
    content_type: str = Field(pattern=r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+/[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
    size_bytes: int = Field(ge=0, le=10 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_url: str | None = None


DOWNLOADABLE_MEDIA_TYPES = frozenset({"image/gif", "image/jpeg", "image/png", "image/webp"})


def is_download_permitted_media_type(content_type: str) -> bool:
    """Return whether Gateway policy permits downloading this valid MIME type."""

    return content_type.lower() in DOWNLOADABLE_MEDIA_TYPES


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
    trace_id: str = Field(min_length=1, max_length=160)
    ok: bool
    output: dict[str, Any] = Field(default_factory=dict)
    failure: Failure | None = None

    @model_validator(mode="after")
    def validate_failure(self) -> ToolResult:
        if self.ok and self.failure:
            raise ValueError("successful tool result cannot have failure")
        if not self.ok and not self.failure:
            raise ValueError("failed tool result requires failure")
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
    context: ExecutionContext


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
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,63}$")
    kind: Literal["workflow_node"]
    input_schema_ref: str = Field(pattern=r"^urn:vireyabot:[a-z][a-z0-9_.-]{1,63}:v1$")
    output_schema_ref: str = Field(pattern=r"^urn:vireyabot:[a-z][a-z0-9_.-]{1,63}:v1$")


class PluginManifest(ContractModel):
    plugin_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,63}$")
    version: str = Field(pattern=r"^0\.[0-9]+\.[0-9]+$")
    api_version: Literal["v1"]
    permissions: list[PluginPermission] = Field(default_factory=list, max_length=32)
    components: list[PluginComponent] = Field(min_length=1, max_length=64)


class WorkflowNode(ContractModel):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    component_ref: str = Field(
        pattern=r"^[a-z][a-z0-9_.-]{1,63}@0\.[0-9]+\.[0-9]+/[a-zA-Z][a-zA-Z0-9_.-]{0,63}$"
    )
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
    trace_id: str = Field(min_length=1, max_length=160)
    status: Literal["queued", "running", "succeeded", "failed", "skipped"]
    input: list[NodeInput] = Field(default_factory=list)
    output: list[NodeOutput] = Field(default_factory=list)
    failure: Failure | None = None

    @model_validator(mode="after")
    def validate_failure(self) -> NodeRun:
        if self.status == "failed" and not self.failure:
            raise ValueError("failed node run requires failure")
        if self.status != "failed" and self.failure:
            raise ValueError("only failed node runs can have failure")
        return self


class ProactivePolicy(ContractModel):
    """Persisted policy snapshot required before a scheduled private send."""

    enabled: bool
    kill_switch: bool
    target_user_id: str = Field(min_length=1, max_length=160)
    workflow_version_id: str = Field(min_length=1, max_length=160)
    quiet_hours: bool = False
    cooldown_active: bool = False
    frequency_limit_hit: bool = False
    explicit_authorization: bool


class ProactiveDecision(StrEnum):
    ALLOWED = "allowed"
    SKIPPED = "skipped"
    DENIED = "denied"


def evaluate_proactive_policy(policy: ProactivePolicy) -> ProactiveDecision:
    """Evaluate only persisted policy inputs; no message is sent here."""

    if not policy.enabled or policy.kill_switch:
        return ProactiveDecision.SKIPPED
    if not policy.explicit_authorization:
        return ProactiveDecision.DENIED
    if policy.quiet_hours or policy.cooldown_active or policy.frequency_limit_hit:
        return ProactiveDecision.SKIPPED
    return ProactiveDecision.ALLOWED


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
    proactive_policy: ProactivePolicy | None = None
    secret_ref: SecretRef | None = None
    audit_record: AuditRecord | None = None


def assert_scope_access(scope: DataScope, *, context: AccessContext) -> None:
    """Require a trusted actor and matching current conversation scope.

    ``None`` is not an authenticated system identity. Global resources require
    an explicit system principal and no conversation. Private resources require
    the same user in a private conversation; group resources require the same
    group in a group conversation.
    """

    actor = context.actor
    conversation = context.conversation
    if scope.kind is DataScopeKind.GLOBAL:
        if actor.kind is not PrincipalKind.SYSTEM or conversation is not None:
            raise ContractAccessError("scope.global_forbidden", "global resource requires explicit system context")
        return

    if actor.kind is not PrincipalKind.USER:
        code = "scope.private_forbidden" if scope.kind is DataScopeKind.PRIVATE else "scope.group_forbidden"
        raise ContractAccessError(code, "resource requires an authenticated user context")

    if scope.kind is DataScopeKind.PRIVATE:
        if (
            actor.id != scope.user_id
            or conversation is None
            or conversation.kind is not ConversationKind.PRIVATE
            or conversation.user_id != scope.user_id
        ):
            raise ContractAccessError("scope.private_forbidden", "private resource is outside the current conversation")
        return

    if (
        conversation is None
        or conversation.kind is not ConversationKind.GROUP
        or conversation.group_id != scope.group_id
    ):
        raise ContractAccessError("scope.group_forbidden", "group resource is outside the current conversation")
