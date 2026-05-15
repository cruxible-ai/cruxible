"""Tests for typed relationship provenance helpers."""

from __future__ import annotations

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


def test_load_provenance_accepts_partial_legacy_dict() -> None:
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
