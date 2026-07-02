"""Tests for service-backed read/render surfaces."""

from __future__ import annotations

from cruxible_core.canonical_views import (
    build_ontology_view,
    build_overview_view,
    build_query_view,
    build_workflow_view,
    canonical_view_payload,
    render_ontology_markdown,
    render_overview_markdown,
)
from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.config.loader import load_config_from_string
from cruxible_core.config.schema import NamedQuerySchema, TraversalStep
from cruxible_core.service import (
    service_describe_query,
    service_explain_receipt,
    service_export_edges,
    service_inspect_view,
    service_list_queries,
    service_query,
)

ENUM_CONFIG_YAML = """\
version: "1.0"
name: enum_fixture
description: Fixture exercising shared and inline enum vocabularies

enums:
  priority:
    values: [low, medium, high, critical]
    ordered: low_to_high
  lifecycle_status:
    description: Work item lifecycle states
    values: [planned, active, blocked, closed]

entity_types:
  Task:
    properties:
      task_id:
        type: string
        primary_key: true
      priority:
        type: string
        enum_ref: priority
      status:
        type: string
        enum_ref: lifecycle_status
      severity:
        type: string
        enum: [trivial, minor, major]

relationships:
  - name: blocks
    from: Task
    to: Task
    properties:
      kind:
        type: string
        enum_ref: priority

named_queries: {}
constraints: []
"""


def _enum_config():
    return load_config_from_string(ENUM_CONFIG_YAML)


def test_service_inspect_view_returns_structured_ontology(
    populated_instance: CruxibleInstance,
) -> None:
    result = service_inspect_view(populated_instance, "ontology")

    assert result.view == "ontology"
    assert result.payload["entity_count"] == 2
    assert result.payload["relationship_count"] == 2
    assert {entity["name"] for entity in result.payload["entity_types"]} == {
        "Part",
        "Vehicle",
    }


def test_service_inspect_view_queries_include_result_shape_metadata(
    populated_instance: CruxibleInstance,
) -> None:
    result = service_inspect_view(populated_instance, "queries")

    query = next(item for item in result.payload["queries"] if item["name"] == "parts_for_vehicle")
    assert query["result_shape"] == "path"
    assert query["dedupe"] == "path"


def test_service_inspect_view_overview_queries_include_result_shape_metadata(
    populated_instance: CruxibleInstance,
) -> None:
    result = service_inspect_view(populated_instance, "overview")

    query = next(
        item for item in result.payload["queries"]["queries"] if item["name"] == "parts_for_vehicle"
    )
    assert query["result_shape"] == "path"
    assert query["dedupe"] == "path"


def test_service_describe_query_infers_required_input_refs(
    populated_instance: CruxibleInstance,
) -> None:
    config = populated_instance.load_config()
    config.named_queries["parts_with_input_refs"] = NamedQuerySchema(
        mode="traversal",
        entry_point="Vehicle",
        traversal=[
            TraversalStep(
                relationship="fits",
                direction="incoming",
                where={
                    "edge.properties.confidence": {"lte": "$input.max_confidence"},
                },
                where_related=[
                    {
                        "relationship": "fits",
                        "direction": "outgoing",
                        "edge": {"properties.confidence": {"gte": "$input.related_confidence"}},
                    }
                ],
                constraint="target.year >= $min_year",
                alias="fit",
            )
        ],
        returns="list[Part]",
        select={"mode": "$input.selected_mode", "part_id": "$result.entity_id"},
        order_by=[{"by": "$input.sort_token"}],
    )
    populated_instance.save_config(config)

    query = service_describe_query(populated_instance, "parts_with_input_refs")

    assert query.required_params == [
        "max_confidence",
        "min_year",
        "related_confidence",
        "selected_mode",
        "sort_token",
        "vehicle_id",
    ]


def test_service_query_definition_exposes_include_contract(
    populated_instance: CruxibleInstance,
) -> None:
    config = populated_instance.load_config()
    config.named_queries["parts_with_include"] = NamedQuerySchema(
        mode="traversal",
        entry_point="Vehicle",
        traversal=[
            TraversalStep(
                relationship="fits",
                direction="incoming",
                alias="fit",
            )
        ],
        returns="list[Part]",
        include={
            "vehicle_fitments": {
                "from": "$entry",
                "relationship": "fits",
                "direction": "incoming",
                "many": True,
                "limit": 5,
            }
        },
    )
    populated_instance.save_config(config)

    described = service_describe_query(populated_instance, "parts_with_include")
    listed = next(
        query
        for query in service_list_queries(populated_instance)
        if query.name == "parts_with_include"
    )

    expected_include = {
        "vehicle_fitments": {
            "from": "$entry",
            "relationship": "fits",
            "direction": "incoming",
            "many": True,
            "required": False,
            "limit": 5,
            "where_related": [],
            "where_not_related": [],
            "order_by": [],
        }
    }
    assert described.include == expected_include
    assert listed.include == expected_include
    assert "from_" not in described.include["vehicle_fitments"]


def test_entryless_named_query_metadata_surfaces(
    populated_instance: CruxibleInstance,
) -> None:
    config = populated_instance.load_config()
    config.named_queries["all_parts"] = NamedQuerySchema(
        mode="collection",
        result_shape="entity",
        returns="Part",
    )
    populated_instance.save_config(config)

    described = service_describe_query(populated_instance, "all_parts")
    listed = next(
        query for query in service_list_queries(populated_instance) if query.name == "all_parts"
    )
    inspected = service_inspect_view(populated_instance, "queries")
    inspected_query = next(
        query for query in inspected.payload["queries"] if query["name"] == "all_parts"
    )

    assert described.mode == "collection"
    assert described.entry_point is None
    assert described.required_params == []
    assert described.example_ids == []
    assert listed.mode == "collection"
    assert listed.entry_point is None
    assert listed.required_params == []
    assert listed.example_ids == []
    assert inspected_query["mode"] == "collection"
    assert inspected_query["entry_point"] is None


def test_service_explain_receipt_renders_markdown(
    populated_instance: CruxibleInstance,
) -> None:
    query = service_query(
        populated_instance,
        "parts_for_vehicle",
        {"vehicle_id": "V-2024-CIVIC-EX"},
    )

    assert query.receipt_id is not None
    explanation = service_explain_receipt(
        populated_instance,
        query.receipt_id,
        format="markdown",
    )

    assert explanation.receipt_id == query.receipt_id
    assert explanation.format == "markdown"
    assert query.receipt_id in explanation.content


def test_service_export_edges_builds_csv_ready_rows(
    populated_instance: CruxibleInstance,
) -> None:
    result = service_export_edges(populated_instance, relationship_type="fits")

    assert result.count == 3
    assert result.fieldnames == [
        "from_type",
        "from_id",
        "to_type",
        "to_id",
        "relationship_type",
        "edge_key",
        "properties_json",
        "metadata_json",
    ]
    assert all(row["relationship_type"] == "fits" for row in result.rows)
    assert all("properties_json" in row for row in result.rows)
    assert all("metadata_json" in row for row in result.rows)


def test_build_ontology_view_collects_shared_and_inline_enums() -> None:
    view = build_ontology_view(_enum_config())

    enums = {enum.name: enum for enum in view.enums}
    # Shared enums named by their enums: key.
    assert enums["priority"].values == ["low", "medium", "high", "critical"]
    assert enums["priority"].ordered is True
    assert enums["lifecycle_status"].ordered is False
    assert enums["lifecycle_status"].description == "Work item lifecycle states"
    # Inline enums named by their fully-qualified property path.
    assert enums["Task.severity"].values == ["trivial", "minor", "major"]
    assert enums["Task.severity"].ordered is False


def test_build_ontology_view_enum_used_by_lists_every_reference() -> None:
    view = build_ontology_view(_enum_config())

    enums = {enum.name: enum for enum in view.enums}
    # priority is referenced by an entity property and a relationship property.
    assert enums["priority"].used_by == ["Task.priority", "blocks.kind"]
    assert enums["lifecycle_status"].used_by == ["Task.status"]
    assert enums["Task.severity"].used_by == ["Task.severity"]


def test_canonical_view_payload_serializes_enum_vocabularies() -> None:
    payload = canonical_view_payload(build_ontology_view(_enum_config()))

    assert "enums" in payload
    priority = next(item for item in payload["enums"] if item["name"] == "priority")
    assert priority == {
        "name": "priority",
        "values": ["low", "medium", "high", "critical"],
        "ordered": True,
        "description": None,
        "used_by": ["Task.priority", "blocks.kind"],
    }


def test_render_ontology_markdown_lists_enum_values_and_orderedness() -> None:
    markdown = render_ontology_markdown(build_ontology_view(_enum_config()))

    assert "## Enum Vocabularies" in markdown
    # Complete value vocabulary is rendered, not sampled.
    assert "low, medium, high, critical" in markdown
    assert "planned, active, blocked, closed" in markdown
    assert "trivial, minor, major" in markdown
    # Ordered enums are flagged; unordered enums are not.
    assert "low_to_high" in markdown


def test_render_overview_markdown_includes_enum_vocabularies() -> None:
    config = _enum_config()
    overview = build_overview_view(
        ontology=build_ontology_view(config),
        workflows=build_workflow_view(config),
        queries=build_query_view(config, query_infos=[]),
        governance=_empty_governance_view(),
    )

    markdown = render_overview_markdown(overview)

    assert "## Enum Vocabularies" in markdown
    assert "low, medium, high, critical" in markdown


def test_render_ontology_markdown_handles_no_enums() -> None:
    config = load_config_from_string(
        """\
version: "1.0"
name: no_enums
entity_types:
  Thing:
    properties:
      thing_id:
        type: string
        primary_key: true
relationships: []
named_queries: {}
constraints: []
"""
    )

    markdown = render_ontology_markdown(build_ontology_view(config))

    assert "## Enum Vocabularies" in markdown
    assert "No configured enums." in markdown


def _empty_governance_view():
    from cruxible_core.canonical_views import GovernanceView

    return GovernanceView(
        governed_relationship_count=0,
        pending_group_count=0,
        total_pending_groups=0,
        approved_resolution_count=0,
        total_resolutions=0,
        pending_truncated=False,
        resolutions_truncated=False,
    )
