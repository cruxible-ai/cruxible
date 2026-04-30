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

    def test_feedback_action_enum(self, server):
        schemas = _get_tool_schemas(server)
        action = schemas["cruxible_feedback"].inputSchema["properties"]["action"]
        assert action["enum"] == ["approve", "reject", "correct", "flag"]

    def test_feedback_source_enum(self, server):
        schemas = _get_tool_schemas(server)
        source = schemas["cruxible_feedback"].inputSchema["properties"]["source"]
        assert source["enum"] == ["human", "agent"]

    def test_outcome_outcome_enum(self, server):
        schemas = _get_tool_schemas(server)
        outcome = schemas["cruxible_outcome"].inputSchema["properties"]["outcome"]
        assert outcome["enum"] == ["correct", "incorrect", "partial", "unknown"]

    def test_find_candidates_strategy_enum(self, server):
        schemas = _get_tool_schemas(server)
        strategy = schemas["cruxible_find_candidates"].inputSchema["properties"]["strategy"]
        assert strategy["enum"] == ["property_match", "shared_neighbors"]

    def test_list_resource_type_enum(self, server):
        schemas = _get_tool_schemas(server)
        resource_type = schemas["cruxible_list"].inputSchema["properties"]["resource_type"]
        assert resource_type["enum"] == ["entities", "edges", "receipts", "feedback", "outcomes"]

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
        assert {"from_type", "from_id", "relationship", "to_type", "to_id"} <= required

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

    def test_ingest_optional_data_params(self, server):
        """cruxible_ingest data params (file_path, data_csv, etc.) are all optional."""
        schemas = _get_tool_schemas(server)
        schema = schemas["cruxible_ingest"].inputSchema
        for param in ("file_path", "data_csv", "data_json", "data_ndjson", "upload_id"):
            assert param in schema["properties"]
        required = set(schema.get("required", []))
        for param in ("file_path", "data_csv", "data_json", "data_ndjson", "upload_id"):
            assert param not in required
        # instance_id and mapping_name remain required
        assert "instance_id" in required
        assert "mapping_name" in required

    def test_list_has_property_filter(self, server):
        schemas = _get_tool_schemas(server)
        props = schemas["cruxible_list"].inputSchema["properties"]
        assert "property_filter" in props

    def test_evaluate_has_exclude_orphan_types(self, server):
        schemas = _get_tool_schemas(server)
        props = schemas["cruxible_evaluate"].inputSchema["properties"]
        assert "exclude_orphan_types" in props

    def test_find_candidates_has_min_distinct_neighbors(self, server):
        schemas = _get_tool_schemas(server)
        props = schemas["cruxible_find_candidates"].inputSchema["properties"]
        assert "min_distinct_neighbors" in props

    def test_init_optional_config_yaml(self, server):
        """cruxible_init has config_yaml in properties, not required."""
        schemas = _get_tool_schemas(server)
        schema = schemas["cruxible_init"].inputSchema
        assert "config_yaml" in schema["properties"]
        required = set(schema.get("required", []))
        assert "config_yaml" not in required
        # root_dir remains required
        assert "root_dir" in required

    def test_world_fork_has_optional_world_ref_and_transport_ref(self, server):
        schemas = _get_tool_schemas(server)
        schema = schemas["cruxible_world_fork"].inputSchema
        props = schema["properties"]
        required = set(schema.get("required", []))
        assert "root_dir" in required
        assert "transport_ref" in props
        assert "world_ref" in props
        assert "kit" in props
        assert "no_kit" in props
        assert "transport_ref" not in required
        assert "world_ref" not in required
        assert "kit" not in required
        assert "no_kit" not in required


class TestOutputSchema:
    """Verify typed returns produce outputSchema with expected keys."""

    @pytest.mark.parametrize(
        "tool_name,expected_keys",
        [
            ("cruxible_init", {"instance_id", "status", "warnings"}),
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
                "cruxible_ingest",
                {
                    "records_ingested",
                    "records_updated",
                    "mapping",
                    "entity_type",
                    "relationship_type",
                    "receipt_id",
                },
            ),
            (
                "cruxible_query",
                {
                    "results",
                    "receipt_id",
                    "receipt",
                    "total_results",
                    "truncated",
                    "steps_executed",
                    "param_hints",
                    "policy_summary",
                },
            ),
            ("cruxible_feedback", {"feedback_id", "applied", "receipt_id"}),
            ("cruxible_outcome", {"outcome_id"}),
            ("cruxible_get_outcome_profile", {"found", "profile_key", "anchor_type", "profile"}),
            ("cruxible_list", {"items", "total"}),
            ("cruxible_find_candidates", {"candidates", "total"}),
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
            ("cruxible_sample", {"entities", "entity_type", "count"}),
            ("cruxible_add_relationship", {"added", "updated", "receipt_id"}),
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
                    "canonical",
                    "apply_digest",
                    "head_snapshot_id",
                    "committed_snapshot_id",
                    "apply_previews",
                    "query_receipt_ids",
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
                    "canonical",
                    "apply_digest",
                    "head_snapshot_id",
                    "committed_snapshot_id",
                    "apply_previews",
                    "query_receipt_ids",
                    "trace_ids",
                    "receipt",
                    "traces",
                },
            ),
            (
                "cruxible_propose_workflow",
                {
                    "workflow",
                    "output",
                    "receipt_id",
                    "group_id",
                    "group_status",
                    "review_priority",
                    "query_receipt_ids",
                    "trace_ids",
                    "prior_resolution",
                    "suppressed",
                    "suppressed_members",
                    "policy_summary",
                    "receipt",
                    "traces",
                },
            ),
            ("cruxible_get_entity", {"found", "entity_type", "entity_id", "properties"}),
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
                },
            ),
        ],
    )
    def test_typed_output_schema(self, server, tool_name, expected_keys):
        schemas = _get_tool_schemas(server)
        output = schemas[tool_name].outputSchema
        assert output["type"] == "object"
        assert set(output["properties"].keys()) == expected_keys

    @pytest.mark.parametrize("tool_name", ["cruxible_receipt", "cruxible_schema"])
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

    def test_ingest_error_includes_details(self, server, governed_client, tmp_project):
        """DataValidationError details survive MCP propagation."""
        del governed_client
        init_result = asyncio.run(
            server.call_tool(
                "cruxible_init",
                {"root_dir": str(tmp_project), "config_path": "config.yaml"},
            )
        )
        if isinstance(init_result, tuple):
            instance_id = init_result[1]["instance_id"]
        else:
            instance_id = init_result["instance_id"]
        bad_csv = tmp_project / "bad_vehicles.csv"
        bad_csv.write_text("wrong_col,another_col\nfoo,bar\n")
        with pytest.raises(ToolError) as exc_info:
            asyncio.run(
                server.call_tool(
                    "cruxible_ingest",
                    {
                        "instance_id": instance_id,
                        "mapping_name": "vehicles",
                        "file_path": str(bad_csv),
                    },
                )
            )
        # The error message should contain the specific column name
        assert "vehicle_id" in str(exc_info.value).lower()

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
