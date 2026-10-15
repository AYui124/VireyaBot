from datetime import UTC, datetime

import pytest

from app.contracts import (
    ContractAccessError,
    ContractCatalog,
    DataScope,
    DataScopeKind,
    ImageSegment,
    MessageEnvelope,
    PluginManifest,
    TextSegment,
    WorkflowGraph,
    WorkflowNode,
    assert_scope_access,
)


def test_qq_image_message_contract_accepts_private_media() -> None:
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
                        "content_type": "image/png",
                        "size_bytes": 128,
                        "sha256": "a" * 64,
                    },
                },
            ],
            "trace_id": "trace-1",
        }
    )
    assert isinstance(message.segments[0], TextSegment)
    assert isinstance(message.segments[1], ImageSegment)


def test_plugin_search_and_published_graph_contracts_accept() -> None:
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
    catalog = ContractCatalog(schema_version="v1", plugin_manifest=manifest, workflow_graph=graph)
    assert catalog.plugin_manifest is not None
    assert catalog.workflow_graph is not None


def test_cross_user_and_cross_group_access_is_denied() -> None:
    private = DataScope(schema_version="v1", kind=DataScopeKind.PRIVATE, user_id="alice")
    group = DataScope(schema_version="v1", kind=DataScopeKind.GROUP, group_id="group-a")

    with pytest.raises(ContractAccessError, match="another user") as private_error:
        assert_scope_access(private, actor_user_id="bob", group_id=None)
    assert private_error.value.code == "scope.private_forbidden"

    with pytest.raises(ContractAccessError, match="another group") as group_error:
        assert_scope_access(group, actor_user_id=None, group_id="group-b")
    assert group_error.value.code == "scope.group_forbidden"


def test_invalid_scope_and_unknown_fields_fail_closed() -> None:
    with pytest.raises(ValueError, match="private conversation"):
        MessageEnvelope.model_validate(
            {
                "schema_version": "v1",
                "id": "msg-1",
                "occurred_at": datetime.now(UTC),
                "conversation": {
                    "schema_version": "v1",
                    "id": "bad",
                    "kind": "private",
                    "user_id": "42",
                    "group_id": "group-a",
                },
                "sender": {"schema_version": "v1", "kind": "user", "id": "42"},
                "segments": [{"schema_version": "v1", "type": "text", "text": "x"}],
                "trace_id": "trace-1",
            }
        )

    with pytest.raises(ValueError):
        WorkflowNode(schema_version="v1", id="node", component_ref="component", unexpected=True)
