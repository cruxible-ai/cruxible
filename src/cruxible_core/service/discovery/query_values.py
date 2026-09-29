"""Live Claim values and verdict flags for ``query`` rows.

Values are read from the accepted projection's Claim index, which already
carries each live Claim's literal or Subject object, so a page of rows costs one
indexed read rather than a fold over every Claim body. Flags reuse the verdict
machinery every other read uses (``claim_resolution_statuses`` over the full
slots the page touches); nothing here re-adjudicates a Claim.

- ``stale``: the Claim's verdict is ``stale`` or ``stale_evidence``.
- ``contradicted``: the verdict is ``contradicted``.
- ``contested``: the slot is unresolved between contenders, the verdict is
  ``unresolved``, or a one-cardinality slot holds more than one live value.
- ``unsure_hold``: a principal's latest examined attestation of the Claim's
  current version is ``unsure`` and has not lapsed (its ``valid_until``, else the
  ClaimType's ``unsure_hold_for``, else the engine default).
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claims import claim_path
from cruxible_client.contracts.compact_query import QueryFlag
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import PlaybillAcceptedCoordinate

FLAG_ORDER: tuple[QueryFlag, ...] = ("stale", "contested", "contradicted", "unsure_hold")
_CHUNK = 400
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class LiveValue:
    """One live Claim's value as a row cell shows it."""

    identity: str
    subject_path: str
    predicate: str
    value: object
    artifact_digest: str


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
    "c.literal_text, c.literal_boolean, c.literal_integer_text, c.artifact_digest"
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
) -> list[LiveValue]:
    """Every live Claim value for these Subjects and predicates (``None`` means all)."""

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
            "WHERE c.lifecycle='live'"
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
            batches.append((base + predicate_clause, predicate_values))
        else:
            for chunk in _chunks(sorted(set(subject_paths))):
                marks = ",".join("?" for _ in chunk)
                batches.append(
                    (
                        base + f" AND c.subject_path IN ({marks})" + predicate_clause,
                        (*chunk, *predicate_values),
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
        )
    )
    index.mark_loaded(missing_paths, missing_predicates)


def _micros(value: datetime) -> int:
    return (value - _EPOCH) // timedelta(microseconds=1)


def claim_flags(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    *,
    claims: Sequence[LiveValue],
    evaluation_time: datetime,
    hold_for: Mapping[str, timedelta],
    default_hold: timedelta,
) -> dict[str, set[QueryFlag]]:
    """Verdict flags per Claim identity, from the shared per-slot verdict derivation.

    ``claims`` must hold every live contender of each slot it touches, so each
    slot's resolution is its full resolution.
    """

    from cruxible_core.service.discovery.search import claim_resolution_statuses
    from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext

    flags: dict[str, set[QueryFlag]] = {item.identity: set() for item in claims}
    if not claims:
        return flags
    identities = tuple(sorted({item.identity for item in claims}))
    context = ClaimVerdictReadContext(instance, coordinate)
    context.prefetch(tuple(claim_path(identity.removeprefix("Claim:")) for identity in identities))
    parsed = tuple(context.claim(identity) for identity in identities)
    verdicts: dict[str, Any] = {}
    statuses = claim_resolution_statuses(
        instance,
        claims=parsed,
        at=PlaybillAcceptedCoordinate.from_internal(coordinate),
        evaluation_time=evaluation_time,
        verdicts_by_identity=verdicts,
        read_context=context,
    )
    for claim in parsed:
        qualified = claim.identity.qualified
        marks = flags.setdefault(qualified, set())
        verdict = getattr(verdicts.get(qualified), "verdict", None)
        if verdict in {"stale", "stale_evidence"}:
            marks.add("stale")
        if verdict == "contradicted":
            marks.add("contradicted")
        if verdict == "unresolved" or statuses.get(claim.identity.name) == "conflicted":
            marks.add("contested")
    for identity in unsure_holds(
        instance,
        coordinate,
        claims=claims,
        evaluation_time=evaluation_time,
        hold_for=hold_for,
        default_hold=default_hold,
    ):
        flags.setdefault(identity, set()).add("unsure_hold")
    return flags


def unsure_holds(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    *,
    claims: Sequence[LiveValue],
    evaluation_time: datetime,
    hold_for: Mapping[str, timedelta],
    default_hold: timedelta,
) -> set[str]:
    """Claim identities an ``unsure`` examined attestation still holds at this time."""

    if not claims:
        return set()
    identities = sorted({item.identity for item in claims})
    predicate_of = {item.identity: item.predicate for item in claims}
    digest_of = {item.identity: item.artifact_digest for item in claims}
    now = _micros(evaluation_time)
    latest: dict[tuple[str, str], tuple[int, str, int | None]] = {}
    with instance.bind_accepted_projection(coordinate) as projection:
        connection = projection.typed.connection
        for chunk in _chunks(identities):
            marks_sql = ",".join("?" for _ in chunk)
            for identity, digest, principal, stance, attested, valid_until in connection.execute(
                "SELECT claim_identity, claim_artifact_digest, principal_id, stance, "
                "attested_at_us, valid_until_us FROM attestations "
                f"WHERE basis='examined_existing' AND claim_identity IN ({marks_sql})",
                tuple(chunk),
            ):
                if digest_of.get(str(identity)) != digest or int(attested) > now:
                    continue
                key = (str(identity), str(principal))
                if key not in latest or latest[key][0] < int(attested):
                    latest[key] = (
                        int(attested),
                        str(stance),
                        None if valid_until is None else int(valid_until),
                    )
    held: set[str] = set()
    step = timedelta(microseconds=1)
    for (identity, _principal), (attested, stance, valid_until) in latest.items():
        if stance != "unsure":
            continue
        lapses = (
            valid_until
            if valid_until is not None
            else attested + hold_for.get(predicate_of.get(identity, ""), default_hold) // step
        )
        if now < lapses:
            held.add(identity)
    return held


def ordered_flags(flags: Iterable[QueryFlag]) -> list[QueryFlag]:
    present = set(flags)
    return [flag for flag in FLAG_ORDER if flag in present]


__all__ = [
    "FLAG_ORDER",
    "LiveValue",
    "ValueIndex",
    "claim_flags",
    "distinct",
    "ensure_values",
    "ordered_flags",
    "read_live_values",
    "subject_labels",
    "subjects_of_kind",
    "unsure_holds",
]
