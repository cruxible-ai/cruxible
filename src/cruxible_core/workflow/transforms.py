"""Built-in workflow steps for transforming item collections."""

from __future__ import annotations

import json
import math
from typing import Any

from cruxible_core.canonical_json import canonical_json
from cruxible_core.config.schema import (
    DedupeItemsSpec,
    FilterItemsSpec,
    JoinItemsSpec,
    ShapeItemsSpec,
)
from cruxible_core.errors import QueryExecutionError
from cruxible_core.predicate import evaluate_comparison, normalize_comparison_op
from cruxible_core.query.filters import matches_exact_filter
from cruxible_core.workflow.refs import resolve_value
from cruxible_core.workflow.step_helpers import (
    _MAX_DUPLICATE_EXAMPLES,
    _resolve_step_items,
)


def _shape_items(
    step_id: str,
    spec: ShapeItemsSpec,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> dict[str, Any]:
    items = _resolve_step_items(spec.items, input_payload, step_outputs)
    output_items: list[dict[str, Any]] = []
    dropped_count = 0
    drop_examples: list[dict[str, Any]] = []

    for index, item in enumerate(items):
        source_row = _ensure_item_mapping(step_id, "shape_items", item)
        renamed_row = _apply_shape_rename(step_id, source_row, spec.rename)
        shaped = (
            dict(renamed_row)
            if spec.include_input
            else {
                target: renamed_row[target]
                for source, target in spec.rename.items()
                if source in source_row and target in renamed_row
            }
        )
        for field_name, template in spec.fields.items():
            shaped[field_name] = resolve_value(
                template,
                input_payload,
                step_outputs,
                item_payload=renamed_row,
                allow_item=True,
            )
        for field_name, cast_type in spec.casts.items():
            if field_name not in shaped or shaped[field_name] is None:
                continue
            shaped[field_name] = _cast_shape_value(
                step_id,
                field_name,
                shaped[field_name],
                cast_type,
            )
        missing = [
            field_name
            for field_name in spec.required
            if _shape_required_value_missing(shaped, field_name)
        ]
        if missing:
            if spec.on_missing_required == "drop":
                dropped_count += 1
                if len(drop_examples) < _MAX_DUPLICATE_EXAMPLES:
                    drop_examples.append({"index": index, "missing": missing})
                continue
            raise QueryExecutionError(
                f"Workflow step '{step_id}' shape_items missing required field(s): "
                f"{', '.join(missing)}"
            )
        output_items.append(shaped)

    return {
        "items": output_items,
        "input_count": len(items),
        "output_count": len(output_items),
        "dropped_count": dropped_count,
        "drop_examples": drop_examples,
    }


def _join_items(
    step_id: str,
    spec: JoinItemsSpec,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> dict[str, Any]:
    left_items = _resolve_step_items(spec.left_items, input_payload, step_outputs)
    right_items = _resolve_step_items(spec.right_items, input_payload, step_outputs)
    right_index: dict[str, list[dict[str, Any]]] = {}
    skipped_right_count = 0

    for right in right_items:
        right_row = _ensure_item_mapping(step_id, "join_items right_items", right)
        right_key = resolve_value(
            spec.right_key,
            input_payload,
            step_outputs,
            item_payload=right_row,
            allow_item=True,
        )
        if right_key is None:
            skipped_right_count += 1
            continue
        right_index.setdefault(canonical_json(right_key), []).append(right_row)

    output_items: list[dict[str, Any]] = []
    matched_left_count = 0
    for left in left_items:
        left_row = _ensure_item_mapping(step_id, "join_items left_items", left)
        left_key = resolve_value(
            spec.left_key,
            input_payload,
            step_outputs,
            item_payload=left_row,
            allow_item=True,
        )
        if left_key is None:
            continue
        matches = right_index.get(canonical_json(left_key), [])
        if matches:
            matched_left_count += 1
        for right_row in matches:
            join_payload = {
                "left": left_row,
                "right": right_row,
                "join_key": left_key,
            }
            output_items.append(
                {
                    field_name: resolve_value(
                        template,
                        input_payload,
                        step_outputs,
                        item_payload=join_payload,
                        allow_item=True,
                    )
                    for field_name, template in spec.fields.items()
                }
            )

    return {
        "items": output_items,
        "left_count": len(left_items),
        "right_count": len(right_items),
        "skipped_right_count": skipped_right_count,
        "matched_left_count": matched_left_count,
        "output_count": len(output_items),
    }


def _filter_items(
    step_id: str,
    spec: FilterItemsSpec,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> dict[str, Any]:
    items = _resolve_step_items(spec.items, input_payload, step_outputs)
    resolved_where = _resolve_filter_where(
        step_id,
        spec.where,
        input_payload,
        step_outputs,
    )
    output_items: list[Any] = []

    for item in items:
        item_row = _ensure_item_mapping(step_id, "filter_items", item)
        if resolved_where and not matches_exact_filter(item_row, resolved_where):
            continue
        matched = True
        for comparison in spec.comparisons:
            left = resolve_value(
                comparison.left,
                input_payload,
                step_outputs,
                item_payload=item_row,
                allow_item=True,
            )
            right = resolve_value(
                comparison.right,
                input_payload,
                step_outputs,
                item_payload=item_row,
                allow_item=True,
            )
            try:
                op = normalize_comparison_op(comparison.op)
                if not evaluate_comparison(left, op, right):
                    matched = False
                    break
            except ValueError as exc:
                raise QueryExecutionError(
                    f"Workflow step '{step_id}' filter_items has unsupported "
                    f"comparison op '{comparison.op}'"
                ) from exc
        if matched:
            output_items.append(item)

    return {
        "items": output_items,
        "input_count": len(items),
        "output_count": len(output_items),
        "filtered_count": len(items) - len(output_items),
    }


def _dedupe_items(
    step_id: str,
    spec: DedupeItemsSpec,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> dict[str, Any]:
    items = _resolve_step_items(spec.items, input_payload, step_outputs)
    selected: dict[str, dict[str, Any]] = {}
    duplicate_count = 0
    duplicate_examples: list[dict[str, Any]] = []

    for index, item in enumerate(items):
        item_row = _ensure_item_mapping(step_id, "dedupe_items", item)
        key_values = [
            resolve_value(
                key_template,
                input_payload,
                step_outputs,
                item_payload=item_row,
                allow_item=True,
            )
            for key_template in spec.keys
        ]
        key_hash = canonical_json(key_values)
        rank_value = (
            _resolve_optional_rank(spec.rank, input_payload, step_outputs, item_row)
            if spec.strategy in {"max", "min"}
            else None
        )
        rank_present = rank_value is not None

        if key_hash not in selected:
            selected[key_hash] = {
                "item": item,
                "index": index,
                "key": key_values,
                "rank": rank_value,
                "rank_present": rank_present,
            }
            continue

        duplicate_count += 1
        existing = selected[key_hash]
        if len(duplicate_examples) < _MAX_DUPLICATE_EXAMPLES:
            duplicate_examples.append(
                {
                    "key": key_values,
                    "kept_index": existing["index"],
                    "duplicate_index": index,
                }
            )

        if spec.strategy == "first":
            continue
        if spec.strategy == "last":
            selected[key_hash] = {
                "item": item,
                "index": index,
                "key": key_values,
                "rank": rank_value,
                "rank_present": rank_present,
            }
            continue

        if _should_replace_ranked_item(
            step_id,
            spec.strategy,
            existing["rank"],
            bool(existing["rank_present"]),
            rank_value,
            rank_present,
        ):
            selected[key_hash] = {
                "item": item,
                "index": index,
                "key": key_values,
                "rank": rank_value,
                "rank_present": rank_present,
            }

    output_items = [entry["item"] for entry in selected.values()]
    return {
        "items": output_items,
        "input_count": len(items),
        "output_count": len(output_items),
        "duplicate_count": duplicate_count,
        "duplicate_examples": duplicate_examples,
    }


def _ensure_item_mapping(
    step_id: str,
    step_kind: str,
    item: Any,
) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise QueryExecutionError(
            f"Workflow step '{step_id}' {step_kind} items must contain mappings"
        )
    return item


def _apply_shape_rename(
    step_id: str,
    source_row: dict[str, Any],
    rename: dict[str, str],
) -> dict[str, Any]:
    renamed_row = dict(source_row)
    for source, target in rename.items():
        if source not in source_row:
            continue
        if target != source and target in source_row:
            raise QueryExecutionError(
                f"Workflow step '{step_id}' shape_items rename collision: '{target}' already exists"
            )
        value = renamed_row.pop(source) if target != source else renamed_row[source]
        renamed_row[target] = value
    return renamed_row


def _cast_shape_value(
    step_id: str,
    field_name: str,
    value: Any,
    cast_type: str,
) -> Any:
    try:
        if cast_type == "str":
            return str(value)
        if cast_type == "int":
            return _cast_shape_int(value)
        if cast_type == "float":
            return _cast_shape_float(value)
        if cast_type == "bool":
            return _cast_shape_bool(value)
        if cast_type == "json":
            return _cast_shape_json(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise QueryExecutionError(
            f"Workflow step '{step_id}' shape_items could not cast field "
            f"'{field_name}' to {cast_type}"
        ) from exc
    raise QueryExecutionError(
        f"Workflow step '{step_id}' shape_items has unsupported cast type '{cast_type}'"
    )


def _cast_shape_int(value: Any) -> int:
    if isinstance(value, bool):
        raise TypeError("bool is not int")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    raise ValueError("invalid int")


def _cast_shape_float(value: Any) -> float:
    if isinstance(value, bool):
        raise TypeError("bool is not float")
    if isinstance(value, int | float):
        numeric = float(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError("empty string")
        numeric = float(stripped)
    else:
        raise TypeError("invalid float")
    if not math.isfinite(numeric):
        raise ValueError("non-finite float")
    return numeric


def _cast_shape_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
    raise ValueError("invalid bool")


def _cast_shape_json(value: Any) -> dict[str, Any] | list[Any]:
    if isinstance(value, dict | list):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict | list):
            return parsed
    raise ValueError("invalid json")


def _shape_required_value_missing(row: dict[str, Any], field_name: str) -> bool:
    return field_name not in row or row[field_name] is None or row[field_name] == ""


def _resolve_filter_where(
    step_id: str,
    where: dict[str, Any],
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> dict[str, Any]:
    resolved = _resolve_filter_where_value(step_id, where, input_payload, step_outputs)
    if not isinstance(resolved, dict):
        raise QueryExecutionError(
            f"Workflow step '{step_id}' filter_items where must resolve to a mapping"
        )
    return resolved


def _resolve_filter_where_value(
    step_id: str,
    value: Any,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        if value == "$input" or value.startswith("$input."):
            return resolve_value(value, input_payload, step_outputs)
        raise QueryExecutionError(
            f"Workflow step '{step_id}' filter_items where reference '{value}' must use $input only"
        )
    if isinstance(value, dict):
        return {
            key: _resolve_filter_where_value(step_id, item, input_payload, step_outputs)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _resolve_filter_where_value(step_id, item, input_payload, step_outputs)
            for item in value
        ]
    return value


def _resolve_optional_rank(
    rank_template: Any,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
    item_payload: dict[str, Any],
) -> Any:
    try:
        return resolve_value(
            rank_template,
            input_payload,
            step_outputs,
            item_payload=item_payload,
            allow_item=True,
        )
    except QueryExecutionError:
        if isinstance(rank_template, str) and rank_template.startswith("$item."):
            return None
        raise


def _should_replace_ranked_item(
    step_id: str,
    strategy: str,
    existing_rank: Any,
    existing_rank_present: bool,
    new_rank: Any,
    new_rank_present: bool,
) -> bool:
    if not new_rank_present:
        return False
    if not existing_rank_present:
        return True
    try:
        if strategy == "max":
            return bool(new_rank > existing_rank)
        return bool(new_rank < existing_rank)
    except TypeError as exc:
        raise QueryExecutionError(
            f"Workflow step '{step_id}' dedupe_items rank values are incomparable"
        ) from exc
