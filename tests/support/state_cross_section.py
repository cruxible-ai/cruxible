"""Allowlist-based state cross-section and diff helpers for golden tests."""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.service.queries import service_evaluate_query_surface

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class CrossSectionLimits:
    """Per-section limits used to keep golden reports small and intentional."""

    entities_per_type: int = 50
    relationships_per_type: int = 50
    groups: int = 25
    group_members: int = 50
    feedback: int = 25
    outcomes: int = 25
    decisions: int = 25
    decision_events: int = 25
    receipts: int = 25
    traces: int = 25
    snapshots: int = 25


@dataclass(frozen=True)
class QueryCrossSectionSpec:
    """One explicitly selected query to include in a cross-section."""

    name: str
    params: Mapping[str, Any] = field(default_factory=dict)
    limit: int = 25
    include_receipt_summary: bool = False


@dataclass(frozen=True)
class StateCrossSectionSpec:
    """Allowlist for the instance state included in a cross-section."""

    entity_types: tuple[str, ...] = ()
    relationship_types: tuple[str, ...] = ()
    queries: tuple[QueryCrossSectionSpec, ...] = ()
    include_groups: bool = False
    include_feedback: bool = False
    include_outcomes: bool = False
    include_decisions: bool = False
    include_receipts: bool = False
    include_traces: bool = False
    include_snapshots: bool = False
    include_world: bool = False
    limits: CrossSectionLimits = field(default_factory=CrossSectionLimits)


class CrossSectionTokenRegistry:
    """Global token registry for one cross-section or before/after diff pair."""

    def __init__(self) -> None:
        self._tokens: dict[tuple[str, str], str] = {}
        self._counters: dict[str, int] = {}

    def token(self, kind: str, value: str) -> str:
        """Return a stable token for a generated value within this registry."""
        key = (kind, value)
        if key not in self._tokens:
            next_value = self._counters.get(kind, 0) + 1
            self._counters[kind] = next_value
            self._tokens[key] = f"<{kind}_{next_value}>"
        return self._tokens[key]


_TOKEN_ID_KEYS: dict[str, str] = {
    "receipt_id": "RECEIPT",
    "group_receipt_id": "RECEIPT",
    "source_workflow_receipt_id": "RECEIPT",
    "trace_id": "TRACE",
    "group_id": "GROUP",
    "pending_group_id": "GROUP",
    "resolution_id": "RESOLUTION",
    "feedback_id": "FEEDBACK",
    "outcome_id": "OUTCOME",
    "decision_record_id": "DECISION_RECORD",
    "decision_event_id": "DECISION_EVENT",
    "snapshot_id": "SNAPSHOT",
    "head_snapshot_id": "SNAPSHOT",
    "origin_snapshot_id": "SNAPSHOT",
    "parent_snapshot_id": "SNAPSHOT",
    "committed_snapshot_id": "SNAPSHOT",
    "pre_pull_snapshot_id": "SNAPSHOT",
}
_TOKEN_ID_LIST_KEYS: dict[str, str] = {
    "receipt_ids": "RECEIPT",
    "query_receipt_ids": "RECEIPT",
    "trace_ids": "TRACE",
    "source_trace_ids": "TRACE",
}
_TIMESTAMP_KEYS = {
    "created_at",
    "updated_at",
    "opened_at",
    "closed_at",
    "resolved_at",
    "finalized_at",
    "started_at",
    "finished_at",
    "generated_at",
    "last_modified_at",
    "published_at",
}
_DURATION_KEYS = {"duration_ms", "duration_s", "elapsed_ms", "elapsed_s"}
_DIGEST_KEYS = {
    "apply_digest",
    "config_digest",
    "graph_digest",
    "input_digest",
    "lock_digest",
    "manifest_digest",
    "output_digest",
}
_PATH_KEY_RE = re.compile(r"(^path$|_path$|_dir$|^root$|^root_dir$)")
_VOLATILE_PATH_MARKERS = ("/tmp/", "/private/tmp/", "/private/var/", "/var/folders/")
_GENERATED_ID_RE = re.compile(
    r"^(?:[A-Z]{2,6}-[0-9a-f]{12}|snap_[0-9a-f]{16}|"
    r"[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})$"
)
_GENERATED_PREFIX_KIND = {
    "RCP": "RECEIPT",
    "TRC": "TRACE",
    "GRP": "GROUP",
    "RES": "RESOLUTION",
    "FB": "FEEDBACK",
    "OUT": "OUTCOME",
    "DR": "DECISION_RECORD",
    "DE": "DECISION_EVENT",
}
_SEMANTIC_VALUE_KEYS = {
    "group_signature",
    "signature",
    "entity_id",
    "from_id",
    "to_id",
    "workflow",
    "workflow_name",
    "query_name",
    "name",
    "provider_name",
    "provider",
    "release_id",
    "world_id",
    "entity_type",
    "relationship_type",
    "from_type",
    "to_type",
    "decision_class",
    "question",
    "reason",
    "reason_code",
    "outcome_code",
    "status",
    "action",
}


def build_state_cross_section(
    instance: InstanceProtocol,
    spec: StateCrossSectionSpec,
    *,
    token_registry: CrossSectionTokenRegistry | None = None,
) -> JsonObject:
    """Build a normalized, allowlist-based report of selected instance state."""
    registry = token_registry or CrossSectionTokenRegistry()
    raw: JsonObject = {"version": 1}
    graph_section = _build_graph_section(instance, spec)
    if graph_section:
        raw["graph"] = graph_section
    if spec.include_world:
        raw["world"] = _build_world_section(instance)
    if spec.include_snapshots:
        raw["snapshots"] = _build_snapshots_section(instance, spec.limits.snapshots)
    if spec.include_groups:
        raw["groups"] = _build_groups_section(
            instance,
            spec.limits.groups,
            spec.limits.group_members,
        )
    if spec.include_feedback:
        raw["feedback"] = _build_feedback_section(instance, spec.limits.feedback)
    if spec.include_outcomes:
        raw["outcomes"] = _build_outcomes_section(instance, spec.limits.outcomes)
    if spec.include_decisions:
        raw["decisions"] = _build_decisions_section(
            instance,
            spec.limits.decisions,
            spec.limits.decision_events,
        )
    if spec.queries:
        raw["queries"] = _build_queries_section(instance, spec.queries)
    if spec.include_receipts:
        raw["receipts"] = _build_receipts_section(instance, spec.limits.receipts)
    if spec.include_traces:
        raw["traces"] = _build_traces_section(instance, spec.limits.traces)
    return _normalize_value(raw, registry=registry)


def diff_state(before: Mapping[str, Any], after: Mapping[str, Any]) -> JsonObject:
    """Return a deterministic diff between two normalized state cross-sections."""
    diff: JsonObject = {"version": 1, "summary": {}}
    graph_diff = _diff_graph(before.get("graph"), after.get("graph"), before, after)
    if graph_diff:
        diff["graph"] = graph_diff
        summary = diff["summary"]
        entities = graph_diff.get("entities", {})
        relationships = graph_diff.get("relationships", {})
        summary["entities_added"] = len(entities.get("added", []))
        summary["entities_removed"] = len(entities.get("removed", []))
        summary["entities_changed"] = len(entities.get("changed", []))
        summary["relationships_added"] = len(relationships.get("added", []))
        summary["relationships_removed"] = len(relationships.get("removed", []))
        summary["relationships_changed"] = len(relationships.get("changed", []))
    for section, id_key in (
        ("groups", "group_id"),
        ("feedback", "feedback_id"),
        ("outcomes", "outcome_id"),
        ("decisions", "decision_record_id"),
        ("receipts", "receipt_id"),
        ("traces", "trace_id"),
        ("snapshots", "snapshot_id"),
    ):
        section_diff = _diff_item_list(
            before.get(section),
            after.get(section),
            id_key=id_key,
        )
        if section_diff:
            diff[section] = section_diff
            diff["summary"][f"{section}_added"] = len(section_diff.get("added", []))
            diff["summary"][f"{section}_removed"] = len(section_diff.get("removed", []))
            diff["summary"][f"{section}_changed"] = len(section_diff.get("changed", []))
    query_diff = _diff_item_list(
        before.get("queries"),
        after.get("queries"),
        key_func=_query_key,
    )
    if query_diff:
        diff["queries"] = query_diff
        diff["summary"]["queries_added"] = len(query_diff.get("added", []))
        diff["summary"]["queries_removed"] = len(query_diff.get("removed", []))
        diff["summary"]["queries_changed"] = len(query_diff.get("changed", []))
    if not diff["summary"]:
        diff["summary"] = {"changed": False}
    return diff


def assert_matches_golden(actual: Mapping[str, Any], golden_path: str | Path) -> None:
    """Assert that ``actual`` matches a canonical JSON golden file."""
    path = Path(golden_path)
    actual_text = _canonical_json(actual)
    if not path.exists():
        raise AssertionError(f"Golden file does not exist: {path}\n\nActual:\n{actual_text}")
    expected_text = path.read_text()
    if expected_text != actual_text:
        diff = "\n".join(
            difflib.unified_diff(
                expected_text.splitlines(),
                actual_text.splitlines(),
                fromfile=str(path),
                tofile="actual",
                lineterm="",
            )
        )
        raise AssertionError(f"Golden mismatch for {path}\n{diff}")


def normalize_cross_section_value(
    value: Any,
    *,
    token_registry: CrossSectionTokenRegistry | None = None,
) -> Any:
    """Normalize arbitrary JSON-like state using the cross-section token rules."""
    registry = token_registry or CrossSectionTokenRegistry()
    return _normalize_value(value, registry=registry)


def _build_graph_section(instance: InstanceProtocol, spec: StateCrossSectionSpec) -> JsonObject:
    graph = instance.load_graph()
    selected_entity_types = tuple(dict.fromkeys(spec.entity_types))
    selected_relationship_types = tuple(dict.fromkeys(spec.relationship_types))
    if not selected_entity_types and not selected_relationship_types:
        return {}

    section: JsonObject = {
        "counts": {
            "entities_total": graph.entity_count(),
            "relationships_total": graph.edge_count(),
        }
    }
    if selected_entity_types:
        section["entities"] = []
        section["counts"]["entities_by_type"] = {
            entity_type: graph.entity_count(entity_type) for entity_type in selected_entity_types
        }
        for entity_type in selected_entity_types:
            entities = sorted(
                graph.list_entities(entity_type),
                key=lambda item: (item.entity_type, item.entity_id),
            )
            for entity in entities[: spec.limits.entities_per_type]:
                section["entities"].append(
                    {
                        "entity_type": entity.entity_type,
                        "entity_id": entity.entity_id,
                        "properties": entity.properties,
                    }
                )
    if selected_relationship_types:
        section["relationships"] = []
        section["counts"]["relationships_by_type"] = {
            relationship_type: graph.edge_count(relationship_type)
            for relationship_type in selected_relationship_types
        }
        for relationship_type in selected_relationship_types:
            relationships = sorted(
                graph.iter_edges(relationship_type),
                key=_relationship_identity_tuple,
            )
            semantic_counts = _relationship_semantic_counts(relationships)
            for relationship in relationships[: spec.limits.relationships_per_type]:
                semantic_key = _relationship_semantic_tuple(relationship)
                section["relationships"].append(
                    _relationship_report(
                        relationship,
                        include_edge_key=semantic_counts[semantic_key] > 1,
                    )
                )
    return section


def _build_world_section(instance: InstanceProtocol) -> JsonObject:
    upstream = instance.get_upstream_metadata()
    return {
        "head_snapshot_id": instance.get_head_snapshot_id(),
        "upstream": _model_dump(upstream) if upstream is not None else None,
    }


def _build_snapshots_section(instance: InstanceProtocol, limit: int) -> list[JsonObject]:
    snapshots = instance.list_snapshots()[:limit]
    return sorted([_model_dump(snapshot) for snapshot in snapshots], key=_snapshot_sort_key)


def _build_groups_section(
    instance: InstanceProtocol,
    group_limit: int,
    member_limit: int,
) -> list[JsonObject]:
    store = instance.get_group_store()
    try:
        groups = store.list_groups(limit=group_limit)
        reports: list[JsonObject] = []
        for group in groups:
            report = _model_dump(group)
            members = store.get_members(group.group_id)
            report["members"] = [
                _model_dump(member)
                for member in sorted(
                    members,
                    key=lambda item: (
                        item.from_type,
                        item.from_id,
                        item.relationship_type,
                        item.to_type,
                        item.to_id,
                    ),
                )[:member_limit]
            ]
            if group.resolution_id is not None:
                resolution = store.get_resolution(group.resolution_id)
                if resolution is not None:
                    report["resolution"] = _model_dump(resolution)
            reports.append(report)
        return sorted(reports, key=_group_sort_key)
    finally:
        store.close()


def _build_feedback_section(instance: InstanceProtocol, limit: int) -> list[JsonObject]:
    store = instance.get_feedback_store()
    try:
        return sorted(
            [_model_dump(record) for record in store.list_feedback(limit=limit)],
            key=_feedback_sort_key,
        )
    finally:
        store.close()


def _build_outcomes_section(instance: InstanceProtocol, limit: int) -> list[JsonObject]:
    store = instance.get_feedback_store()
    try:
        return sorted(
            [_model_dump(record) for record in store.list_outcomes(limit=limit)],
            key=_outcome_sort_key,
        )
    finally:
        store.close()


def _build_decisions_section(
    instance: InstanceProtocol,
    record_limit: int,
    event_limit: int,
) -> list[JsonObject]:
    store = instance.get_decision_store()
    try:
        records = store.list_records(limit=record_limit)
        reports: list[JsonObject] = []
        for record in records:
            report = _model_dump(record)
            report["events"] = [
                _model_dump(event)
                for event in store.list_events(record.decision_record_id, limit=event_limit)
            ]
            reports.append(report)
        return sorted(reports, key=_decision_sort_key)
    finally:
        store.close()


def _build_receipts_section(instance: InstanceProtocol, limit: int) -> list[JsonObject]:
    store = instance.get_receipt_store()
    try:
        group_sort_keys = _receipt_group_sort_keys(instance)
        return sorted(
            store.list_receipts(limit=limit),
            key=lambda receipt: _receipt_sort_key(receipt, group_sort_keys),
        )
    finally:
        store.close()


def _build_traces_section(instance: InstanceProtocol, limit: int) -> list[JsonObject]:
    store = instance.get_receipt_store()
    try:
        return sorted(store.list_traces(limit=limit), key=_trace_sort_key)
    finally:
        store.close()


def _build_queries_section(
    instance: InstanceProtocol,
    queries: tuple[QueryCrossSectionSpec, ...],
) -> list[JsonObject]:
    reports: list[JsonObject] = []
    for query in queries:
        result = service_evaluate_query_surface(
            instance,
            query.name,
            dict(query.params),
            limit=query.limit,
        )
        report: JsonObject = {
            "name": query.name,
            "params": dict(query.params),
            "limit": query.limit,
            "total_results": result.total,
            "truncated": result.truncated,
            "results": sorted(
                [
                    {
                        "entity_type": item.entity_type,
                        "entity_id": item.entity_id,
                        "properties": item.properties,
                    }
                    for item in result.items
                ],
                key=lambda item: (item["entity_type"], item["entity_id"], _canonical_json(item)),
            ),
        }
        if query.include_receipt_summary:
            report["receipt"] = {
                "operation_type": result.receipt.operation_type if result.receipt else None,
                "query_name": result.receipt.query_name if result.receipt else None,
                "parameters": result.receipt.parameters if result.receipt else None,
            }
        reports.append(report)
    return sorted(reports, key=_query_sort_key)


def _normalize_value(
    value: Any,
    *,
    registry: CrossSectionTokenRegistry,
    key: str | None = None,
) -> Any:
    if isinstance(value, Mapping):
        return {
            str(item_key): _normalize_value(item_value, registry=registry, key=str(item_key))
            for item_key, item_value in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, list):
        if key in _TOKEN_ID_LIST_KEYS:
            kind = _TOKEN_ID_LIST_KEYS[key]
            return [registry.token(kind, str(item)) if item is not None else None for item in value]
        return [_normalize_value(item, registry=registry) for item in value]
    if isinstance(value, tuple):
        return [_normalize_value(item, registry=registry) for item in value]
    if key in _TOKEN_ID_KEYS and value is not None:
        return registry.token(_TOKEN_ID_KEYS[key], str(value))
    if (
        isinstance(value, str)
        and key not in _SEMANTIC_VALUE_KEYS
        and (kind := _generated_id_kind(value)) is not None
    ):
        return registry.token(kind, value)
    if key in _TIMESTAMP_KEYS and value is not None:
        return "<TIMESTAMP>"
    if key in _DURATION_KEYS and value is not None:
        return "<DURATION>"
    if key in _DIGEST_KEYS and value is not None:
        return "<DIGEST>"
    if isinstance(value, str) and key not in _SEMANTIC_VALUE_KEYS:
        embedded = _normalize_embedded_generated_id(value, registry)
        if embedded is not None:
            return embedded
    if (
        isinstance(value, str)
        and key is not None
        and _PATH_KEY_RE.search(key)
        and _is_volatile_path(value)
    ):
        return registry.token("PATH", value)
    if isinstance(value, str) and key not in _SEMANTIC_VALUE_KEYS:
        file_uri = _normalize_volatile_file_uri(value, registry)
        if file_uri is not None:
            return file_uri
    return value


def _diff_graph(
    before_graph: Any,
    after_graph: Any,
    before_cross_section: Mapping[str, Any],
    after_cross_section: Mapping[str, Any],
) -> JsonObject:
    if not isinstance(before_graph, Mapping) and not isinstance(after_graph, Mapping):
        return {}
    before_graph = before_graph if isinstance(before_graph, Mapping) else {}
    after_graph = after_graph if isinstance(after_graph, Mapping) else {}
    result: JsonObject = {}
    entity_diff = _diff_item_list(
        before_graph.get("entities"),
        after_graph.get("entities"),
        key_func=_entity_key,
        change_labeler=_changed_fields,
        annotator=lambda item, source: _annotate_ownership(
            item,
            source,
            before_cross_section if source == "before" else after_cross_section,
        ),
    )
    if entity_diff:
        result["entities"] = entity_diff
    relationship_diff = _diff_item_list(
        before_graph.get("relationships"),
        after_graph.get("relationships"),
        key_func=_relationship_key,
        change_labeler=_changed_fields,
        annotator=lambda item, source: _annotate_ownership(
            item,
            source,
            before_cross_section if source == "before" else after_cross_section,
        ),
    )
    if relationship_diff:
        result["relationships"] = relationship_diff
    count_changes = _changed_fields(
        before_graph.get("counts") if isinstance(before_graph.get("counts"), Mapping) else {},
        after_graph.get("counts") if isinstance(after_graph.get("counts"), Mapping) else {},
    )
    if count_changes:
        result["count_changes"] = count_changes
    return result


def _diff_item_list(
    before_items: Any,
    after_items: Any,
    *,
    id_key: str | None = None,
    key_func: Any | None = None,
    change_labeler: Any | None = None,
    annotator: Any | None = None,
) -> JsonObject:
    if not isinstance(before_items, list) and not isinstance(after_items, list):
        return {}
    before_items = before_items if isinstance(before_items, list) else []
    after_items = after_items if isinstance(after_items, list) else []
    if key_func is None:
        if id_key is None:
            raise ValueError("id_key or key_func is required")

        def key_func(item: Mapping[str, Any]) -> str:
            return str(item.get(id_key))

    before_index = _index_items(before_items, key_func)
    after_index = _index_items(after_items, key_func)
    before_keys = set(before_index)
    after_keys = set(after_index)
    result: JsonObject = {}
    added = [
        _diff_item(after_index[key], "after", annotator) for key in sorted(after_keys - before_keys)
    ]
    removed = [
        _diff_item(before_index[key], "before", annotator)
        for key in sorted(before_keys - after_keys)
    ]
    changed = []
    for key in sorted(before_keys & after_keys):
        before_item = before_index[key]
        after_item = after_index[key]
        if before_item != after_item:
            fields = (
                change_labeler(before_item, after_item)
                if change_labeler is not None
                else _changed_fields(before_item, after_item)
            )
            changed.append(
                {
                    "key": key,
                    "changed_fields": fields,
                    "before": _diff_item(before_item, "before", annotator),
                    "after": _diff_item(after_item, "after", annotator),
                }
            )
    if added:
        result["added"] = added
    if removed:
        result["removed"] = removed
    if changed:
        result["changed"] = changed
    return result


def _index_items(items: list[Any], key_func: Any) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    for item in items:
        if isinstance(item, Mapping):
            indexed[key_func(item)] = item
    return indexed


def _diff_item(
    item: Mapping[str, Any],
    source: str,
    annotator: Any | None,
) -> JsonObject:
    output = _compact_default_assertion_state(dict(item))
    if annotator is not None:
        output.update(annotator(item, source))
    return output


def _compact_default_assertion_state(value: Any) -> Any:
    if isinstance(value, list):
        return [_compact_default_assertion_state(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _compact_default_assertion_state(item) for key, item in value.items()}
    if result.get("group_override") is False:
        result.pop("group_override")
    return result


def _changed_fields(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    changed: list[str] = []
    keys = set(before) | set(after)
    for key in sorted(keys):
        before_value = before.get(key)
        after_value = after.get(key)
        if (
            key == "properties"
            and isinstance(before_value, Mapping)
            and isinstance(after_value, Mapping)
        ):
            property_keys = set(before_value) | set(after_value)
            changed.extend(
                f"properties.{property_key}"
                for property_key in sorted(property_keys)
                if before_value.get(property_key) != after_value.get(property_key)
            )
        elif before_value != after_value:
            changed.append(str(key))
    return changed


def _relationship_report(
    relationship: Mapping[str, Any],
    *,
    include_edge_key: bool,
) -> JsonObject:
    report = {
        "relationship_type": relationship.get("relationship_type"),
        "from_type": relationship.get("from_type"),
        "from_id": relationship.get("from_id"),
        "to_type": relationship.get("to_type"),
        "to_id": relationship.get("to_id"),
        "properties": relationship.get("properties", {}),
        "metadata": relationship.get("metadata", {}),
    }
    if include_edge_key:
        report["edge_key"] = relationship.get("edge_key")
    return report


def _relationship_identity_tuple(
    relationship: Mapping[str, Any],
) -> tuple[str, str, str, str, str, str]:
    return (
        str(relationship.get("relationship_type")),
        str(relationship.get("from_type")),
        str(relationship.get("from_id")),
        str(relationship.get("to_type")),
        str(relationship.get("to_id")),
        str(relationship.get("edge_key")),
    )


def _relationship_semantic_tuple(
    relationship: Mapping[str, Any],
) -> tuple[str, str, str, str, str]:
    return (
        str(relationship.get("relationship_type")),
        str(relationship.get("from_type")),
        str(relationship.get("from_id")),
        str(relationship.get("to_type")),
        str(relationship.get("to_id")),
    )


def _relationship_semantic_counts(
    relationships: list[Mapping[str, Any]],
) -> dict[tuple[str, str, str, str, str], int]:
    counts: dict[tuple[str, str, str, str, str], int] = {}
    for relationship in relationships:
        key = _relationship_semantic_tuple(relationship)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _entity_key(entity: Mapping[str, Any]) -> str:
    return f"{entity.get('entity_type')}:{entity.get('entity_id')}"


def _relationship_key(relationship: Mapping[str, Any]) -> str:
    return "|".join(_relationship_identity_tuple(relationship))


def _query_key(query: Mapping[str, Any]) -> str:
    return _canonical_json(
        {
            "name": query.get("name"),
            "params": query.get("params"),
            "limit": query.get("limit"),
        }
    ).strip()


def _annotate_ownership(
    item: Mapping[str, Any],
    _source: str,
    cross_section: Mapping[str, Any],
) -> JsonObject:
    upstream = _upstream_metadata(cross_section)
    if not upstream:
        return {}
    owned_entity_types = set(upstream.get("owned_entity_types") or [])
    owned_relationship_types = set(upstream.get("owned_relationship_types") or [])
    if "entity_type" in item:
        return {
            "ownership": "upstream" if item.get("entity_type") in owned_entity_types else "local"
        }
    relationship_type = item.get("relationship_type")
    if relationship_type in owned_relationship_types:
        return {"ownership": "upstream"}
    if item.get("from_type") in owned_entity_types or item.get("to_type") in owned_entity_types:
        return {"ownership": "cross_boundary"}
    return {"ownership": "local"}


def _group_sort_key(group: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    stable = _group_stable_sort_key(group)
    return (*stable, str(group.get("group_id")))


def _group_stable_sort_key(group: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(group.get("relationship_type")),
        str(group.get("signature")),
        str(group.get("group_kind")),
        str(group.get("status")),
    )


def _feedback_sort_key(record: Mapping[str, Any]) -> tuple[str, str, str, str, str, str, str]:
    target = record.get("target")
    target = target if isinstance(target, Mapping) else {}
    return (
        str(target.get("relationship_type")),
        str(target.get("from_type")),
        str(target.get("from_id")),
        str(target.get("to_type")),
        str(target.get("to_id")),
        str(record.get("action")),
        str(record.get("feedback_id")),
    )


def _outcome_sort_key(record: Mapping[str, Any]) -> tuple[str, str, str, str, str, str]:
    return (
        str(record.get("anchor_type")),
        str(record.get("anchor_id")),
        str(record.get("relationship_type")),
        str(record.get("outcome_code")),
        str(record.get("outcome")),
        str(record.get("outcome_id")),
    )


def _decision_sort_key(record: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(record.get("decision_class")),
        str(record.get("subject_type")),
        str(record.get("subject_id")),
        str(record.get("question")),
        str(record.get("decision_record_id")),
    )


def _receipt_sort_key(
    receipt: Mapping[str, Any],
    group_sort_keys: Mapping[str, tuple[str, str, str, str]],
) -> tuple[str, str, str, str, str]:
    parameters = receipt.get("parameters")
    parameters = parameters if isinstance(parameters, Mapping) else {}
    stable_parameters = dict(parameters)
    group_id = stable_parameters.pop("group_id", None)
    group_sort_key = group_sort_keys.get(str(group_id), ("", "", "", ""))
    return (
        str(receipt.get("operation_type")),
        _canonical_json({"group": group_sort_key}).strip(),
        str(receipt.get("query_name")),
        str(receipt.get("workflow_name")),
        _canonical_json(stable_parameters).strip(),
    )


def _trace_sort_key(trace: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(trace.get("workflow_name")),
        str(trace.get("step_id")),
        str(trace.get("provider_name")),
        str(trace.get("trace_id")),
    )


def _snapshot_sort_key(snapshot: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(snapshot.get("label")),
        str(snapshot.get("config_digest")),
        str(snapshot.get("graph_digest")),
        str(snapshot.get("snapshot_id")),
    )


def _query_sort_key(query: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(query.get("name")),
        _canonical_json(query.get("params") or {}).strip(),
        str(query.get("limit")),
    )


def _receipt_group_sort_keys(
    instance: InstanceProtocol,
) -> dict[str, tuple[str, str, str, str]]:
    store = instance.get_group_store()
    try:
        groups = store.list_groups(limit=1000)
    finally:
        store.close()
    return {group.group_id: _group_stable_sort_key(_model_dump(group)) for group in groups}


def _upstream_metadata(cross_section: Mapping[str, Any]) -> Mapping[str, Any]:
    world = cross_section.get("world")
    if not isinstance(world, Mapping):
        return {}
    upstream = world.get("upstream")
    return upstream if isinstance(upstream, Mapping) else {}


def _model_dump(value: Any) -> JsonObject:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    raise TypeError(f"Cannot dump value of type {type(value).__name__}")


def _is_volatile_path(value: str) -> bool:
    return value.startswith(_VOLATILE_PATH_MARKERS) or any(
        marker in value for marker in _VOLATILE_PATH_MARKERS
    )


def _normalize_volatile_file_uri(
    value: str,
    registry: CrossSectionTokenRegistry,
) -> str | None:
    if not value.startswith("file://"):
        return None
    path = value.removeprefix("file://")
    if not _is_volatile_path(path):
        return None
    return f"file://{registry.token('PATH', path)}"


def _normalize_embedded_generated_id(
    value: str,
    registry: CrossSectionTokenRegistry,
) -> str | None:
    if ":" not in value:
        return None
    prefix, suffix = value.split(":", 1)
    kind = _generated_id_kind(suffix)
    if kind is None:
        return None
    return f"{prefix}:{registry.token(kind, suffix)}"


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _generated_id_kind(value: str) -> str | None:
    if not _GENERATED_ID_RE.match(value):
        return None
    if value.startswith("snap_"):
        return "SNAPSHOT"
    if "-" in value:
        prefix = value.split("-", 1)[0]
        return _GENERATED_PREFIX_KIND.get(prefix, "ID")
    return "ID"
