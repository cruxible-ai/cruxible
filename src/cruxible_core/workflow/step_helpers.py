"""Shared helpers for built-in workflow steps."""

from __future__ import annotations

from typing import Any

from cruxible_core.errors import QueryExecutionError
from cruxible_core.workflow.refs import resolve_value

MAX_DUPLICATE_EXAMPLES = 10


def resolve_step_items(
    items_template: Any,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> list[Any]:
    items = resolve_value(items_template, input_payload, step_outputs)
    if not isinstance(items, list):
        raise QueryExecutionError("Built-in workflow step 'items' must resolve to a list")
    return items
