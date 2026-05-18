"""Tests for service-backed read/render surfaces."""

from __future__ import annotations

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.service import (
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
