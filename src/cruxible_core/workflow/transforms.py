"""Built-in workflow steps for transforming item collections."""

from __future__ import annotations

import json
import math
from typing import Any

from cruxible_core.config.schema import (
    DedupeItemsSpec,
    FilterItemsSpec,
    JoinItemsSpec,
    ShapeItemsSpec,
)
from cruxible_core.errors import QueryExecutionError
from cruxible_core.predicate import evaluate_typed_comparison
from cruxible_core.primitives import canonical_json
from cruxible_core.query.filters import matches_exact_filter
from cruxible_core.workflow.refs import resolve_value
from cruxible_core.workflow.step_helpers import (
    MAX_DUPLICATE_EXAMPLES,
    attach_source_metadata,
    carry_query_result_index,
    merge_read_metadata,
    resolve_step_items,
    source_read_metadata,
)


def shape_items(
    step_id: str,
    spec: ShapeItemsSpec,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> dict[str, Any]:
    """Project, rename, enrich, cast, and validate item-shaped workflow data.

    The step resolves ``spec.items`` to a list, requires every item to be a
    mapping, applies top-level renames, then builds each output row. When
    ``include_input`` is true the renamed input row is retained; otherwise the
    output starts with only renamed fields. Additional ``fields`` are resolved
    with ``$item`` bound to the renamed row, casts run after field resolution,
    and required fields either raise or drop the row depending on
    ``on_missing_required``.

    Args:
        step_id: Workflow step id used in execution errors.
        spec: Parsed ``shape_items`` step configuration.
        input_payload: Workflow input payload available to refs.
        step_outputs: Outputs produced by earlier workflow steps.

    Returns:
        A mapping with shaped ``items`` plus input/output/drop counts and a
        bounded sample of dropped-row examples.

    Raises:
        QueryExecutionError: If an input item is not a mapping, a rename
            collides, a cast fails, or required fields are missing in ``error``
            mode.
    """
    items = resolve_step_items(spec.items, input_payload, step_outputs)
    source_metadata = source_read_metadata(spec.items, step_outputs)
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
                if len(drop_examples) < MAX_DUPLICATE_EXAMPLES:
                    drop_examples.append({"index": index, "missing": missing})
                continue
            raise QueryExecutionError(
                f"Workflow step '{step_id}' shape_items missing required field(s): "
                f"{', '.join(missing)}"
            )
        shaped = carry_query_result_index(source_row, shaped)
        output_items.append(shaped)

    return attach_source_metadata(
        {
            "items": output_items,
            "input_count": len(items),
            "output_count": len(output_items),
            "dropped_count": dropped_count,
            "drop_examples": drop_examples,
        },
        source_metadata,
    )


def join_items(
    step_id: str,
    spec: JoinItemsSpec,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> dict[str, Any]:
    """Inner-join two item collections and project joined rows.

    The step resolves left and right collections, indexes right-side rows by the
    canonical JSON form of ``right_key``, and matches left rows by the canonical
    JSON form of ``left_key``. Joined output fields are resolved with ``$item``
    bound to a payload containing ``left``, ``right``, and ``join_key``.
    Right-side rows with null keys are counted as skipped; left-side rows with
    null keys simply produce no output rows.

    Args:
        step_id: Workflow step id used in execution errors.
        spec: Parsed ``join_items`` step configuration.
        input_payload: Workflow input payload available to refs.
        step_outputs: Outputs produced by earlier workflow steps.

    Returns:
        A mapping with joined ``items`` plus left/right/skipped/matched/output
        counts.

    Raises:
        QueryExecutionError: If any left or right item is not a mapping, or if a
            referenced value cannot be resolved.
    """
    left_items = resolve_step_items(spec.left_items, input_payload, step_outputs)
    right_items = resolve_step_items(spec.right_items, input_payload, step_outputs)
    left_source_metadata = source_read_metadata(spec.left_items, step_outputs)
    right_source_metadata = source_read_metadata(spec.right_items, step_outputs)
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

    output = {
        "items": output_items,
        "left_count": len(left_items),
        "right_count": len(right_items),
        "skipped_right_count": skipped_right_count,
        "matched_left_count": matched_left_count,
        "output_count": len(output_items),
    }
    if left_source_metadata:
        output["left_source_metadata"] = left_source_metadata
    if right_source_metadata:
        output["right_source_metadata"] = right_source_metadata
    return attach_source_metadata(
        output,
        merge_read_metadata(left_source_metadata, right_source_metadata),
    )


def filter_items(
    step_id: str,
    spec: FilterItemsSpec,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> dict[str, Any]:
    """Filter an item collection using exact-match filters and comparisons.

    The ``where`` mapping is resolved once before iteration and supports only
    ``$input`` references, which keeps it config/input scoped rather than
    item-scoped. Each item must be a mapping, first passes the exact ``where``
    filter, then passes every comparison with ``$item`` bound to that row.
    Matching output preserves the original item objects.

    Args:
        step_id: Workflow step id used in execution errors.
        spec: Parsed ``filter_items`` step configuration.
        input_payload: Workflow input payload available to refs.
        step_outputs: Outputs produced by earlier workflow steps.

    Returns:
        A mapping with filtered ``items`` plus input/output/filtered counts.

    Raises:
        QueryExecutionError: If an item is not a mapping, ``where`` resolves to
            a non-mapping, ``where`` uses a disallowed reference, or a comparison
            operator is unsupported.
    """
    items = resolve_step_items(spec.items, input_payload, step_outputs)
    source_metadata = source_read_metadata(spec.items, step_outputs)
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
                if not evaluate_typed_comparison(
                    left,
                    comparison.op,
                    right,
                    value_type=comparison.value_type,
                ):
                    matched = False
                    break
            except ValueError as exc:
                raise QueryExecutionError(
                    f"Workflow step '{step_id}' filter_items has unsupported "
                    f"comparison op '{comparison.op}'"
                ) from exc
        if matched:
            output_items.append(item)

    return attach_source_metadata(
        {
            "items": output_items,
            "input_count": len(items),
            "output_count": len(output_items),
            "filtered_count": len(items) - len(output_items),
        },
        source_metadata,
    )


def dedupe_items(
    step_id: str,
    spec: DedupeItemsSpec,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> dict[str, Any]:
    """Deduplicate an item collection by resolved key values.

    Each item must be a mapping. The dedupe key is the canonical JSON form of
    the configured key values, allowing composite keys and stable comparison of
    structured values. ``first`` keeps the first item for each key, ``last``
    keeps the last, and ``max``/``min`` compare a resolved rank value. Missing
    ``$item`` rank refs are treated as absent rank values rather than hard
    errors, so ranked strategies can tolerate sparse inputs.

    Args:
        step_id: Workflow step id used in execution errors.
        spec: Parsed ``dedupe_items`` step configuration.
        input_payload: Workflow input payload available to refs.
        step_outputs: Outputs produced by earlier workflow steps.

    Returns:
        A mapping with deduped ``items`` plus input/output/duplicate counts and
        a bounded sample of duplicate examples.

    Raises:
        QueryExecutionError: If an item is not a mapping, a key/rank reference
            cannot be resolved, or ranked values are incomparable.
    """
    items = resolve_step_items(spec.items, input_payload, step_outputs)
    source_metadata = source_read_metadata(spec.items, step_outputs)
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
        if len(duplicate_examples) < MAX_DUPLICATE_EXAMPLES:
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
    return attach_source_metadata(
        {
            "items": output_items,
            "input_count": len(items),
            "output_count": len(output_items),
            "duplicate_count": duplicate_count,
            "duplicate_examples": duplicate_examples,
        },
        source_metadata,
    )


def _ensure_item_mapping(
    step_id: str,
    step_kind: str,
    item: Any,
) -> dict[str, Any]:
    """Return ``item`` as a mapping or raise a step-specific execution error."""
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
    """Apply top-level shape renames while rejecting target collisions."""
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
    """Cast one shaped field according to the configured shape cast type."""
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
    """Cast a value to int without accepting bools or non-integer strings."""
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
    """Cast a value to a finite float without accepting bools."""
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
    """Cast bool-like values accepted by shape_items."""
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
    """Return dict/list JSON values or parse them from a JSON string."""
    if isinstance(value, dict | list):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict | list):
            return parsed
    raise ValueError("invalid json")


def _shape_required_value_missing(row: dict[str, Any], field_name: str) -> bool:
    """Return whether a required shaped field is absent, null, or empty."""
    return field_name not in row or row[field_name] is None or row[field_name] == ""


def _resolve_filter_where(
    step_id: str,
    where: dict[str, Any],
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> dict[str, Any]:
    """Resolve a filter_items ``where`` mapping using only input-scoped refs."""
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
    """Resolve one ``where`` value while forbidding item/step-scoped refs."""
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
    """Resolve a dedupe rank, treating missing ``$item`` rank refs as absent."""
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
    """Return whether a ranked dedupe candidate should replace the current item."""
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
