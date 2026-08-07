"""Migration 0009: the additive column, the derived tables, and the proof column."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from cruxible_core.storage.sqlite import (
    _ALL_STORAGE_MIGRATIONS,
    PROCEDURE_GRAPH_MIGRATION,
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


def test_the_migration_id_is_in_the_known_set() -> None:
    """A migration absent from the set is silently skipped on stamped databases.

    ``_schema_is_current`` gates the whole upgrade on the stamped SET, so the
    id has to be a member or an existing instance never runs it -- and an
    unstamped one would take the write lock on every connect.
    """
    assert PROCEDURE_GRAPH_MIGRATION in _ALL_STORAGE_MIGRATIONS
    assert PROCEDURE_GRAPH_MIGRATION == "0009_procedure_graph"


def test_a_fresh_database_has_the_column_the_tables_and_the_stamp(tmp_path: Path) -> None:
    store = SQLiteStorageBackend(tmp_path / "state.db")
    conn = store.connect()
    store._initialize_connection(conn)
    conn.commit()
    try:
        assert "definition_format_version" in _columns(conn, "procedures")
        assert _columns(conn, "procedure_node_digests") >= {
            "procedure_id",
            "node_id",
            "local_digest",
            "subtree_digest",
            "structural_digest",
            "kind",
        }
        assert _columns(conn, "procedure_acceptance_node_pins") >= {
            "pin_payload_json",
            "pin_digest",
        }
        assert SQLiteStorageBackend.has_migration_on_connection(conn, PROCEDURE_GRAPH_MIGRATION)
    finally:
        conn.close()


def test_an_existing_database_missing_the_column_gets_it_added(tmp_path: Path) -> None:
    """`CREATE TABLE IF NOT EXISTS` is a no-op on an existing table.

    Without the ALTER an upgraded instance would be permanently without the
    column while reporting a current schema.
    """
    db_path = tmp_path / "state.db"
    store = SQLiteStorageBackend(db_path)
    _bootstrap(store)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("ALTER TABLE procedures DROP COLUMN definition_format_version")
        conn.execute(
            "DELETE FROM storage_migrations WHERE migration_id = ?",
            (PROCEDURE_GRAPH_MIGRATION,),
        )
        conn.commit()
    finally:
        conn.close()

    reopened = SQLiteStorageBackend(db_path)
    _bootstrap(reopened)
    conn = reopened.connect()
    try:
        assert "definition_format_version" in _columns(conn, "procedures")
        assert SQLiteStorageBackend.has_migration_on_connection(conn, PROCEDURE_GRAPH_MIGRATION)
    finally:
        conn.close()


def test_the_proof_column_stops_a_stamped_but_unaltered_database_reading_as_current(
    tmp_path: Path,
) -> None:
    """A torn write leaves the stamp without the change.

    The migration row alone would be satisfied by exactly that; the column
    alone would be satisfied by a fresh-schema database that never stamped. The
    steady-state check reads both.
    """
    db_path = tmp_path / "state.db"
    store = SQLiteStorageBackend(db_path)
    _bootstrap(store)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("ALTER TABLE procedures DROP COLUMN definition_format_version")
        conn.commit()
        # Stamp present, change absent.
        assert SQLiteStorageBackend.has_migration_on_connection(conn, PROCEDURE_GRAPH_MIGRATION)
        assert SQLiteStorageBackend._schema_is_current(conn) is False
    finally:
        conn.close()


def test_existing_rows_take_format_v1(tmp_path: Path) -> None:
    """The truth, not a default: every definition written before the column was v1."""
    db_path = tmp_path / "state.db"
    store = SQLiteStorageBackend(db_path)
    _bootstrap(store)
    conn = sqlite3.connect(db_path)
    try:
        default = next(
            row[4]
            for row in conn.execute("PRAGMA table_info(procedures)")
            if row[1] == "definition_format_version"
        )
        assert str(default) == "1"
    finally:
        conn.close()
