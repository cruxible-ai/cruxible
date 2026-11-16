"""A content observation derives again only the stored facts it changes (open question 2).

A Claim whose freshness reads observed content and whose stored verdict is
supported and current keeps its stored row: the observation only makes it
last longer, and the row is derived again when its expiry nears next's lead.
A Claim whose stored verdict lost its freshness is derived again. Counted at
two sizes: the rows an observation touches do not grow with current Claims.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

import pytest

from cruxible_core.consumers.next import facts

_KEY = ("sha256:" + "a" * 64, "sha256:" + "b" * 64)
_NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def _state(current: int, stale: int) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(facts._SCHEMA)  # noqa: SLF001
    columns = [row[1] for row in connection.execute("PRAGMA table_info(claim_facts)")]
    for index in range(current + stale):
        row = {name: None for name in columns}
        row.update(
            claim_id=f"Claim:c{index}",
            path=f"claims/c{index}.json",
            artifact_digest="sha256:" + "0" * 64,
            lifecycle="live",
            predicate="p",
            subject_path="subjects/k/s.json",
            res_slot="slot",
            conflict_slot="slot",
            input_digests="[]",
            pin_targets="[]",
            verdict="supported" if index < current else "stale_evidence",
            currency="current" if index < current else "stale",
        )
        connection.execute(
            f"INSERT INTO claim_facts({','.join(row)}) VALUES({','.join('?' for _ in row)})",
            tuple(row.values()),
        )
        connection.execute(
            "INSERT INTO fact_reads VALUES(?,?)",
            (f"observed:{_KEY[0]}|{_KEY[1]}", f"Claim:c{index}"),
        )
    return connection


@pytest.mark.parametrize("current", [10, 100])
def test_an_observation_touches_only_the_claims_it_changes(
    monkeypatch: pytest.MonkeyPatch, current: int
) -> None:
    from cruxible_core.evidence import claim_reobservations

    monkeypatch.setattr(
        claim_reobservations.ClaimReobservationStore,
        "observed_after",
        lambda self, sequence, through=None: (frozenset(), frozenset({_KEY})),
    )
    monkeypatch.setattr(claim_reobservations.ClaimReobservationStore, "__init__", _no_init)
    connection = _state(current, stale=2)
    common: dict[str, Any] = {
        "generation": 1,
        "generation_oid": "o",
        "compiler": "c",
        "door_root": "d",
        "landing_log": "l",
        "landing": 0,
        "reobs_journal": "j",
        "expire": 0,
        "sweep": 0,
    }
    cursors = facts._Cursors(heads="h", reobs=1, **common)  # noqa: SLF001
    heads = facts.ReadHeads(coordinate=None, reobs=2, **common)  # type: ignore[arg-type]
    touched = facts._touched(object(), connection, cursors, heads, now=_NOW)  # noqa: SLF001
    # The two stale Claims are derived again; no current one is, at either size.
    assert touched.claims == {f"Claim:c{current}", f"Claim:c{current + 1}"}


def _no_init(self: Any, instance: Any) -> None:
    self.instance = instance
