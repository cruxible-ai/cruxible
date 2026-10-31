"""The closed transforms: records in, records or one value out, three-valued.

A record is ``{record, key, fields: {f: field_result}}``; a plain row a call or
project produced reads as a record whose every field is present. A predicate
is the closed form (comparisons, membership, presence, ``all_of``/``any_of``/
``not``) evaluated Kleene-wise: an invalid field, or an absent required one,
makes it ``unknown``. Nothing here guesses a value.

Every output carries ``complete``. It is the AND of the inputs' completeness,
and ``filter``, ``aggregate`` and ``derive`` also lose it when a predicate they
evaluated was ``unknown``: a row dropped because its status was unreadable
still taints any count or negative derived from what was kept.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cruxible_client.contracts.canonical import CanonicalValue, canonical_bytes, normalize_canonical
from cruxible_client.contracts.errors import ExecutionError
from cruxible_client.contracts.procedures.nodes import (
    TransformAggregateSpec,
    TransformDedupeSpec,
    TransformDeriveSpec,
    TransformFilterSpec,
    TransformJoinSpec,
    TransformShapeSpec,
    TransformUnionSpec,
)
from cruxible_client.contracts.shapes import (
    EnumResolver,
    FieldAbsent,
    FieldInvalid,
    FieldPresent,
    Predicate,
    Problem,
    Truth,
    cast_value,
    evaluate_predicate,
    parse_type,
)

#: A record as a transform carries it: canonical JSON, field results by tag.
Record = dict[str, Any]


class TransformInputInvalid(ExecutionError):
    """A closed transform's input is not records (or a list a record can be read from)."""


@dataclass(frozen=True)
class Records:
    records: list[Record]
    complete: bool


def _present(value: object, at: str) -> dict[str, object]:
    return {"tag": "present", "value": normalize_canonical(value), "at": at}


def as_records(value: object, *, label: str) -> Records:
    """Read records from what a reference resolved to.

    An extract's or a closed transform's value (``{records, complete}``)
    carries its own completeness; a list of records or of plain rows is read
    as given, complete.
    """

    if isinstance(value, Mapping) and isinstance(value.get("records"), list):
        complete = value.get("complete")
        return Records(
            records=[_record(item, index, label) for index, item in enumerate(value["records"])],
            complete=complete is not False,
        )
    if isinstance(value, list):
        return Records(
            records=[_record(item, index, label) for index, item in enumerate(value)],
            complete=True,
        )
    raise TransformInputInvalid(f"{label} reads records, not {type(value).__name__}")


def _record(item: object, index: int, label: str) -> Record:
    if (
        isinstance(item, Mapping)
        and isinstance(item.get("fields"), Mapping)
        and isinstance(item.get("record"), str)
    ):
        return dict(item)
    if isinstance(item, Mapping):
        locator = f"/{index}"
        return {
            "record": locator,
            "key": {},
            "fields": {
                str(name): _present(member, f"{locator}/{name}")
                for name, member in item.items()
                if member is not None
            },
        }
    raise TransformInputInvalid(f"{label} item {index} is not a record")


def _results(record: Record) -> dict[str, FieldPresent | FieldAbsent | FieldInvalid]:
    found: dict[str, FieldPresent | FieldAbsent | FieldInvalid] = {}
    for name, result in record["fields"].items():
        tag = result.get("tag") if isinstance(result, Mapping) else None
        if tag == "present":
            found[name] = FieldPresent(value=result.get("value"), at=str(result.get("at", "")))
        elif tag == "absent":
            found[name] = FieldAbsent()
        else:
            problem = result.get("problem", {}) if isinstance(result, Mapping) else {}
            found[name] = FieldInvalid(
                problem=Problem.model_validate(
                    {"code": problem.get("code", "type_mismatch"), **_problem_extra(problem)}
                )
            )
    return found


def _problem_extra(problem: Mapping[str, Any]) -> dict[str, Any]:
    return {key: problem[key] for key in ("field", "lexeme") if problem.get(key) is not None}


def _truth(predicate: Predicate, record: Record, parameters: Mapping[str, object] | None) -> Truth:
    return evaluate_predicate(predicate, fields=_results(record), parameters=parameters or {})


def _wire(result: FieldPresent | FieldAbsent | FieldInvalid) -> dict[str, object]:
    if isinstance(result, FieldPresent):
        from cruxible_core.procedures.extract import wire_value

        return {
            "tag": "present",
            "value": normalize_canonical(wire_value(result.value)),
            "at": result.at,
        }
    return result.model_dump(mode="json")


def _value(record: Record, field: str) -> tuple[str, object]:
    """``("present", value)``, ``("absent", None)`` or ``("unknown", None)``."""

    result = record["fields"].get(field)
    if not isinstance(result, Mapping) or result.get("tag") == "absent":
        return ("absent", None)
    if result.get("tag") == "present":
        return ("present", result.get("value"))
    return ("unknown", None)


def records_value(records: Sequence[Record], complete: bool) -> dict[str, object]:
    """A closed transform's list value: records, their present rows, completeness."""

    return {
        "records": list(records),
        "rows": [
            {
                name: result["value"]
                for name, result in record["fields"].items()
                if isinstance(result, Mapping) and result.get("tag") == "present"
            }
            for record in records
        ],
        "complete": complete,
        "count": len(records),
    }


def scalar_value(result: dict[str, object], complete: bool) -> dict[str, object]:
    """A closed transform's one value: its field result, plain value, completeness."""

    return {
        "value": result,
        "result": result.get("value") if result.get("tag") == "present" else None,
        "complete": complete,
    }


def apply_closed_transform(
    kind: str,
    spec: object,
    resolve: Callable[[object], object],
    *,
    parameters: Mapping[str, object] | None = None,
    enums: EnumResolver | None = None,
    max_items: int | None = None,
) -> tuple[CanonicalValue, bool]:
    """``(value, complete)`` of one closed transform; ``resolve`` reads a reference."""

    if isinstance(spec, TransformShapeSpec):
        source = as_records(resolve(spec.input), label="shape input")
        casts = {name: field for name, field in spec.fields.items()}
        shaped: list[Record] = []
        for record in source.records:
            results = _results(record)
            fields: dict[str, object] = {}
            for name, field in casts.items():
                if field.from_ is None:
                    fields[name] = _present(field.value, f"{record['record']}/{name}")
                    continue
                _item, plane, member = field.from_.split(".")
                if plane == "key":
                    key = record.get("key", {})
                    result: FieldPresent | FieldAbsent | FieldInvalid = (
                        FieldPresent(value=key[member], at=f"{record['record']}/key/{member}")
                        if member in key
                        else FieldAbsent()
                    )
                else:
                    result = results.get(member, FieldAbsent())
                if field.as_ is not None:
                    result = cast_value(
                        result, parse_type(field.as_), field=name, enum_members=enums
                    )
                fields[name] = _wire(result)
            shaped.append(
                {"record": record["record"], "key": record.get("key", {}), "fields": fields}
            )
        _bound(shaped, max_items)
        return normalize_canonical(records_value(shaped, source.complete)), source.complete
    if isinstance(spec, TransformFilterSpec):
        source = as_records(resolve(spec.input), label="filter input")
        kept: list[Record] = []
        unknown = False
        for record in source.records:
            truth = _truth(spec.where, record, parameters)  # type: ignore[arg-type]
            if truth == "true":
                kept.append(record)
            elif truth == "unknown":
                unknown = True
        _bound(kept, max_items)
        complete = source.complete and not unknown
        return normalize_canonical(records_value(kept, complete)), complete
    if isinstance(spec, TransformDedupeSpec):
        source = as_records(resolve(spec.input), label="dedupe input")
        seen: set[bytes] = set()
        out: list[Record] = []
        unknown = False
        for record in source.records:
            values = [_value(record, name) for name in spec.keys]
            if any(state == "unknown" for state, _v in values):
                unknown = True
                continue
            identity = canonical_bytes(normalize_canonical([list(item) for item in values]))
            if identity in seen:
                continue
            seen.add(identity)
            out.append(record)
        complete = source.complete and not unknown
        return normalize_canonical(records_value(out, complete)), complete
    if isinstance(spec, TransformUnionSpec):
        parts = [as_records(resolve(item), label="union input") for item in spec.inputs]
        merged = [record for part in parts for record in part.records]
        _bound(merged, max_items)
        complete = all(part.complete for part in parts)
        return normalize_canonical(records_value(merged, complete)), complete
    if isinstance(spec, TransformJoinSpec):
        left = as_records(resolve(spec.left), label="join left")
        right = as_records(resolve(spec.right), label="join right")
        unknown = False
        index: dict[bytes, list[Record]] = {}
        for record in right.records:
            state, value = _value(record, spec.right_field)
            if state == "unknown":
                unknown = True
            elif state == "present":
                index.setdefault(canonical_bytes(normalize_canonical(value)), []).append(record)
        joined: list[Record] = []
        for record in left.records:
            state, value = _value(record, spec.left_field)
            if state == "unknown":
                unknown = True
                continue
            matches = (
                []
                if state == "absent"
                else index.get(canonical_bytes(normalize_canonical(value)), [])
            )
            if spec.mode == "anti":
                if not matches:
                    joined.append(record)
                continue
            for match in matches:
                fields = dict(record["fields"])
                for name, source_field in spec.right_fields.items():
                    fields[name] = match["fields"].get(source_field, {"tag": "absent"})
                joined.append(
                    {"record": record["record"], "key": record.get("key", {}), "fields": fields}
                )
        _bound(joined, max_items)
        complete = left.complete and right.complete and not unknown
        return normalize_canonical(records_value(joined, complete)), complete
    if isinstance(spec, TransformAggregateSpec):
        source = as_records(resolve(spec.input), label="aggregate input")
        return _aggregate(spec, source, parameters)
    if isinstance(spec, TransformDeriveSpec):
        return _derive(spec, resolve, parameters)
    raise ExecutionError(f"unsupported closed transform {kind!r}")  # pragma: no cover


def _bound(records: Sequence[Record], max_items: int | None) -> None:
    if max_items is not None and len(records) > max_items:
        raise TransformItemBudgetExceeded(limit=max_items, observed=len(records))


class TransformItemBudgetExceeded(ExecutionError):
    def __init__(self, *, limit: int, observed: int) -> None:
        self.limit = limit
        self.observed = observed
        super().__init__(f"a closed transform produced {observed} records, over {limit}")


def _aggregate(
    spec: TransformAggregateSpec, source: Records, parameters: Mapping[str, object] | None
) -> tuple[CanonicalValue, bool]:
    complete = source.complete
    if spec.op == "count":
        result = _present(len(source.records), "/count")
        return normalize_canonical(scalar_value(result, complete)), complete
    if spec.op in {"any", "all"}:
        truths = [_truth(spec.where, record, parameters) for record in source.records]  # type: ignore[arg-type]
        decisive, other = ("true", "false") if spec.op == "any" else ("false", "true")
        if decisive in truths:
            verdict: str = decisive
        elif "unknown" in truths:
            verdict = "unknown"
        else:
            verdict = other
        if verdict == "unknown":
            unknown_result: dict[str, object] = {
                "tag": "invalid",
                "problem": {"code": "type_mismatch", "field": spec.op},
            }
            return normalize_canonical(scalar_value(unknown_result, False)), False
        result = _present(verdict == "true", f"/{spec.op}")
        return normalize_canonical(scalar_value(result, complete)), complete
    assert spec.field is not None
    best: tuple[Any, Record] | None = None
    unknown = False
    for record in source.records:
        state, value = _value(record, spec.field)
        if state == "unknown":
            unknown = True
            continue
        if state != "present":
            continue
        key = _ordering(value)
        if best is None or (key > best[0] if spec.op == "max" else key < best[0]):
            best = (key, record)
    complete = complete and not unknown
    if spec.op == "first_by":
        chosen = [] if best is None else [best[1]]
        return normalize_canonical(records_value(chosen, complete)), complete
    if best is None:
        absent: dict[str, object] = {"tag": "absent"}
        return normalize_canonical(scalar_value(absent, complete)), complete
    field = best[1]["fields"][spec.field]
    return normalize_canonical(scalar_value(dict(field), complete)), complete


def _ordering(value: object) -> Any:
    from decimal import Decimal, InvalidOperation

    if isinstance(value, bool):
        return (0, value)
    if isinstance(value, int):
        return (1, Decimal(value))
    if isinstance(value, str):
        try:
            return (1, Decimal(value))
        except InvalidOperation:
            return (2, value)
    return (3, canonical_bytes(normalize_canonical(value)))


def _derive(
    spec: TransformDeriveSpec,
    resolve: Callable[[object], object],
    parameters: Mapping[str, object] | None,
) -> tuple[CanonicalValue, bool]:
    fields: dict[str, object] = {}
    complete = True
    for name, reference in spec.input.items():
        value = resolve(reference)
        if isinstance(value, Mapping) and isinstance(value.get("value"), Mapping):
            # A closed transform's one value (an aggregate, another derive).
            fields[name] = dict(value["value"])
            complete = complete and value.get("complete") is not False
        elif isinstance(value, Mapping) and value.get("tag") in {"present", "absent", "invalid"}:
            fields[name] = dict(value)
        elif value is None:
            fields[name] = {"tag": "absent"}
        else:
            fields[name] = _present(value, f"/{name}")
    record: Record = {"record": "/", "key": {}, "fields": fields}
    for case in spec.cases:
        truth = _truth(case.when, record, parameters)  # type: ignore[arg-type]
        if truth == "true":
            return normalize_canonical(
                scalar_value(_present(case.value, "/derive"), complete)
            ), complete
        if truth == "unknown":
            unknown: dict[str, object] = {
                "tag": "invalid",
                "problem": {"code": "type_mismatch", "field": "derive"},
            }
            return normalize_canonical(scalar_value(unknown, False)), False
    return normalize_canonical(scalar_value(_present(spec.else_, "/derive"), complete)), complete


__all__ = [
    "Records",
    "TransformInputInvalid",
    "TransformItemBudgetExceeded",
    "apply_closed_transform",
    "as_records",
    "records_value",
    "scalar_value",
]
