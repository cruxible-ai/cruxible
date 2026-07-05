"""Shared helpers for built-in workflow steps."""

from __future__ import annotations

from typing import Any

from cruxible_core.errors import QueryExecutionError
from cruxible_core.workflow.refs import resolve_value

MAX_DUPLICATE_EXAMPLES = 10
SOURCE_METADATA_KEY = "source_metadata"

READ_METADATA_KEYS = (
    "total_results",
    "returned_results",
    "limit",
    "truncated",
    "limit_truncated",
    "path_truncated",
    "truncation_reasons",
    "result_shape",
    "dedupe",
    "relationship_state",
    "policy_summary",
    "receipt_id",
    "query_receipt_ids",
    "max_paths",
    "max_paths_per_result",
    "total_path_count",
    "retained_path_count",
)

TRUNCATION_REASON_ORDER = (
    "limit",
    "max_paths",
    "max_paths_per_result",
)


def resolve_step_items(
    items_template: Any,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> list[Any]:
    items = resolve_value(items_template, input_payload, step_outputs)
    if not isinstance(items, list):
        raise QueryExecutionError("Built-in workflow step 'items' must resolve to a list")
    return items


def source_read_metadata(
    items_template: Any,
    step_outputs: dict[str, Any],
) -> dict[str, Any]:
    """Return read metadata for a collection sourced from a prior step."""
    source = _source_step_output(items_template, step_outputs)
    if source is None:
        return {}
    metadata = extract_read_metadata(source["output"])
    if not metadata:
        return {}
    metadata.setdefault("source_step", source["step"])
    metadata.setdefault("source_ref", items_template)
    metadata["input_ref"] = items_template
    return metadata


def attach_source_metadata(
    output: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Attach source read metadata when a transform derives from read output."""
    if metadata:
        output[SOURCE_METADATA_KEY] = metadata
    return output


def attach_query_result_index(row: dict[str, Any], index: int) -> dict[str, Any]:
    """Attach the original query receipt result index to a workflow row."""
    return WorkflowIndexedRow(row, query_result_index=index)


def attach_query_source_lineage(
    row: dict[str, Any],
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    """Attach internal query-source lineage without adding public payload keys."""
    if not sources:
        return row
    return WorkflowIndexedRow(
        row,
        query_result_index=query_result_index(row),
        query_sources=sources,
    )


def carry_query_result_index(source: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    """Preserve query result lineage through transforms that reshape one row."""
    index = query_result_index(source)
    sources = query_source_lineage(source)
    if index is None and not sources:
        return target
    return WorkflowIndexedRow(
        target,
        query_result_index=index,
        query_sources=sources,
    )


def query_result_index(row: Any) -> int | None:
    """Return a row's original query receipt index when it is known."""
    if isinstance(row, WorkflowIndexedRow):
        return row.query_result_index
    return None


def query_source_lineage(row: Any) -> list[dict[str, Any]]:
    """Return internal query-source lineage attached to a workflow row."""
    if isinstance(row, WorkflowIndexedRow):
        return [dict(source) for source in row.query_sources]
    return []


class WorkflowIndexedRow(dict[str, Any]):
    """Workflow row carrying internal lineage outside the public keyspace."""

    def __init__(
        self,
        row: dict[str, Any],
        *,
        query_result_index: int | None,
        query_sources: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(row)
        self.query_result_index = query_result_index
        self.query_sources = list(query_sources or [])


def extract_read_metadata(step_output: Any) -> dict[str, Any]:
    """Extract direct or propagated read metadata from a workflow step output."""
    if not isinstance(step_output, dict):
        return {}
    direct = {key: step_output[key] for key in READ_METADATA_KEYS if key in step_output}
    if direct:
        return direct
    source_metadata = step_output.get(SOURCE_METADATA_KEY)
    if isinstance(source_metadata, dict):
        return dict(source_metadata)
    return {}


def merge_read_metadata(*metadata_items: dict[str, Any]) -> dict[str, Any]:
    """Merge read truncation summaries from multiple transform inputs."""
    non_empty = [metadata for metadata in metadata_items if metadata]
    if not non_empty:
        return {}
    reasons = _ordered_truncation_reasons(
        reason
        for metadata in non_empty
        for reason in metadata.get("truncation_reasons", [])
        if isinstance(reason, str)
    )
    query_receipt_ids = _ordered_unique(
        receipt_id for metadata in non_empty for receipt_id in _metadata_query_receipt_ids(metadata)
    )
    merged = {
        "truncated": any(bool(metadata.get("truncated")) for metadata in non_empty),
        "limit_truncated": any(bool(metadata.get("limit_truncated")) for metadata in non_empty),
        "path_truncated": any(bool(metadata.get("path_truncated")) for metadata in non_empty),
        "truncation_reasons": reasons,
    }
    if query_receipt_ids:
        merged["query_receipt_ids"] = query_receipt_ids
        if len(query_receipt_ids) == 1:
            merged["receipt_id"] = query_receipt_ids[0]
    return merged


def source_read_metadata_from_template(
    template: Any,
    step_outputs: dict[str, Any],
) -> dict[str, Any]:
    """Merge read metadata from all step references used by a template."""
    return merge_read_metadata(
        *[
            metadata
            for ref in _iter_step_refs(template)
            if (metadata := source_read_metadata(ref, step_outputs))
        ]
    )


def _source_step_output(
    items_template: Any,
    step_outputs: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(items_template, str) or not items_template.startswith("$steps."):
        return None
    step_ref = items_template[len("$steps.") :]
    step_name, _, _ = step_ref.partition(".")
    if step_name not in step_outputs:
        return None
    return {"step": step_name, "output": step_outputs[step_name]}


def _metadata_query_receipt_ids(metadata: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    receipt_id = metadata.get("receipt_id")
    if isinstance(receipt_id, str):
        ids.append(receipt_id)
    query_receipt_ids = metadata.get("query_receipt_ids")
    if isinstance(query_receipt_ids, list):
        ids.extend(item for item in query_receipt_ids if isinstance(item, str))
    return ids


def _iter_step_refs(value: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(value, str):
        if value.startswith("$steps."):
            refs.append(value)
        return refs
    if isinstance(value, dict):
        for item in value.values():
            refs.extend(_iter_step_refs(item))
    elif isinstance(value, list):
        for item in value:
            refs.extend(_iter_step_refs(item))
    return refs


def _ordered_unique(values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _ordered_truncation_reasons(reasons: Any) -> list[str]:
    unique = set(reasons)
    ordered = [reason for reason in TRUNCATION_REASON_ORDER if reason in unique]
    ordered.extend(sorted(unique.difference(TRUNCATION_REASON_ORDER)))
    return ordered
