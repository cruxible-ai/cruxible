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
) -> None:
    """``{verb} INTO {into} VALUES (...),(...)``: every row, in order, a chunk per statement.

    ``into`` is the table and, when needed, its column list. All rows have the
    width of the first.
    """

    if not rows:
        return
    width = len(rows[0])
    marks = "(" + ",".join("?" * width) + ")"
    for part in _chunks(rows, max(1, MAX_PARAMETERS // width)):
        connection.execute(
            f"{verb} INTO {into} VALUES " + ",".join(marks for _ in part),
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


__all__ = ["MAX_PARAMETERS", "execute_in", "insert_rows", "select_in"]
