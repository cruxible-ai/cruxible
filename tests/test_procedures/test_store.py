"""ProcedureStore round-trip, immutability, optimistic-update, and run tests."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator

import pytest

from cruxible_core.graph.evidence import EvidenceRef
from cruxible_core.procedure.store import ProcedureStore
from cruxible_core.procedure.types import (
    ProcedureDefinition,
    ProcedureRecord,
    ProcedureRun,
    compute_procedure_definition_digest,
)
from tests.test_procedures.conftest import actor


def _definition(name: str = "stored_procedure") -> ProcedureDefinition:
    return ProcedureDefinition.model_validate(
        {
            "name": name,
            "steps": [
                {
                    "id": "eligible",
                    "assert_exists": {"ref": "$input.value"},
                }
            ],
            "returns": "eligible",
            "precondition": {"status": "ready"},
            "budget": {"wall_clock_s": 60, "max_provider_calls": 0},
        }
    )


def _record(
    procedure_id: str = "PRC-store000001",
    *,
    name: str = "stored_procedure",
    supersedes_procedure_id: str | None = None,
) -> ProcedureRecord:
    definition = _definition(name)
    return ProcedureRecord(
        procedure_id=procedure_id,
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition),
        supersedes_procedure_id=supersedes_procedure_id,
        proposed_actor_context=actor("proposer"),
    )


@pytest.fixture
def store() -> Generator[ProcedureStore, None, None]:
    procedure_store = ProcedureStore(":memory:")
    yield procedure_store
    procedure_store.close()


def test_tables_exist(store: ProcedureStore) -> None:
    rows = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {row["name"] for row in rows}
    assert {"procedures", "procedure_runs"}.issubset(names)


def test_procedure_definition_round_trip(store: ProcedureStore) -> None:
    record = _record()
    store.save_procedure(record)

    loaded = store.get_procedure(record.procedure_id)

    assert loaded == record
    assert loaded is not None
    assert loaded.definition.precondition == {"status": "ready"}
    assert loaded.proposed_actor_context == actor("proposer")


def test_definition_rows_are_insert_only(store: ProcedureStore) -> None:
    record = _record()
    store.save_procedure(record)

    with pytest.raises(sqlite3.IntegrityError):
        store.save_procedure(record.model_copy(update={"status": "live"}))

    assert store.get_procedure(record.procedure_id) == record


def test_distillation_evidence_accepts_artifact_receipt_and_trace_refs(
    store: ProcedureStore,
) -> None:
    evidence_refs = [
        EvidenceRef(
            source="source_artifact",
            source_record_id="SRC-1",
            artifact_id="ART-1",
        ),
        EvidenceRef(source="receipt", source_record_id="RCP-1"),
        EvidenceRef(source="trace", source_record_id="TRC-1"),
    ]
    record = _record().model_copy(update={"evidence_refs": evidence_refs})
    store.save_procedure(record)

    loaded = store.get_procedure(record.procedure_id)
    assert loaded is not None
    assert loaded.evidence_refs == evidence_refs


def test_store_refuses_definition_digest_mismatch(store: ProcedureStore) -> None:
    record = _record().model_copy(update={"definition_digest": "sha256:bad"})

    with pytest.raises(ValueError, match="definition digest mismatch"):
        store.save_procedure(record)


def test_optimistic_transition_checks_status_and_version(store: ProcedureStore) -> None:
    record = _record()
    store.save_procedure(record)

    assert not store.transition_procedure(
        record.procedure_id,
        from_status="pending",
        to_status="live",
        expected_version=2,
        resolved_actor_context=actor("reviewer"),
        promoted_config_digest="sha256:config",
        promoted_lock_digest="sha256:lock",
    )
    assert store.transition_procedure(
        record.procedure_id,
        from_status="pending",
        to_status="live",
        expected_version=1,
        resolved_actor_context=actor("reviewer"),
        resolved_at="2026-07-22T13:00:00Z",
        promoted_config_digest="sha256:config",
        promoted_lock_digest="sha256:lock",
    )

    loaded = store.get_procedure(record.procedure_id)
    assert loaded is not None
    assert loaded.status == "live"
    assert loaded.version == 2
    assert loaded.promoted_config_digest == "sha256:config"
    assert loaded.promoted_lock_digest == "sha256:lock"
    assert loaded.resolved_actor_context == actor("reviewer")


def test_store_rejects_invalid_lifecycle_edges_and_missing_transition_fields(
    store: ProcedureStore,
) -> None:
    record = _record()
    store.save_procedure(record)

    with pytest.raises(ValueError, match="invalid procedure transition"):
        store.transition_procedure(
            record.procedure_id,
            from_status="pending",
            to_status="retired",
            expected_version=1,
            reason="skip review",
            retired_actor_context=actor("reviewer"),
        )
    with pytest.raises(ValueError, match="promotion requires reviewer attribution"):
        store.transition_procedure(
            record.procedure_id,
            from_status="pending",
            to_status="live",
            expected_version=1,
        )


def test_supersede_link_and_filters_round_trip(store: ProcedureStore) -> None:
    first = _record("PRC-store000001")
    second = _record(
        "PRC-store000002",
        supersedes_procedure_id=first.procedure_id,
    )
    other = _record("PRC-store000003", name="other")
    store.save_procedure(first)
    store.save_procedure(second)
    store.save_procedure(other)

    assert store.count_procedures(name="stored_procedure") == 2
    stored = store.list_procedures(name="stored_procedure")
    assert {item.procedure_id for item in stored} == {
        first.procedure_id,
        second.procedure_id,
    }
    assert store.get_procedure(second.procedure_id).supersedes_procedure_id == first.procedure_id  # type: ignore[union-attr]


def test_started_and_finalized_run_round_trips(store: ProcedureStore) -> None:
    procedure = _record()
    store.save_procedure(procedure)
    started = ProcedureRun(
        run_id="PRN-started0001",
        procedure_id=procedure.procedure_id,
        definition_digest=procedure.definition_digest,
    )
    finalized = ProcedureRun.model_validate(
        {
            "run_id": "PRN-final00001",
            "procedure_id": procedure.procedure_id,
            "definition_digest": procedure.definition_digest,
            "status": "finalized",
            "verdict": "succeeded",
            "budget_spent": {"wall_clock_s": 1.25, "provider_calls": 0},
            "receipt_id": "RCP-run0000001",
            "finalized_at": "2026-07-22T13:00:00Z",
        }
    )
    store.save_run(started)
    store.save_run(finalized)

    assert store.get_run(started.run_id) == started
    assert store.get_run(finalized.run_id) == finalized
    assert store.count_runs(procedure_id=procedure.procedure_id) == 2
    assert store.list_runs(status="started") == [started]


def test_run_requires_existing_procedure(store: ProcedureStore) -> None:
    run = ProcedureRun(
        procedure_id="PRC-missing0001",
        definition_digest="sha256:missing",
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.save_run(run)
