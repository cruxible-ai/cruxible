"""Tests for service-layer query operations."""

from __future__ import annotations

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError
from cruxible_core.graph.assertion_state import (
    RelationshipAssertion,
    RelationshipReviewState,
)
from cruxible_core.graph.types import RelationshipMetadata
from cruxible_core.service import (
    OperationContext,
    service_create_decision_record,
    service_list_decision_events,
    service_query_inline_surface,
)


def _inline_collection_definition(name: str = "brake_parts") -> dict[str, object]:
    return {
        "name": name,
        "mode": "collection",
        "returns": "Part",
        "result_shape": "entity",
        "where": {"result.properties.category": {"eq": "brakes"}},
    }


def _inline_path_definition(name: str = "fit_paths") -> dict[str, object]:
    return {
        "name": name,
        "mode": "traversal",
        "entry_point": "Vehicle",
        "traversal": [
            {
                "relationship": "fits",
                "direction": "incoming",
                "filter": {"verified": True},
            }
        ],
        "returns": "Part",
        "result_shape": "path",
    }


def test_inline_collection_query_persists_receipt_and_not_config(
    populated_instance: CruxibleInstance,
) -> None:
    result = service_query_inline_surface(
        populated_instance,
        _inline_collection_definition(),
        {},
    )

    assert result.total == 2
    assert result.limit == 50
    assert result.receipt_id is not None
    assert result.receipt is not None
    assert result.receipt.query_name == "inline:brake_parts"
    assert "brake_parts" not in populated_instance.load_config().named_queries

    store = populated_instance.get_receipt_store()
    try:
        receipts = store.list_receipts(query_name="inline:brake_parts")
    finally:
        store.close()
    assert [receipt["receipt_id"] for receipt in receipts] == [result.receipt_id]


def test_inline_traversal_query_uses_path_budget_defaults(
    populated_instance: CruxibleInstance,
) -> None:
    result = service_query_inline_surface(
        populated_instance,
        _inline_path_definition(),
        {"vehicle_id": "V-2024-CIVIC-EX"},
    )

    assert result.total == 2
    assert result.limit == 50
    assert result.max_paths == 1000
    assert result.max_paths_per_result == 25
    assert result.param_hints is not None
    assert result.param_hints.required_params == ["vehicle_id"]


def test_inline_relationship_state_override_uses_existing_policy(
    populated_instance: CruxibleInstance,
) -> None:
    graph = populated_instance.load_graph()
    graph.update_relationship_state(
        "Part",
        "BP-1001",
        "Vehicle",
        "V-2024-CIVIC-EX",
        "fits",
        metadata=RelationshipMetadata(
            assertion=RelationshipAssertion(review=RelationshipReviewState(status="approved"))
        ),
    )
    populated_instance.save_graph(graph)

    result = service_query_inline_surface(
        populated_instance,
        {
            "name": "accepted_fitments",
            "mode": "collection",
            "returns": "fits",
            "result_shape": "relationship",
            "allow_relationship_state_override": True,
        },
        {},
        relationship_state="accepted",
    )

    assert result.relationship_state == "accepted"
    assert [(row.from_id, row.to_id) for row in result.items] == [("BP-1001", "V-2024-CIVIC-EX")]


def test_inline_query_records_decision_event(
    populated_instance: CruxibleInstance,
) -> None:
    record = service_create_decision_record(
        populated_instance,
        question="Which brake parts should we inspect?",
        opened_by="agent",
    ).record

    result = service_query_inline_surface(
        populated_instance,
        _inline_collection_definition("decision_brake_parts"),
        {},
        context=OperationContext(
            decision_record_id=record.decision_record_id,
            surface="cli",
        ),
    )

    events = service_list_decision_events(
        populated_instance,
        decision_record_id=record.decision_record_id,
    ).items
    assert len(events) == 1
    assert events[0].command == "query_inline:decision_brake_parts"
    assert events[0].receipt_id == result.receipt_id
    assert events[0].surface == "cli"


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [
        ("limit", 501, "inline query limit must be <= 500"),
        ("limit", "501", "inline query limit must be <= 500"),
        ("max_paths", 5001, "inline query max_paths must be <= 5000"),
        ("max_paths", "5001", "inline query max_paths must be <= 5000"),
        (
            "max_paths_per_result",
            101,
            "inline query max_paths_per_result must be <= 100",
        ),
        (
            "max_paths_per_result",
            "101",
            "inline query max_paths_per_result must be <= 100",
        ),
    ],
)
def test_inline_query_rejects_budget_caps(
    populated_instance: CruxibleInstance,
    field_name: str,
    value: object,
    message: str,
) -> None:
    definition = _inline_path_definition("over_budget")
    definition[field_name] = value

    with pytest.raises(ConfigError, match=message):
        service_query_inline_surface(
            populated_instance,
            definition,
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )


def test_inline_query_rejects_missing_name(
    populated_instance: CruxibleInstance,
) -> None:
    definition = _inline_collection_definition()
    definition.pop("name")

    with pytest.raises(ConfigError, match="requires non-empty name"):
        service_query_inline_surface(populated_instance, definition, {})
