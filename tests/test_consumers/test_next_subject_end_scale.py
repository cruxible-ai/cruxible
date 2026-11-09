"""The subject-end rule's cost tracks the change, not an end's fan-out.

A hub end keyed by N pairs: one pair's visibility change while the hub is
still observed reaches one key and costs a constant number of reads, at 20
pairs as at 2,000. When the hub's last observation is retired, every pair's
entries genuinely change: the change reaches each pair's key and the
derivation reads them in pages, so its reads grow by pages, not per pair.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest

from cruxible_core.consumers.next import facts, rows

END = "subjects/end/hub.json"
RETIRED = "Claim:CLM-retired"
OBSERVED = "Claim:CLM-observed"


def _hub(size: int) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(facts._SCHEMA)
    columns = [
        str(row[1])
        for row in connection.execute("PRAGMA table_info(claim_facts)")
        if row[3] and row[1] not in {"claim_id", "subject_path", "lifecycle"}
    ]

    def add(claim_id: str, subject: str, obj: str | None, is_key: int, lifecycle: str) -> None:
        connection.execute(
            f"INSERT INTO claim_facts(claim_id,subject_path,object_path,is_key,lifecycle,visible,"
            f"{','.join(columns)}) VALUES (?,?,?,?,?,1,{','.join('?' for _ in columns)})",
            (claim_id, subject, obj, is_key, lifecycle, *("x" for _ in columns)),
        )

    add(RETIRED, END, None, 0, "retired")
    add(OBSERVED, END, None, 0, "live")
    for index in range(size):
        pair = f"subjects/pair/{index:05d}.json"
        add(f"Claim:CLM-key-{index:05d}", pair, END, 1, "live")
        add(f"Claim:CLM-judgment-{index:05d}", pair, None, 0, "live")
    return connection


def _fact(connection: sqlite3.Connection, claim_id: str) -> dict[str, Any]:
    return facts._stored_facts(connection, (claim_id,))[claim_id]


def _counted(connection: sqlite3.Connection, run: Any) -> tuple[Any, int]:
    selects: list[str] = []
    connection.set_trace_callback(
        lambda sql: selects.append(sql) if sql.lstrip().startswith("SELECT") else None
    )
    try:
        return run(), len(selects)
    finally:
        connection.set_trace_callback(None)


def _context(connection: sqlite3.Connection) -> Any:
    digest = "sha256:" + "a" * 64
    claim = SimpleNamespace(lifecycle=SimpleNamespace(predecessor_digest=digest))
    return SimpleNamespace(connection=connection, claim=lambda _identity: claim)


def _pair_change(size: int) -> tuple[set[tuple[str, str]], int, dict[str, Any], int]:
    connection = _hub(size)
    judgment = _fact(connection, "Claim:CLM-judgment-00000")
    keys, dirty = _counted(
        connection,
        lambda: facts._subject_end_keys(connection, [({**judgment, "visible": 0}, judgment)]),
    )
    found, derive = _counted(
        connection, lambda: rows.end_keys(_context(connection), [key for _f, key in keys])
    )
    return keys, dirty, found, derive


def test_one_pair_change_on_an_observed_hub_is_flat_in_its_pairs() -> None:
    small_keys, small_dirty, small_found, small_derive = _pair_change(20)
    large_keys, large_dirty, large_found, large_derive = _pair_change(2_000)
    pair = rows.end_key(END, "subjects/pair/00000.json")
    assert small_keys == large_keys == {(rows.END, pair)}
    assert small_found == large_found == {pair: None}
    assert (small_dirty, small_derive) == (large_dirty, large_derive)
    assert small_dirty + small_derive <= 6


@pytest.mark.parametrize("size", [20, 2_000])
def test_retiring_a_hubs_last_observation_reaches_each_pair_in_pages(size: int) -> None:
    connection = _hub(size)
    before = _fact(connection, OBSERVED)
    connection.execute("UPDATE claim_facts SET lifecycle='retired' WHERE claim_id=?", (OBSERVED,))
    after = _fact(connection, OBSERVED)
    keys, dirty = _counted(
        connection, lambda: facts._subject_end_keys(connection, [(before, after)])
    )
    assert len(keys) == size
    assert dirty <= 4
    found, derive = _counted(
        connection, lambda: rows.end_keys(_context(connection), [key for _f, key in keys])
    )
    contributed = [result for result in found.values() if result is not None]
    assert len(contributed) == size
    # Two retired observations of the hub, one entry each for each pair's judgment.
    assert {len(result.contributions) for result in contributed} == {2}
    pages = -(-size // rows.END_PAGE)
    assert derive <= 2 + 2 * pages
