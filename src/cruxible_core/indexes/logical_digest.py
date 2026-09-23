"""Combinable logical digest of a typed projection (``playbill-projection-logical-v4``).

Each logical table's rows form a multiset hashed additively: a row contributes
SHAKE-128 of its domain-separated encoding read as a 16384-bit integer, and the
table's sum is taken modulo 2**16384. A successor's sum is its parent's plus the
rows inserted minus the rows deleted, so an incremental build maintains the
digest in O(changed rows), and a full recomputation over every row yields the
same value. At this width Wagner's generalized-birthday attack on additive
hashing costs about 2**(2*sqrt(16384)) = 2**256 work.

The per-table sums live in ``logical_accumulators``, which the logical digest
itself excludes. A first bind recomputes them from the rows and refuses a piece
whose stored sums differ, so a successor never carries forward an unverified sum.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping

from cruxible_client.contracts.canonical import LogicalDigest, typed_digest
from cruxible_client.contracts.errors import ProjectionIntegrityError
from cruxible_core.indexes.projection import PROJECTION_STORAGE_SCHEMA_VERSION

LOGICAL_DIGEST_DOMAIN = "playbill-projection-logical-v4"
ROW_DOMAIN = b"playbill-projection-logical-v4-row\x00"
_BITS = 16384
_BYTES = _BITS // 8
_MASK = (1 << _BITS) - 1
# Binding metadata, the carried tree inventory and the accumulators are not
# logical rows.
NON_LOGICAL_TABLES = frozenset(
    {"generation_metadata", "assembler_metadata", "tree_inventory", "logical_accumulators"}
)

TableSums = dict[str, tuple[int, int]]


def _encode_value(value: object) -> object:
    if isinstance(value, bytes):
        return {"$bytes": value.hex()}
    raise TypeError(f"projection value is not encodable: {type(value).__name__}")


def row_term(table: str, row: tuple[object, ...] | list[object]) -> int:
    """One row's additive contribution; STRICT tables fix each value's storage class."""

    encoded = json.dumps(
        [table, list(row)], separators=(",", ":"), ensure_ascii=False, default=_encode_value
    ).encode("utf-8")
    return int.from_bytes(hashlib.shake_128(ROW_DOMAIN + encoded).digest(_BYTES), "little")


def logical_tables(connection: sqlite3.Connection) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """(name, sql, columns) of every logical table, in schema order."""

    tables = []
    for name, sql in connection.execute(
        "SELECT name,sql FROM main.sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        if name in NON_LOGICAL_TABLES:
            continue
        columns = tuple(
            str(row[1]) for row in connection.execute(f"PRAGMA main.table_info({name})")
        )
        tables.append((str(name), str(sql), columns))
    return tuple(tables)


def compute_table_sums(connection: sqlite3.Connection) -> TableSums:
    """Recompute every logical table's (row count, row sum) from its rows."""

    sums: TableSums = {}
    for name, _sql, _columns in logical_tables(connection):
        count = total = 0
        for row in connection.execute(f"SELECT * FROM main.{name}"):
            total += row_term(name, row)
            count += 1
        sums[name] = (count, total & _MASK)
    return sums


def stored_table_sums(connection: sqlite3.Connection) -> TableSums:
    return {
        str(name): (int(count), int.from_bytes(total, "little"))
        for name, count, total in connection.execute(
            "SELECT table_name,row_count,row_sum FROM main.logical_accumulators"
        )
    }


def store_table_sums(connection: sqlite3.Connection, sums: Mapping[str, tuple[int, int]]) -> None:
    connection.execute("DELETE FROM main.logical_accumulators")
    connection.executemany(
        "INSERT INTO main.logical_accumulators VALUES (?,?,?)",
        (
            (name, count, total.to_bytes(_BYTES, "little"))
            for name, (count, total) in sorted(sums.items())
        ),
    )


def logical_digest_from_sums(connection: sqlite3.Connection, sums: TableSums) -> LogicalDigest:
    """Bind the sums to the exact schema; every logical table must have a sum."""

    from cruxible_core.indexes.typed_sqlite import schema_objects

    tables = logical_tables(connection)
    if set(sums) != {name for name, _sql, _columns in tables}:
        raise ProjectionIntegrityError("projection logical sums do not cover its tables")
    return typed_digest(
        LogicalDigest,
        LOGICAL_DIGEST_DOMAIN,
        {
            "storage_schema_version": PROJECTION_STORAGE_SCHEMA_VERSION,
            "schema": [list(row) for row in schema_objects(connection)],
            "tables": [
                {
                    "name": name,
                    "sql": sql,
                    "rows": sums[name][0],
                    "sum": sums[name][1].to_bytes(_BYTES, "little").hex(),
                }
                for name, sql, _columns in tables
            ],
        },
    )


def verify_logical_digest(connection: sqlite3.Connection, expected: str) -> None:
    """Recompute from rows; the stored sums and the recorded digest must both agree."""

    sums = compute_table_sums(connection)
    if stored_table_sums(connection) != sums:
        raise ProjectionIntegrityError("projection logical digest sums differ from its rows")
    if logical_digest_from_sums(connection, sums).tagged != expected:
        raise ProjectionIntegrityError("projection canonical logical digest mismatch")


class RowDeltaRecorder:
    """Log every row a transaction inserts, deletes or updates, via TEMP triggers.

    TEMP objects live outside the database file, so the published schema is
    unchanged. Foreign-key actions fire triggers, and ``recursive_triggers``
    makes REPLACE-conflict deletions fire them too, so no row change escapes.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._tables = logical_tables(connection)
        connection.execute("PRAGMA recursive_triggers=ON")
        for index, (name, _sql, columns) in enumerate(self._tables):
            # Trigger bodies may not qualify table names; TEMP resolves first.
            log = f"_row_delta_{index}"
            old = ",".join(f'OLD."{column}"' for column in columns)
            new = ",".join(f'NEW."{column}"' for column in columns)
            # Untyped columns keep each value's storage class exactly.
            connection.execute(
                f"CREATE TEMP TABLE {log} (sign, {','.join(f'c{i}' for i in range(len(columns)))})"
            )
            connection.execute(
                f"CREATE TEMP TRIGGER _row_delta_{index}_i AFTER INSERT ON {name} "
                f"BEGIN INSERT INTO {log} VALUES (1,{new}); END"
            )
            connection.execute(
                f"CREATE TEMP TRIGGER _row_delta_{index}_d AFTER DELETE ON {name} "
                f"BEGIN INSERT INTO {log} VALUES (-1,{old}); END"
            )
            connection.execute(
                f"CREATE TEMP TRIGGER _row_delta_{index}_u AFTER UPDATE ON {name} "
                f"BEGIN INSERT INTO {log} VALUES (-1,{old}); "
                f"INSERT INTO {log} VALUES (1,{new}); END"
            )

    def apply(self, parent: TableSums) -> tuple[TableSums, frozenset[str]]:
        """The successor's sums, and the tables whose rows actually changed."""

        sums = dict(parent)
        changed: set[str] = set()
        for index, (name, _sql, _columns) in enumerate(self._tables):
            if name not in sums:
                raise ProjectionIntegrityError("parent projection has no logical sum for a table")
            count, total = sums[name]
            net: dict[tuple[object, ...], int] = {}
            for sign, *row in self._connection.execute(f"SELECT * FROM temp._row_delta_{index}"):
                key = tuple(row)
                net[key] = net.get(key, 0) + int(sign)
            for row, multiplicity in net.items():
                if multiplicity:
                    count += multiplicity
                    total += multiplicity * row_term(name, row)
                    changed.add(name)
            if count < 0:
                raise ProjectionIntegrityError("projection row delta removes absent rows")
            sums[name] = (count, total & _MASK)
        return sums, frozenset(changed)

    def close(self) -> None:
        for index in range(len(self._tables)):
            for suffix in ("i", "d", "u"):
                self._connection.execute(f"DROP TRIGGER IF EXISTS temp._row_delta_{index}_{suffix}")
            self._connection.execute(f"DROP TABLE IF EXISTS temp._row_delta_{index}")


__all__ = [
    "LOGICAL_DIGEST_DOMAIN",
    "NON_LOGICAL_TABLES",
    "RowDeltaRecorder",
    "compute_table_sums",
    "logical_digest_from_sums",
    "logical_tables",
    "row_term",
    "store_table_sums",
    "stored_table_sums",
    "verify_logical_digest",
]
