"""Triggers and Lines as compact ``query`` kinds.

``query kind=Trigger`` and ``query kind=Line`` list the accepted Triggers and
Lines with the same grammar every compact query uses (``where``, ``contains``,
``select``, ``order_by``, paging), on CLI, MCP and SDK alike. They are read
straight from the typed-state index at the coordinate, not through the query
engine: neither is a Subject kind nor a definition kind of the query grammar.

Without a ``lifecycle`` filter only live rows are listed. A Trigger row's
schedule detail (cron expression, cadence, capture contract) and version are
columns ``select`` adds; a Line row says whether the Line is enabled (its
automation is running) and names its live Triggers.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.compact_query import QueryColumn, QueryRequest
from cruxible_client.contracts.operational_reads import OrientLine
from cruxible_client.contracts.procedures.line_specs import LineSpecAny
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

_LISTING_DIGEST_DOMAIN = "playbill-compact-listing-v1"
_LIFECYCLES = ("live", "retired")
_SCHEDULES = ("cadence", "capture_landing", "cron", "generation_accepted", "window_close")


@dataclass(frozen=True)
class ListedField:
    """One column of a listed kind: its type, and whether ``where`` may filter on it."""

    column: QueryColumn
    filterable: bool = True
    #: Shown without ``select``; detail columns appear only when selected.
    default: bool = True


_FIELDS: Mapping[ListedKind, tuple[ListedField, ...]] = {
    "Trigger": (
        ListedField(QueryColumn(name="name", type="string")),
        ListedField(QueryColumn(name="schedule", type="enum", members=_SCHEDULES)),
        ListedField(QueryColumn(name="target_kind", type="enum", members=("action", "line"))),
        ListedField(QueryColumn(name="target", type="string")),
        ListedField(QueryColumn(name="lifecycle", type="enum", members=_LIFECYCLES)),
        ListedField(QueryColumn(name="version", type="integer"), filterable=False, default=False),
        ListedField(QueryColumn(name="cron", type="string"), filterable=False, default=False),
        ListedField(QueryColumn(name="cadence", type="integer"), filterable=False, default=False),
        ListedField(
            QueryColumn(name="capture_contract", type="string"), filterable=False, default=False
        ),
    ),
    "Line": (
        ListedField(QueryColumn(name="name", type="string")),
        ListedField(QueryColumn(name="procedure", type="string")),
        ListedField(
            QueryColumn(name="authority", type="enum", members=("observe", "propose", "settle"))
        ),
        ListedField(QueryColumn(name="lifecycle", type="enum", members=_LIFECYCLES)),
        ListedField(QueryColumn(name="enabled", type="boolean")),
        ListedField(QueryColumn(name="triggers", type="string", cardinality="many")),
        ListedField(QueryColumn(name="version", type="integer"), filterable=False, default=False),
    ),
}


@dataclass(frozen=True)
class ListedAnswer:
    """Every matching row in order, with the columns shown and the keys paging binds."""

    spec_digest: str
    columns: tuple[QueryColumn, ...]
    rows: list[dict[str, Any]]
    keys: list[tuple[str, ...]]


def _trigger_rows(projection: Any) -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []
    for identity, revision in projection.typed.connection.execute(
        "SELECT identity, revision FROM triggers ORDER BY identity"
    ):
        trigger = cast(Trigger, projection.typed.source(str(identity)))
        schedule = trigger.schedule
        contract: str | None = None
        if isinstance(schedule, CaptureLandingSchedule):
            contract = schedule.event.capture_contract_identity.qualified
        elif isinstance(schedule, WindowCloseSchedule) and isinstance(
            schedule.window, CaptureEventWindow
        ):
            contract = schedule.window.event.capture_contract_identity.qualified
        line = trigger.line
        rows.append(
            (
                trigger.identity.qualified,
                {
                    "name": trigger.identity.name,
                    "schedule": schedule.kind,
                    "target_kind": trigger.target.kind,
                    "target": line.qualified if line is not None else str(trigger.action),
                    "lifecycle": trigger.lifecycle.state,
                    "version": int(revision),
                    "cron": schedule.expression if isinstance(schedule, CronSchedule) else None,
                    "cadence": (
                        schedule.interval_seconds if isinstance(schedule, CadenceSchedule) else None
                    ),
                    "capture_contract": contract,
                },
            )
        )
    return rows


def _line_rows(
    projection: Any, state: Mapping[str, OrientLine]
) -> list[tuple[str, dict[str, Any]]]:
    from cruxible_core.service.discovery.operational import aimed_triggers

    lines = [
        cast(LineSpecAny, projection.typed.source(str(identity)))
        for (identity,) in projection.typed.connection.execute(
            "SELECT identity FROM lines ORDER BY identity"
        )
    ]
    revisions = {
        str(identity): int(revision)
        for identity, revision in projection.typed.connection.execute(
            "SELECT identity, revision FROM lines"
        )
    }
    aimed = aimed_triggers(projection, tuple(line.identity.qualified for line in lines))
    rows: list[tuple[str, dict[str, Any]]] = []
    for line in lines:
        identity = line.identity.qualified
        orient = state[identity]
        rows.append(
            (
                identity,
                {
                    "name": line.identity.name,
                    "procedure": orient.procedure,
                    "authority": orient.authority,
                    "lifecycle": orient.lifecycle,
                    # Enabled: the Line's automation is admitting work now.
                    "enabled": orient.arm in {"running", "stalled"},
                    "triggers": [item.identity.qualified for item in aimed.get(identity, ())],
                    "version": revisions.get(identity, 1),
                },
            )
        )
    return rows


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


def _cell_values(row: Mapping[str, Any], name: str) -> list[object]:
    cell = row.get(name)
    if cell is None:
        return []
    return list(cell) if isinstance(cell, list) else [cell]


def _matcher(
    kind: ListedKind, request: QueryRequest
) -> tuple[Callable[[Mapping[str, Any]], bool], bool]:
    """The row predicate ``where`` states, and whether it names a lifecycle."""

    fields = {item.column.name: item for item in _FIELDS[kind] if item.filterable}
    checks: list[tuple[str, str, object]] = []
    names_lifecycle = False
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
        if item.operator == "in":
            value: object = tuple(
                _filter_value(kind, listed, entry, f"{path}.in")
                for entry in cast(tuple[object, ...], item.value)
            )
        elif item.operator == "contains":
            value = str(item.value)
        else:
            value = _filter_value(kind, listed, item.value, f"{path}.{item.operator}")
        names_lifecycle = names_lifecycle or item.field == "lifecycle"
        checks.append((item.field, item.operator, value))
    needle = None if request.contains is None else request.contains.casefold()

    def matches(row: Mapping[str, Any]) -> bool:
        for name, operator, value in checks:
            cell = _cell_values(row, name)
            if operator == "eq":
                matched = value in cell
            elif operator == "ne":
                matched = value not in cell
            elif operator == "in":
                matched = any(entry in cell for entry in cast(tuple[object, ...], value))
            else:
                matched = any(str(value).casefold() in str(entry).casefold() for entry in cell)
            if not matched:
                return False
        if needle is not None:
            texts = [str(entry) for name in row for entry in _cell_values(row, name)]
            return any(needle in text.casefold() for text in texts)
        return True

    return matches, names_lifecycle


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


def _ordered(
    kind: ListedKind,
    rows: list[tuple[str, dict[str, Any]]],
    order_by: Sequence[str],
) -> list[tuple[str, dict[str, Any]]]:
    names = [item.column.name for item in _FIELDS[kind]]
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
    for raw in reversed(order_by):
        name = raw.removeprefix("-").removeprefix("+")
        rows = sorted(
            rows,
            key=lambda item: str(item[1].get(name, "")),
            reverse=raw.startswith("-"),
        )
    return rows


def listed_kind_answer(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    request: QueryRequest,
    *,
    evaluation_time: datetime,
) -> ListedAnswer:
    """Every Trigger or Line the request selects, at the coordinate, as compact rows."""

    kind = cast(ListedKind, request.kind)
    if request.follow:
        raise query_refusal(
            "cruxible.query.follow_not_relation",
            f"{kind} rows have no relations to follow",
            repair=f"drop follow; get the {kind}'s card for what it names",
            field_path="follow",
        )
    matches, names_lifecycle = _matcher(kind, request)
    columns = _columns(kind, request)
    state: dict[str, OrientLine] = {}
    if kind == "Line":
        from cruxible_core.service.discovery.operational import line_rows

        # Each Line's automation state, read from the dispatch store as orient reads it.
        state = {
            row.line: row
            for row in line_rows(instance, coordinate, evaluation_time=evaluation_time)
        }
    with instance.bind_accepted_projection(coordinate) as projection:
        candidates = (
            _trigger_rows(projection) if kind == "Trigger" else _line_rows(projection, state)
        )
    kept = [
        (identity, row)
        for identity, row in candidates
        if (names_lifecycle or row["lifecycle"] == "live") and matches(row)
    ]
    kept = _ordered(kind, kept, request.order_by)
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
    return ListedAnswer(
        spec_digest=spec_digest,
        columns=columns,
        rows=[{name: value for name, value in row.items() if name in shown} for _, row in kept],
        keys=[(identity,) for identity, _ in kept],
    )


__all__ = ["LISTED_KINDS", "ListedAnswer", "listed_kind_answer"]
