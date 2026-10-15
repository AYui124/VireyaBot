from datetime import UTC, datetime

import pytest

from app.contracts import (
    AccessContext,
    ContractAccessError,
    ContractCatalog,
    ConversationRef,
    DataScope,
    DataScopeKind,
    ExecutionContext,
    Failure,
    ImageSegment,
    MessageEnvelope,
    NodeInput,
    NodeRun,
    PluginManifest,
    Principal,
    ProactiveDecision,
    ProactivePolicy,
    ToolResult,
    WorkflowGraph,
    WorkflowNode,
    assert_scope_access,
    evaluate_proactive_policy,
    is_download_permitted_media_type,
)


def user_private_context(user_id: str = "42") -> AccessContext:
    return AccessContext(
        schema_version="v1",
        actor=Principal(schema_version="v1", kind="user", id=user_id),
        conversation=ConversationRef(
            schema_version="v1", id=f"private-{user_id}", kind="private", user_id=user_id
        ),
    )


@pytest.mark.parametrize(
    ("content_type", "valid"),
    [
        ("image/png", True),
        ("image/svg+xml", True),
        ("image/png/extra", False),
        ("image/;", False),
    ],
)
def test_media_ref_accepts_mime_tokens_without_parameters(content_type: str, valid: bool) -> None:
    payload = {
        "schema_version": "v1",
        "id": "media-1",
        "content_type": content_type,
        "size_bytes": 128,
        "sha256": "a" * 64,
    }
    if valid:
        ImageSegment(schema_version="v1", type="image", media=payload)
    else:
        with pytest.raises(ValueError):
            ImageSegment(schema_version="v1", type="image", media=payload)

def test_qq_image_walkthrough_accepts_private_media_and_scope() -> None:
    message = MessageEnvelope.model_validate(
        {
            "schema_version": "v1",
            "id": "msg-1",
            "occurred_at": datetime.now(UTC),
            "conversation": {
                "schema_version": "v1",
                "id": "private-42",
                "kind": "private",
                "user_id": "42",
            },
            "sender": {"schema_version": "v1", "kind": "user", "id": "42"},
            "segments": [
                {"schema_version": "v1", "type": "text", "text": "请看这张图"},
                {
                    "schema_version": "v1",
                    "type": "image",
                    "media": {
                        "schema_version": "v1",
                        "id": "media-1",
                        "content_type": "image/svg+xml",
                        "size_bytes": 128,
                        "sha256": "a" * 64,
                    },
                },
            ],
            "trace_id": "trace-1",
        }
    )
    assert isinstance(message.segments[1], ImageSegment)
    assert is_download_permitted_media_type(message.segments[1].media.content_type) is False
    assert_scope_access(
        DataScope(schema_version="v1", kind="private", user_id="42"),
        context=user_private_context(),
    )


def test_scope_requires_current_conversation_and_trusted_identity() -> None:
    private = DataScope(schema_version="v1", kind=DataScopeKind.PRIVATE, user_id="alice")
    group = DataScope(schema_version="v1", kind=DataScopeKind.GROUP, group_id="group-a")

    with pytest.raises(ContractAccessError) as same_user_group:
        assert_scope_access(
            private,
            context=AccessContext(
                schema_version="v1",
                actor=Principal(schema_version="v1", kind="user", id="alice"),
                conversation=ConversationRef(
                    schema_version="v1", id="group-a", kind="group", group_id="group-a"
                ),
            ),
        )
    assert same_user_group.value.code == "scope.private_forbidden"

    with pytest.raises(ContractAccessError) as cross_user:
        assert_scope_access(private, context=user_private_context("bob"))
    assert cross_user.value.code == "scope.private_forbidden"

    with pytest.raises(ContractAccessError) as cross_group:
        assert_scope_access(
            group,
            context=AccessContext(
                schema_version="v1",
                actor=Principal(schema_version="v1", kind="user", id="alice"),
                conversation=ConversationRef(
                    schema_version="v1", id="group-b", kind="group", group_id="group-b"
                ),
            ),
        )
    assert cross_group.value.code == "scope.group_forbidden"

    global_scope = DataScope(schema_version="v1", kind=DataScopeKind.GLOBAL)
    with pytest.raises(ContractAccessError, match="explicit system"):
        assert_scope_access(
            global_scope,
            context=AccessContext(
                schema_version="v1",
                actor=Principal(schema_version="v1", kind="user", id="alice"),
            ),
        )
    assert_scope_access(
        global_scope,
        context=AccessContext(
            schema_version="v1", actor=Principal(schema_version="v1", kind="system", id="system")
        ),
    )


def test_plugin_search_walkthrough_pins_component_schema_and_runner_context() -> None:
    manifest = PluginManifest.model_validate(
        {
            "schema_version": "v1",
            "plugin_id": "web-search",
            "version": "0.1.0",
            "api_version": "v1",
            "permissions": ["web.search"],
            "components": [
                {
                    "schema_version": "v1",
                    "id": "search",
                    "kind": "workflow_node",
                    "input_schema_ref": "urn:vireyabot:search-input:v1",
                    "output_schema_ref": "urn:vireyabot:search-output:v1",
                }
            ],
        }
    )
    graph = WorkflowGraph(
        schema_version="v1",
        nodes=[WorkflowNode(schema_version="v1", id="search", component_ref="web-search@0.1.0/search")],
    )
    context = ExecutionContext(
        schema_version="v1",
        actor=Principal(schema_version="v1", kind="user", id="alice"),
        conversation=ConversationRef(
            schema_version="v1", id="private-alice", kind="private", user_id="alice"
        ),
        scope=DataScope(schema_version="v1", kind="private", user_id="alice"),
        workflow_version_id="wf-v1",
        component_ref="web-search@0.1.0/search",
        capabilities=["web.search"],
    )
    node_input = NodeInput(schema_version="v1", name="query", value="VireyaBot", context=context)
    catalog = ContractCatalog(schema_version="v1", plugin_manifest=manifest, workflow_graph=graph)
    assert catalog.plugin_manifest is not None
    assert node_input.context.component_ref == graph.nodes[0].component_ref
    assert node_input.context.capabilities == ["web.search"]


def test_failure_contracts_require_trace_and_failure_state() -> None:
    failure = Failure(
        schema_version="v1", trace_id="trace-1", error_code="provider.timeout", retryable=True
    )
    result = ToolResult(schema_version="v1", call_id="call-1", trace_id="trace-1", ok=False, failure=failure)
    assert result.failure is not None and result.failure.error_code == "provider.timeout"

    with pytest.raises(ValueError, match="requires failure"):
        ToolResult(schema_version="v1", call_id="call-1", trace_id="trace-1", ok=False)
    with pytest.raises(ValueError, match="requires failure"):
        NodeRun(schema_version="v1", run_id="run-1", node_id="node-1", trace_id="trace-1", status="failed")


def test_proactive_private_walkthrough_applies_authorization_and_safety_gates() -> None:
    allowed = ProactivePolicy(
        schema_version="v1",
        enabled=True,
        kill_switch=False,
        target_user_id="alice",
        workflow_version_id="wf-v1",
        explicit_authorization=True,
    )
    assert evaluate_proactive_policy(allowed) is ProactiveDecision.ALLOWED

    for field in ("kill_switch", "quiet_hours", "cooldown_active", "frequency_limit_hit"):
        blocked = allowed.model_copy(update={field: True})
        assert evaluate_proactive_policy(blocked) is ProactiveDecision.SKIPPED

    unauthorized = allowed.model_copy(update={"explicit_authorization": False})
    assert evaluate_proactive_policy(unauthorized) is ProactiveDecision.DENIED


def test_invalid_scope_refs_and_unknown_fields_fail_closed() -> None:
    with pytest.raises(ValueError, match="private conversation"):
        ConversationRef(
            schema_version="v1", id="bad", kind="private", user_id="42", group_id="group-a"
        )
    with pytest.raises(ValueError):
        WorkflowNode(schema_version="v1", id="node", component_ref="component", unexpected=True)
    with pytest.raises(ValueError):
        WorkflowNode(schema_version="v1", id="node", component_ref="web-search@latest/search")
