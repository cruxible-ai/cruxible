"""Procedure-reading grain invariants and append-only persistence."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from cruxible_core.procedure.reading_store import ProcedureReadingStore
from cruxible_core.procedure.types import ProcedureReading
from tests.test_procedures.conftest import actor

_OBSERVED_AT = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _unit_reading(**updates: object) -> ProcedureReading:
    payload: dict[str, object] = {
        "reading_id": "PRD-reading00001",
        "subject_grain": "procedure_unit",
        "procedure_id": "PRC-procedure001",
        "definition_digest": "sha256:definition",
        "grade": "attestation",
        "verdict": "satisfied",
        "observed_at": _OBSERVED_AT,
        "recorded_at": _OBSERVED_AT,
        "actor_context": actor("reader"),
    }
    payload.update(updates)
    return ProcedureReading.model_validate(payload)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"definition_digest": None}, "procedure_unit readings require definition_digest"),
        (
            {"node_id": "decide", "node_local_digest": "sha256:node"},
            "procedure_unit readings require all node and arm pointers null",
        ),
        (
            {
                "subject_grain": "node",
                "definition_digest": None,
                "node_id": "decide",
            },
            "node readings require node_id and node_local_digest",
        ),
        (
            {
                "subject_grain": "arm",
                "definition_digest": None,
                "node_id": "converged",
                "node_local_digest": "sha256:node",
                "from_node_id": "decide",
                "from_node_local_digest": "sha256:guard",
                "arm_subtree_digest": "sha256:arm",
            },
            "arm readings require from-node, node, arm_label, and arm_subtree coordinates",
        ),
    ],
)
def test_grain_invariants_are_enforced(updates: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _unit_reading(**updates)


def test_store_round_trips_reading_and_scoped_idempotency() -> None:
    store = ProcedureReadingStore()
    reading = _unit_reading(
        idempotency_key="reading-key",
        parameter_pins={"threshold": "sha256:revision"},
        value={"score": 0.91},
        situation_shape={"task_category": "triage"},
    )
    try:
        store.save_reading(reading)

        assert store.get_reading(reading.reading_id) == reading
        assert (
            store.find_idempotent_reading(
                idempotency_key="reading-key",
                procedure_id=reading.procedure_id,
                actor_org_id=reading.actor_context.org_id,
                actor_id=reading.actor_context.actor_id,
            )
            == reading
        )
    finally:
        store.close()


def test_reading_rows_are_insert_only() -> None:
    store = ProcedureReadingStore()
    reading = _unit_reading()
    try:
        store.save_reading(reading)
        with pytest.raises(sqlite3.IntegrityError):
            store.save_reading(reading.model_copy(update={"verdict": "contradicted"}))
        assert store.get_reading(reading.reading_id) == reading
    finally:
        store.close()


def test_arm_label_keeps_converging_arms_distinct() -> None:
    common = {
        "subject_grain": "arm",
        "procedure_id": "PRC-procedure001",
        "node_id": "converged",
        "node_local_digest": "sha256:node",
        "from_node_id": "decide",
        "from_node_local_digest": "sha256:guard",
        "arm_subtree_digest": "sha256:arm",
        "grade": "attestation",
        "verdict": "satisfied",
        "observed_at": _OBSERVED_AT,
        "recorded_at": _OBSERVED_AT,
        "actor_context": actor("reader"),
    }
    on_true = ProcedureReading.model_validate({**common, "arm_label": "on_true"})
    on_false = ProcedureReading.model_validate({**common, "arm_label": "on_false"})

    assert on_true.arm_label != on_false.arm_label
