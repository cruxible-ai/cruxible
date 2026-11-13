"""Set-based SQL for derived index writes: one statement per table per batch, not per row.

A publication's rows (members, owners, pins, citations, keys) were written one
statement per row, and its replaced paths deleted one statement per path per
owner table, so a change of N members ran some 70 statements per member. These
helpers bind a whole batch to one statement (chunked under SQLite's historic
999-parameter floor), so the statement count follows the tables a change
touches, while the rows each statement writes or deletes are exactly the rows
the per-row statements wrote or deleted, in the same order.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from typing import Any

#: Bound parameters per statement: under SQLITE_MAX_VARIABLE_NUMBER's historic 999.
MAX_PARAMETERS = 900


def _chunks(values: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def insert_rows(
    connection: sqlite3.Connection,
    into: str,
    rows: Sequence[Sequence[Any]],
    *,
    verb: str = "INSERT",
    suffix: str = "",
) -> None:
    """``{verb} INTO {into} VALUES (...),(...)``: every row, in order, a chunk per statement.

    ``into`` is the table and, when needed, its column list. All rows have the
    width of the first. ``suffix`` appends a caller-owned conflict clause.
    """

    if not rows:
        return
    width = len(rows[0])
    marks = "(" + ",".join("?" * width) + ")"
    for part in _chunks(rows, max(1, MAX_PARAMETERS // width)):
        connection.execute(
            f"{verb} INTO {into} VALUES " + ",".join(marks for _ in part) + suffix,
            [value for row in part for value in row],
        )


def select_in(
    connection: sqlite3.Connection,
    sql: str,
    values: Sequence[Any],
    *,
    prefix: Sequence[Any] = (),
) -> list[Any]:
    """Every row of ``sql`` with its one ``IN ({})`` list bound to ``values``, chunked.

    ``prefix`` binds the parameters before the list. Rows come chunk by chunk;
    a caller that needs an order sorts them.
    """

    found: list[Any] = []
    for part in _chunks(list(dict.fromkeys(values)), MAX_PARAMETERS - len(prefix)):
        found.extend(
            connection.execute(sql.format(",".join("?" * len(part))), (*prefix, *part)).fetchall()
        )
    return found


def execute_in(
    connection: sqlite3.Connection,
    sql: str,
    values: Sequence[Any],
    *,
    prefix: Sequence[Any] = (),
) -> None:
    """Run ``sql`` (a DELETE or UPDATE with one ``IN ({})`` list) for every value, chunked."""

    for part in _chunks(list(dict.fromkeys(values)), MAX_PARAMETERS - len(prefix)):
        connection.execute(sql.format(",".join("?" * len(part))), (*prefix, *part))


def execute_values(
    connection: sqlite3.Connection,
    sql: str,
    rows: Sequence[Sequence[Any]],
) -> list[Any]:
    """Bind rows to a ``VALUES {}`` relation, chunked; return any selected rows.

    Callers aggregate duplicate keys before updates so a key occurs in one chunk.
    """
    if not rows:
        return []
    width = len(rows[0])
    marks = "(" + ",".join("?" * width) + ")"
    found: list[Any] = []
    for part in _chunks(rows, max(1, MAX_PARAMETERS // width)):
        found.extend(
            connection.execute(
                sql.format(",".join(marks for _ in part)),
                [value for row in part for value in row],
            ).fetchall()
        )
    return found


def replace_owned_rows(
    connection: sqlite3.Connection,
    table: str,
    rows: Sequence[Sequence[Any]],
    *,
    owner_column: str,
    owners: Sequence[str],
    key_columns: tuple[str, ...],
    predicate: str = "1",
) -> list[tuple[Any, ...]]:
    """Replace only differing rows of the selected owners; return removed rows.

    The caller supplies complete rows, with their non-null primary key first.
    Reads are indexed by changed owners, never by the table's whole population.
    Delete all obsolete keys before inserting, including keys whose value moved,
    so secondary unique constraints see the same result as a full replacement.
    """
    desired = {tuple(row) for row in rows}
    if len(desired) != len(rows):
        raise sqlite3.IntegrityError("duplicate desired owner row")
    previous = [
        tuple(row)
        for row in select_in(
            connection,
            f"SELECT * FROM {table} WHERE {predicate} AND {owner_column} IN ({{}})",
            owners,
        )
    ]
    old = set(previous)
    removed = [row for row in previous if row not in desired]
    execute_values(
        connection,
        f"DELETE FROM {table} WHERE ({','.join(key_columns)}) IN (VALUES {{}})",
        [row[: len(key_columns)] for row in removed],
    )
    insert_rows(connection, table, [row for row in rows if tuple(row) not in old])
    return removed


__all__ = [
    "MAX_PARAMETERS",
    "execute_in",
    "execute_values",
    "insert_rows",
    "replace_owned_rows",
    "select_in",
]
