"""The combinable logical digest: incremental sums equal a full recomputation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cruxible_client.contracts.errors import ProjectionIntegrityError
from cruxible_core.indexes.logical_digest import (
    RowDeltaRecorder,
    compute_table_sums,
    logical_digest_from_sums,
    row_term,
    store_table_sums,
    verify_logical_digest,
)

_SCHEMA = """
CREATE TABLE owners (identity TEXT PRIMARY KEY, path TEXT NOT NULL) STRICT;
CREATE TABLE pins (
    source TEXT NOT NULL REFERENCES owners(identity) ON DELETE CASCADE,
    "order" INTEGER NOT NULL, payload BLOB, PRIMARY KEY (source, "order")
) STRICT;
CREATE TABLE logical_accumulators (
    table_name TEXT PRIMARY KEY, row_count INTEGER NOT NULL CHECK(row_count >= 0),
    row_sum BLOB NOT NULL
) STRICT, WITHOUT ROWID;
"""


def _database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(_SCHEMA)
    connection.executemany(
        "INSERT INTO owners VALUES (?,?)", [(f"o{i}", f"p/{i}") for i in range(20)]
    )
    connection.executemany(
        "INSERT INTO pins VALUES (?,?,?)",
        [(f"o{i}", j, bytes([i, j]) if j % 2 else None) for i in range(20) for j in range(3)],
    )
    store_table_sums(connection, compute_table_sums(connection))
    connection.commit()
    return connection


def test_incremental_sums_follow_inserts_deletes_updates_cascades_and_replace(
    tmp_path: Path,
) -> None:
    connection = _database(tmp_path / "piece.sqlite")
    parent = compute_table_sums(connection)
    recorder = RowDeltaRecorder(connection)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("DELETE FROM owners WHERE identity='o3'")  # cascades to pins
    connection.execute("UPDATE owners SET path='moved' WHERE identity='o4'")
    connection.execute("INSERT INTO owners VALUES ('o99','p/99')")
    connection.execute("INSERT INTO pins VALUES ('o99',0,x'00ff')")
    connection.execute("INSERT OR REPLACE INTO pins VALUES ('o5',1,x'01')")
    # A row deleted and re-inserted unchanged nets to nothing.
    connection.execute("DELETE FROM pins WHERE source='o6' AND \"order\"=0")
    connection.execute("INSERT INTO pins VALUES ('o6',0,NULL)")
    sums, changed = recorder.apply(parent)
    connection.commit()
    recorder.close()

    assert sums == compute_table_sums(connection)
    assert changed == {"owners", "pins"}
    assert not connection.execute("SELECT name FROM sqlite_temp_master").fetchall()


def test_order_and_table_membership_are_bound(tmp_path: Path) -> None:
    assert row_term("owners", ("a", "b")) != row_term("pins", ("a", "b"))
    assert row_term("owners", ("a", "b")) != row_term("owners", ("b", "a"))
    assert row_term("owners", (1,)) != row_term("owners", ("1",))
    assert row_term("owners", (b"\x01",)) != row_term("owners", ("01",))


def test_verification_refuses_stored_sums_that_differ_from_rows(tmp_path: Path) -> None:
    connection = _database(tmp_path / "piece.sqlite")
    sums = compute_table_sums(connection)
    expected = logical_digest_from_sums(connection, sums).tagged
    verify_logical_digest(connection, expected)

    count, total = sums["owners"]
    store_table_sums(connection, {**sums, "owners": (count, total + 1)})
    with pytest.raises(ProjectionIntegrityError, match="sums differ"):
        verify_logical_digest(connection, expected)
    store_table_sums(connection, sums)
    connection.execute("UPDATE owners SET path='tampered' WHERE identity='o1'")
    with pytest.raises(ProjectionIntegrityError, match="sums differ"):
        verify_logical_digest(connection, expected)
