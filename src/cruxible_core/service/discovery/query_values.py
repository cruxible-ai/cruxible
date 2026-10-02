"""Live Claim values for ``query`` rows.

Values are read from the accepted projection's Claim index, which already
carries each live Claim's literal or Subject object, so a page of rows costs one
indexed read rather than a fold over every Claim body. Row flags come from the
read verbs' shared derivation (``read_flags``), over the full slots a page
touches.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance

_CHUNK = 400


@dataclass(frozen=True)
class LiveValue:
    """One live Claim's value as a row cell shows it."""

    identity: str
    subject_path: str
    predicate: str
    value: object
    artifact_digest: str
    # An exact-content value: ``value`` is its content digest, and ``span`` the
    # byte range of that content the Claim states, when it states one.
    exact: bool = False
    span: tuple[int, int] | None = None
    role: str = "normative"
    qualifier: str | None = None
    lifecycle: str = "live"


def _chunks(values: Sequence[str]) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), _CHUNK):
        yield values[start : start + _CHUNK]


def subject_labels(connection: sqlite3.Connection, paths: Iterable[str]) -> dict[str, str]:
    """Map Subject paths to ``kind/id``."""

    wanted = sorted(set(paths))
    labels: dict[str, str] = {}
    for chunk in _chunks(wanted):
        marks = ",".join("?" for _ in chunk)
        for path, kind, subject_id in connection.execute(
            f"SELECT path, subject_kind, subject_id FROM subjects WHERE path IN ({marks})",
            tuple(chunk),
        ):
            labels[str(path)] = f"{kind}/{subject_id}"
    return labels


def subject_lifecycles(connection: sqlite3.Connection, paths: Iterable[str]) -> dict[str, str]:
    """Map Subject paths to their lifecycle, ``live`` or ``retired``."""

    wanted = sorted(set(paths))
    lifecycles: dict[str, str] = {}
    for chunk in _chunks(wanted):
        marks = ",".join("?" for _ in chunk)
        for path, lifecycle in connection.execute(
            f"SELECT path, lifecycle FROM subjects WHERE path IN ({marks})",
            tuple(chunk),
        ):
            lifecycles[str(path)] = "retired" if lifecycle == "retired" else "live"
    return lifecycles


def subjects_of_kind(connection: sqlite3.Connection, kind: str) -> dict[str, str]:
    return {
        str(path): f"{kind}/{subject_id}"
        for path, subject_id in connection.execute(
            "SELECT path, subject_id FROM subjects WHERE subject_kind=?", (kind,)
        )
    }


_VALUE_COLUMNS = (
    "c.identity, c.subject_path, c.predicate, c.object_kind, "
    "s.subject_kind, s.subject_id, c.object_content_digest, c.literal_type, "
    "c.literal_text, c.literal_boolean, c.literal_integer_text, c.artifact_digest, "
    "c.object_span_start_text, c.object_span_end_text, c.role, c.qualifier"
)


def _value_of(row: Sequence[Any], source: Any) -> object:
    (
        identity,
        _subject_path,
        _predicate,
        object_kind,
        object_kind_name,
        object_id,
        content_digest,
        literal_type,
        literal_text,
        literal_boolean,
        literal_integer,
        _digest,
        _span_start,
        _span_end,
        _role,
        _qualifier,
    ) = row
    if object_kind == "subject":
        return None if object_kind_name is None else f"{object_kind_name}/{object_id}"
    if object_kind == "exact_content":
        return content_digest
    if literal_type == "string":
        return literal_text
    if literal_type == "boolean":
        return bool(literal_boolean)
    if literal_type == "integer":
        return int(literal_integer)
    if literal_type == "null":
        return None
    claim = source(str(identity))
    return None if claim is None else getattr(claim.statement.object, "value", None)


def read_live_values(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    *,
    subject_paths: Sequence[str] | None,
    predicates: Sequence[str] | None,
    lifecycle: str = "live",
) -> list[LiveValue]:
    """Every live (or retired) Claim value for these Subjects and predicates (``None``: all)."""

    if subject_paths is not None and not subject_paths:
        return []
    if predicates is not None and not predicates:
        return []
    values: list[LiveValue] = []
    with instance.bind_accepted_projection(coordinate) as projection:
        connection = projection.typed.connection
        base = (
            f"SELECT {_VALUE_COLUMNS} FROM claims c "
            "LEFT JOIN subjects s ON s.path = c.object_path "
            "WHERE c.lifecycle=?"
        )
        predicate_clause = ""
        predicate_values: tuple[str, ...] = ()
        if predicates is not None:
            predicate_values = tuple(sorted(set(predicates)))
            predicate_clause = (
                " AND c.predicate IN (" + ",".join("?" for _ in predicate_values) + ")"
            )
        batches: list[tuple[str, tuple[str, ...]]] = []
        if subject_paths is None:
            batches.append((base + predicate_clause, (lifecycle, *predicate_values)))
        else:
            for chunk in _chunks(sorted(set(subject_paths))):
                marks = ",".join("?" for _ in chunk)
                batches.append(
                    (
                        base + f" AND c.subject_path IN ({marks})" + predicate_clause,
                        (lifecycle, *chunk, *predicate_values),
                    )
                )
        for sql, parameters in batches:
            for row in connection.execute(sql + " ORDER BY c.identity", parameters):
                values.append(
                    LiveValue(
                        identity=str(row[0]),
                        subject_path=str(row[1]),
                        predicate=str(row[2]),
                        value=_value_of(row, projection.typed.source),
                        artifact_digest=str(row[11]),
                        exact=row[3] == "exact_content",
                        span=(
                            None
                            if row[3] != "exact_content" or row[12] is None
                            else (int(row[12]), int(row[13]))
                        ),
                        role=str(row[14]),
                        qualifier=None if row[15] is None else str(row[15]),
                        lifecycle=lifecycle,
                    )
                )
    return values


def distinct(values: Iterable[object]) -> list[object]:
    """Distinct values in canonical byte order."""

    seen: dict[bytes, object] = {}
    for value in values:
        try:
            key = canonical_bytes(value)
        except Exception:  # pragma: no cover - a canonical index value always encodes
            key = repr(value).encode("utf-8")
        seen.setdefault(key, value)
    return [seen[key] for key in sorted(seen)]


class ValueIndex:
    """Live values grouped by Subject path and predicate."""

    def __init__(self, values: Iterable[LiveValue] = ()) -> None:
        self._by_slot: dict[tuple[str, str], list[LiveValue]] = defaultdict(list)
        self._by_subject: dict[str, list[LiveValue]] = defaultdict(list)
        self._loaded: set[tuple[str, str]] = set()
        self._seen: set[str] = set()
        self.add(values)

    def add(self, values: Iterable[LiveValue]) -> None:
        for item in values:
            if item.identity in self._seen:
                continue
            self._seen.add(item.identity)
            self._by_slot[(item.subject_path, item.predicate)].append(item)
            self._by_subject[item.subject_path].append(item)

    def mark_loaded(self, paths: Iterable[str], predicates: Iterable[str]) -> None:
        self._loaded.update((path, predicate) for path in paths for predicate in predicates)

    def missing(self, paths: Iterable[str], predicates: Iterable[str]) -> tuple[set[str], set[str]]:
        wanted = [(path, predicate) for path in paths for predicate in predicates]
        absent = [slot for slot in wanted if slot not in self._loaded]
        return {slot[0] for slot in absent}, {slot[1] for slot in absent}

    def slot(self, path: str, predicate: str) -> list[LiveValue]:
        return self._by_slot.get((path, predicate), [])

    def subject(self, path: str) -> list[LiveValue]:
        return self._by_subject.get(path, [])


def ensure_values(
    index: ValueIndex,
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    *,
    paths: Iterable[str],
    predicates: Iterable[str],
    lifecycle: str = "live",
) -> None:
    """Load any (Subject, predicate) slots the index has not read yet."""

    wanted_paths = sorted(set(paths))
    wanted_predicates = sorted(set(predicates))
    missing_paths, missing_predicates = index.missing(wanted_paths, wanted_predicates)
    if not missing_paths:
        return
    index.add(
        read_live_values(
            instance,
            coordinate,
            subject_paths=sorted(missing_paths),
            predicates=sorted(missing_predicates),
            lifecycle=lifecycle,
        )
    )
    index.mark_loaded(missing_paths, missing_predicates)


__all__ = [
    "LiveValue",
    "ValueIndex",
    "distinct",
    "ensure_values",
    "read_live_values",
    "subject_labels",
    "subject_lifecycles",
    "subjects_of_kind",
]
