"""The pair index: which live facts a pair Subject holds, found from either end (R12).

A pair Subject is a Subject of a kind keyed by Subject-valued key parts (its
ends). Its key Claims are links that stay live for as long as the pair exists,
so no Claims index can tell a pair holding a live fact from a historical shell.
Three derived tables in the typed projection can:

``pair_ends(pair_path, part, end_path)``
    One row per key part of every pair Subject, literal parts included
    (``end_path`` NULL), written once in the pair's creation change from its
    kind's identity rule and its endpoint Claims, and never deleted. A pair
    kind's identity rule and endpoints are frozen for life (R13, F1, F10), so
    this is exactly what a full build reads back, whatever later happens to the
    kind's lifecycle or the pair's ``subject_keys`` row [F9].
``pair_slot_live(pair_path, predicate, live_count)``
    One row per pair slot holding a lifecycle-live non-key Claim (any polarity,
    role or source), with the number of such Claims.
``pair_units(end_path, part, predicate, pair_path)``
    One row per Subject-valued part of each pair with a ``pair_slot_live`` row:
    "which pairs of this end hold a live ``predicate`` fact", one seek.

Two more say which ends a judgment can be left standing on with nothing
current under it (``next``'s subject-end rule, ``subject_end_entries``):

``subject_observations(subject_path, live, retired)``
    Per Subject, its lifecycle-live Claims outside its kind's key parts (its
    observations) and its retired Claims of any predicate; a Subject with
    neither has no row.
``pair_end_unobserved(end_path)``
    Each end of a pair holding a live fact (a ``pair_units`` end) with no live
    observation and at least one retired Claim: the only ends the rule can
    reach. Empty means the rule yields nothing, one probe.

The index proposes candidates only, never authority [F7]: every consumer
decides eligibility from the pair's actual Claims. It may over-propose, never
under-propose.

Upkeep follows the change [F8]: each touched Claim's ``(lifecycle, subject,
predicate)`` is read once before the batch overwrites it and once after, and
the difference of live counts per pair slot is applied. A slot's 0 -> 1 and
1 -> 0 transitions insert or delete its ``pair_units`` rows by full key, the
ends read from ``pair_ends``. Retired history in a slot is never read. The
same difference moves each touched Subject's observation counts, and each
touched Subject and each end of a touched pair is then re-judged from its
counts and one ``pair_units`` seek. Cost: O(touched Claims x parts). A
SubjectKind change recounts that kind's Subjects (its key parts decide what
an observation is). A full build (``before=None``) reads every pair Subject's
key and live Claims once, and every Claim's lifecycle once.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from typing import cast

from cruxible_client.contracts.errors import ProjectionIntegrityError
from cruxible_core.indexes.sql_batch import insert_rows, select_in

SCHEMA_STATEMENTS = (
    """CREATE TABLE pair_ends (
            pair_path TEXT NOT NULL, part TEXT NOT NULL, end_path TEXT,
            PRIMARY KEY(pair_path, part)
        ) STRICT""",
    """CREATE TABLE pair_slot_live (
            pair_path TEXT NOT NULL, predicate TEXT NOT NULL,
            live_count INTEGER NOT NULL CHECK(live_count>0),
            PRIMARY KEY(pair_path, predicate)
        ) STRICT""",
    """CREATE TABLE pair_units (
            end_path TEXT NOT NULL, part TEXT NOT NULL, predicate TEXT NOT NULL,
            pair_path TEXT NOT NULL,
            PRIMARY KEY(end_path, part, predicate, pair_path)
        ) STRICT""",
    """CREATE TABLE subject_observations (
            subject_path TEXT PRIMARY KEY NOT NULL,
            live INTEGER NOT NULL CHECK(live>=0), retired INTEGER NOT NULL CHECK(retired>=0),
            CHECK(live>0 OR retired>0)
        ) STRICT""",
    """CREATE TABLE pair_end_unobserved (end_path TEXT PRIMARY KEY NOT NULL) STRICT""",
)

#: ``(lifecycle, subject_path, predicate)`` of one Claim row.
ClaimState = tuple[str, str, str]

_SUBJECTS = "subjects/"
# An endpoint Claim, whatever its lifecycle: written once, never revised (F10).
# ``+lifecycle`` is absent on purpose; the seek is ``claims_by_subject_predicate``.
_ENDPOINT_SQL = (
    "SELECT object_path FROM main.claims WHERE subject_path=? AND predicate=? "
    "AND subject_selector_scheme='artifact-v1' AND qualifier IS NULL AND object_kind='subject' "
    "ORDER BY identity LIMIT 1"
)


def _kind_of(path: str) -> str:
    return path.removeprefix(_SUBJECTS).partition("/")[0]


class _PairKinds:
    """Each kind's key parts and which of them are Subject-valued, read once per kind."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._known: dict[str, tuple[tuple[str, bool], ...] | None] = {}

    def parts(self, kind: str) -> tuple[tuple[str, bool], ...] | None:
        """``(part, Subject-valued)`` in key order for a pair kind, else None."""

        if kind not in self._known:
            row = self._connection.execute(
                "SELECT key_predicates FROM main.subject_kinds WHERE identity=? AND rule='keys'",
                (f"SubjectKind:{kind}",),
            ).fetchone()
            parts: tuple[tuple[str, bool], ...] = ()
            if row is not None:
                predicates = tuple(str(item) for item in json.loads(str(row[0])))
                kinds = dict(
                    (str(predicate), str(object_kind))
                    for predicate, object_kind in select_in(
                        self._connection,
                        "SELECT predicate,object_kind FROM main.claim_types WHERE identity IN ({})",
                        [f"ClaimType:{predicate}" for predicate in predicates],
                    )
                )
                parts = tuple(
                    (predicate, kinds.get(predicate) == "subject") for predicate in predicates
                )
            self._known[kind] = parts if any(subject for _part, subject in parts) else None
        return self._known[kind]


def _end_rows(
    connection: sqlite3.Connection, pair: str, parts: tuple[tuple[str, bool], ...]
) -> list[tuple[str, str, str | None]]:
    rows: list[tuple[str, str, str | None]] = []
    for part, subject_valued in parts:
        end: str | None = None
        if subject_valued:
            found = connection.execute(_ENDPOINT_SQL, (pair, part)).fetchone()
            end = None if found is None else str(found[0])
        rows.append((pair, part, end))
    return rows


def refresh_pair_index(
    connection: sqlite3.Connection,
    *,
    before: Mapping[str, ClaimState] | None = None,
    after: Mapping[str, ClaimState] | None = None,
    created: Iterable[str] = (),
    changed_kinds: Iterable[str] = (),
) -> None:
    """Bring the pair index and its unobserved-end tables up to date.

    ``before=None`` rebuilds them from the ``subjects``, ``claims``,
    ``subject_kinds`` and ``claim_types`` rows. Otherwise ``before`` and
    ``after`` hold each touched Claim's ``(lifecycle, subject, predicate)`` as
    the parent held it and as the batch left it (absent: no row), and
    ``created`` the Subjects the batch created; their ``pair_ends`` rows are
    written first, so a pair and its facts can be accepted together.
    ``changed_kinds`` names the SubjectKinds the batch wrote: their Subjects'
    observations are recounted under the kinds' current key parts.

    A Claim counts toward a pair slot only when its Subject's kind is a pair
    kind (read once per kind; a kind's pair parts are frozen for life, so no
    other Subject has ``pair_ends`` rows) and then only off its key parts.
    """

    kinds = _PairKinds(connection)
    if before is None:
        _full_build(connection, kinds)
        _full_observations(connection)
        return
    end_rows: list[tuple[str, str, str | None]] = []
    for pair in sorted(set(created)):
        parts = kinds.parts(_kind_of(pair))
        if parts is not None:
            end_rows.extend(_end_rows(connection, pair, parts))
    insert_rows(connection, "main.pair_ends", end_rows, suffix=" ON CONFLICT DO NOTHING")
    ends: dict[str, dict[str, str | None]] = {}

    def ends_of(subject: str) -> dict[str, str | None]:
        if subject not in ends:
            ends[subject] = (
                {
                    str(part): None if end is None else str(end)
                    for part, end in connection.execute(
                        "SELECT part,end_path FROM main.pair_ends WHERE pair_path=?", (subject,)
                    )
                }
                if subject.startswith(_SUBJECTS) and kinds.parts(_kind_of(subject)) is not None
                else {}
            )
        return ends[subject]

    delta: dict[tuple[str, str], int] = {}
    for states, sign in ((before, -1), (after or {}, 1)):
        for lifecycle, subject, predicate in states.values():
            if lifecycle != "live":
                continue
            pair_ends = ends_of(subject)
            if not pair_ends or predicate in pair_ends:
                continue
            delta[(subject, predicate)] = delta.get((subject, predicate), 0) + sign
    for (pair, predicate), change in sorted(delta.items()):
        if change:
            _apply(connection, pair, predicate, change, ends_of(pair))
    _refresh_observations(
        connection,
        before=before,
        after=after or {},
        changed_kinds=frozenset(changed_kinds),
        ends_of=ends_of,
    )


class _KindKeys:
    """Each kind's key parts (its identity rule's predicates), read once per kind.

    The current SubjectKind row whatever its lifecycle: never fewer key parts
    than ``next`` reads from the live kind, so an end is never counted observed
    by a Claim ``next`` treats as a key part.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._known: dict[str, frozenset[str]] = {}

    def of(self, kind: str) -> frozenset[str]:
        if kind not in self._known:
            row = self._connection.execute(
                "SELECT key_predicates FROM main.subject_kinds WHERE identity=? AND rule='keys'",
                (f"SubjectKind:{kind}",),
            ).fetchone()
            self._known[kind] = frozenset(
                () if row is None else (str(item) for item in json.loads(str(row[0])))
            )
        return self._known[kind]


def _observation(
    keys: _KindKeys, lifecycle: str, subject: str, predicate: str
) -> tuple[int, int] | None:
    """What one Claim adds to its Subject's ``(live, retired)`` counts, if anything."""

    if not subject.startswith(_SUBJECTS):
        return None
    if lifecycle == "retired":
        return (0, 1)
    if lifecycle == "live" and predicate not in keys.of(_kind_of(subject)):
        return (1, 0)
    return None


def _kind_range(kind: str) -> tuple[str, str]:
    low = f"{_SUBJECTS}{kind}/"
    return low, low[:-1] + chr(ord("/") + 1)


def _counted(
    keys: _KindKeys, rows: Iterable[tuple[object, object, object, object]]
) -> dict[str, list[int]]:
    """``(subject, predicate, lifecycle, count)`` groups folded into per-Subject counts."""

    counts: dict[str, list[int]] = {}
    for subject, predicate, lifecycle, amount in rows:
        found = _observation(keys, str(lifecycle), str(subject), str(predicate))
        if found is not None:
            counted = counts.setdefault(str(subject), [0, 0])
            counted[0] += found[0] * int(cast(int, amount))
            counted[1] += found[1] * int(cast(int, amount))
    return counts


_GROUPS_SQL = (
    "SELECT subject_path,predicate,lifecycle,count(*) FROM main.claims "
    "WHERE subject_path>=? AND subject_path<? GROUP BY subject_path,predicate,lifecycle"
)
_UNOBSERVED_SQL = (
    "SELECT 1 FROM main.subject_observations WHERE subject_path=? AND live=0 AND retired>0 "
    "AND EXISTS (SELECT 1 FROM main.pair_units WHERE end_path=?)"
)


def _full_observations(connection: sqlite3.Connection) -> None:
    for table in ("pair_end_unobserved", "subject_observations"):
        connection.execute(f"DELETE FROM main.{table}")
    counts = _counted(
        _KindKeys(connection), connection.execute(_GROUPS_SQL, (_SUBJECTS, "subjects0"))
    )
    insert_rows(
        connection,
        "main.subject_observations",
        [(subject, live, retired) for subject, (live, retired) in sorted(counts.items())],
    )
    connection.execute(
        "INSERT INTO main.pair_end_unobserved SELECT DISTINCT u.end_path FROM main.pair_units AS u "
        "JOIN main.subject_observations AS o ON o.subject_path=u.end_path "
        "WHERE o.live=0 AND o.retired>0"
    )


def _refresh_observations(
    connection: sqlite3.Connection,
    *,
    before: Mapping[str, ClaimState],
    after: Mapping[str, ClaimState],
    changed_kinds: frozenset[str],
    ends_of: Callable[[str], Mapping[str, str | None]],
) -> None:
    """Apply the touched Claims' count differences, then re-judge every end they can move.

    An end moves when a Claim about it changes (its counts) or when a pair it
    ends changes (its ``pair_units`` rows): each is re-judged with one keyed
    read. A recounted kind re-judges its ends from ``pair_units`` and the table.
    """

    keys = _KindKeys(connection)
    delta: dict[str, list[int]] = {}
    touched: set[str] = set()
    for states, sign in ((before, -1), (after, 1)):
        for lifecycle, subject, predicate in states.values():
            touched.add(subject)
            if subject.startswith(_SUBJECTS) and _kind_of(subject) in changed_kinds:
                continue  # recounted below
            found = _observation(keys, lifecycle, subject, predicate)
            if found is not None:
                counted = delta.setdefault(subject, [0, 0])
                counted[0] += sign * found[0]
                counted[1] += sign * found[1]
    for subject, (live, retired) in sorted(delta.items()):
        if live or retired:
            _add_counts(connection, subject, live, retired)
    judged = {subject for subject in touched if subject.startswith(_SUBJECTS)}
    for subject in sorted(touched):
        judged.update(end for end in ends_of(subject).values() if end)
    for kind in sorted(changed_kinds):
        low, high = _kind_range(kind)
        connection.execute(
            "DELETE FROM main.subject_observations WHERE subject_path>=? AND subject_path<?",
            (low, high),
        )
        insert_rows(
            connection,
            "main.subject_observations",
            [
                (subject, live, retired)
                for subject, (live, retired) in sorted(
                    _counted(keys, connection.execute(_GROUPS_SQL, (low, high))).items()
                )
            ],
        )
        for sql in (
            "SELECT DISTINCT end_path FROM main.pair_units WHERE end_path>=? AND end_path<?",
            "SELECT end_path FROM main.pair_end_unobserved WHERE end_path>=? AND end_path<?",
        ):
            judged.update(str(row[0]) for row in connection.execute(sql, (low, high)))
    for end in sorted(judged):
        if connection.execute(_UNOBSERVED_SQL, (end, end)).fetchone() is None:
            connection.execute("DELETE FROM main.pair_end_unobserved WHERE end_path=?", (end,))
        else:
            connection.execute(
                "INSERT INTO main.pair_end_unobserved VALUES (?) ON CONFLICT DO NOTHING", (end,)
            )


def _add_counts(connection: sqlite3.Connection, subject: str, live: int, retired: int) -> None:
    row = connection.execute(
        "SELECT live,retired FROM main.subject_observations WHERE subject_path=?", (subject,)
    ).fetchone()
    new_live = live + (0 if row is None else int(row[0]))
    new_retired = retired + (0 if row is None else int(row[1]))
    if new_live < 0 or new_retired < 0:
        raise ProjectionIntegrityError("subject observation count would go below zero")
    if new_live == 0 and new_retired == 0:
        connection.execute("DELETE FROM main.subject_observations WHERE subject_path=?", (subject,))
    elif row is None:
        connection.execute(
            "INSERT INTO main.subject_observations VALUES (?,?,?)", (subject, new_live, new_retired)
        )
    else:
        connection.execute(
            "UPDATE main.subject_observations SET live=?,retired=? WHERE subject_path=?",
            (new_live, new_retired, subject),
        )


def _apply(
    connection: sqlite3.Connection,
    pair: str,
    predicate: str,
    change: int,
    pair_ends: Mapping[str, str | None],
) -> None:
    row = connection.execute(
        "SELECT live_count FROM main.pair_slot_live WHERE pair_path=? AND predicate=?",
        (pair, predicate),
    ).fetchone()
    old = 0 if row is None else int(row[0])
    new = old + change
    if new < 0:
        raise ProjectionIntegrityError("pair slot live count would go below zero")
    units = [(end, part, predicate, pair) for part, end in sorted(pair_ends.items()) if end]
    if new == 0:
        connection.execute(
            "DELETE FROM main.pair_slot_live WHERE pair_path=? AND predicate=?", (pair, predicate)
        )
        connection.executemany(
            "DELETE FROM main.pair_units WHERE end_path=? AND part=? AND predicate=? "
            "AND pair_path=?",
            units,
        )
    elif old == 0:
        connection.execute("INSERT INTO main.pair_slot_live VALUES (?,?,?)", (pair, predicate, new))
        insert_rows(connection, "main.pair_units", units)
    else:
        connection.execute(
            "UPDATE main.pair_slot_live SET live_count=? WHERE pair_path=? AND predicate=?",
            (new, pair, predicate),
        )


def _full_build(connection: sqlite3.Connection, kinds: _PairKinds) -> None:
    for table in ("pair_units", "pair_slot_live", "pair_ends"):
        connection.execute(f"DELETE FROM main.{table}")
    end_rows: list[tuple[str, str, str | None]] = []
    slot_rows: list[tuple[str, str, int]] = []
    unit_rows: list[tuple[str, str, str, str]] = []
    for (identity,) in connection.execute(
        "SELECT identity FROM main.subject_kinds WHERE rule='keys' ORDER BY identity"
    ).fetchall():
        kind = str(identity).removeprefix("SubjectKind:")
        parts = kinds.parts(kind)
        if parts is None:
            continue
        low = f"{_SUBJECTS}{kind}/"
        high = low[:-1] + chr(ord("/") + 1)
        part_names = {part for part, _subject in parts}
        for (path,) in connection.execute(
            "SELECT path FROM main.subjects WHERE path>=? AND path<? ORDER BY path", (low, high)
        ).fetchall():
            pair = str(path)
            rows = _end_rows(connection, pair, parts)
            end_rows.extend(rows)
            for predicate, count in connection.execute(
                "SELECT predicate,count(*) FROM main.claims WHERE subject_path=? "
                "AND lifecycle='live' GROUP BY predicate",
                (pair,),
            ):
                if str(predicate) in part_names:
                    continue
                slot_rows.append((pair, str(predicate), int(count)))
                unit_rows.extend(
                    (end, part, str(predicate), pair) for _pair, part, end in rows if end
                )
    insert_rows(connection, "main.pair_ends", end_rows)
    insert_rows(connection, "main.pair_slot_live", slot_rows)
    insert_rows(connection, "main.pair_units", unit_rows)


__all__ = [
    "SCHEMA_STATEMENTS",
    "ClaimState",
    "refresh_pair_index",
]
