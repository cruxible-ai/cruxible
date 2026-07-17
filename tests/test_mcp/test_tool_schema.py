"""Schema-focused tests for MCP tool registrations.

Verifies that Literal params produce enum constraints, typed returns
produce outputSchema, and errors propagate as ToolError.
"""

from __future__ import annotations

import asyncio

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from cruxible_core.mcp.server import create_server
from cruxible_core.runtime.instance import CruxibleInstance


@pytest.fixture
def server():
    return create_server()


def _get_tool_schemas(server):
    """Return {name: Tool} mapping from the server."""
    tools = asyncio.run(server.list_tools())
    return {t.name: t for t in tools}


class TestInputSchema:
    """Verify Literal params produce enum constraints."""

    def test_query_tool_description_surfaces_invocation_guidance(self, server):
        schemas = _get_tool_schemas(server)
        description = schemas["cruxible_query"].description or ""
        assert description.startswith("Use when ")
        assert "cruxible_list_queries" in description
        assert "cruxible_describe_query" in description
        assert "entry_point primary-key field" in description
        assert "cruxible_schema" in description

    def test_feedback_tool_description_surfaces_explicit_coordinate_guidance(self, server):
        schemas = _get_tool_schemas(server)
        description = schemas["cruxible_feedback"].description or ""
        assert description.startswith("Use when ")
        assert "edge_key" in description
        assert "receipt_id is optional" in description

    def test_feedback_action_enum(self, server):
        schemas = _get_tool_schemas(server)
        action = schemas["cruxible_feedback"].inputSchema["properties"]["action"]
        assert action["enum"] == ["approve", "reject", "correct", "flag"]

    def test_feedback_source_enum(self, server):
        schemas = _get_tool_schemas(server)
        source = schemas["cruxible_feedback"].inputSchema["properties"]["source"]
        assert source["enum"] == ["human", "agent"]

    def test_feedback_receipt_is_optional_for_explicit_coordinates(self, server):
        schemas = _get_tool_schemas(server)
        required = set(schemas["cruxible_feedback"].inputSchema["required"])
        props = schemas["cruxible_feedback"].inputSchema["properties"]
        assert "receipt_id" in props
        assert "receipt_id" not in required

    def test_feedback_from_query_schema(self, server):
        schemas = _get_tool_schemas(server)
        props = schemas["cruxible_feedback_from_query"].inputSchema["properties"]
        required = set(schemas["cruxible_feedback_from_query"].inputSchema["required"])
        assert {"instance_id", "receipt_id", "result_index", "action"} <= required
        assert props["action"]["enum"] == ["approve", "reject", "correct", "flag"]
        assert props["source"]["enum"] == ["human", "agent"]
        assert "reason_code" in props
        assert "scope_hints" in props
        assert "path_index" in props
        assert "path_alias" in props

    def test_outcome_outcome_enum(self, server):
        schemas = _get_tool_schemas(server)
        outcome = schemas["cruxible_outcome"].inputSchema["properties"]["outcome"]
        assert outcome["enum"] == ["correct", "incorrect", "partial", "unknown"]

    def test_list_resource_type_enum(self, server):
        schemas = _get_tool_schemas(server)
        resource_type = schemas["cruxible_list"].inputSchema["properties"]["resource_type"]
        assert resource_type["enum"] == ["entities", "edges", "receipts", "feedback", "outcomes"]

    def test_query_relationship_state_enum(self, server):
        schemas = _get_tool_schemas(server)
        relationship_state = schemas["cruxible_query"].inputSchema["properties"][
            "relationship_state"
        ]
        enum_schema = next(
            item for item in relationship_state["anyOf"] if item.get("type") == "string"
        )
        assert enum_schema["enum"] == [
            "live",
            "accepted",
            "all",
            "not-live",
            "pending",
            "reviewable",
        ]

    def test_query_inline_schema(self, server):
        schemas = _get_tool_schemas(server)
        schema = schemas["cruxible_query_inline"].inputSchema
        required = set(schema["required"])
        assert {"instance_id", "definition"} <= required
        assert "params" in schema["properties"]
        assert "limit" in schema["properties"]
        definition = schema["properties"]["definition"]
        ref = definition["$ref"]
        def_name = ref.split("/")[-1]
        definition_schema = schema["$defs"][def_name]
        assert {"name", "mode", "returns"} <= set(definition_schema["required"])

    def test_add_relationship_schema(self, server):
        """RelationshipInput fields appear as required in the relationships array schema."""
        schemas = _get_tool_schemas(server)
        schema = schemas["cruxible_add_relationship"].inputSchema
        rels_prop = schema["properties"]["relationships"]
        assert rels_prop["type"] == "array"
        ref = rels_prop["items"]["$ref"]
        def_name = ref.split("/")[-1]
        rel_def = schema["$defs"][def_name]
        required = set(rel_def["required"])
        assert {"from_type", "from_id", "relationship_type", "to_type", "to_id"} <= required
        assert {
            "evidence_refs",
            "source_evidence",
            "evidence_rationale",
            "pending",
        } <= set(rel_def["properties"])
        assert "evidence_refs" not in required
        assert "source_evidence" not in required
        assert "evidence_rationale" not in required
        assert "pending" not in required

    def test_add_entity_schema(self, server):
        """EntityInput fields appear as required in the entities array schema."""
        schemas = _get_tool_schemas(server)
        schema = schemas["cruxible_add_entity"].inputSchema
        ents_prop = schema["properties"]["entities"]
        assert ents_prop["type"] == "array"
        ref = ents_prop["items"]["$ref"]
        def_name = ref.split("/")[-1]
        ent_def = schema["$defs"][def_name]
        required = set(ent_def["required"])
        assert {"entity_type", "entity_id"} <= required

    def test_batch_direct_write_schema(self, server):
        schemas = _get_tool_schemas(server)
        schema = schemas["cruxible_batch_direct_write"].inputSchema
        assert {"instance_id", "payload"} <= set(schema["required"])
        assert "dry_run" in schema["properties"]
        payload_ref = schema["properties"]["payload"]["$ref"]
        payload_name = payload_ref.split("/")[-1]
        payload_def = schema["$defs"][payload_name]
        assert {"entities", "relationships", "shared_evidence"} <= set(payload_def["properties"])

    def test_add_constraint_severity_enum(self, server):
        schemas = _get_tool_schemas(server)
        severity = schemas["cruxible_add_constraint"].inputSchema["properties"]["severity"]
        assert severity["enum"] == ["warning", "error"]

    def test_get_relationship_optional_edge_key(self, server):
        schemas = _get_tool_schemas(server)
        props = schemas["cruxible_get_relationship"].inputSchema["properties"]
        assert "edge_key" in props
        required = set(schemas["cruxible_get_relationship"].inputSchema.get("required", []))
        assert "edge_key" not in required

    def test_validate_optional_config_params(self, server):
        """cruxible_validate has config_path and config_yaml, neither required."""
        schemas = _get_tool_schemas(server)
        schema = schemas["cruxible_validate"].inputSchema
        assert "config_path" in schema["properties"]
        assert "config_yaml" in schema["properties"]
        required = set(schema.get("required", []))
        assert "config_path" not in required
        assert "config_yaml" not in required

    def test_list_has_property_filter(self, server):
        schemas = _get_tool_schemas(server)
        props = schemas["cruxible_list"].inputSchema["properties"]
        assert "property_filter" in props
        assert "where" in props
        assert "fields" in props

    def test_sample_has_fields(self, server):
        schemas = _get_tool_schemas(server)
        props = schemas["cruxible_sample"].inputSchema["properties"]
        assert "fields" in props

    def test_evaluate_has_filters(self, server):
        schemas = _get_tool_schemas(server)
        props = schemas["cruxible_evaluate"].inputSchema["properties"]
        assert "exclude_orphan_types" in props
        assert "severity_filter" in props
        assert "category_filter" in props

    def test_init_optional_config_yaml(self, server):
        """cruxible_init has config_yaml in properties, not required."""
        schemas = _get_tool_schemas(server)
        schema = schemas["cruxible_init"].inputSchema
        assert "config_yaml" in schema["properties"]
        assert "bare" in schema["properties"]
        required = set(schema.get("required", []))
        assert "config_yaml" not in required
        # root_dir remains required
        assert "root_dir" in required

    def test_create_state_overlay_has_optional_state_ref_and_transport_ref(self, server):
        schemas = _get_tool_schemas(server)
        schema = schemas["cruxible_state_create_overlay"].inputSchema
        props = schema["properties"]
        required = set(schema.get("required", []))
        assert "root_dir" in required
        assert "transport_ref" in props
        assert "state_ref" in props
        assert "kit" in props
        assert "no_kit" in props
        assert "transport_ref" not in required
        assert "state_ref" not in required
        assert "kit" not in required
        assert "no_kit" not in required

    def test_register_source_artifact_has_optional_source_artifact_id(self, server):
        schemas = _get_tool_schemas(server)
        schema = schemas["cruxible_register_source_artifact"].inputSchema
        props = schema["properties"]
        required = set(schema.get("required", []))
        assert "source_artifact_id" in props
        assert "source_artifact_id" not in required
        assert any(item.get("type") == "string" for item in props["source_artifact_id"]["anyOf"])

    def test_new_curated_agent_tools_have_expected_inputs(self, server):
        schemas = _get_tool_schemas(server)
        assert "limit" in schemas["cruxible_inspect_governance"].inputSchema["properties"]
        assert "relationship_type" in schemas["cruxible_inspect_entity"].inputSchema["properties"]
        history_props = schemas["cruxible_inspect_entity_history"].inputSchema["properties"]
        assert "entity_id" in history_props
        assert "limit" in history_props
        assert "offset" in history_props
        assert "config_yaml" in schemas["cruxible_reload_config"].inputSchema["properties"]
        assert (
            "config_source_manifest" in schemas["cruxible_reload_config"].inputSchema["properties"]
        )
        assert (
            "current_source_manifest" in schemas["cruxible_config_status"].inputSchema["properties"]
        )
        assert "snapshot_id" in schemas["cruxible_clone_snapshot"].inputSchema["required"]
        assert "root_dir" in schemas["cruxible_clone_snapshot"].inputSchema["required"]


class TestOutputSchema:
    """Verify typed returns produce outputSchema with expected keys."""

    @pytest.mark.parametrize(
        "tool_name,expected_keys",
        [
            ("cruxible_init", {"instance_id", "status", "warnings", "base_kit_id"}),
            (
                "cruxible_validate",
                {
                    "valid",
                    "name",
                    "entity_types",
                    "relationships",
                    "named_queries",
                    "warnings",
                },
            ),
            (
                "cruxible_query",
                {
                    "items",
                    "receipt_id",
                    "receipt",
                    "total",
                    "limit",
                    "offset",
                    "truncated",
                    "limit_truncated",
                    "path_truncated",
                    "truncation_reasons",
                    "max_paths",
                    "max_paths_per_result",
                    "total_path_count",
                    "retained_path_count",
                    "steps_executed",
                    "result_shape",
                    "dedupe",
                    "relationship_state",
                    "param_hints",
                    "policy_summary",
                },
            ),
            ("cruxible_feedback", {"feedback_id", "applied", "receipt_id"}),
            ("cruxible_feedback_from_query", {"feedback_id", "applied", "receipt_id"}),
            ("cruxible_outcome", {"outcome_id"}),
            ("cruxible_get_outcome_profile", {"found", "profile_key", "anchor_type", "profile"}),
            ("cruxible_list", {"items", "total", "limit", "offset", "truncated"}),
            (
                "cruxible_stats",
                {
                    "entity_count",
                    "edge_count",
                    "entity_counts",
                    "relationship_counts",
                    "status_counts",
                    "head_snapshot_id",
                },
            ),
            (
                "cruxible_evaluate",
                {
                    "entity_count",
                    "edge_count",
                    "findings",
                    "summary",
                    "constraint_summary",
                    "quality_summary",
                },
            ),
            (
                "cruxible_lint",
                {
                    "config_name",
                    "config_warnings",
                    "compatibility_warnings",
                    "evaluation",
                    "feedback_reports",
                    "outcome_reports",
                    "summary",
                    "has_issues",
                },
            ),
            (
                "cruxible_sample",
                {"items", "entity_type", "total", "limit", "offset", "truncated"},
            ),
            (
                "cruxible_inspect_entity_history",
                {
                    "entity_type",
                    "entity_id",
                    "items",
                    "total",
                    "limit",
                    "offset",
                    "truncated",
                    "legacy_entity_write_count",
                    "warnings",
                },
            ),
            ("cruxible_inspect_ontology", {"view", "payload"}),
            ("cruxible_inspect_workflows", {"view", "payload"}),
            ("cruxible_inspect_queries", {"view", "payload"}),
            ("cruxible_inspect_governance", {"view", "payload"}),
            ("cruxible_inspect_overview", {"view", "payload"}),
            (
                "cruxible_add_relationship",
                {
                    "added",
                    "updated",
                    "pending_conflicts",
                    "updated_group_backed_edges",
                    "receipt_id",
                },
            ),
            ("cruxible_add_entity", {"entities_added", "entities_updated", "receipt_id"}),
            ("cruxible_add_constraint", {"name", "added", "config_updated", "warnings"}),
            ("cruxible_get_feedback_profile", {"found", "relationship_type", "profile"}),
            (
                "cruxible_analyze_feedback",
                {
                    "relationship_type",
                    "feedback_count",
                    "action_counts",
                    "source_counts",
                    "reason_code_counts",
                    "coded_groups",
                    "uncoded_feedback_count",
                    "uncoded_examples",
                    "constraint_suggestions",
                    "decision_policy_suggestions",
                    "quality_check_candidates",
                    "provider_fix_candidates",
                    "warnings",
                },
            ),
            (
                "cruxible_analyze_outcomes",
                {
                    "anchor_type",
                    "outcome_count",
                    "outcome_counts",
                    "outcome_code_counts",
                    "coded_groups",
                    "uncoded_outcome_count",
                    "uncoded_examples",
                    "trust_adjustment_suggestions",
                    "workflow_review_policy_suggestions",
                    "query_policy_suggestions",
                    "provider_fix_candidates",
                    "debug_packages",
                    "workflow_debug_packages",
                    "warnings",
                },
            ),
            ("cruxible_add_decision_policy", {"name", "added", "config_updated", "warnings"}),
            (
                "cruxible_lock_workflow",
                {"lock_path", "config_digest", "providers_locked", "artifacts_locked"},
            ),
            ("cruxible_plan_workflow", {"plan"}),
            (
                "cruxible_run_workflow",
                {
                    "workflow",
                    "output",
                    "receipt_id",
                    "mode",
                    "workflow_type",
                    "canonical",
                    "apply_digest",
                    "head_snapshot_id",
                    "committed_snapshot_id",
                    "apply_previews",
                    "query_receipt_ids",
                    "read_metadata",
                    "trace_ids",
                    "receipt",
                    "traces",
                },
            ),
            (
                "cruxible_apply_workflow",
                {
                    "workflow",
                    "output",
                    "receipt_id",
                    "mode",
                    "workflow_type",
                    "canonical",
                    "apply_digest",
                    "head_snapshot_id",
                    "committed_snapshot_id",
                    "apply_previews",
                    "query_receipt_ids",
                    "read_metadata",
                    "trace_ids",
                    "receipt",
                    "traces",
                },
            ),
            ("cruxible_test_workflow", {"total", "passed", "failed", "cases"}),
            (
                "cruxible_reload_config",
                {"config_path", "updated", "warnings", "type_delta", "strandings"},
            ),
            (
                "cruxible_config_status",
                {
                    "status",
                    "config_path",
                    "materialized_matches",
                    "sources_checked",
                    "composed_matches",
                    "changed_sources",
                    "provenance",
                },
            ),
            (
                "cruxible_propose_workflow",
                {
                    "workflow",
                    "output",
                    "receipt_id",
                    "mode",
                    "workflow_type",
                    "canonical",
                    "group_id",
                    "group_status",
                    "review_priority",
                    "query_receipt_ids",
                    "trace_ids",
                    "prior_resolution",
                    "suppressed",
                    "suppressed_members",
                    "policy_summary",
                    "read_metadata",
                    "receipt",
                    "traces",
                },
            ),
            ("cruxible_create_snapshot", {"snapshot"}),
            (
                "cruxible_instance_backup",
                {"instance_id", "artifact_path", "manifest"},
            ),
            (
                "cruxible_instance_restore",
                {"instance_id", "root_dir", "manifest", "registry_status"},
            ),
            (
                "cruxible_instance_relocate",
                {
                    "instance_id",
                    "from_dir",
                    "to_dir",
                    "manifest",
                    "source_removed",
                    "registry_status",
                },
            ),
            (
                "cruxible_list_snapshots",
                {"items", "total", "limit", "offset", "truncated"},
            ),
            ("cruxible_clone_snapshot", {"instance_id", "snapshot", "admin_credential"}),
            (
                "cruxible_get_entity",
                {"found", "entity_type", "entity_id", "properties", "metadata"},
            ),
            (
                "cruxible_get_relationship",
                {
                    "found",
                    "from_type",
                    "from_id",
                    "relationship_type",
                    "to_type",
                    "to_id",
                    "edge_key",
                    "properties",
                    "metadata",
                },
            ),
        ],
    )
    def test_typed_output_schema(self, server, tool_name, expected_keys):
        schemas = _get_tool_schemas(server)
        output = schemas[tool_name].outputSchema
        assert output["type"] == "object"
        assert set(output["properties"].keys()) == expected_keys

    @pytest.mark.parametrize(
        "tool_name",
        # cruxible_inspect_entity returns a dict because its result is a UNION
        # of the legacy single-hop and expanded neighborhood contract models;
        # a union annotation would make FastMCP wrap the payload in a
        # {"result": ...} envelope and break the legacy top-level shape.
        ["cruxible_receipt", "cruxible_schema", "cruxible_inspect_entity"],
    )
    def test_dict_output_schema(self, server, tool_name):
        schemas = _get_tool_schemas(server)
        output = schemas[tool_name].outputSchema
        assert output["type"] == "object"
        assert output.get("additionalProperties") is True


class TestErrorPropagation:
    """Verify errors raise ToolError through server.call_tool."""

    def test_invalid_instance_raises(self, server):
        with pytest.raises(ToolError):
            asyncio.run(server.call_tool("cruxible_schema", {"instance_id": "/no/such/instance"}))

    def test_bad_receipt_raises(self, server, tmp_project):
        CruxibleInstance.init(tmp_project, "config.yaml")
        with pytest.raises(ToolError, match="RCP-missing"):
            asyncio.run(
                server.call_tool(
                    "cruxible_receipt",
                    {"instance_id": str(tmp_project), "receipt_id": "RCP-missing"},
                )
            )

    def test_validate_bad_config_raises(self, server, tmp_path):
        """ConfigError details survive MCP propagation."""
        bad_config = tmp_path / "bad.yaml"
        bad_config.write_text(
            "version: '1.0'\n"
            "name: bad\n"
            "entity_types:\n"
            "  A:\n"
            "    properties:\n"
            "      id: {type: string, primary_key: true}\n"
            "relationships:\n"
            "  - name: bad_rel\n"
            "    from: A\n"
            "    to: Ghost\n"
        )
        with pytest.raises(ToolError, match="Ghost"):
            asyncio.run(
                server.call_tool(
                    "cruxible_validate",
                    {"config_path": str(bad_config)},
                )
            )

    def test_validate_missing_file_raises(self, server, tmp_path):
        """Missing config file raises ToolError with path detail."""
        with pytest.raises(ToolError, match="nonexistent.yaml"):
            asyncio.run(
                server.call_tool(
                    "cruxible_validate",
                    {"config_path": str(tmp_path / "nonexistent.yaml")},
                )
            )

    def test_validate_missing_primary_key_raises(self, server, tmp_path):
        """Missing primary_key: true on properties is caught by cruxible_validate."""
        config = tmp_path / "no_pk.yaml"
        config.write_text(
            "version: '1.0'\n"
            "name: no_pk\n"
            "entity_types:\n"
            "  Thing:\n"
            "    properties:\n"
            "      name: {type: string}\n"
            "relationships: []\n"
        )
        with pytest.raises(ToolError, match="primary_key"):
            asyncio.run(
                server.call_tool(
                    "cruxible_validate",
                    {"config_path": str(config)},
                )
            )
