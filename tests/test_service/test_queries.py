"""Tests for service-layer query operations."""

from __future__ import annotations

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError
from cruxible_core.graph.assertion_state import (
    RelationshipAssertion,
    RelationshipReviewState,
)
from cruxible_core.graph.types import RelationshipInstance, RelationshipMetadata
from cruxible_core.service import (
    OperationContext,
    service_create_decision_record,
    service_list,
    service_list_decision_events,
    service_query_inline_surface,
    service_sample,
)
from cruxible_core.service.queries import (
    _compile_edge_list_where,
    _relationship_matches_list_where,
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


def test_list_entities_without_fields_preserves_full_properties(
    populated_instance: CruxibleInstance,
) -> None:
    result = service_list(
        populated_instance,
        "entities",
        entity_type="Part",
        limit=1,
    )

    assert result.total == 2
    assert result.items[0].properties == {
        "category": "brakes",
        "name": "Ceramic Brake Pads",
        "part_number": "BP-1001",
        "price": 49.99,
    }


def test_list_entities_projects_requested_fields(
    populated_instance: CruxibleInstance,
) -> None:
    result = service_list(
        populated_instance,
        "entities",
        entity_type="Part",
        fields=["name", "category"],
        limit=1,
    )

    assert result.total == 2
    assert result.items[0].entity_type == "Part"
    assert result.items[0].entity_id == "BP-1001"
    assert result.items[0].properties == {
        "category": "brakes",
        "name": "Ceramic Brake Pads",
    }


def test_entity_projection_accepts_identity_aliases(
    populated_instance: CruxibleInstance,
) -> None:
    result = service_list(
        populated_instance,
        "entities",
        entity_type="Vehicle",
        fields=["id", "entity_id", "type", "entity_type", "make"],
        limit=1,
    )

    assert result.items[0].entity_type == "Vehicle"
    assert result.items[0].entity_id == "V-2024-ACCORD-SPORT"
    assert result.items[0].properties == {"make": "Honda"}


def test_entity_projection_rejects_unknown_fields(
    populated_instance: CruxibleInstance,
) -> None:
    with pytest.raises(ConfigError, match="Unknown field\\(s\\) for entity type 'Part': nope"):
        service_list(
            populated_instance,
            "entities",
            entity_type="Part",
            fields=["name", "nope"],
        )


def test_entity_projection_preserves_filter_and_pagination(
    populated_instance: CruxibleInstance,
) -> None:
    result = service_list(
        populated_instance,
        "entities",
        entity_type="Vehicle",
        property_filter={"model": "Civic"},
        fields=["make"],
        limit=1,
        offset=0,
    )

    assert result.total == 1
    assert result.items[0].entity_id == "V-2024-CIVIC-EX"
    assert result.items[0].properties == {"make": "Honda"}


def test_sample_entities_projects_requested_fields(
    populated_instance: CruxibleInstance,
) -> None:
    result = service_sample(
        populated_instance,
        "Part",
        limit=1,
        fields=["name"],
    )

    assert len(result) == 1
    assert result[0].entity_id == "BP-1001"
    assert result[0].properties == {"name": "Ceramic Brake Pads"}


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


def _dangling_replaces_edge() -> RelationshipInstance:
    """A stored `replaces` edge whose target Part is absent from the graph."""
    return RelationshipInstance(
        relationship_type="replaces",
        from_type="Part",
        from_id="BP-1002",
        to_type="Part",
        to_id="BP-MISSING",
        properties={"direction": "upgrade", "confidence": 0.95},
    )


def test_list_edges_keeps_missing_endpoint_edge_with_and_without_where(
    populated_instance: CruxibleInstance,
) -> None:
    """`list edges` treats a missing-endpoint edge identically with/without where.

    `list edges` is a stored-relationship inspection surface, so a stored edge
    stays visible even when an endpoint entity is missing. The where-is-None path
    keeps such an edge; pin that the where-set path keeps it too (no silent
    contract difference) when the edge properties satisfy the filter.
    """
    config = populated_instance.load_config()
    graph = populated_instance.load_graph()
    edge = _dangling_replaces_edge()
    assert graph.get_entity(edge.to_type, edge.to_id) is None

    without_where = _relationship_matches_list_where(config, graph, edge, None)

    matching_where = _compile_edge_list_where(
        config,
        "replaces",
        property_filter=None,
        where={"direction": {"eq": "upgrade"}},
    )
    with_matching_where = _relationship_matches_list_where(config, graph, edge, matching_where)

    assert without_where is True
    assert with_matching_where == without_where


def test_list_edges_missing_endpoint_edge_still_filtered_by_edge_properties(
    populated_instance: CruxibleInstance,
) -> None:
    """A missing endpoint never bypasses the edge-property predicate itself."""
    config = populated_instance.load_config()
    graph = populated_instance.load_graph()
    edge = _dangling_replaces_edge()

    non_matching_where = _compile_edge_list_where(
        config,
        "replaces",
        property_filter=None,
        where={"direction": {"eq": "downgrade"}},
    )

    assert _relationship_matches_list_where(config, graph, edge, non_matching_where) is False
