"""Tests for typed relationship provenance helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.provenance import (
    RelationshipProvenance,
    dump_provenance,
    load_provenance,
    make_provenance,
    provenance_group_id,
    stamp_provenance_modified,
)


def test_make_provenance_returns_model_and_dump_returns_dict() -> None:
    provenance = make_provenance("workflow_apply", "workflow:canonical-fitment")

    assert isinstance(provenance, RelationshipProvenance)
    dumped = dump_provenance(provenance)
    assert dumped["source"] == "workflow_apply"
    assert dumped["source_ref"] == "workflow:canonical-fitment"
    assert isinstance(dumped["created_at"], str)


def test_load_provenance_accepts_partial_dict() -> None:
    provenance = load_provenance({"source_ref": "group:GRP-test"})

    assert provenance is not None
    assert provenance.source_ref == "group:GRP-test"
    assert provenance_group_id(provenance) == "GRP-test"
    assert dump_provenance(provenance) == {"source_ref": "group:GRP-test"}


def test_extra_fields_survive_stamp_and_dump() -> None:
    provenance = load_provenance(
        {
            "source": "group_resolve",
            "source_ref": "group:GRP-test",
            "created_at": "2026-01-01T00:00:00+00:00",
            "calibration_id": "catalog_match_v3",
        }
    )

    assert provenance is not None
    stamped = stamp_provenance_modified(provenance, "feedback:approve")
    dumped = dump_provenance(stamped)

    assert dumped["source"] == "group_resolve"
    assert dumped["source_ref"] == "group:GRP-test"
    assert dumped["calibration_id"] == "catalog_match_v3"
    assert dumped["created_at"] == "2026-01-01T00:00:00+00:00"
    assert dumped["last_modified_by"] == "feedback:approve"
    assert isinstance(dumped["last_modified_at"], str)


def test_load_provenance_rejects_non_dict_values() -> None:
    assert load_provenance(None) is None
    assert load_provenance("not provenance") is None


def _actor_context() -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id="user-1",
        org_id="org-1",
        operation_id="op-1",
        timestamp=datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC),
    )


def test_make_provenance_stamps_write_correlation_fields() -> None:
    provenance = make_provenance(
        "group_resolve",
        "group:GRP-test",
        receipt_id="RCP-abc123",
        resolution_id="RES-xyz789",
        actor_context=_actor_context(),
    )

    dumped = dump_provenance(provenance)
    assert dumped["receipt_id"] == "RCP-abc123"
    assert dumped["resolution_id"] == "RES-xyz789"
    assert dumped["created_actor_context"]["actor_id"] == "user-1"
    assert dumped["created_actor_context"]["org_id"] == "org-1"
    assert "last_modified_actor_context" not in dumped


def test_correlation_fields_default_null_and_stay_out_of_dump() -> None:
    provenance = make_provenance("batch_direct_write", "batch_direct_write")

    assert provenance.receipt_id is None
    assert provenance.resolution_id is None
    assert provenance.created_actor_context is None
    dumped = dump_provenance(provenance)
    assert "receipt_id" not in dumped
    assert "resolution_id" not in dumped
    assert "created_actor_context" not in dumped


def test_stamp_modified_preserves_creation_correlation_fields() -> None:
    provenance = make_provenance(
        "batch_direct_write",
        "batch_direct_write",
        receipt_id="RCP-create",
    )

    stamped = stamp_provenance_modified(
        provenance,
        "agent",
        actor_context=_actor_context(),
    )

    assert stamped.receipt_id == "RCP-create"
    assert stamped.created_actor_context is None
    assert stamped.last_modified_actor_context is not None
    assert stamped.last_modified_actor_context.actor_id == "user-1"


def test_historical_provenance_loads_with_null_correlation_fields() -> None:
    provenance = load_provenance(
        {
            "source": "workflow_apply",
            "source_ref": "workflow:canonical-fitment:apply",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )

    assert provenance is not None
    assert provenance.receipt_id is None
    assert provenance.resolution_id is None
    assert provenance.created_actor_context is None
    assert provenance.last_modified_actor_context is None
