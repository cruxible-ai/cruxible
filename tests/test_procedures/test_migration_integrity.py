"""T7 sweep-integrity coverage for supervised procedure convergence."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.procedure.digest import (
    _compute_definition_digest_v1,
    compute_node_digests,
)
from cruxible_core.procedure.types import ProcedureRecord, ProcedureRun
from cruxible_core.service import (
    service_accept_procedure,
    service_migrate_procedures,
    service_propose_procedure,
    service_record_reading,
)
from cruxible_core.temporal import utc_now
from tests.test_procedures.conftest import actor, provider_definition

_HISTORICAL_TABLE_KEYS = {
    "receipts": ("receipt_id",),
    "procedure_runs": ("run_id",),
    "procedure_readings": ("reading_id",),
    "procedure_acceptance_node_pins": (
        "procedure_id",
        "node_id",
        "pin_kind",
        "pin_key",
    ),
}


def _raw_historical_rows(
    state_db: Path,
) -> dict[str, dict[tuple[object, ...], tuple[object, ...]]]:
    snapshots: dict[str, dict[tuple[object, ...], tuple[object, ...]]] = {}
    with sqlite3.connect(state_db) as conn:
        conn.row_factory = sqlite3.Row
        for table, key_columns in _HISTORICAL_TABLE_KEYS.items():
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            snapshots[table] = {
                tuple(row[column] for column in key_columns): tuple(row) for row in rows
            }
    return snapshots


def _accepted_v1(instance: InstanceProtocol) -> ProcedureRecord:
    proposed = service_propose_procedure(
        instance,
        provider_definition("t7_historical_integrity"),
        actor_context=actor("historical-author"),
    )
    return service_accept_procedure(
        instance,
        proposed.procedure.procedure_id,
        expected_version=proposed.procedure.version,
        actor_context=actor("historical-reviewer"),
    ).procedure


def test_t7_sweep_integrity_preserves_history_and_retires_without_deleting(
    procedure_instance: InstanceProtocol,
) -> None:
    predecessor = _accepted_v1(procedure_instance)
    historical_run = ProcedureRun(
        procedure_id=predecessor.procedure_id,
        definition_digest=predecessor.definition_digest,
    )
    with procedure_instance.write_transaction() as uow:
        uow.procedures.save_run(historical_run)
    historical_reading = service_record_reading(
        procedure_instance,
        predecessor.procedure_id,
        subject_grain="procedure_unit",
        grade="attestation",
        verdict="satisfied",
        observed_at=utc_now(),
        actor_context=actor("historical-reader"),
        run_id=historical_run.run_id,
        note="must remain attached to the frozen predecessor",
    )
    state_db = procedure_instance.get_instance_dir() / "state.db"
    before_rows = _raw_historical_rows(state_db)
    before_local_digests = {
        node_id: digest.local_digest
        for node_id, digest in compute_node_digests(predecessor.definition).items()
    }
    assert _compute_definition_digest_v1(predecessor.definition) == predecessor.definition_digest
    assert before_rows["procedure_runs"]
    assert before_rows["procedure_readings"]
    assert before_rows["procedure_acceptance_node_pins"]
    assert any(
        predecessor.definition_digest in str(row) for row in before_rows["receipts"].values()
    )

    result = service_migrate_procedures(
        procedure_instance,
        apply=True,
        proposer_actor=actor("migration-proposer"),
        reviewer_actor=actor("migration-reviewer"),
    )

    assert [(item.name, item.outcome) for item in result.items] == [
        ("t7_historical_integrity", "accepted")
    ]
    successor_id = result.items[0].successor_procedure_id
    assert successor_id is not None
    after_rows = _raw_historical_rows(state_db)
    for table, historical_rows in before_rows.items():
        for key, raw_row in historical_rows.items():
            assert after_rows[table][key] == raw_row, f"historical {table} row {key} changed"

    store = procedure_instance.get_procedure_store()
    readings = procedure_instance.get_procedure_reading_store()
    try:
        retired = store.get_procedure(predecessor.procedure_id)
        successor = store.get_procedure(successor_id)
        all_versions = store.list_procedures(name="t7_historical_integrity", limit=10)
        predecessor_readings = readings.list_readings(procedure_id=predecessor.procedure_id)
        successor_readings = readings.list_readings(procedure_id=successor_id)
    finally:
        store.close()
        readings.close()

    assert retired is not None
    assert retired.status == "retired"
    assert retired.definition_digest == predecessor.definition_digest
    assert _compute_definition_digest_v1(retired.definition) == retired.definition_digest
    assert successor is not None
    assert successor.status == "live"
    assert successor.definition.graph_format == 2
    assert successor.definition_digest != retired.definition_digest
    assert {row.procedure_id for row in all_versions} == {
        predecessor.procedure_id,
        successor_id,
    }
    assert [row.reading_id for row in predecessor_readings] == [historical_reading.reading_id]
    assert successor_readings == []
    assert {
        node_id: digest.local_digest
        for node_id, digest in compute_node_digests(successor.definition).items()
    } == before_local_digests
