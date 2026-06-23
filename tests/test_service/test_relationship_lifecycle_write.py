"""Typed, review-SAFE relationship lifecycle write channel.

The new capability: a direct write can set an edge's ``assertion.lifecycle``
(e.g. retract a live edge) WITHOUT touching the governed review axis. These tests
prove the three properties an adversarial reviewer cares about:

  (a) the relationship shape is preserved -- only ``assertion.lifecycle`` changes;
  (b) review-safety -- a lifecycle write can NEVER mutate ``assertion.review`` or
      ``group_override``, including the case where the edge was already approved;
  (c) the lifecycle write round-trips through storage and gates the edge.
"""

from __future__ import annotations

import pytest

from cruxible_client import contracts
from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.graph.assertion_state import (
    RelationshipLifecycleState,
    RelationshipReviewState,
)
from cruxible_core.service import service_list
from cruxible_core.service.mutations import service_batch_direct_write
from cruxible_core.service.queries import service_get_relationship
from cruxible_core.service.types import BatchDirectWriteInput, BatchRelationshipWriteInput

_FITS = dict(
    from_type="Part",
    from_id="BP-1001",
    relationship_type="fits",
    to_type="Vehicle",
    to_id="V-2024-CIVIC-EX",
)


def _retract_fits_via_batch(
    instance: CruxibleInstance,
    *,
    status: str = "retracted",
    reason: str | None = "superseded by newer fitment",
) -> None:
    service_batch_direct_write(
        instance,
        BatchDirectWriteInput(
            relationships=[
                BatchRelationshipWriteInput(
                    **_FITS,
                    properties={"verified": True, "source": "catalog"},
                    lifecycle=RelationshipLifecycleState(status=status, reason=reason),  # type: ignore[arg-type]
                )
            ]
        ),
    )


def _get_fits(instance: CruxibleInstance):
    return service_get_relationship(
        instance,
        from_type="Part",
        from_id="BP-1001",
        relationship_type="fits",
        to_type="Vehicle",
        to_id="V-2024-CIVIC-EX",
    )


# ---------------------------------------------------------------------------
# (a) shape preservation + (c) round-trip
# ---------------------------------------------------------------------------


def test_relationship_lifecycle_write_sets_only_lifecycle(
    populated_instance: CruxibleInstance,
) -> None:
    before = _get_fits(populated_instance)
    assert before is not None
    assert before.metadata.assertion.lifecycle.status == "active"

    _retract_fits_via_batch(populated_instance)

    after = _get_fits(populated_instance)
    assert after is not None
    # Lifecycle slice is updated and round-trips through storage.
    assert after.metadata.assertion.lifecycle.status == "retracted"
    assert after.metadata.assertion.lifecycle.reason == "superseded by newer fitment"
    # Edge properties are untouched.
    assert after.properties["verified"] is True
    assert after.properties["source"] == "catalog"


def test_retracted_edge_is_gated_out_of_live_reads(
    populated_instance: CruxibleInstance,
) -> None:
    _retract_fits_via_batch(populated_instance)

    def _edge_ids(state: str) -> set[tuple[str, str]]:
        result = service_list(
            populated_instance,
            "edges",
            relationship_type="fits",
            relationship_state=state,
        )
        return {(item["from_id"], item["to_id"]) for item in result.items}

    # The retracted edge falls out of the live edge view...
    assert ("BP-1001", "V-2024-CIVIC-EX") not in _edge_ids("live")
    # ...but is surfaced by the not-live view.
    assert ("BP-1001", "V-2024-CIVIC-EX") in _edge_ids("not-live")


# ---------------------------------------------------------------------------
# (b) review-safety: a lifecycle write CANNOT mutate review / group_override
# ---------------------------------------------------------------------------


def test_lifecycle_write_cannot_mutate_review_state(
    populated_instance: CruxibleInstance,
) -> None:
    """A lifecycle write preserves the edge's review axis exactly.

    Seed the edge as approved-by-group with group_override set (the shape a
    governed/group-resolve path produces), then drive a lifecycle write. The
    review status, source, and group_override MUST be byte-identical afterwards;
    only the lifecycle slice may change.
    """
    # Stamp an approved review + group_override directly on the stored edge.
    graph = populated_instance.load_graph()
    rel = graph.get_relationship(
        "Part", "BP-1001", "Vehicle", "V-2024-CIVIC-EX", "fits"
    )
    assert rel is not None
    rel.metadata = rel.metadata.model_copy(
        update={
            "assertion": rel.metadata.assertion.model_copy(
                update={
                    "review": RelationshipReviewState(
                        status="approved",
                        source="group",
                        updated_by="group:seed",
                    ),
                    "group_override": True,
                }
            )
        }
    )
    graph.replace_relationship_state(
        "Part",
        "BP-1001",
        "Vehicle",
        "V-2024-CIVIC-EX",
        "fits",
        properties=rel.properties,
        metadata=rel.metadata,
    )
    populated_instance.save_graph(graph)

    review_before = _get_fits(populated_instance).metadata.assertion.review.model_dump(
        mode="json"
    )
    override_before = _get_fits(populated_instance).metadata.assertion.group_override
    assert review_before["status"] == "approved"
    assert override_before is True

    # Now retract via the typed lifecycle write.
    _retract_fits_via_batch(populated_instance)

    after = _get_fits(populated_instance)
    assert after is not None
    # Lifecycle changed...
    assert after.metadata.assertion.lifecycle.status == "retracted"
    # ...but review state and group_override are byte-identical (NOT self-approved
    # or flipped by the lifecycle write).
    assert after.metadata.assertion.review.model_dump(mode="json") == review_before
    assert after.metadata.assertion.group_override is True


def test_relationship_lifecycle_contract_forbids_review_fields() -> None:
    """The contract input is structurally incapable of carrying review state.

    The typed split is the whole point: a lifecycle write cannot smuggle a review
    approval/rejection or group_override flip through this channel because the
    input model forbids any field other than status/reason.
    """
    # Valid lifecycle-only input parses.
    contracts.RelationshipLifecycleInput.model_validate({"status": "retracted"})

    # Attempting to attach a review approval is rejected (extra="forbid").
    with pytest.raises(Exception):
        contracts.RelationshipLifecycleInput.model_validate(
            {"status": "retracted", "review": {"status": "approved"}}
        )
    # Same for group_override.
    with pytest.raises(Exception):
        contracts.RelationshipLifecycleInput.model_validate(
            {"status": "retracted", "group_override": True}
        )
    # The model has no review / group_override fields at all.
    assert "review" not in contracts.RelationshipLifecycleInput.model_fields
    assert "group_override" not in contracts.RelationshipLifecycleInput.model_fields


def test_relationship_lifecycle_status_validated_against_relationship_vocab() -> None:
    """An entity-only status (`retired`) is rejected for a relationship lifecycle."""
    with pytest.raises(Exception):
        contracts.RelationshipLifecycleInput.model_validate({"status": "retired"})


# ---------------------------------------------------------------------------
# add_relationship path (MCP / HTTP): lifecycle is HONORED, not dropped
# ---------------------------------------------------------------------------
#
# Regression for the MAJOR finding: a lifecycle write via the non-batch
# add_relationship path (the one ``cruxible_add_relationship`` MCP + the HTTP
# add-relationship route use) used to validate, be accepted, then be silently
# discarded -- a no-op. These tests drive that exact path end to end and prove
# the lifecycle is applied while review/group_override stay untouched.


def _retract_fits_via_add_relationship(
    instance: CruxibleInstance,
    *,
    status: str = "retracted",
    reason: str | None = "superseded via add path",
) -> None:
    """Retract the edge through the non-batch add_relationship path.

    Goes through the runtime contract -> service mapping the MCP/HTTP add-
    relationship surfaces use (``add_relationships_with_provenance`` ->
    ``_relationship_input_to_service`` -> ``service_add_relationship_inputs``),
    NOT the batch direct-write path.
    """
    from cruxible_core.runtime import api
    from cruxible_core.runtime.instance_manager import get_manager

    manager = get_manager()
    manager.clear()
    instance_id = "inst-rel-lifecycle-add"
    manager.register(instance_id, instance)
    try:
        api.add_relationships_with_provenance(
            instance_id,
            [
                contracts.RelationshipInput(
                    **_FITS,
                    properties={"verified": True, "source": "catalog"},
                    lifecycle=contracts.RelationshipLifecycleInput(
                        status=status,  # type: ignore[arg-type]
                        reason=reason,
                    ),
                )
            ],
            provenance_source="mcp_add",
            provenance_source_ref="add_relationship",
        )
    finally:
        manager.clear()


def test_add_relationship_path_applies_lifecycle(
    populated_instance: CruxibleInstance,
) -> None:
    """The non-batch add_relationship path actually applies the lifecycle write."""
    before = _get_fits(populated_instance)
    assert before is not None
    assert before.metadata.assertion.lifecycle.status == "active"

    _retract_fits_via_add_relationship(populated_instance)

    after = _get_fits(populated_instance)
    assert after is not None
    # Lifecycle is HONORED (was a silent no-op before the fix).
    assert after.metadata.assertion.lifecycle.status == "retracted"
    assert after.metadata.assertion.lifecycle.reason == "superseded via add path"
    # Edge properties untouched.
    assert after.properties["verified"] is True


def test_add_relationship_lifecycle_write_preserves_review_and_override(
    populated_instance: CruxibleInstance,
) -> None:
    """The add-path lifecycle write leaves review / group_override untouched.

    Same review-safety property the batch path guarantees: seed an approved,
    group-overridden edge, then drive a lifecycle write through the add path and
    assert ONLY ``assertion.lifecycle`` changed.
    """
    graph = populated_instance.load_graph()
    rel = graph.get_relationship("Part", "BP-1001", "Vehicle", "V-2024-CIVIC-EX", "fits")
    assert rel is not None
    rel.metadata = rel.metadata.model_copy(
        update={
            "assertion": rel.metadata.assertion.model_copy(
                update={
                    "review": RelationshipReviewState(
                        status="approved",
                        source="group",
                        updated_by="group:seed",
                    ),
                    "group_override": True,
                }
            )
        }
    )
    graph.replace_relationship_state(
        "Part",
        "BP-1001",
        "Vehicle",
        "V-2024-CIVIC-EX",
        "fits",
        properties=rel.properties,
        metadata=rel.metadata,
    )
    populated_instance.save_graph(graph)

    review_before = _get_fits(populated_instance).metadata.assertion.review.model_dump(mode="json")
    assert review_before["status"] == "approved"

    _retract_fits_via_add_relationship(populated_instance)

    after = _get_fits(populated_instance)
    assert after is not None
    # Lifecycle changed via the typed channel...
    assert after.metadata.assertion.lifecycle.status == "retracted"
    # ...but review state and group_override are byte-identical (review-safe).
    assert after.metadata.assertion.review.model_dump(mode="json") == review_before
    assert after.metadata.assertion.group_override is True
