"""Migration 0010: procedure readings and finalize-time fired-node facts."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from tests.test_cli.conftest import CAR_PARTS_YAML
from tests.test_procedures.conftest import actor

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.procedure.types import ProcedureReading
from cruxible_core.storage.sqlite import (
    _ALL_STORAGE_MIGRATIONS,
    _AUDIT_ONLY_TABLES,
    PROCEDURE_READINGS_MIGRATION,
    SQLiteStorageBackend,
)


def _bootstrap(store: SQLiteStorageBackend) -> None:
    conn = store.connect()
    try:
        store._initialize_connection(conn)
        conn.commit()
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_migration_is_next_free_and_classifications_match_read_consumers() -> None:
    assert PROCEDURE_READINGS_MIGRATION == "0010_procedure_readings"
    assert PROCEDURE_READINGS_MIGRATION in _ALL_STORAGE_MIGRATIONS
    assert "procedure_readings" not in _AUDIT_ONLY_TABLES
    assert "procedure_run_fired_nodes" in _AUDIT_ONLY_TABLES


def test_existing_database_gains_both_tables_and_stamp(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = SQLiteStorageBackend(db_path)
    _bootstrap(store)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP TABLE procedure_run_fired_nodes")
        conn.execute("DROP TABLE procedure_readings")
        conn.execute(
            "DELETE FROM storage_migrations WHERE migration_id = ?",
            (PROCEDURE_READINGS_MIGRATION,),
        )
        conn.commit()
    finally:
        conn.close()

    _bootstrap(SQLiteStorageBackend(db_path))
    conn = sqlite3.connect(db_path)
    try:
        assert _columns(conn, "procedure_readings") >= {
            "subject_grain",
            "arm_label",
            "parameter_pins_json",
            "actor_org_id",
        }
        assert _columns(conn, "procedure_run_fired_nodes") >= {
            "run_id",
            "sequence",
            "node_local_digest",
            "node_subtree_digest",
            "arm_label",
        }
        assert SQLiteStorageBackend.has_migration_on_connection(conn, PROCEDURE_READINGS_MIGRATION)
    finally:
        conn.close()


def test_standalone_reading_write_advances_read_revision(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(CAR_PARTS_YAML)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    before = instance.get_read_revision()
    observed_at = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    reading = ProcedureReading(
        subject_grain="procedure_unit",
        procedure_id="PRC-procedure001",
        definition_digest="sha256:definition",
        grade="attestation",
        verdict="satisfied",
        observed_at=observed_at,
        recorded_at=observed_at,
        actor_context=actor("reader"),
    )

    with instance.write_transaction() as uow:
        uow.procedure_readings.save_reading(reading)

    assert instance.get_read_revision() == before + 1
    store = instance.get_procedure_reading_store()
    try:
        assert store.get_reading(reading.reading_id) == reading
    finally:
        store.close()
