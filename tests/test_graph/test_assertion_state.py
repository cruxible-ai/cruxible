"""Tests for relationship assertion helpers."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cruxible_core.graph.assertion_state import (
    EntityLifecycleState,
    RelationshipAssertion,
    RelationshipLifecycleState,
    RelationshipReviewState,
    SupersessionPointer,
    relationship_is_live,
)
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.provenance import RelationshipProvenance
from cruxible_core.graph.types import (
    EntityInstance,
    RelationshipInstance,
    RelationshipMetadata,
    mint_claim_id,
)


def test_default_assertion_is_unreviewed_active() -> None:
    assertion = RelationshipAssertion()

    assert assertion.review.status == "unreviewed"
    assert assertion.review.source == "system"
    assert assertion.lifecycle.status == "active"


def test_relationship_metadata_contains_typed_assertion_and_provenance() -> None:
    metadata = RelationshipMetadata(
        provenance=RelationshipProvenance(source="ingest", source_ref="feed-1"),
        assertion=RelationshipAssertion(
            review=RelationshipReviewState(status="approved", source="human")
        ),
    )

    assert metadata.provenance is not None
    assert metadata.provenance.source == "ingest"
    assert metadata.provenance.source_ref == "feed-1"
    assert metadata.assertion.review.status == "approved"
    assert metadata.assertion.review.source == "human"
    assert metadata.assertion.lifecycle.status == "active"


@pytest.mark.parametrize(
    ("assertion", "expected"),
    [
        (RelationshipAssertion(), True),
        (
            RelationshipAssertion(
                review=RelationshipReviewState(status="approved", source="human")
            ),
            True,
        ),
        (
            RelationshipAssertion(review=RelationshipReviewState(status="pending", source="human")),
            False,
        ),
        (
            RelationshipAssertion(
                review=RelationshipReviewState(status="rejected", source="agent")
            ),
            False,
        ),
        (
            RelationshipAssertion(lifecycle=RelationshipLifecycleState(status="inactive")),
            False,
        ),
    ],
)
def test_relationship_is_live_handles_review_and_lifecycle(
    assertion: RelationshipAssertion,
    expected: bool,
) -> None:
    assert relationship_is_live(assertion) is expected
    assert relationship_is_live(RelationshipMetadata(assertion=assertion)) is expected


def test_relationship_is_live_honors_effective_window() -> None:
    future = RelationshipAssertion(
        lifecycle=RelationshipLifecycleState(
            status="active",
            effective_from=datetime(2999, 1, 1, tzinfo=timezone.utc),
        )
    )
    expired = RelationshipAssertion(
        lifecycle=RelationshipLifecycleState(
            status="active",
            effective_until=datetime(2000, 1, 1, tzinfo=timezone.utc),
        )
    )

    assert relationship_is_live(future) is False
    assert relationship_is_live(expired) is False


def test_invalid_assertion_timestamp_is_not_silently_downgraded() -> None:
    with pytest.raises(ValueError):
        RelationshipAssertion.model_validate(
            {
                "review": {"status": "approved", "source": "human"},
                "lifecycle": {
                    "status": "active",
                    "effective_from": "not-a-datetime",
                },
            }
        )


def test_supersession_pointer_types_the_two_named_shapes() -> None:
    """Edges are named by claim_id, entities by their natural key."""
    edge_pointer = RelationshipLifecycleState.model_validate(
        {"status": "superseded", "superseded_by": {"claim_id": "CLM-abc"}}
    )
    assert edge_pointer.superseded_by is not None
    assert edge_pointer.superseded_by.claim_id == "CLM-abc"

    entity_pointer = EntityLifecycleState.model_validate(
        {"status": "superseded", "superseded_by": {"entity_type": "Part", "entity_id": "BP-1"}}
    )
    assert entity_pointer.superseded_by is not None
    assert entity_pointer.superseded_by.entity_id == "BP-1"


def test_supersession_pointer_stays_open_for_future_kinds() -> None:
    """extra='allow' is deliberate: a new pointer kind needs no data migration."""
    pointer = SupersessionPointer.model_validate({"procedure_id": "PRC-1"})
    assert pointer.claim_id is None
    assert pointer.model_dump()["procedure_id"] == "PRC-1"


@pytest.mark.parametrize(
    "payload",
    [
        {},  # names nothing at all
        {"entity_type": "Part"},  # half an entity reference
        {"entity_id": "BP-1"},
        {"claim_id": "CLM-abc", "entity_type": "Part", "entity_id": "BP-1"},  # both kinds
    ],
)
def test_supersession_pointer_refuses_incoherent_shapes(payload: dict) -> None:
    with pytest.raises(ValueError):
        SupersessionPointer.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"entity_type": "Part"},
        {"entity_id": "BP-1"},
        {"claim_id": "CLM-abc", "entity_type": "Part", "entity_id": "BP-1"},
        "not-even-a-mapping",
    ],
)
def test_a_stored_incoherent_pointer_loads_as_none_instead_of_bricking_the_graph(
    payload: object,
) -> None:
    """LOAD tolerance. The write-path refusal must not become an unloadable graph.

    Lifecycle state is decoded out of persisted metadata on every graph load, so
    a refusal here is not "one bad write rejected" -- it is an instance whose
    graph can no longer be read at all, by any path, including the ones you
    would use to repair it.
    """
    state = RelationshipLifecycleState.model_validate(
        {"status": "superseded", "superseded_by": payload, "supersedes": payload}
    )
    assert state.superseded_by is None
    assert state.supersedes is None
    # ...and the write path still refuses the same shapes loudly.
    if isinstance(payload, dict):
        with pytest.raises(ValueError):
            SupersessionPointer.model_validate(payload)


def test_a_graph_with_a_stored_empty_supersession_pointer_loads_clean() -> None:
    """The end-to-end shape of the tolerance: one stray value, graph still loads."""
    graph = EntityGraph()
    graph.add_entity(EntityInstance(entity_type="Part", entity_id="BP-1", properties={}))
    graph.add_entity(EntityInstance(entity_type="Vehicle", entity_id="V-1", properties={}))
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="fits",
            from_type="Part",
            from_id="BP-1",
            to_type="Vehicle",
            to_id="V-1",
            claim_id=mint_claim_id(),
        )
    )
    payload = graph.to_dict()
    for edge in payload["edges"]:
        edge["metadata"]["assertion"]["lifecycle"]["supersedes"] = {}

    reloaded = EntityGraph.from_dict(payload)
    edge_instance = reloaded.get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits")
    assert edge_instance is not None
    assert edge_instance.metadata.assertion.lifecycle.supersedes is None


def test_lifecycle_serialization_is_unchanged_when_no_pointer_is_set() -> None:
    """Typing the field must not move any bytes for the (universal) unset case."""
    dumped = RelationshipLifecycleState().model_dump(mode="json")
    assert dumped["supersedes"] is None
    assert dumped["superseded_by"] is None
    assert list(dumped) == [
        "status",
        "reason",
        "effective_from",
        "effective_until",
        "closed_at",
        "closed_by",
        "supersedes",
        "superseded_by",
    ]
