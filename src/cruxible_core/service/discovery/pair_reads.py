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
"""

from __future__ import annotations

import heapq
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

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


@dataclass(frozen=True)
class PairsPage:
    groups: tuple[tuple[EndPairGroup, tuple[str, ...]], ...]
    #: The last listed ``(part, pair path)`` when more follow, else None.
    last: tuple[str, str] | None


class PairsCursorInvalid(ValueError):
    """The cursor names a key part this end has no pairs through at its coordinate."""


def pairs_page(
    connection: sqlite3.Connection,
    end_path: str,
    *,
    after: tuple[str, str] | None,
    limit: int,
) -> PairsPage:
    """One page of an end's pairs, by kind, part and path, after ``after``.

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
    listed: list[tuple[EndPairGroup, tuple[str, ...]]] = []
    remaining = limit
    for index in range(start, len(groups)):
        group = groups[index]
        since = after[1] if after is not None and index == start else ""
        found = group_pairs(connection, end_path, group, after=since, limit=remaining + 1)
        taken = tuple(found[:remaining])
        if taken:
            listed.append((group, taken))
            remaining -= len(taken)
        if remaining == 0:
            # Every later group holds at least one pair (its part was found).
            more = len(found) > len(taken) or index + 1 < len(groups)
            return PairsPage(tuple(listed), (group.part, taken[-1]) if more and taken else None)
    return PairsPage(tuple(listed), None)


__all__ = [
    "EndPairGroup",
    "PairsCursorInvalid",
    "PairsPage",
    "end_pair_groups",
    "group_pairs",
    "pair_ends",
    "pairs_page",
]
