"""Served reads of pair Subjects: a pair's ends, and the pairs a Subject is an end of.

A pair Subject is keyed by its ends (Subject-valued key parts). Both reads go
through the pair index (``indexes.pair_units``), never a scan:

- a pair's ends are its ``pair_ends`` rows, one primary-key range: written once
  in the pair's creation change and never deleted, so a retired pair still
  names its ends;
- the pairs of an end are its ``pair_units`` rows (pairs holding a live fact),
  read as keyset seeks. The parts and fact predicates present under the end
  are each found with one seek (``> previous ORDER BY ... LIMIT 1``), then each
  page merges one bounded seek per predicate. A page costs its rows times the
  pair kind's fact predicates, whatever the end's other pairs or any other
  Subject's hold. A pair whose facts are all retired (a historical shell) has
  no units; ``incoming`` reaches it through its key Claims.

``pair_units`` proposes candidates only, never authority [F7]. A candidate is
listed only when it holds a visible positive fact: one of its live non-none
fact Claims passes the served eligibility gate (``claim_row_outcome``) under
``PAIR_LISTING_POLICY`` at the read's instant -- the policy a ``with FACT``
pair hop applies by default. A candidate whose facts are all hidden
(not yet effective, expired, stale, contradicted) is counted as ``hidden``,
never listed. A read examines a bounded number of candidates (``CARD_SCAN``
per card group, ``page_scan(limit)`` per page) and judges them in one batched
fact read; a cursor continues after the last candidate examined, so hidden
candidates never take page slots and never cost a later page anything.
"""

from __future__ import annotations

import heapq
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cruxible_client.contracts.query.definitions import QueryEvaluationPolicy
from cruxible_core.indexes.sql_batch import select_in

_SUBJECTS = "subjects/"

#: A pair's ends: its ``pair_ends`` primary-key range (literal parts have no end).
PAIR_ENDS_SQL = "SELECT part, end_path FROM pair_ends WHERE pair_path=?"
#: A kind's key parts in key order, whatever its lifecycle (a pair kind's are frozen).
KIND_KEYS_SQL = "SELECT key_predicates FROM subject_kinds WHERE identity=?"
#: The next key part naming this end, with one of its pairs: one seek.
_NEXT_PART_SQL = (
    "SELECT part, pair_path FROM pair_units WHERE end_path=? AND part>? ORDER BY part LIMIT 1"
)
#: The next fact predicate under one (end, part): one seek.
_NEXT_PREDICATE_SQL = (
    "SELECT predicate FROM pair_units WHERE end_path=? AND part=? AND predicate>? "
    "ORDER BY predicate LIMIT 1"
)
#: One predicate's pairs of the end after a pair: a bounded keyset seek.
_PAGE_SQL = (
    "SELECT pair_path FROM pair_units WHERE end_path=? AND part=? AND predicate=? "
    "AND pair_path>? ORDER BY pair_path LIMIT ?"
)


#: The visibility a pair listing applies: a query's default (``visible: current``).
PAIR_LISTING_POLICY = QueryEvaluationPolicy(
    visible_verdicts=("supported",),
    visible_currency=("current",),
    conflict_behavior="surface_conflicts",
)
#: Candidates a card examines per (pair kind, part) group.
CARD_SCAN = 10
#: The live positive fact Claims of candidate pairs: one index seek per pair.
_FACT_CLAIMS_SQL = (
    "SELECT path FROM claims INDEXED BY claims_by_subject_predicate WHERE subject_path=? "
    "AND predicate IN ({}) AND lifecycle='live' AND object_kind<>'none'"
)


def page_scan(limit: int) -> int:
    """Candidates one ``detail="pairs"`` page examines: its rows, twice, and a margin."""

    return 2 * limit + 8


def _kind_of(path: str) -> str:
    return path.removeprefix(_SUBJECTS).partition("/")[0]


def pair_ends(connection: sqlite3.Connection, pair_path: str) -> tuple[tuple[str, str], ...]:
    """A pair's Subject-valued key parts and the end each names, in key order.

    Empty for a Subject that is not a pair. Literal key parts are left out.
    """

    rows = {
        str(part): str(end)
        for part, end in connection.execute(PAIR_ENDS_SQL, (pair_path,))
        if end is not None
    }
    if not rows:
        return ()
    found = connection.execute(KIND_KEYS_SQL, (f"SubjectKind:{_kind_of(pair_path)}",)).fetchone()
    order = [] if found is None else [str(item) for item in json.loads(str(found[0]))]
    ranked = {part: index for index, part in enumerate(order)}
    return tuple(sorted(rows.items(), key=lambda item: (ranked.get(item[0], len(ranked)), item[0])))


@dataclass(frozen=True)
class EndPairGroup:
    """The pairs of one pair kind naming an end through one key part."""

    kind: str
    part: str
    #: The fact predicates some pair of the group holds live, in byte order.
    predicates: tuple[str, ...]


def end_pair_groups(connection: sqlite3.Connection, end_path: str) -> tuple[EndPairGroup, ...]:
    """Each (pair kind, key part) with a pair holding a live fact, by kind then part.

    One seek per part and per predicate under it. A key part belongs to one
    pair kind (a key ClaimType is about its kind only), so any of its pairs
    names the kind.
    """

    groups: list[EndPairGroup] = []
    part = ""
    while (row := connection.execute(_NEXT_PART_SQL, (end_path, part)).fetchone()) is not None:
        part = str(row[0])
        predicates: list[str] = []
        predicate = ""
        while (
            found := connection.execute(_NEXT_PREDICATE_SQL, (end_path, part, predicate)).fetchone()
        ) is not None:
            predicate = str(found[0])
            predicates.append(predicate)
        groups.append(
            EndPairGroup(kind=_kind_of(str(row[1])), part=part, predicates=tuple(predicates))
        )
    return tuple(
        sorted(groups, key=lambda group: (group.kind.encode("utf-8"), group.part.encode("utf-8")))
    )


def group_pairs(
    connection: sqlite3.Connection,
    end_path: str,
    group: EndPairGroup,
    *,
    after: str = "",
    limit: int,
) -> list[str]:
    """The group's first ``limit`` pairs after ``after``, in path order.

    One seek of at most ``limit`` rows per fact predicate, merged: a pair
    holding several facts is listed once.
    """

    streams = [
        [
            str(path)
            for (path,) in connection.execute(_PAGE_SQL, (end_path, group.part, p, after, limit))
        ]
        for p in group.predicates
    ]
    pairs: list[str] = []
    for path in heapq.merge(*streams):
        if pairs and pairs[-1] == path:
            continue
        pairs.append(path)
        if len(pairs) == limit:
            break
    return pairs


def fact_claim_paths(
    connection: sqlite3.Connection, candidates: Sequence[tuple[EndPairGroup, str]]
) -> tuple[str, ...]:
    """The live positive fact Claims of the candidates: one seek per candidate."""

    paths: set[str] = set()
    for group, pair in candidates:
        if group.predicates:
            paths.update(
                str(path)
                for (path,) in select_in(
                    connection,
                    _FACT_CLAIMS_SQL,
                    group.predicates,
                    prefix=[pair],
                )
            )
    return tuple(sorted(paths))


def visible_pairs(
    instance: Any,
    coordinate: Any,
    *,
    pairs: Sequence[str],
    claim_paths: Sequence[str],
    evaluation_time: datetime,
) -> frozenset[str]:
    """The candidate pairs holding a fact visible under ``PAIR_LISTING_POLICY``.

    One batched fact read of exactly ``claim_paths`` (the candidates' live
    positive facts), each row judged by the shared gate every query backend
    uses. Called outside any bound projection.
    """

    from cruxible_core.query.backends import claim_row_outcome
    from cruxible_core.service.discovery.query import build_accepted_query_facts

    if not claim_paths:
        return frozenset()
    facts = build_accepted_query_facts(
        instance,
        coordinate=coordinate,
        entry_subject_paths=tuple(sorted(set(pairs))),
        claim_paths=tuple(claim_paths),
    )
    subjects = {subject.path: subject for subject in facts.subjects}
    providers = {provider.identity.qualified: provider for provider in facts.providers}
    found: set[str] = set()
    for row in facts.claims:
        if row.subject_path in found:
            continue
        outcome = claim_row_outcome(
            row,
            subject=subjects.get(row.subject_path),
            providers=providers,
            policy=PAIR_LISTING_POLICY,
            evaluation_time=evaluation_time,
        )
        if outcome.visible is not None:
            found.add(row.subject_path)
    return frozenset(found)


class PairsCursorInvalid(ValueError):
    """The cursor names a key part this end has no pairs through at its coordinate."""


def page_candidates(
    connection: sqlite3.Connection,
    end_path: str,
    *,
    after: tuple[str, str] | None,
    count: int,
) -> list[tuple[EndPairGroup, str]]:
    """The first ``count`` candidates after ``after``, by kind, part and path.

    The cursor is pinned to one coordinate, so the groups it names are the
    ones this read finds; a part it does not find refuses.
    """

    groups: Sequence[EndPairGroup] = end_pair_groups(connection, end_path)
    start = 0
    if after is not None:
        parts = [group.part for group in groups]
        if after[0] not in parts:
            raise PairsCursorInvalid(after[0])
        start = parts.index(after[0])
    found: list[tuple[EndPairGroup, str]] = []
    for index in range(start, len(groups)):
        group = groups[index]
        since = after[1] if after is not None and index == start else ""
        found.extend(
            (group, pair)
            for pair in group_pairs(
                connection, end_path, group, after=since, limit=count - len(found)
            )
        )
        if len(found) == count:
            break
    return found


@dataclass(frozen=True)
class PairsPage:
    #: Each group on the page: its listed pairs and how many candidates it hid.
    groups: tuple[tuple[EndPairGroup, tuple[str, ...], int], ...]
    #: The last examined ``(part, pair path)`` when more candidates follow, else None.
    last: tuple[str, str] | None


def cut_page(
    candidates: Sequence[tuple[EndPairGroup, str]],
    visible: frozenset[str],
    *,
    limit: int,
    scan: int,
) -> PairsPage:
    """One page from the examined candidates: at most ``limit`` visible pairs.

    ``candidates`` holds at most ``scan + 1`` (the extra one only says more
    follow). The page consumes candidates in order until ``limit`` pairs are
    listed or ``scan`` are examined; hidden ones are counted per group.
    """

    examined = list(candidates[:scan])
    rows: list[tuple[EndPairGroup, list[str], list[int]]] = []
    listed = 0
    last: tuple[EndPairGroup, str] | None = None
    stopped = False
    for group, pair in examined:
        shown = pair in visible
        if shown and listed == limit:
            stopped = True
            break
        if not rows or rows[-1][0] != group:
            rows.append((group, [], [0]))
        if shown:
            rows[-1][1].append(pair)
            listed += 1
        else:
            rows[-1][2][0] += 1
        last = (group, pair)
    more = stopped or len(candidates) > scan
    return PairsPage(
        tuple((group, tuple(pairs), hidden[0]) for group, pairs, hidden in rows),
        (last[0].part, last[1]) if more and last is not None else None,
    )


__all__ = [
    "CARD_SCAN",
    "PAIR_LISTING_POLICY",
    "EndPairGroup",
    "PairsCursorInvalid",
    "PairsPage",
    "cut_page",
    "end_pair_groups",
    "fact_claim_paths",
    "group_pairs",
    "page_candidates",
    "page_scan",
    "pair_ends",
    "visible_pairs",
]
