"""Unit tests for the shared output-profile serializer.

Pins the three profile contracts at the serializer seam:

* ``standard`` returns the input object untouched (identity, not a copy);
* ``full`` is an alias of standard (full ⊇ standard, today equal);
* ``compact`` is the bounded identity card that always preserves the
  governance markers (entity lifecycle; edge review + lifecycle) while
  dropping actor_context and provenance blobs.
"""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest

from cruxible_core.graph.assertion_state import (
    EntityLifecycleState,
    EntityLifecycleStatus,
    RelationshipLifecycleStatus,
    RelationshipReviewStatus,
)
from cruxible_core.graph.types import (
    EntityInstance,
    EntityMetadata,
    RelationshipInstance,
)
from cruxible_core.query.profiles import (
    COMPACT_MAX_SCALAR_PROPERTIES,
    compact_display_properties,
    inspect_neighbor_payload,
    profile_edge_payload,
    profile_entity_payload,
    profile_inspect_payload,
    profile_list_items,
    profile_query_item,
)

ACTOR_CONTEXT = {
    "actor_type": "human_user",
    "actor_id": "operator",
    "org_id": "local",
    "operation_id": "op_test",
    "timestamp": "2026-01-01T00:00:00+00:00",
}


def _entity_payload() -> dict:
    entity = EntityInstance(
        entity_type="WorkItem",
        entity_id="wi-1",
        properties={
            "title": "Ship the thing",
            "status": "active",
            "description": "long body " * 20,
            "priority": 2,
        },
        metadata=EntityMetadata(
            lifecycle=EntityLifecycleState(status="superseded"),
            actor_context=ACTOR_CONTEXT,  # type: ignore[arg-type]
        ),
    )
    return entity.model_dump(mode="json")


def _pending_edge_payload() -> dict:
    edge = RelationshipInstance(
        relationship_type="depends_on",
        from_type="WorkItem",
        from_id="wi-1",
        to_type="WorkItem",
        to_id="wi-2",
        edge_key=0,
        properties={"dependency_basis": "schema"},
    )
    edge.metadata.assertion.review.status = "pending"
    edge.metadata.assertion.review.source = "agent"
    payload = edge.model_dump(mode="json")
    payload["metadata"]["provenance"] = {
        "source_ref": "add_relationship",
        "actor_context": ACTOR_CONTEXT,
    }
    return payload


class TestStandardAndFull:
    def test_standard_returns_the_same_object(self) -> None:
        entity = _entity_payload()
        edge = _pending_edge_payload()
        assert profile_entity_payload(entity, "standard") is entity
        assert profile_edge_payload(edge, "standard") is edge
        assert profile_query_item(entity, "standard") is entity

    def test_full_is_an_alias_of_standard(self) -> None:
        entity = _entity_payload()
        edge = _pending_edge_payload()
        assert profile_entity_payload(entity, "full") is entity
        assert profile_edge_payload(edge, "full") is edge


class TestCompactEntity:
    def test_drops_actor_context_and_preserves_lifecycle_marker(self) -> None:
        compact = profile_entity_payload(_entity_payload(), "compact")
        assert compact["metadata"] == {"lifecycle": {"status": "superseded"}}
        assert "actor_context" not in str(compact)

    def test_bounded_display_properties(self) -> None:
        compact = profile_entity_payload(_entity_payload(), "compact")
        # Display keys win when present; the long description is dropped.
        assert compact["properties"] == {"title": "Ship the thing", "status": "active"}
        assert list(compact) == ["entity_type", "entity_id", "properties", "metadata"]

    def test_scalar_fallback_is_bounded_at_five(self) -> None:
        properties = {f"p{i}": i for i in range(8)}
        properties["blob"] = {"nested": True}
        picked = compact_display_properties(properties)
        assert picked == {f"p{i}": i for i in range(COMPACT_MAX_SCALAR_PROPERTIES)}

    def test_scalar_fallback_selection_is_insertion_order_independent(self) -> None:
        """The fallback picks by sorted key, not by dict insertion order.

        Persistence canonicalizes property JSON with sorted keys while the
        in-memory graph cache keeps insertion order; the selected five must be
        the same for both orderings of the same property set.
        """
        keys = [f"k{i}" for i in range(7)]
        forward = {key: key.upper() for key in keys}
        reverse = {key: key.upper() for key in reversed(keys)}

        forward_pick = compact_display_properties(forward)
        reverse_pick = compact_display_properties(reverse)

        assert forward_pick == reverse_pick
        assert list(forward_pick) == list(reverse_pick) == sorted(keys)[:5]


class TestCompactEdge:
    def test_keeps_identity_properties_and_governance_markers(self) -> None:
        compact = profile_edge_payload(_pending_edge_payload(), "compact")
        assert compact["relationship_type"] == "depends_on"
        assert compact["from_type"] == "WorkItem"
        assert compact["from_id"] == "wi-1"
        assert compact["to_type"] == "WorkItem"
        assert compact["to_id"] == "wi-2"
        assert compact["edge_key"] == 0
        assert compact["properties"] == {"dependency_basis": "schema"}
        # Reviewable (pending) governance markers survive compact.
        assert compact["metadata"]["assertion"]["review"] == {
            "status": "pending",
            "source": "agent",
        }
        assert compact["metadata"]["assertion"]["lifecycle"] == {"status": "active"}

    def test_drops_provenance_and_actor_context(self) -> None:
        compact = profile_edge_payload(_pending_edge_payload(), "compact")
        assert "provenance" not in compact["metadata"]
        assert "actor_context" not in str(compact)


class TestCompactQueryRows:
    def test_path_row_compacts_entities_and_segments(self) -> None:
        entity = _entity_payload()
        edge = _pending_edge_payload()
        row = {
            "entry": entity,
            "result": entity,
            "entities": [entity],
            "path": [edge],
            "includes": {},
        }
        compact = profile_query_item(row, "compact")
        assert compact["entry"]["metadata"] == {"lifecycle": {"status": "superseded"}}
        assert compact["path"][0]["metadata"]["assertion"]["review"]["status"] == "pending"
        assert "actor_context" not in str(compact)

    def test_projected_row_keeps_values_and_compacts_source(self) -> None:
        row = {"values": {"title": "x"}, "source": _entity_payload()}
        compact = profile_query_item(row, "compact")
        assert compact["values"] == {"title": "x"}
        assert "actor_context" not in str(compact["source"])


class TestCompactInspect:
    def test_neighbor_payload_and_envelope_fields(self) -> None:
        edge = _pending_edge_payload()
        neighbor = inspect_neighbor_payload(
            direction="outgoing",
            relationship_type="depends_on",
            edge_key=0,
            properties={"dependency_basis": "schema"},
            metadata=edge["metadata"],
            entity=_entity_payload(),
            profile="compact",
        )
        assert neighbor["metadata"]["assertion"]["review"]["status"] == "pending"
        assert neighbor["entity"]["metadata"] == {"lifecycle": {"status": "superseded"}}

        payload = profile_inspect_payload(
            {
                "found": True,
                "entity_type": "WorkItem",
                "entity_id": "wi-1",
                "properties": {"title": "t", "extra_scalar": 1},
                "metadata": _entity_payload()["metadata"],
                "neighbors": [neighbor],
                "total_neighbors": 41,
            },
            "compact",
        )
        # Envelope facts are never trimmed.
        assert payload["found"] is True
        assert payload["total_neighbors"] == 41
        assert payload["properties"] == {"title": "t"}


def _edge_payload_with_state(
    review_status: str,
    lifecycle_status: str,
    group_override: bool,
) -> dict:
    edge = RelationshipInstance(
        relationship_type="depends_on",
        from_type="WorkItem",
        from_id="wi-1",
        to_type="WorkItem",
        to_id="wi-2",
        edge_key=0,
        properties={"dependency_basis": "schema"},
    )
    edge.metadata.assertion.review.status = review_status  # type: ignore[assignment]
    edge.metadata.assertion.review.source = "human"
    edge.metadata.assertion.review.updated_by = "reviewer-1"
    edge.metadata.assertion.lifecycle.status = lifecycle_status  # type: ignore[assignment]
    edge.metadata.assertion.lifecycle.reason = "state-change reason"
    edge.metadata.assertion.group_override = group_override
    payload = edge.model_dump(mode="json")
    payload["metadata"]["provenance"] = {
        "source_ref": "add_relationship",
        "actor_context": ACTOR_CONTEXT,
    }
    return payload


# Enumerate the full per-kind status vocabularies from the schema Literals so a
# vocabulary change automatically widens this matrix.
_REVIEW_STATUSES = get_args(RelationshipReviewStatus)
_RELATIONSHIP_LIFECYCLE_STATUSES = get_args(RelationshipLifecycleStatus)
_ENTITY_LIFECYCLE_STATUSES = get_args(EntityLifecycleStatus)


class TestCompactGovernanceMarkerMatrix:
    """Exact marker values survive compact for EVERY governance state (F-003).

    Guards against a future "preserve markers only when pending/active" bug:
    every review status x relationship lifecycle status x group_override
    combination, and every entity lifecycle status, is asserted by VALUE.
    """

    @pytest.mark.parametrize("group_override", [False, True])
    @pytest.mark.parametrize("lifecycle_status", _RELATIONSHIP_LIFECYCLE_STATUSES)
    @pytest.mark.parametrize("review_status", _REVIEW_STATUSES)
    def test_edge_markers_survive_compact_for_every_state(
        self,
        review_status: str,
        lifecycle_status: str,
        group_override: bool,
    ) -> None:
        payload = _edge_payload_with_state(review_status, lifecycle_status, group_override)
        compact = profile_edge_payload(payload, "compact")

        expected_assertion: dict = {
            "review": {
                "status": review_status,
                "source": "human",
                "updated_by": "reviewer-1",
            },
            "lifecycle": {
                "status": lifecycle_status,
                "reason": "state-change reason",
            },
        }
        if group_override:
            expected_assertion["group_override"] = True
        assert compact["metadata"] == {"assertion": expected_assertion}
        assert "provenance" not in compact["metadata"]
        assert "actor_context" not in str(compact)

    @pytest.mark.parametrize("lifecycle_status", _ENTITY_LIFECYCLE_STATUSES)
    def test_entity_lifecycle_marker_survives_compact_for_every_status(
        self, lifecycle_status: str
    ) -> None:
        entity = EntityInstance(
            entity_type="WorkItem",
            entity_id="wi-1",
            properties={"title": "t"},
            metadata=EntityMetadata(
                lifecycle=EntityLifecycleState(
                    status=lifecycle_status,  # type: ignore[arg-type]
                    reason="lifecycle reason",
                ),
                actor_context=ACTOR_CONTEXT,  # type: ignore[arg-type]
            ),
        )
        compact = profile_entity_payload(entity.model_dump(mode="json"), "compact")
        assert compact["metadata"] == {
            "lifecycle": {"status": lifecycle_status, "reason": "lifecycle reason"}
        }

    def test_entity_without_lifecycle_compacts_to_empty_metadata(self) -> None:
        entity = EntityInstance(
            entity_type="WorkItem",
            entity_id="wi-1",
            properties={"title": "t"},
            metadata=EntityMetadata(actor_context=ACTOR_CONTEXT),  # type: ignore[arg-type]
        )
        compact = profile_entity_payload(entity.model_dump(mode="json"), "compact")
        assert compact["metadata"] == {}


class TestCompactPersistenceRoundTrip:
    """F-001 regression: compact selection is stable across a storage reload.

    Fresh writes keep the caller's insertion order in the in-memory graph;
    persistence canonicalizes property JSON with sorted keys. The entity below
    is written with ADVERSARIAL (reverse-sorted) key order and more scalars
    than the compact cap, then reloaded from sqlite; the compact card must be
    identical (values AND key order) before and after the round-trip.
    """

    _CONFIG = """\
version: "1.0"
name: profile_round_trip
entity_types:
  Widget:
    properties:
      widget_ref: {type: string, primary_key: true}
      alpha_prop: {type: string, optional: true}
      bravo_prop: {type: string, optional: true}
      charlie_prop: {type: string, optional: true}
      delta_prop: {type: string, optional: true}
      echo_prop: {type: string, optional: true}
      foxtrot_prop: {type: string, optional: true}
relationships: []
"""

    def test_compact_properties_identical_before_and_after_reload(self, tmp_path: Path) -> None:
        from cruxible_core.runtime.instance import CruxibleInstance

        (tmp_path / "config.yaml").write_text(self._CONFIG)
        instance = CruxibleInstance.init(tmp_path, "config.yaml")

        adversarial_keys = [
            "widget_ref",
            "foxtrot_prop",
            "echo_prop",
            "delta_prop",
            "charlie_prop",
            "bravo_prop",
            "alpha_prop",
        ]
        entity = EntityInstance(
            entity_type="Widget",
            entity_id="W-1",
            properties={key: key.upper() for key in adversarial_keys},
        )
        graph = instance.load_graph()
        graph.add_entity(entity)
        instance.save_graph(graph)

        fresh_compact = profile_entity_payload(entity.model_dump(mode="json"), "compact")

        restored_entity = CruxibleInstance.load(tmp_path).load_graph().get_entity("Widget", "W-1")
        assert restored_entity is not None
        # The round-trip really changed the raw ordering (storage canonicalizes
        # with sorted keys) — otherwise this test would pass vacuously.
        assert list(restored_entity.properties) == sorted(adversarial_keys)
        assert list(restored_entity.properties) != adversarial_keys

        restored_compact = profile_entity_payload(
            restored_entity.model_dump(mode="json"), "compact"
        )

        assert fresh_compact == restored_compact
        assert list(fresh_compact["properties"]) == list(restored_compact["properties"])
        # Sorted-key selection: the first five scalars alphabetically.
        assert list(fresh_compact["properties"]) == [
            "alpha_prop",
            "bravo_prop",
            "charlie_prop",
            "delta_prop",
            "echo_prop",
        ]


class TestListItems:
    def test_non_entity_resources_pass_through(self) -> None:
        receipts = [{"receipt_id": "RCP-1", "actor_context": ACTOR_CONTEXT}]
        assert profile_list_items(receipts, "receipts", "compact") is receipts

    def test_edges_and_entities_are_profiled(self) -> None:
        edges = profile_list_items([_pending_edge_payload()], "edges", "compact")
        entities = profile_list_items([_entity_payload()], "entities", "compact")
        assert "provenance" not in edges[0]["metadata"]
        assert entities[0]["metadata"] == {"lifecycle": {"status": "superseded"}}
