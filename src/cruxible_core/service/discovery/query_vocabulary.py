"""Accepted vocabulary for the ``query`` read verb: kinds, predicates and their value types.

Every name a caller hands ``query`` -- a kind, a field, an enum member, an alias,
a query name -- is checked here against accepted state before anything is
evaluated. A wrong name never evaluates to an empty answer: it refuses with a
code, the nearest valid names, and a one-line repair.

This module also renders a ClaimType as the compact definition row the verb
answers with, naming the CaptureContracts its evidence rules admit by identity.
A v6 rule already names them by identity; an older rule names exact contract
versions by digest, and each digest is resolved to the identity it was accepted
under. A digest that resolves to nothing is shown as ``unresolved:<prefix>``,
never as a bare digest.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal

from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.compact_query import QueryFilterOperator
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.contract_names import CaptureContractNames
from cruxible_core.service.discovery.field_names import (
    reserved_meaning,
    resolve_field_in,
    short_field_name,
)
from cruxible_core.service.read_refusals import NEAREST_LIMIT, ReadRefusalError, nearest

ValueType = Literal[
    "string",
    "enum",
    "integer",
    "decimal",
    "boolean",
    "timestamp",
    "date",
    "subject",
    "exact_content",
    "json",
]
"""How a predicate's values compare. ``json`` is a literal with no scalar schema type."""

SUBJECT_ID_FIELD = "subject_id"
_ORDERED = frozenset({"string", "integer", "decimal", "timestamp", "date"})
_COMPARISONS = frozenset({"lt", "lte", "gt", "gte"})
_OPERATORS: dict[str, frozenset[str]] = {
    "string": frozenset({"eq", "ne", "lt", "lte", "gt", "gte", "in", "exists", "contains"}),
    "enum": frozenset({"eq", "ne", "in", "exists", "contains"}),
    "integer": frozenset({"eq", "ne", "lt", "lte", "gt", "gte", "in", "exists"}),
    "decimal": frozenset({"eq", "ne", "lt", "lte", "gt", "gte", "in", "exists"}),
    "boolean": frozenset({"eq", "ne", "in", "exists"}),
    "timestamp": frozenset({"eq", "ne", "lt", "lte", "gt", "gte", "in", "exists"}),
    "date": frozenset({"eq", "ne", "lt", "lte", "gt", "gte", "in", "exists"}),
    "subject": frozenset({"eq", "ne", "in", "exists", "contains"}),
    "exact_content": frozenset({"exists"}),
    "json": frozenset({"eq", "ne", "in", "exists"}),
}
_SUBJECT_ID_OPERATORS = frozenset({"eq", "ne", "lt", "lte", "gt", "gte", "in", "contains"})
ORDERABLE_TYPES = _ORDERED | {"boolean", "enum", "subject"}


def query_refusal(
    code: str,
    message: str,
    *,
    nearest: Iterable[str] = (),
    repair: str | None = None,
    field_path: str | None = None,
) -> ReadRefusalError:
    """A ``query`` input that names something accepted state does not, or cannot run.

    ``nearest`` are the valid names the caller most likely meant and ``repair``
    is the one-line fix; both ride the shared read refusal (``candidates`` and
    ``repair_line``), with the request path in ``field_path``.
    """

    return ReadRefusalError(
        code,
        message,
        http_status=400,
        candidates=nearest,
        repair_line=repair,
        field_path=field_path,
    )


def query_not_found(name: str, *, nearest: Iterable[str]) -> ReadRefusalError:
    """A named QueryDefinition that accepted state does not hold: a coded 404."""

    return ReadRefusalError(
        "cruxible.query.name_not_found",
        f"no accepted QueryDefinition is named {name!r}",
        http_status=404,
        candidates=nearest,
        repair_line="Pass one of the accepted query names (orient lists them)",
        field_path="name",
    )


@dataclass(frozen=True)
class PredicateInfo:
    """One live accepted predicate and how its values compare."""

    predicate: str
    claim_type: ClaimType
    claim_type_digest: str
    value_type: ValueType
    members: tuple[str, ...]
    cardinality: Literal["one", "many"]
    subject_kinds: tuple[str, ...]
    object_kinds: tuple[str, ...]

    @property
    def type_name(self) -> str:
        return self.value_type

    def operators(self) -> frozenset[str]:
        return _OPERATORS[self.value_type]


def value_type_of(claim_type: ClaimType) -> tuple[ValueType, tuple[str, ...]]:
    """Classify a ClaimType's object: its comparison type and any enum members."""

    if claim_type.object_kind == "subject":
        return "subject", ()
    if claim_type.object_kind == "exact_content":
        return "exact_content", ()
    schema = claim_type.literal_schema or {}
    members = schema.get("enum")
    if isinstance(members, list) and members and all(isinstance(item, str) for item in members):
        return "enum", tuple(str(item) for item in members)
    declared = schema.get("type")
    if declared == "string":
        fmt = schema.get("format")
        if fmt == "date-time":
            return "timestamp", ()
        if fmt == "date":
            return "date", ()
        return "string", ()
    if declared == "integer":
        return "integer", ()
    if declared == "number":
        return "decimal", ()
    if declared == "boolean":
        return "boolean", ()
    return "json", ()


@dataclass(frozen=True)
class QueryVocabulary:
    """The live ClaimTypes and Subject kinds at one accepted coordinate."""

    predicates: Mapping[str, PredicateInfo]
    kinds: tuple[str, ...]

    def predicates_of(self, kind: str) -> tuple[PredicateInfo, ...]:
        return tuple(
            info for _name, info in sorted(self.predicates.items()) if kind in info.subject_kinds
        )

    def incoming(self, kind: str) -> tuple[PredicateInfo, ...]:
        """The Subject-valued predicates whose values may name Subjects of ``kind``.

        These are the reverse follows from ``kind`` (``orient(kind=K)`` lists
        them as ``incoming``). A predicate admitting no object kinds admits no
        Subject objects, so it points at nothing.
        """

        return tuple(
            info
            for _name, info in sorted(self.predicates.items())
            if info.value_type == "subject" and kind in info.object_kinds
        )

    def resolve_incoming(self, kind: str, name: str) -> tuple[PredicateInfo, ...]:
        """Every incoming predicate of ``kind`` a reverse-follow field names.

        The shared naming rule applies to the SOURCE kind: a full predicate name
        wins, otherwise ``source + "." + name`` for each kind that carries an
        incoming predicate. One result means resolved; several are ambiguous.
        """

        applicable: dict[str, dict[str, PredicateInfo]] = {}
        for info in self.incoming(kind):
            for source in info.subject_kinds:
                applicable.setdefault(source, {})[info.predicate] = info
        return tuple(self.predicates[item] for item in resolve_field_in(name, applicable))

    def require_kind(self, kind: str, *, field_path: str = "kind") -> str:
        if kind in self.kinds:
            return kind
        raise query_refusal(
            "cruxible.query.unknown_kind",
            f"no accepted Subject kind is named {kind!r}",
            nearest=nearest(kind, (*self.kinds, "ClaimType", "Procedure", "Trigger", "Line")),
            repair="use one of the listed kinds; orient names every kind",
            field_path=field_path,
        )

    def _resolved(self, kinds: tuple[str, ...], name: str) -> dict[str, PredicateInfo]:
        """Every predicate one field names for any of these kinds (Addenda 2 and 3)."""

        applicable = {
            kind: {info.predicate: info for info in self.predicates_of(kind)} for kind in kinds
        }
        return {
            predicate: self.predicates[predicate]
            for predicate in resolve_field_in(name, applicable)
        }

    def field_name(self, info: PredicateInfo, kinds: tuple[str, ...]) -> str:
        """How a predicate of these kinds is shown, so the name resolves back to it.

        For one kind this is exactly ``short_field_name``; over several (a follow
        target) the short form is kept only when it names no other predicate.
        """

        for kind in kinds:
            if kind not in info.subject_kinds:
                continue
            short = short_field_name(info.predicate, kind, self.predicates)
            if tuple(self._resolved(kinds, short)) == (info.predicate,):
                return short
        return info.predicate

    def resolve_field(
        self,
        kinds: tuple[str, ...],
        name: str,
        *,
        field_path: str,
        owner: str | None = None,
    ) -> PredicateInfo | Literal["subject_id"]:
        """Resolve a field by the shared read-verb naming rule, or refuse with the nearest."""

        if name == SUBJECT_ID_FIELD and reserved_meaning(name, self.predicates):
            return "subject_id"
        found = self._resolved(kinds, name)
        if len(found) == 1:
            return next(iter(found.values()))
        label = owner or " / ".join(kinds)
        if found:
            raise query_refusal(
                "cruxible.query.ambiguous_field",
                f"{name!r} names {len(found)} predicates of {label}",
                nearest=tuple(sorted(found))[:NEAREST_LIMIT],
                repair="name the predicate in full",
                field_path=field_path,
            )
        admitted = {info.predicate: info for kind in kinds for info in self.predicates_of(kind)}
        shown = {self.field_name(info, kinds) for info in admitted.values()}
        suggestions = nearest(name, (*shown, *admitted, SUBJECT_ID_FIELD))
        if name in self.predicates:
            message = f"predicate {name!r} does not apply to {label}"
        else:
            message = f"{label} has no field {name!r}"
        raise query_refusal(
            "cruxible.query.unknown_field",
            message,
            nearest=suggestions,
            repair=f"use a predicate of {label} (orient kind={label} lists them) or subject_id",
            field_path=field_path,
        )


def load_query_vocabulary(
    instance: PlaybillInstance, coordinate: AcceptedProjectionCoordinate
) -> QueryVocabulary:
    """Read the live accepted ClaimTypes and every Subject kind in use."""

    predicates: dict[str, PredicateInfo] = {}
    kinds: set[str] = set()
    with instance.bind_accepted_projection(coordinate) as projection:
        for row in projection.typed.envelopes(kind="claim-type"):
            claim_type = projection.typed.source(row.identity)
            if not isinstance(claim_type, ClaimType) or claim_type.lifecycle.state != "live":
                continue
            value_type, members = value_type_of(claim_type)
            predicates[claim_type.predicate] = PredicateInfo(
                predicate=claim_type.predicate,
                claim_type=claim_type,
                claim_type_digest=row.artifact_digest,
                value_type=value_type,
                members=members,
                cardinality=claim_type.cardinality,
                subject_kinds=tuple(claim_type.allowed_subject_kinds),
                object_kinds=tuple(claim_type.allowed_object_subject_kinds),
            )
            kinds.update(claim_type.allowed_subject_kinds)
            kinds.update(claim_type.allowed_object_subject_kinds)
        kinds.update(
            str(row[0])
            for row in projection.typed.connection.execute(
                "SELECT DISTINCT subject_kind FROM subjects"
            )
        )
    return QueryVocabulary(predicates=predicates, kinds=tuple(sorted(kinds)))


# -- value checks -----------------------------------------------------------


def _refuse_value(
    info_label: str, value: object, expected: str, *, field_path: str, example: str
) -> ReadRefusalError:
    return query_refusal(
        "cruxible.query.value_type_mismatch",
        f"{value!r} is not a {expected} value for {info_label}",
        repair=f"pass a {expected}, for example {example}",
        field_path=field_path,
    )


def _parse_instant(value: str) -> datetime | None:
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            day = date.fromisoformat(value)
        except ValueError:
            return None
        return datetime(day.year, day.month, day.day, tzinfo=UTC)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def render_instant(value: datetime) -> str:
    utc = value.astimezone(UTC)
    spec = "microseconds" if utc.microsecond else "seconds"
    return utc.isoformat(timespec=spec).replace("+00:00", "Z")


_SUBJECT_REF_RE = re.compile(r"^(?:Subject:)?(?:subjects/)?([a-z][a-z0-9_.]*)/(.+?)(?:\.json)?$")


def subject_ref(value: str) -> str | None:
    """Normalize ``kind/id``, ``Subject:kind/id`` or a Subject path to ``kind/id``."""

    match = _SUBJECT_REF_RE.fullmatch(value)
    if match is None:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def check_value(
    info: PredicateInfo | Literal["subject_id"],
    operator: QueryFilterOperator,
    value: object,
    *,
    field_path: str,
    label: str,
) -> object:
    """Check one filter value against its predicate and return its typed form.

    Strings are coerced where the CLI can only spell a string (``3``, ``true``).
    The returned value is canonical: an ISO instant ends in ``Z``, a Subject
    reference is ``kind/id``, an enum member is checked against the list.
    """

    if operator == "exists":
        if not isinstance(value, bool):
            raise _refuse_value(label, value, "boolean", field_path=field_path, example="true")
        return value
    if operator == "contains":
        if not isinstance(value, str) or not value:
            raise _refuse_value(label, value, "text", field_path=field_path, example='"v1"')
        return value
    if isinstance(info, str):
        if not isinstance(value, str):
            raise _refuse_value(label, value, "string", field_path=field_path, example='"my-id"')
        return value
    value_type = info.value_type
    if value_type in {"string", "enum"}:
        if not isinstance(value, str):
            raise _refuse_value(label, value, "string", field_path=field_path, example='"text"')
        if value_type == "enum" and value not in info.members:
            raise query_refusal(
                "cruxible.query.unknown_member",
                f"{value!r} is not a member of {label}",
                nearest=tuple(info.members),
                repair=f"use one of: {', '.join(info.members)}",
                field_path=field_path,
            )
        return value
    if value_type == "integer":
        if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value):
            return int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            raise _refuse_value(label, value, "integer", field_path=field_path, example="3")
        return value
    if value_type == "decimal":
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise _refuse_value(label, value, "number", field_path=field_path, example='"2.5"')
        if isinstance(value, str) and not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value):
            raise _refuse_value(label, value, "number", field_path=field_path, example='"2.5"')
        return value
    if value_type == "boolean":
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower() == "true"
        if not isinstance(value, bool):
            raise _refuse_value(label, value, "boolean", field_path=field_path, example="true")
        return value
    if value_type == "timestamp":
        parsed = _parse_instant(value) if isinstance(value, str) else None
        if parsed is None:
            raise _refuse_value(
                label,
                value,
                "ISO-8601 instant",
                field_path=field_path,
                example='"2026-09-26T00:00:00Z"',
            )
        return render_instant(parsed)
    if value_type == "date":
        try:
            return date.fromisoformat(value).isoformat() if isinstance(value, str) else None
        except ValueError:
            pass
        raise _refuse_value(
            label, value, "ISO-8601 date", field_path=field_path, example='"2026-09-26"'
        )
    if value_type == "subject":
        ref = subject_ref(value) if isinstance(value, str) else None
        if ref is None:
            raise _refuse_value(
                label, value, "Subject reference", field_path=field_path, example='"kind/id"'
            )
        kind = ref.split("/", 1)[0]
        if info.object_kinds and kind not in info.object_kinds:
            raise query_refusal(
                "cruxible.query.value_type_mismatch",
                f"{label} names Subjects of {', '.join(info.object_kinds)}, not {kind!r}",
                nearest=info.object_kinds,
                repair="pass kind/id of an admitted kind",
                field_path=field_path,
            )
        return ref
    return value


def check_operator(
    info: PredicateInfo | Literal["subject_id"],
    operator: str,
    *,
    field_path: str,
    label: str,
) -> None:
    admitted = _SUBJECT_ID_OPERATORS if isinstance(info, str) else info.operators()
    if operator in admitted:
        return
    type_name = "subject_id" if isinstance(info, str) else info.value_type
    raise query_refusal(
        "cruxible.query.operator_not_applicable",
        f"{operator!r} does not apply to {label} ({type_name})",
        nearest=tuple(sorted(admitted)),
        repair=f"use one of: {', '.join(sorted(admitted))}",
        field_path=field_path,
    )


# -- compact definitions ----------------------------------------------------


def object_label(info: PredicateInfo) -> str:
    if info.value_type == "subject":
        return "subject:" + "|".join(info.object_kinds) if info.object_kinds else "subject"
    return info.value_type


def claim_type_row(info: PredicateInfo, contracts: CaptureContractNames) -> dict[str, Any]:
    """One ClaimType as a compact definition row: values, never digests."""

    row: dict[str, Any] = {
        "predicate": info.predicate,
        "subject_kinds": list(info.subject_kinds),
        "object": object_label(info),
        "cardinality": info.cardinality,
    }
    if info.members:
        row["members"] = list(info.members)
    description = getattr(info.claim_type, "description", None)
    if isinstance(description, str) and description:
        row["description"] = description
    row["evidence"] = list(contracts.admitted(info.claim_type))
    return row


__all__ = [
    "ORDERABLE_TYPES",
    "PredicateInfo",
    "QueryVocabulary",
    "SUBJECT_ID_FIELD",
    "ValueType",
    "check_operator",
    "check_value",
    "claim_type_row",
    "load_query_vocabulary",
    "query_not_found",
    "query_refusal",
    "object_label",
    "render_instant",
    "subject_ref",
    "value_type_of",
]
