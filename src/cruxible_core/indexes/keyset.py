"""Keyset pages that seek on every key column, ties included.

A row-value bound such as ``(a, b, c) < (?, ?, ?)`` reads well, but SQLite
seeks an index on its first column only and filters the rest: when many rows
share the bound's ``a`` (a run's records share one instant, a placement
plateau shares one time), a page walks that whole tie before its first row.
``rows_before`` splits the bound into one seek per column instead -- equal on
the leading columns and below on the next, deepest first -- so each seek is
an index range on every column it names and a page costs its rows.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence


def rows_before(
    conn: sqlite3.Connection,
    *,
    select: str,
    source: str,
    where: str,
    args: Sequence[object],
    columns: Sequence[str],
    bound: Sequence[object] | None,
    limit: int,
) -> list[sqlite3.Row]:
    """At most ``limit`` rows strictly before ``bound``, newest ``columns`` first.

    ``where`` and ``args`` select the index prefix (equalities); ``columns``
    are the index's following columns, in order. Without a bound it is one
    range; with one, at most ``len(columns)`` ranges, each taken only while the
    page is short: the rows equal to the bound on all but the last column and
    below it there, then equal on one column fewer, and so on.
    """

    order = ",".join(f"{column} DESC" for column in columns)
    if bound is None:
        return list(
            conn.execute(
                f"SELECT {select} FROM {source} WHERE {where} ORDER BY {order} LIMIT ?",
                (*args, limit),
            ).fetchall()
        )
    rows: list[sqlite3.Row] = []
    for depth in range(len(columns) - 1, -1, -1):
        if len(rows) >= limit:
            break
        equal = "".join(f" AND {column}=?" for column in columns[:depth])
        rows.extend(
            conn.execute(
                f"SELECT {select} FROM {source} WHERE {where}{equal} AND {columns[depth]}<? "
                f"ORDER BY {order} LIMIT ?",
                (*args, *bound[: depth + 1], limit - len(rows)),
            ).fetchall()
        )
    return rows


__all__ = ["rows_before"]
