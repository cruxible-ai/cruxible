"""Per-ClaimType evidence counts over live Claims, maintained per change (W8).

``claim_type_evidence_counts`` holds, per predicate, the live Claims and how
many of them cite independent evidence: a ``citation_uses`` row with
``role='evidence'`` and ``origin='independent'`` (a Capture other than its
author's own words). A publication reads the state of only the Claims it
replaces or writes, before and after (batched keyed reads), and writes the
changed predicates' rows in one statement: no per-Claim statement, no trigger
program, never a recount. Cold construction counts once from the rows.

A read splits the rest by the ClaimType's ``evidence_requirement``: a live Claim
without captured evidence is ``own_word`` where the rule permits a Claim on its
own words (``self`` or ``none``), and ``unmet`` where it requires a Capture
(``captured``). Accepted citations were admitted at acceptance, so a cited
independent Capture is ``admitted``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping

from cruxible_core.indexes.sql_batch import execute_in, insert_rows, select_in

SCHEMA_SQL = """
CREATE TABLE claim_type_evidence_counts (
    predicate TEXT PRIMARY KEY,
    live INTEGER NOT NULL CHECK(live > 0),
    captured INTEGER NOT NULL CHECK(captured >= 0 AND captured <= live)
) STRICT, WITHOUT ROWID;
"""

#: A Claim's state for the counts: its predicate and whether it cites
#: independent evidence; absent when it is not a live Claim.
EvidenceStates = dict[str, tuple[str, bool]]


def evidence_states(connection: sqlite3.Connection, identities: Iterable[str]) -> EvidenceStates:
    """The live Claims among ``identities``, each with its predicate and captured flag."""

    wanted = sorted(set(identities))
    if not wanted:
        return {}
    live = {
        str(identity): str(predicate)
        for identity, predicate in select_in(
            connection,
            "SELECT identity,predicate FROM claims WHERE lifecycle='live' AND identity IN ({})",
            wanted,
        )
    }
    captured = {
        str(row[0])
        for row in select_in(
            connection,
            "SELECT DISTINCT owner_key FROM citation_uses WHERE owner_kind='Claim' "
            "AND role='evidence' AND origin='independent' AND owner_key IN ({})",
            sorted(live),
        )
    }
    return {identity: (predicate, identity in captured) for identity, predicate in live.items()}


def apply_evidence_changes(
    connection: sqlite3.Connection, before: Mapping[str, tuple[str, bool]], after: EvidenceStates
) -> None:
    """Move each changed predicate's counts by the Claims that left and entered it."""

    deltas: dict[str, list[int]] = {}
    for states, sign in ((before, -1), (after, 1)):
        for predicate, captured in states.values():
            delta = deltas.setdefault(predicate, [0, 0])
            delta[0] += sign
            delta[1] += sign * int(captured)
    moved = sorted(predicate for predicate, delta in deltas.items() if delta != [0, 0])
    if not moved:
        return
    held = {
        str(predicate): (int(live), int(captured))
        for predicate, live, captured in select_in(
            connection,
            "SELECT predicate,live,captured FROM claim_type_evidence_counts "
            "WHERE predicate IN ({})",
            moved,
        )
    }
    rows = []
    for predicate in moved:
        live, captured = held.get(predicate, (0, 0))
        rows.append((predicate, live + deltas[predicate][0], captured + deltas[predicate][1]))
    execute_in(
        connection,
        "DELETE FROM claim_type_evidence_counts WHERE predicate IN ({})",
        [predicate for predicate in moved if predicate in held],
    )
    insert_rows(connection, "claim_type_evidence_counts", [row for row in rows if row[1] > 0])


def rebuild_evidence_counts(connection: sqlite3.Connection) -> None:
    """Count every live Claim once: cold construction only."""

    connection.execute("DELETE FROM claim_type_evidence_counts")
    connection.execute(
        "INSERT INTO claim_type_evidence_counts "
        "SELECT c.predicate, count(*), sum(EXISTS (SELECT 1 FROM citation_uses u "
        "WHERE u.owner_kind='Claim' AND u.owner_key=c.identity AND u.role='evidence' "
        "AND u.origin='independent')) FROM claims c WHERE c.lifecycle='live' "
        "GROUP BY c.predicate"
    )


__all__ = [
    "SCHEMA_SQL",
    "apply_evidence_changes",
    "evidence_states",
    "rebuild_evidence_counts",
]
