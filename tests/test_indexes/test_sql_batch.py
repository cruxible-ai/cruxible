"""Set-based index writes: one statement per table per batch, the same rows as per-row SQL."""

from __future__ import annotations

import sqlite3

from cruxible_core.indexes.sql_batch import MAX_PARAMETERS, execute_in, insert_rows, select_in


def test_batched_writes_touch_exactly_the_rows_per_row_statements_would() -> None:
    rows = [(index, f"v{index}", None if index % 3 else b"\x00") for index in range(1000)]
    batched, single = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    for connection in (batched, single):
        connection.execute("CREATE TABLE t (k INTEGER PRIMARY KEY, v TEXT, b BLOB)")
    statements: list[str] = []
    batched.set_trace_callback(statements.append)
    insert_rows(batched, "t", rows)
    batched.set_trace_callback(None)
    single.executemany("INSERT INTO t VALUES (?,?,?)", rows)
    # 1000 rows of 3 values, MAX_PARAMETERS // 3 rows a statement: 4, not 1000.
    inserts = [sql for sql in statements if sql.startswith("INSERT")]
    assert MAX_PARAMETERS // 3 == 300 and len(inserts) == 4
    gone = [index for index in range(0, 1000, 7)]
    execute_in(batched, "DELETE FROM t WHERE k IN ({})", gone)
    single.executemany("DELETE FROM t WHERE k=?", [(index,) for index in gone])
    dump = "SELECT k, v, b, typeof(b) FROM t ORDER BY k"
    assert batched.execute(dump).fetchall() == single.execute(dump).fetchall()
    found = select_in(
        batched, "SELECT k FROM t WHERE v=? AND k IN ({})", list(range(2000)), prefix=("v8",)
    )
    assert found == [(8,)]
    assert select_in(batched, "SELECT k FROM t WHERE k IN ({})", []) == []
    insert_rows(batched, "t", [(1, "replaced", None)], verb="INSERT OR REPLACE")
    assert batched.execute("SELECT v FROM t WHERE k=1").fetchone() == ("replaced",)
