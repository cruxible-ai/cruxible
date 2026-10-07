"""Triggers and Lines as compact ``query`` kinds.

``query kind=Trigger`` and ``query kind=Line`` list the accepted Triggers and
Lines with the same grammar every compact query uses (``where``, ``contains``,
``select``, ``order_by``, paging), on CLI, MCP and SDK alike. They are read
from the typed-state index at the coordinate, not through the query engine:
neither is a Subject kind nor a definition kind of the query grammar.

The index selects what it can (lifecycle, name, schedule, target and target
kind; a Line's procedure and authority), in identity order, and the answer
stops at ``LISTING_MAX_RESULTS`` matching rows: past it the answer is
``capped`` and says so, as a compact query that hits its server cap does. A
Trigger's schedule detail (cron expression, cadence, capture contract) is read
from its artifact only when the request selects, searches or orders by it. A
Line row lists at most ``LINE_TRIGGER_NAMES_MAX`` of its Triggers beside their
total. Without a ``lifecycle`` filter only live rows are listed. ``order_by``
sorts by each column's type, nulls last in either direction, ties by identity.
A Line's ``triggers`` filters and a search read every live Trigger aimed at it,
not only the names its row shows.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.compact_query import QueryColumn, QueryRequest
from cruxible_client.contracts.procedures.models import RUNG_AUTHORITY
from cruxible_client.contracts.procedures.windows import CaptureEventWindow
from cruxible_client.contracts.triggers import (
    CadenceSchedule,
    CaptureLandingSchedule,
    CronSchedule,
    Trigger,
    WindowCloseSchedule,
)
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.query_vocabulary import query_refusal
from cruxible_core.service.read_refusals import nearest

ListedKind = Literal["Trigger", "Line"]
LISTED_KINDS: tuple[ListedKind, ...] = ("Trigger", "Line")
#: The most rows one Trigger or Line listing answers; past it the answer is capped.
LISTING_MAX_RESULTS = 2000
#: The most Trigger names one Line row lists; ``triggers_total`` counts them all.
LINE_TRIGGER_NAMES_MAX = 25

_LISTING_DIGEST_DOMAIN = "playbill-compact-listing-v1"
_LIFECYCLES = ("live", "retired")
_SCHEDULES = ("cadence", "capture_landing", "cron", "generation_accepted", "window_close")
_AUTHORITIES = ("observe", "propose", "settle")
_RUNG_OF: dict[str, int] = {authority: rung for rung, authority in RUNG_AUTHORITY.items()}
#: Rows scanned per index chunk while matching.
_CHUNK = 500
#: A Line row's hidden flag: one of its Triggers, shown or not, contains the search text.
_TRIGGER_HIT = "_trigger_hit"


@dataclass(frozen=True)
class ListedField:
    """One column of a listed kind: its type, whether ``where`` filters it, and where.

    ``index_column`` names the typed-state column an ``eq`` or ``in`` filter on
    this field is pushed into; ``detail`` columns need the artifact itself.
    """

    column: QueryColumn
    filterable: bool = True
    #: Shown without ``select``; detail columns appear only when selected.
    default: bool = True
    index_column: str | None = None
    detail: bool = False


_FIELDS: Mapping[ListedKind, tuple[ListedField, ...]] = {
    "Trigger": (
        ListedField(QueryColumn(name="name", type="string"), index_column="identity"),
        ListedField(
            QueryColumn(name="schedule", type="enum", members=_SCHEDULES),
            index_column="schedule_kind",
        ),
        ListedField(
            QueryColumn(name="target_kind", type="enum", members=("action", "line")),
            index_column="target_kind",
        ),
        ListedField(QueryColumn(name="target", type="string"), index_column="target"),
        ListedField(
            QueryColumn(name="lifecycle", type="enum", members=_LIFECYCLES),
            index_column="lifecycle",
        ),
        ListedField(QueryColumn(name="version", type="integer"), filterable=False, default=False),
        ListedField(
            QueryColumn(name="cron", type="string"), filterable=False, default=False, detail=True
        ),
        ListedField(
            QueryColumn(name="cadence", type="integer"),
            filterable=False,
            default=False,
            detail=True,
        ),
        ListedField(
            QueryColumn(name="capture_contract", type="string"),
            filterable=False,
            default=False,
            detail=True,
        ),
    ),
    "Line": (
        ListedField(QueryColumn(name="name", type="string"), index_column="identity"),
        ListedField(
            QueryColumn(name="procedure", type="string"), index_column="procedure_identity"
        ),
        ListedField(
            QueryColumn(name="authority", type="enum", members=_AUTHORITIES),
            index_column="requested_terminal_rung",
        ),
        ListedField(
            QueryColumn(name="lifecycle", type="enum", members=_LIFECYCLES),
            index_column="lifecycle",
        ),
        ListedField(QueryColumn(name="enabled", type="boolean")),
        ListedField(QueryColumn(name="triggers", type="string", cardinality="many")),
        ListedField(QueryColumn(name="triggers_total", type="integer"), filterable=False),
        ListedField(QueryColumn(name="version", type="integer"), filterable=False, default=False),
    ),
}


@dataclass(frozen=True)
class ListedAnswer:
    """Every matching row in order (at most the ceiling), with columns, keys and cap."""

    spec_digest: str
    columns: tuple[QueryColumn, ...]
    rows: list[dict[str, Any]]
    keys: list[tuple[str, ...]]
    capped: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Check:
    field: str
    operator: str
    value: object


# -- filters ------------------------------------------------------------------------


def _filter_value(kind: ListedKind, field: ListedField, value: object, path: str) -> str | bool:
    """A filter value as the column holds it: a string, or a boolean for ``enabled``."""

    if field.column.type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower() == "true"
        raise query_refusal(
            "cruxible.query.value_type_mismatch",
            f"{kind} {field.column.name} values are true or false",
            repair=f"pass {field.column.name}=true or {field.column.name}=false",
            field_path=path,
        )
    if not isinstance(value, str):
        raise query_refusal(
            "cruxible.query.value_type_mismatch",
            f"{kind} {field.column.name} values are strings",
            repair='pass a string, for example "nightly"',
            field_path=path,
        )
    members = field.column.members
    if members is not None and value not in members:
        raise query_refusal(
            "cruxible.query.value_type_mismatch",
            f"{value!r} is not a {kind} {field.column.name}",
            nearest=nearest(value, members) or members,
            repair=f"use one of {', '.join(members)}",
            field_path=path,
        )
    return value


def _checks(kind: ListedKind, request: QueryRequest) -> list[_Check]:
    fields = {item.column.name: item for item in _FIELDS[kind] if item.filterable}
    checks: list[_Check] = []
    for index, item in enumerate(request.where):
        path = f"where[{index}]"
        listed = fields.get(item.field)
        if listed is None:
            raise query_refusal(
                "cruxible.query.unknown_field",
                f"{kind} rows have no filterable field {item.field!r}",
                nearest=nearest(item.field, fields) or tuple(fields),
                repair=f"filter {kind} on {', '.join(fields)}",
                field_path=f"{path}.field",
            )
        allowed = (
            {"eq", "ne"} if listed.column.type == "boolean" else {"eq", "ne", "in", "contains"}
        )
        if item.operator not in allowed:
            raise query_refusal(
                "cruxible.query.operator_not_applicable",
                f"{item.operator!r} does not apply to {kind} {item.field}",
                nearest=tuple(sorted(allowed)),
                repair=f"use {' or '.join(sorted(allowed))}",
                field_path=path,
            )
        value: object
        if item.operator == "in":
            value = tuple(
                _filter_value(kind, listed, entry, f"{path}.in")
                for entry in cast(tuple[object, ...], item.value)
            )
        elif item.operator == "contains":
            value = str(item.value)
        else:
            value = _filter_value(kind, listed, item.value, f"{path}.{item.operator}")
        checks.append(_Check(item.field, item.operator, value))
    return checks


def _index_value(kind: ListedKind, field: str, value: object) -> object:
    """A filter value as the typed-state index stores it."""

    if field == "name":
        return f"{kind}:{value}"
    if field == "authority":
        return _RUNG_OF[str(value)]
    return value


def _index_selection(
    kind: ListedKind, checks: Sequence[_Check]
) -> tuple[str, list[object], list[_Check]]:
    """The SQL predicate the index answers, its parameters, and the checks left for rows."""

    fields = {item.column.name: item for item in _FIELDS[kind]}
    clauses: list[str] = []
    parameters: list[object] = []
    remaining: list[_Check] = []
    for check in checks:
        if kind == "Line" and check.field == "triggers":
            # A row shows only the first Trigger names; a filter reads them all.
            clause, aimed = _aimed_trigger_clause(check)
            clauses.append(clause)
            parameters.extend(aimed)
            continue
        column = fields[check.field].index_column
        if column is None or check.operator not in {"eq", "in"}:
            remaining.append(check)
            continue
        values = cast(tuple[object, ...], check.value) if check.operator == "in" else (check.value,)
        clauses.append(f"{column} IN ({','.join('?' * len(values))})")
        parameters.extend(_index_value(kind, check.field, value) for value in values)
    if not any(check.field == "lifecycle" for check in checks):
        clauses.append("lifecycle='live'")
    # Residual filters alone leave nothing for the index to select on.
    return " AND ".join(clauses) or "1", parameters, remaining


#: Whether any live Trigger aimed at the outer ``lines`` row satisfies a condition.
_AIMED = (
    "EXISTS (SELECT 1 FROM triggers t WHERE t.target_kind='line' AND t.lifecycle='live' "
    "AND t.target=lines.identity AND {condition})"
)


def _aimed_trigger_clause(check: _Check) -> tuple[str, list[object]]:
    """A Line ``triggers`` filter over every live Trigger aimed at the Line."""

    if check.operator == "contains":
        return _AIMED.format(condition="instr(lower(t.identity), lower(?)) > 0"), [check.value]
    values = (
        list(cast(tuple[object, ...], check.value)) if check.operator == "in" else [check.value]
    )
    exists = _AIMED.format(condition=f"t.identity IN ({','.join('?' * len(values))})")
    return (f"NOT {exists}" if check.operator == "ne" else exists), values


def _cell_values(row: Mapping[str, Any], name: str) -> list[object]:
    cell = row.get(name)
    if cell is None:
        return []
    return list(cell) if isinstance(cell, list) else [cell]


def _matcher(checks: Sequence[_Check], contains: str | None) -> Callable[[Mapping[str, Any]], bool]:
    needle = None if contains is None else contains.casefold()

    def matches(row: Mapping[str, Any]) -> bool:
        for check in checks:
            cell = _cell_values(row, check.field)
            if check.operator == "eq":
                matched = check.value in cell
            elif check.operator == "ne":
                matched = check.value not in cell
            elif check.operator == "in":
                matched = any(entry in cell for entry in cast(tuple[object, ...], check.value))
            else:
                matched = any(
                    str(check.value).casefold() in str(entry).casefold() for entry in cell
                )
            if not matched:
                return False
        if needle is not None:
            # A Line row carries whether any of its Triggers, shown or not, matched.
            if row.get(_TRIGGER_HIT):
                return True
            texts = [
                str(entry)
                for name in row
                if not name.startswith("_")
                for entry in _cell_values(row, name)
            ]
            return any(needle in text.casefold() for text in texts)
        return True

    return matches


# -- rows ---------------------------------------------------------------------------


def _trigger_detail(trigger: Trigger) -> dict[str, Any]:
    schedule = trigger.schedule
    contract: str | None = None
    if isinstance(schedule, CaptureLandingSchedule):
        contract = schedule.event.capture_contract_identity.qualified
    elif isinstance(schedule, WindowCloseSchedule) and isinstance(
        schedule.window, CaptureEventWindow
    ):
        contract = schedule.window.event.capture_contract_identity.qualified
    return {
        "cron": schedule.expression if isinstance(schedule, CronSchedule) else None,
        "cadence": schedule.interval_seconds if isinstance(schedule, CadenceSchedule) else None,
        "capture_contract": contract,
    }


def _trigger_rows(
    projection: Any, where: str, parameters: Sequence[object], *, detail: bool
) -> Iterator[tuple[str, dict[str, Any]]]:
    cursor = projection.typed.connection.execute(
        "SELECT identity, revision, schedule_kind, target_kind, target, lifecycle "
        f"FROM triggers WHERE {where} ORDER BY identity",
        tuple(parameters),
    )
    while chunk := cursor.fetchmany(_CHUNK):
        for identity, revision, schedule, target_kind, target, lifecycle in chunk:
            row: dict[str, Any] = {
                "name": str(identity).removeprefix("Trigger:"),
                "schedule": str(schedule),
                "target_kind": str(target_kind),
                "target": str(target),
                "lifecycle": str(lifecycle),
                "version": int(revision),
            }
            if detail:
                row.update(_trigger_detail(cast(Trigger, projection.typed.source(str(identity)))))
            yield str(identity), row


def _line_rows(
    instance: PlaybillInstance,
    projection: Any,
    where: str,
    parameters: Sequence[object],
    *,
    evaluation_time: datetime,
    contains: str | None,
) -> Iterator[tuple[str, dict[str, Any]]]:
    from cruxible_core.service.discovery.operational import (
        aimed_trigger_page,
        line_arm_states,
    )

    hit = (
        "0"
        if contains is None
        else _AIMED.format(condition="instr(lower(t.identity), lower(?)) > 0")
    )
    cursor = projection.typed.connection.execute(
        "SELECT identity, revision, procedure_identity, requested_terminal_rung, lifecycle, "
        f"identity_digest, {hit} FROM lines WHERE {where} ORDER BY identity",
        (*(() if contains is None else (contains,)), *parameters),
    )
    while chunk := cursor.fetchmany(_CHUNK):
        identities = tuple(str(item[0]) for item in chunk)
        aimed = aimed_trigger_page(projection, identities, limit=LINE_TRIGGER_NAMES_MAX)
        # The Line's automation state, read from the dispatch store as orient reads it.
        arms = line_arm_states(
            instance, {str(item[0]): str(item[5]) for item in chunk}, now=evaluation_time
        )
        for identity, revision, procedure, rung, lifecycle, _digest, trigger_hit in chunk:
            name = str(identity)
            yield (
                name,
                {
                    "name": name.removeprefix("Line:"),
                    "procedure": str(procedure),
                    "authority": RUNG_AUTHORITY[max(int(rung), 1)],
                    "lifecycle": str(lifecycle),
                    # Enabled: the Line's automation is admitting work now.
                    "enabled": arms.get(name) in {"running", "stalled"},
                    "triggers": list(aimed[name].identities),
                    "triggers_total": aimed[name].total,
                    "version": int(revision),
                    _TRIGGER_HIT: bool(trigger_hit),
                },
            )


# -- shape --------------------------------------------------------------------------


def _columns(kind: ListedKind, request: QueryRequest) -> tuple[QueryColumn, ...]:
    fields = _FIELDS[kind]
    names = [item.column.name for item in fields]
    if not request.select:
        return tuple(item.column for item in fields if item.default)
    chosen: list[QueryColumn] = []
    for index, name in enumerate(request.select):
        match = next((item.column for item in fields if item.column.name == name), None)
        if match is None:
            raise query_refusal(
                "cruxible.query.unknown_field",
                f"{kind} rows have no column {name!r}",
                nearest=nearest(name, names) or tuple(names),
                repair=f"select from {', '.join(names)}",
                field_path=f"select[{index}]",
            )
        chosen.append(match)
    return tuple(chosen)


def _order_names(kind: ListedKind, order_by: Sequence[str]) -> list[tuple[str, bool]]:
    names = [item.column.name for item in _FIELDS[kind]]
    ordered: list[tuple[str, bool]] = []
    for index, raw in enumerate(order_by):
        name = raw.removeprefix("-").removeprefix("+")
        if name not in names:
            raise query_refusal(
                "cruxible.query.unknown_field",
                f"{kind} rows have no column {name!r}",
                nearest=nearest(name, names) or tuple(names),
                repair=f"order by one of {', '.join(names)}",
                field_path=f"order_by[{index}]",
            )
        ordered.append((name, raw.startswith("-")))
    return ordered


def _sort_key(value: object) -> Any:
    """A comparable key in the column's own type: numbers as numbers, lists element-wise."""

    if isinstance(value, list):
        return tuple(str(entry) for entry in value)
    if isinstance(value, bool):
        return int(value)
    return value


def ordered_rows(
    rows: Sequence[tuple[str, Mapping[str, Any]]], order: Sequence[tuple[str, bool]]
) -> list[tuple[str, Mapping[str, Any]]]:
    """Rows sorted by each (column, descending) in turn: typed, nulls last, ties by identity."""

    def compare(left: tuple[str, Mapping[str, Any]], right: tuple[str, Mapping[str, Any]]) -> int:
        for name, descending in order:
            first, second = left[1].get(name), right[1].get(name)
            if first is None or second is None:
                if first is None and second is None:
                    continue
                return 1 if first is None else -1
            a, b = _sort_key(first), _sort_key(second)
            if a != b:
                result = -1 if a < b else 1
                return -result if descending else result
        return (left[0] > right[0]) - (left[0] < right[0])

    return sorted(rows, key=functools.cmp_to_key(compare))


def listed_kind_answer(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    request: QueryRequest,
    *,
    evaluation_time: datetime,
    max_results: int | None = None,
) -> ListedAnswer:
    """The Triggers or Lines the request selects, at the coordinate, as compact rows."""

    if max_results is None:
        max_results = LISTING_MAX_RESULTS

    kind = cast(ListedKind, request.kind)
    if request.follow:
        raise query_refusal(
            "cruxible.query.follow_not_relation",
            f"{kind} rows have no relations to follow",
            repair=f"drop follow; get the {kind}'s card for what it names",
            field_path="follow",
        )
    checks = _checks(kind, request)
    columns = _columns(kind, request)
    order = _order_names(kind, request.order_by)
    where, parameters, remaining = _index_selection(kind, checks)
    matches = _matcher(remaining, request.contains)
    detail_names = {item.column.name for item in _FIELDS[kind] if item.detail}
    detail = request.contains is not None or bool(
        detail_names & ({column.name for column in columns} | {name for name, _ in order})
    )
    kept: list[tuple[str, Mapping[str, Any]]] = []
    capped = False
    with instance.bind_accepted_projection(coordinate) as projection:
        rows = (
            _trigger_rows(projection, where, parameters, detail=detail)
            if kind == "Trigger"
            else _line_rows(
                instance,
                projection,
                where,
                parameters,
                evaluation_time=evaluation_time,
                contains=request.contains,
            )
        )
        for identity, row in rows:
            if not matches(row):
                continue
            if len(kept) == max_results:
                capped = True
                break
            kept.append((identity, row))
    if order:
        kept = ordered_rows(kept, order)
    shown = {column.name for column in columns} | {"name"}
    spec_digest = typed_digest(
        Sha256Value,
        _LISTING_DIGEST_DOMAIN,
        {
            "kind": kind,
            "where": [item.model_dump(mode="json", by_alias=True) for item in request.where],
            "contains": request.contains,
            "select": list(request.select),
            "order_by": list(request.order_by),
        },
    ).tagged
    cap: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    if capped:
        cap = (f"max_results={max_results}",)
        notes = (
            f"the answer hit the server cap {cap[0]}; rows past it are not listed; "
            "narrow the query with where",
        )
    return ListedAnswer(
        spec_digest=spec_digest,
        columns=columns,
        rows=[{name: value for name, value in row.items() if name in shown} for _, row in kept],
        keys=[(identity,) for identity, _ in kept],
        capped=cap,
        notes=notes,
    )


__all__ = [
    "LINE_TRIGGER_NAMES_MAX",
    "LISTED_KINDS",
    "LISTING_MAX_RESULTS",
    "ListedAnswer",
    "listed_kind_answer",
    "ordered_rows",
]
