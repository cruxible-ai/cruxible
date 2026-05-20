"""Tests for service-backed read/render surfaces."""

from __future__ import annotations

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.config.schema import NamedQuerySchema, TraversalStep
from cruxible_core.service import (
    service_describe_query,
    service_explain_receipt,
    service_export_edges,
    service_inspect_view,
    service_query,
    service_render_wiki,
)


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
        item
        for item in result.payload["queries"]["queries"]
        if item["name"] == "parts_for_vehicle"
    )
    assert query["result_shape"] == "path"
    assert query["dedupe"] == "path"


def test_service_describe_query_infers_required_input_refs(
    populated_instance: CruxibleInstance,
) -> None:
    config = populated_instance.load_config()
    config.named_queries["parts_with_input_refs"] = NamedQuerySchema(
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
                        "edge": {
                            "properties.confidence": {
                                "gte": "$input.related_confidence"
                            }
                        },
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


def test_service_render_wiki_returns_page_payloads(
    populated_instance: CruxibleInstance,
) -> None:
    result = service_render_wiki(
        populated_instance,
        focus=["Part:BP-1001"],
        include_types=["Part"],
        scope="local",
    )

    assert result.page_count == len(result.pages)
    assert any(page.path == "index.md" for page in result.pages)
    assert any("BP-1001" in page.content for page in result.pages)


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
    result = service_export_edges(populated_instance, relationship="fits")

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
