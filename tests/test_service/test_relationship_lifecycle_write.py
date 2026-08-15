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

from types import SimpleNamespace

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import TerminalLifecycleWriteRefusedError
from cruxible_core.graph.assertion_state import (
    RelationshipLifecycleState,
    RelationshipReviewState,
)
from cruxible_core.graph.types import RelationshipInstance
from cruxible_core.query.enums import QueryVisibilityState
from cruxible_core.service import service_list
from cruxible_core.service.lifecycle_inputs import relationship_lifecycle_state
from cruxible_core.service.mutations import (
    service_add_relationship_inputs,
    service_batch_direct_write,
)
from cruxible_core.service.queries import service_get_relationship
from cruxible_core.service.types import (
    BatchDirectWriteInput,
    BatchRelationshipWriteInput,
    RelationshipWriteInput,
)
from tests.support.terminal_lifecycle import seed_relationship_lifecycle

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
    """Drive the FREE-write batch path with a lifecycle status.

    Terminal statuses are refused here (that is the point of the tests below);
    non-terminal ones still write.
    """
    service_batch_direct_write(
        instance,
        BatchDirectWriteInput(
            relationships=[
                BatchRelationshipWriteInput(
                    from_type="Part",
                    from_id="BP-1001",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-2024-CIVIC-EX",
                    properties={"verified": True, "source": "catalog"},
                    lifecycle=RelationshipLifecycleState(status=status, reason=reason),  # type: ignore[arg-type]
                )
            ]
        ),
    )


def _seed_retracted_fits(
    instance: CruxibleInstance,
    *,
    status: str = "retracted",
    reason: str | None = "superseded by newer fitment",
) -> None:
    """Seed a TERMINAL edge lifecycle through the trusted chokepoint capability.

    ``retracted``/``superseded`` are refused on every free-write path. The
    read-gating and shape-preservation properties below still need a retracted
    edge to exist, so they seed it the way the dedicated receipted verbs will:
    ``apply_relationship(..., trusted_lifecycle_transition=True)``.
    """
    seed_relationship_lifecycle(
        instance,
        **_FITS,
        status=status,
        reason=reason,
        properties={"verified": True, "source": "catalog"},
    )


def _get_fits(instance: CruxibleInstance) -> RelationshipInstance | None:
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
    """A NON-terminal lifecycle write through the free batch path sets only lifecycle."""
    before = _get_fits(populated_instance)
    assert before is not None
    assert before.metadata.assertion.lifecycle.status == "active"

    _retract_fits_via_batch(populated_instance, status="inactive", reason="paused for audit")

    after = _get_fits(populated_instance)
    assert after is not None
    # Lifecycle slice is updated and round-trips through storage.
    assert after.metadata.assertion.lifecycle.status == "inactive"
    assert after.metadata.assertion.lifecycle.reason == "paused for audit"
    # Edge properties are untouched.
    assert after.properties["verified"] is True
    assert after.properties["source"] == "catalog"


def test_trusted_lifecycle_transition_sets_only_lifecycle(
    populated_instance: CruxibleInstance,
) -> None:
    """The trusted capability writes a TERMINAL status with the same shape guarantee.

    The free-write path refuses ``retracted`` (asserted below); the chokepoint stays
    open to machinery that has earned the transition, and when it writes, it writes
    only the lifecycle slice.
    """
    _seed_retracted_fits(populated_instance)

    after = _get_fits(populated_instance)
    assert after is not None
    assert after.metadata.assertion.lifecycle.status == "retracted"
    assert after.metadata.assertion.lifecycle.reason == "superseded by newer fitment"
    assert after.properties["verified"] is True
    assert after.properties["source"] == "catalog"


def test_retracted_edge_is_gated_out_of_live_reads(
    populated_instance: CruxibleInstance,
) -> None:
    _seed_retracted_fits(populated_instance)

    def _edge_ids(state: QueryVisibilityState) -> set[tuple[str, str]]:
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

    before = _get_fits(populated_instance)
    assert before is not None
    review_before = before.metadata.assertion.review.model_dump(mode="json")
    override_before = before.metadata.assertion.group_override
    assert review_before["status"] == "approved"
    assert override_before is True

    # Now retract via the trusted lifecycle transition (the free path refuses it).
    _seed_retracted_fits(populated_instance)

    after = _get_fits(populated_instance)
    assert after is not None
    # Lifecycle changed...
    assert after.metadata.assertion.lifecycle.status == "retracted"
    # ...but review state and group_override are byte-identical (NOT self-approved
    # or flipped by the lifecycle write).
    assert after.metadata.assertion.review.model_dump(mode="json") == review_before
    assert after.metadata.assertion.group_override is True


def test_relationship_lifecycle_mapper_cannot_carry_review_fields() -> None:
    """The retained mapper copies only lifecycle state into the core model."""
    state = relationship_lifecycle_state(
        SimpleNamespace(
            status="active",
            reason="because",
            review={"status": "approved"},
            group_override=True,
        )
    )

    assert state is not None
    assert state.model_dump(mode="json") == {
        "status": "active",
        "reason": "because",
        "effective_from": None,
        "effective_until": None,
        "closed_at": None,
        "closed_by": None,
        "supersedes": None,
        "superseded_by": None,
    }
    assert "review" not in RelationshipLifecycleState.model_fields
    assert "group_override" not in RelationshipLifecycleState.model_fields


def test_relationship_lifecycle_status_validated_against_relationship_vocab() -> None:
    """An entity-only status (`retired`) is rejected for a relationship lifecycle."""
    with pytest.raises(Exception):
        relationship_lifecycle_state(SimpleNamespace(status="retired", reason=None))


# ---------------------------------------------------------------------------
# Terminal statuses are NOT free-writable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["retracted", "superseded"])
def test_terminal_relationship_lifecycle_is_refused_on_the_free_write_path(
    status: str,
) -> None:
    """Retracting/superseding a claim is a judgement, not a property edit."""
    with pytest.raises(
        TerminalLifecycleWriteRefusedError,
        match="cruxible relationship retract",
    ):
        relationship_lifecycle_state(
            SimpleNamespace(status=status, reason="because")  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("status", ["active", "inactive"])
def test_non_terminal_relationship_lifecycle_stays_writable(status: str) -> None:
    """Participation flips are reversible and remain a plain write."""
    state = relationship_lifecycle_state(
        SimpleNamespace(status=status, reason="because")  # type: ignore[arg-type]
    )
    assert state is not None
    assert state.status == status


# ---------------------------------------------------------------------------
# The refusal at the CHOKEPOINT, not just the contract mapper
# ---------------------------------------------------------------------------
#
# The mapper refusal above only covers payloads shaped as contract inputs. These
# tests drive the EXPORTED SERVICE FUNCTIONS with typed core models -- the channel
# the local CLI uses, which never passes through a mapper -- and prove the graph
# chokepoint refuses them too. Before this fix these same calls succeeded.


@pytest.mark.parametrize("status", ["retracted", "superseded"])
def test_batch_direct_write_service_refuses_terminal_lifecycle(
    populated_instance: CruxibleInstance,
    status: str,
) -> None:
    """The BATCH shape is refused at the chokepoint, and nothing is persisted."""
    with pytest.raises(
        TerminalLifecycleWriteRefusedError,
        match="cruxible relationship retract",
    ):
        _retract_fits_via_batch(populated_instance, status=status)

    after = _get_fits(populated_instance)
    assert after is not None
    assert after.metadata.assertion.lifecycle.status == "active"


def test_batch_direct_write_dry_run_refuses_terminal_lifecycle(
    populated_instance: CruxibleInstance,
) -> None:
    """A preview refuses identically — a dry run must not report the write as valid."""
    with pytest.raises(TerminalLifecycleWriteRefusedError):
        service_batch_direct_write(
            populated_instance,
            BatchDirectWriteInput(
                relationships=[
                    BatchRelationshipWriteInput(
                        from_type="Part",
                        from_id="BP-1001",
                        relationship_type="fits",
                        to_type="Vehicle",
                        to_id="V-2024-CIVIC-EX",
                        properties={"verified": True, "source": "catalog"},
                        lifecycle=RelationshipLifecycleState(status="retracted"),
                    )
                ]
            ),
            dry_run=True,
        )


@pytest.mark.parametrize("status", ["retracted", "superseded"])
def test_service_add_relationship_inputs_refuses_terminal_lifecycle(
    populated_instance: CruxibleInstance,
    status: str,
) -> None:
    """``service_add_relationship_inputs`` is refused at the chokepoint too."""
    with pytest.raises(TerminalLifecycleWriteRefusedError):
        service_add_relationship_inputs(
            populated_instance,
            [
                RelationshipWriteInput(
                    from_type="Part",
                    from_id="BP-1001",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-2024-CIVIC-EX",
                    properties={"verified": True, "source": "catalog"},
                    lifecycle=RelationshipLifecycleState(status=status),  # type: ignore[arg-type]
                )
            ],
            source="add_relationship",
            source_ref="add_relationship",
        )

    after = _get_fits(populated_instance)
    assert after is not None
    assert after.metadata.assertion.lifecycle.status == "active"


def test_terminal_lifecycle_refused_on_a_NEW_edge_too(
    populated_instance: CruxibleInstance,
) -> None:
    """The refusal covers creates, not just updates.

    ``apply_relationship``'s add branch builds a fresh assertion and overlays the
    supplied lifecycle onto it, so a create could otherwise be born retracted --
    live state that never existed, with no reviewer and no receipted judgement.
    """
    with pytest.raises(TerminalLifecycleWriteRefusedError):
        service_batch_direct_write(
            populated_instance,
            BatchDirectWriteInput(
                relationships=[
                    BatchRelationshipWriteInput(
                        # The one Part/Vehicle pair the fixture leaves unlinked.
                        from_type="Part",
                        from_id="BP-1002",
                        relationship_type="fits",
                        to_type="Vehicle",
                        to_id="V-2024-ACCORD-SPORT",
                        properties={"verified": True, "source": "catalog"},
                        lifecycle=RelationshipLifecycleState(status="retracted"),
                    )
                ]
            ),
        )


# ---------------------------------------------------------------------------
# Retained direct-service relationship lifecycle path
# ---------------------------------------------------------------------------


def _deactivate_fits_via_add_relationship(
    instance: CruxibleInstance,
    *,
    status: str = "inactive",
    reason: str | None = "paused via add path",
) -> None:
    service_add_relationship_inputs(
        instance,
        [
            RelationshipWriteInput(
                from_type="Part",
                from_id="BP-1001",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-2024-CIVIC-EX",
                properties={"verified": True, "source": "catalog"},
                lifecycle=RelationshipLifecycleState(
                    status=status,  # type: ignore[arg-type]
                    reason=reason,
                ),
            )
        ],
        source="direct_service_add",
        source_ref="test_relationship_lifecycle",
    )


def test_add_relationship_path_applies_lifecycle(
    populated_instance: CruxibleInstance,
) -> None:
    before = _get_fits(populated_instance)
    assert before is not None
    assert before.metadata.assertion.lifecycle.status == "active"

    _deactivate_fits_via_add_relationship(populated_instance)

    after = _get_fits(populated_instance)
    assert after is not None
    assert after.metadata.assertion.lifecycle.status == "inactive"
    assert after.metadata.assertion.lifecycle.reason == "paused via add path"
    assert after.properties["verified"] is True


def test_add_relationship_lifecycle_write_preserves_review_and_override(
    populated_instance: CruxibleInstance,
) -> None:
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

    before = _get_fits(populated_instance)
    assert before is not None
    review_before = before.metadata.assertion.review.model_dump(mode="json")
    assert review_before["status"] == "approved"

    _deactivate_fits_via_add_relationship(populated_instance)

    after = _get_fits(populated_instance)
    assert after is not None
    assert after.metadata.assertion.lifecycle.status == "inactive"
    assert after.metadata.assertion.review.model_dump(mode="json") == review_before
    assert after.metadata.assertion.group_override is True
