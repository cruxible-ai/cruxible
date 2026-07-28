"""Focused core tests for working-set capture fidelity and compatibility."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cruxible_core.working_set import (
    HEADER_LINE,
    append_records,
    normalize_edge_record,
    normalize_entity_record,
    read_records,
    write_records,
)


def _entity_record(
    properties: dict[str, object],
    *,
    revision: int,
    as_of: str,
) -> dict[str, Any]:
    return normalize_entity_record(
        {
            "entity_type": "WorkItem",
            "entity_id": "WI-1",
            "properties": properties,
            "metadata": {},
        },
        read_revision=revision,
        as_of=as_of,
        receipt_refs=[],
        source_cmd="test",
        config_digest="digest",
    )


def test_projected_scalars_survive_entity_normalization_with_deterministic_bound() -> None:
    projected = _entity_record(
        {
            "z_projected": 3,
            "dependency_basis": "blocked by API work",
            "title": "Ship fidelity fixes",
            "nested": {"not": "scalar"},
        },
        revision=1,
        as_of="2026-07-28T12:00:00+00:00",
    )
    assert projected["props"] == {
        "title": "Ship fidelity fixes",
        "dependency_basis": "blocked by API work",
        "z_projected": 3,
    }

    bounded = _entity_record(
        {"title": "Bounded", **{f"field_{index:02d}": index for index in range(70)}},
        revision=1,
        as_of="2026-07-28T12:00:00+00:00",
    )
    assert list(bounded["props"]) == [
        "title",
        *(f"field_{index:02d}" for index in range(63)),
    ]


def test_same_coordinate_supersede_merges_props_and_losing_record_is_dropped(
    tmp_path: Path,
) -> None:
    path = tmp_path / "records.jsonl"
    old = _entity_record(
        {"title": "Old", "old_only": "preserved", "shared": "old"},
        revision=2,
        as_of="2026-07-28T12:00:00+00:00",
    )
    new = _entity_record(
        {"title": "New", "new_only": "added", "shared": "new"},
        revision=2,
        as_of="2026-07-28T12:01:00+00:00",
    )
    write_records(path, [old])
    append_records(path, [new])
    [merged] = read_records(path)
    assert merged["props"] == {
        "new_only": "added",
        "old_only": "preserved",
        "shared": "new",
        "title": "New",
    }

    losing = _entity_record(
        {"losing_only": "must not merge", "shared": "older"},
        revision=1,
        as_of="2026-07-28T12:02:00+00:00",
    )
    append_records(path, [losing])
    assert read_records(path) == [merged]


def test_cross_revision_supersede_replaces_wholesale(tmp_path: Path) -> None:
    """A newer-revision winner must NOT inherit older-revision fields.

    The merged record is stamped with the winner's revision, so folding a
    revision-1 projection into a revision-2 record would present a stale
    value as fresh at revision 2.
    """
    path = tmp_path / "records.jsonl"
    projected = _entity_record(
        {"title": "Old", "dependency_basis": "read at rev 1"},
        revision=1,
        as_of="2026-07-28T12:00:00+00:00",
    )
    compact = _entity_record(
        {"title": "New"},
        revision=2,
        as_of="2026-07-28T12:01:00+00:00",
    )
    write_records(path, [projected])
    append_records(path, [compact])
    [record] = read_records(path)
    assert record["props"] == {"title": "New"}
    assert record["read_revision"] == 2

    # Re-arm an old-only field at the winner's exact coordinates, so the
    # digest-change assertion below can actually detect an illegal merge.
    projected_again = _entity_record(
        {"title": "New", "dependency_basis": "read at rev 2"},
        revision=2,
        as_of="2026-07-28T12:02:00+00:00",
    )
    append_records(path, [projected_again])
    [record] = read_records(path)
    assert record["props"]["dependency_basis"] == "read at rev 2"

    config_changed = normalize_entity_record(
        {
            "entity_type": "WorkItem",
            "entity_id": "WI-1",
            "properties": {"title": "Other config"},
            "metadata": {},
        },
        read_revision=2,
        as_of="2026-07-28T12:03:00+00:00",
        receipt_refs=[],
        source_cmd="test",
        config_digest="other-digest",
    )
    append_records(path, [config_changed])
    [record] = read_records(path)
    assert record["props"] == {"title": "Other config"}


def test_same_batch_duplicates_collapse_with_same_coordinate_merge(tmp_path: Path) -> None:
    path = tmp_path / "records.jsonl"
    first = _entity_record(
        {"title": "A", "only_a": "x"},
        revision=1,
        as_of="2026-07-28T12:00:00+00:00",
    )
    second = _entity_record(
        {"title": "B", "only_b": "y"},
        revision=1,
        as_of="2026-07-28T12:01:00+00:00",
    )
    append_records(path, [first, second])
    [record] = read_records(path)
    assert record["props"] == {"only_a": "x", "only_b": "y", "title": "B"}


def test_edge_normalization_preserves_corroboration() -> None:
    record = normalize_edge_record(
        {
            "relationship_type": "depends_on",
            "from_type": "WorkItem",
            "from_id": "WI-1",
            "to_type": "WorkItem",
            "to_id": "WI-2",
            "edge_key": 4,
            "claim_id": "claim-4",
            "properties": {"basis": "API dependency"},
            "metadata": {},
            "corroboration": {"supporting_claims": 2, "sources": ["plan", "review"]},
        },
        read_revision=3,
        as_of="2026-07-28T12:00:00+00:00",
        receipt_refs=[],
        source_cmd="test",
        config_digest="digest",
    )
    assert record["corroboration"] == {
        "supporting_claims": 2,
        "sources": ["plan", "review"],
    }


def test_old_format_records_still_parse(tmp_path: Path) -> None:
    path = tmp_path / "records.jsonl"
    legacy = {
        "kind": "edge",
        "relationship_type": "depends_on",
        "from_type": "WorkItem",
        "from_id": "WI-1",
        "to_type": "WorkItem",
        "to_id": "WI-2",
        "edge_key": 1,
        "props": {},
        "read_revision": 1,
        "as_of": "2026-01-01T00:00:00+00:00",
        "receipt_refs": [],
        "source_cmd": "legacy",
    }
    path.write_text(f"{HEADER_LINE}\n{json.dumps(legacy)}\n")
    assert read_records(path) == [legacy]
