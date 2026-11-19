"""Triggers, Lines and the other definition kinds as compact ``query`` kinds.

``query kind=Trigger``, ``Line``, ``SubjectKind``, ``CaptureContract``,
``ProcedureMandate`` and ``QueryDefinition`` list those accepted definitions
with the same grammar every compact query uses (``where``, ``contains``,
``select``, ``order_by``, paging), on CLI, MCP and SDK alike. They are read
from the typed-state index at the coordinate, not through the query engine:
none is a Subject kind nor a definition kind of the query grammar (ClaimType
and Procedure are, and go through its artifact entry).

A row reads its own artifact and what the ontology says about it: a
SubjectKind's live Subject count from the maintained ``query_subject_counts``
and its owning kit from the installed kits' receipts, a CaptureContract's
admitting ClaimTypes from the live ClaimTypes the query's vocabulary already
parsed, a mandate's state from the evaluation time. Every listing costs the
ontology's size; nothing it reads grows with the Claims, Subjects or Captures.

The index selects what it can (lifecycle and name on every kind; a Trigger's
schedule, target and target kind; a Line's procedure and authority; a
SubjectKind's rule and uniqueness; a CaptureContract's grade; a mandate's
procedure and grant), in identity order, and the answer stops at
``LISTING_MAX_RESULTS`` matching rows: past it the answer is ``capped`` and
says so, as a compact query that hits its server cap does. A Trigger's
schedule detail (cron expression, cadence, capture contract) is read from its
artifact only when the request selects, searches or orders by it. A Line row
lists at most ``LINE_TRIGGER_NAMES_MAX`` of its Triggers beside their total.
Without a ``lifecycle`` filter only live rows are listed. Integer columns
compare (``lt`` .. ``gte``), timestamps compare and order as instants.
``order_by`` sorts by each column's type, nulls last in either direction, ties
by identity. A Line's ``triggers`` filters and a search read every live
Trigger aimed at it, not only the names its row shows.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal, cast

from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.captures import CaptureContractAny as CaptureContract
from cruxible_client.contracts.captures import contract_source_kinds
from cruxible_client.contracts.claim_types import effective_evidence_requirement
from cruxible_client.contracts.compact_query import QueryColumn, QueryRequest
from cruxible_client.contracts.procedure_mandates import (
    MANDATE_CHANGE_KIND_ORDER,
    ProcedureMandate,
    mandate_grant,
)
from cruxible_client.contracts.procedures.models import RUNG_AUTHORITY
from cruxible_client.contracts.procedures.windows import CaptureEventNameWindow
from cruxible_client.contracts.query.definitions import QueryDefinition
from cruxible_client.contracts.subject_kinds import SubjectKind, SubjectKindKeys
from cruxible_client.contracts.triggers import (
    CadenceSchedule,
    CaptureLandingSchedule,
    CronSchedule,
    Trigger,
    WindowCloseSchedule,
)
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.indexes.query_selection import LIVE_SUBJECT_COUNTS_SQL
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.contract_names import CaptureContractNames
from cruxible_core.service.discovery.kit_owners import kit_owners, owned_by
from cruxible_core.service.discovery.query_vocabulary import (
    QUERYABLE_DEFINITION_KINDS,
    QueryVocabulary,
    coerce_text_spelling,
    query_refusal,
    render_instant,
)
from cruxible_core.service.read_refusals import nearest

ListedKind = Literal[
    "Trigger", "Line", "SubjectKind", "CaptureContract", "ProcedureMandate", "QueryDefinition"
]
LISTED_KINDS: tuple[ListedKind, ...] = (
    "Trigger",
    "Line",
    "SubjectKind",
    "CaptureContract",
    "ProcedureMandate",
    "QueryDefinition",
)
assert set(LISTED_KINDS) <= set(QUERYABLE_DEFINITION_KINDS)
#: The most rows one Trigger or Line listing answers; past it the answer is capped.
LISTING_MAX_RESULTS = 2000
#: The most Trigger names one Line row lists; ``triggers_total`` counts them all.
LINE_TRIGGER_NAMES_MAX = 25

_LISTING_DIGEST_DOMAIN = "playbill-compact-listing-v1"
_LIFECYCLES = ("live", "retired")
_SCHEDULES = ("cadence", "capture_landing", "cron", "generation_accepted", "window_close")
_AUTHORITIES = ("observe", "propose", "settle")
_MANDATE_STATES = ("active", "expired", "expiring", "not_yet_valid", "retired", "suspended")
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
    #: How a filter value is stored in ``index_column`` when it is not the value itself.
    index_map: Mapping[str, object] | None = None


@dataclass(frozen=True)
class _KindSpec:
    """A listed kind's typed-state table and the column that names its rows."""

    table: str
    key: str = "name"


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
        ListedField(
            QueryColumn(name="logical_sources", type="string", cardinality="many"),
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
    "SubjectKind": (
        ListedField(QueryColumn(name="kind", type="string"), index_column="identity"),
        ListedField(
            QueryColumn(name="rule", type="enum", members=("keys", "minted")),
            index_column="rule",
        ),
        ListedField(QueryColumn(name="keys", type="string", cardinality="many")),
        ListedField(
            QueryColumn(name="uniqueness", type="enum", members=("hint", "unique")),
            index_column="uniqueness",
        ),
        ListedField(QueryColumn(name="description", type="string")),
        ListedField(QueryColumn(name="subjects", type="integer")),
        ListedField(QueryColumn(name="kit", type="string")),
        ListedField(
            QueryColumn(name="lifecycle", type="enum", members=_LIFECYCLES),
            index_column="lifecycle",
        ),
        ListedField(QueryColumn(name="version", type="integer"), filterable=False, default=False),
    ),
    "CaptureContract": (
        ListedField(QueryColumn(name="name", type="string"), index_column="identity"),
        ListedField(QueryColumn(name="version", type="integer"), filterable=False),
        ListedField(
            QueryColumn(name="lifecycle", type="enum", members=_LIFECYCLES),
            index_column="lifecycle",
        ),
        ListedField(
            QueryColumn(
                name="source_kinds",
                type="enum",
                members=("cas", "external", "ledger"),
                cardinality="many",
            )
        ),
        ListedField(
            QueryColumn(name="grade", type="enum", members=("derived", "observed", "predicted")),
            index_column="epistemic_grade",
        ),
        ListedField(QueryColumn(name="admitted_by", type="integer")),
        ListedField(QueryColumn(name="required_by", type="integer")),
    ),
    "ProcedureMandate": (
        ListedField(QueryColumn(name="name", type="string"), index_column="identity"),
        ListedField(QueryColumn(name="line", type="string"), index_column="line_identity"),
        ListedField(
            QueryColumn(name="grant", type="enum", members=("propose", "settle")),
            index_column="rung",
            index_map={"propose": 2, "settle": 3},
        ),
        ListedField(QueryColumn(name="state", type="enum", members=_MANDATE_STATES)),
        ListedField(QueryColumn(name="scope", type="string", cardinality="many")),
        ListedField(
            QueryColumn(
                name="change_kinds",
                type="enum",
                members=MANDATE_CHANGE_KIND_ORDER,
                cardinality="many",
            )
        ),
        ListedField(QueryColumn(name="condition", type="string")),
        ListedField(QueryColumn(name="suspended", type="boolean")),
        ListedField(
            QueryColumn(name="lifecycle", type="enum", members=_LIFECYCLES),
            index_column="lifecycle",
        ),
        ListedField(QueryColumn(name="expires_at", type="timestamp")),
        ListedField(QueryColumn(name="version", type="integer"), filterable=False, default=False),
    ),
    "QueryDefinition": (
        ListedField(QueryColumn(name="name", type="string"), index_column="identity"),
        ListedField(QueryColumn(name="parameters", type="string", cardinality="many")),
        ListedField(QueryColumn(name="description", type="string")),
        ListedField(
            QueryColumn(
                name="result_shape",
                type="enum",
                members=("artifact_definition", "path", "relation_claim", "subject"),
            )
        ),
        ListedField(
            QueryColumn(name="lifecycle", type="enum", members=_LIFECYCLES),
            index_column="lifecycle",
        ),
        ListedField(QueryColumn(name="version", type="integer"), filterable=False, default=False),
    ),
}
_SPECS: Mapping[ListedKind, _KindSpec] = {
    "Trigger": _KindSpec("triggers"),
    "Line": _KindSpec("lines"),
    "SubjectKind": _KindSpec("subject_kinds", key="kind"),
    "CaptureContract": _KindSpec("capture_contracts"),
    "ProcedureMandate": _KindSpec("procedure_mandates"),
    "QueryDefinition": _KindSpec("query_definitions"),
}
assert set(_FIELDS) == set(_SPECS) == set(LISTED_KINDS)


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
    type: str = "string"


#: The operators each column type answers; ``in`` and ``contains`` take strings.
_OPERATORS: Mapping[str, frozenset[str]] = {
    "boolean": frozenset({"eq", "ne"}),
    "integer": frozenset({"eq", "ne", "lt", "lte", "gt", "gte", "in"}),
    "timestamp": frozenset({"eq", "ne", "lt", "lte", "gt", "gte"}),
}
_TEXT_OPERATORS = frozenset({"eq", "ne", "in", "contains"})
_COMPARISONS = frozenset({"lt", "lte", "gt", "gte"})


# -- filters ------------------------------------------------------------------------


def _instant(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _filter_value(
    kind: ListedKind, field: ListedField, value: object, path: str
) -> str | bool | int:
    """A filter value as the column holds it: a string, a boolean or an integer.

    The CLI spells every value as text, so an integer or boolean column reads
    its one canonical spelling (``3``, ``true``); a timestamp is any ISO-8601
    instant, compared as an instant.
    """

    if field.column.type == "integer":
        value = coerce_text_spelling(value, "integer")
        if isinstance(value, bool) or not isinstance(value, int):
            raise query_refusal(
                "cruxible.query.value_type_mismatch",
                f"{kind} {field.column.name} values are integers",
                repair=f"pass an integer, for example {field.column.name}>0",
                field_path=path,
            )
        return value
    if field.column.type == "timestamp":
        instant = _instant(value) if isinstance(value, str) else None
        if instant is None:
            raise query_refusal(
                "cruxible.query.value_type_mismatch",
                f"{kind} {field.column.name} values are ISO-8601 instants",
                repair='pass an instant such as "2026-12-31T00:00:00Z"',
                field_path=path,
            )
        return render_instant(instant)
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
        allowed = _OPERATORS.get(listed.column.type, _TEXT_OPERATORS)
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
        checks.append(_Check(item.field, item.operator, value, listed.column.type))
    return checks


def _index_value(kind: ListedKind, field: ListedField, value: object) -> object:
    """A filter value as the typed-state index stores it."""

    if field.index_column == "identity":
        return f"{kind}:{value}"
    if field.column.name == "authority":
        return _RUNG_OF[str(value)]
    if field.index_map is not None:
        return field.index_map[str(value)]
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
        parameters.extend(_index_value(kind, fields[check.field], value) for value in values)
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


def _compared(check: _Check, entry: object) -> bool:
    """Whether one cell value stands in a comparison's order to the filter value."""

    if check.type == "timestamp":
        left, right = _instant(str(entry)), _instant(str(check.value))
        if left is None or right is None:
            return False
        ordered: tuple[Any, Any] = (left, right)
    elif isinstance(entry, int) and not isinstance(entry, bool):
        ordered = (entry, check.value)
    else:
        return False
    first, second = ordered
    if check.operator == "lt":
        return bool(first < second)
    if check.operator == "lte":
        return bool(first <= second)
    if check.operator == "gt":
        return bool(first > second)
    return bool(first >= second)


def _matcher(checks: Sequence[_Check], contains: str | None) -> Callable[[Mapping[str, Any]], bool]:
    needle = None if contains is None else contains.casefold()

    def matches(row: Mapping[str, Any]) -> bool:
        for check in checks:
            cell = _cell_values(row, check.field)
            if check.operator in _COMPARISONS:
                matched = any(_compared(check, entry) for entry in cell)
            elif check.type == "timestamp" and check.operator in {"eq", "ne"}:
                found = any(
                    _compared(replace(check, operator="gte"), entry)
                    and _compared(replace(check, operator="lte"), entry)
                    for entry in cell
                )
                matched = found if check.operator == "eq" else not found
            elif check.operator == "eq":
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
    sources: tuple[str, ...] = ()
    if isinstance(schedule, CaptureLandingSchedule):
        contract = schedule.event.capture_contract_identity.qualified
        sources = schedule.event.logical_sources
    elif isinstance(schedule, WindowCloseSchedule) and isinstance(
        schedule.window, CaptureEventNameWindow
    ):
        contract = schedule.window.event.capture_contract_identity.qualified
        sources = schedule.window.event.logical_sources
    return {
        "cron": schedule.expression if isinstance(schedule, CronSchedule) else None,
        "cadence": schedule.interval_seconds if isinstance(schedule, CadenceSchedule) else None,
        "capture_contract": contract,
        "logical_sources": list(sources),
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


def _definition_rows(
    projection: Any,
    kind: ListedKind,
    where: str,
    parameters: Sequence[object],
    row: Callable[[Any], dict[str, Any]],
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Each selected definition's row, in identity order: its artifact read once."""

    cursor = projection.typed.connection.execute(
        f"SELECT identity, revision, lifecycle FROM {_SPECS[kind].table} "
        f"WHERE {where} ORDER BY identity",
        tuple(parameters),
    )
    while chunk := cursor.fetchmany(_CHUNK):
        for identity, revision, lifecycle in chunk:
            yield (
                str(identity),
                {
                    **row(projection.typed.source(str(identity))),
                    "lifecycle": str(lifecycle),
                    "version": int(revision),
                },
            )


@dataclass(frozen=True)
class _RowContext:
    """What a definition row may read beside its own artifact, once per listing."""

    instance: PlaybillInstance
    coordinate: AcceptedProjectionCoordinate
    projection: Any
    vocabulary: QueryVocabulary
    evaluation_time: datetime


def _subject_kind_row(context: _RowContext) -> Callable[[Any], dict[str, Any]]:
    """A SubjectKind row: its identity rule, the kind's live Subjects and its owning kit.

    The count is the maintained ``query_subject_counts`` row and the kit the
    installed kits' receipts: one read of each per listing, vocabulary-sized.
    """

    connection = context.projection.typed.connection
    counts = {
        str(kind): int(amount) for kind, amount in connection.execute(LIVE_SUBJECT_COUNTS_SQL)
    }
    owners = kit_owners(context.instance, connection)

    def row(source: Any) -> dict[str, Any]:
        declared = cast(SubjectKind, source)
        rule = declared.identity_rule
        return {
            "kind": declared.subject_kind,
            "rule": rule.rule,
            "keys": list(declared.keys),
            "uniqueness": rule.uniqueness if isinstance(rule, SubjectKindKeys) else None,
            "description": declared.description,
            "subjects": counts.get(declared.subject_kind, 0),
            "kit": owned_by(owners, "SubjectKind", declared.subject_kind),
        }

    return row


def _capture_contract_row(context: _RowContext) -> Callable[[Any], dict[str, Any]]:
    """A CaptureContract row: what it captures, and the live ClaimTypes that rely on it.

    ``admitted_by`` counts the live ClaimTypes whose evidence rules admit the
    contract; ``required_by`` those of them that require a Capture
    (``evidence_requirement: captured``). Both read the ClaimTypes the query's
    vocabulary already parsed: ontology-sized, never a Claim or a Capture.
    """

    names = CaptureContractNames(
        context.instance, context.coordinate, connection=context.projection.typed.connection
    )
    admitted: dict[str, int] = {}
    required: dict[str, int] = {}
    for info in context.vocabulary.predicates.values():
        captured = effective_evidence_requirement(info.claim_type) == "captured"
        for name in names.admitted(info.claim_type, qualified=True):
            admitted[name] = admitted.get(name, 0) + 1
            required[name] = required.get(name, 0) + int(captured)

    def row(source: Any) -> dict[str, Any]:
        contract = cast(CaptureContract, source)
        identity = contract.identity.qualified
        return {
            "name": contract.identity.name,
            "source_kinds": list(contract_source_kinds(contract)),
            "grade": contract.epistemic_grade,
            "admitted_by": admitted.get(identity, 0),
            "required_by": required.get(identity, 0),
        }

    return row


def _mandate_row(context: _RowContext) -> Callable[[Any], dict[str, Any]]:
    """A ProcedureMandate row: what it grants, over which ClaimTypes, under what condition.

    ``state`` is the mandate's at the evaluation time, as orient's mandates
    rows show it; a v1 mandate has no scope, condition or kill switch.
    """

    from cruxible_core.service.discovery.operational import mandate_state

    def row(source: Any) -> dict[str, Any]:
        mandate = cast(ProcedureMandate, source)
        current = mandate if isinstance(mandate, ProcedureMandate) else None
        scope = () if current is None else current.scope
        kinds = {kind for item in scope for kind in item.change_kinds}
        condition = None if current is None else current.condition
        return {
            "name": mandate.identity.name,
            "line": mandate.line.target.qualified,
            "grant": mandate_grant(mandate),
            "state": mandate_state(mandate, now=context.evaluation_time),
            "scope": [item.claim_type.target.qualified for item in scope],
            "change_kinds": [kind for kind in MANDATE_CHANGE_KIND_ORDER if kind in kinds],
            "condition": None if condition is None else condition.query.target.qualified,
            "suspended": current is not None and current.suspended,
            "expires_at": render_instant(mandate.expires_at),
        }

    return row


def _query_definition_row(context: _RowContext) -> Callable[[Any], dict[str, Any]]:
    """A QueryDefinition row: what a named run takes and what it answers.

    ``parameters`` read ``name: type``, with ``?`` after an optional one's
    name, as orient's queries rows show them.
    """

    del context

    def row(source: Any) -> dict[str, Any]:
        query = cast(QueryDefinition, source)
        return {
            "name": query.identity.name,
            "parameters": [
                f"{item.name}{'' if item.required else '?'}: {item.value_type}"
                for item in query.parameters
            ],
            "description": query.description,
            "result_shape": query.result_shape,
        }

    return row


_ROWS: Mapping[str, Callable[[_RowContext], Callable[[Any], dict[str, Any]]]] = {
    "SubjectKind": _subject_kind_row,
    "CaptureContract": _capture_contract_row,
    "ProcedureMandate": _mandate_row,
    "QueryDefinition": _query_definition_row,
}


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
        if match not in chosen:
            chosen.append(match)
    # The key column always leads, so a selected row still says what it is.
    key = next((item.column for item in fields if item.column.name == _SPECS[kind].key), None)
    return tuple(chosen) if key is None or key in chosen else (key, *chosen)


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


def _sort_key(value: object, column_type: str | None = None) -> Any:
    """A comparable key in the column's own type: numbers as numbers, lists element-wise,
    timestamps as instants."""

    if isinstance(value, list):
        return tuple(str(entry) for entry in value)
    if isinstance(value, bool):
        return int(value)
    if column_type == "timestamp" and isinstance(value, str):
        return _instant(value) or value
    return value


def ordered_rows(
    rows: Sequence[tuple[str, Mapping[str, Any]]],
    order: Sequence[tuple[str, bool]],
    types: Mapping[str, str] | None = None,
) -> list[tuple[str, Mapping[str, Any]]]:
    """Rows sorted by each (column, descending) in turn: typed, nulls last, ties by identity."""

    column_types = types or {}

    def compare(left: tuple[str, Mapping[str, Any]], right: tuple[str, Mapping[str, Any]]) -> int:
        for name, descending in order:
            first, second = left[1].get(name), right[1].get(name)
            if first is None or second is None:
                if first is None and second is None:
                    continue
                return 1 if first is None else -1
            a = _sort_key(first, column_types.get(name))
            b = _sort_key(second, column_types.get(name))
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
    vocabulary: QueryVocabulary,
    evaluation_time: datetime,
    max_results: int | None = None,
) -> ListedAnswer:
    """The listed definitions the request selects, at the coordinate, as compact rows."""

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
        rows: Iterator[tuple[str, dict[str, Any]]]
        if kind == "Trigger":
            rows = _trigger_rows(projection, where, parameters, detail=detail)
        elif kind == "Line":
            rows = _line_rows(
                instance,
                projection,
                where,
                parameters,
                evaluation_time=evaluation_time,
                contains=request.contains,
            )
        else:
            context = _RowContext(instance, coordinate, projection, vocabulary, evaluation_time)
            rows = _definition_rows(projection, kind, where, parameters, _ROWS[kind](context))
        for identity, row in rows:
            if not matches(row):
                continue
            if len(kept) == max_results:
                capped = True
                break
            kept.append((identity, row))
    if order:
        kept = ordered_rows(
            kept, order, {item.column.name: item.column.type for item in _FIELDS[kind]}
        )
    shown = {column.name for column in columns} | {_SPECS[kind].key}
    order_of = [item.column.name for item in _FIELDS[kind]]
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
        # Cells in column order, whatever order a row was built in.
        rows=[
            {name: row[name] for name in order_of if name in shown and name in row}
            for _, row in kept
        ],
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
