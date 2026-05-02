"""Workflow execution runtime."""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from cruxible_core.canonical_json import canonical_json
from cruxible_core.config.schema import (
    CoreConfig,
    DedupeItemsSpec,
    FilterItemsSpec,
    JoinItemsSpec,
    ShapeItemsSpec,
)
from cruxible_core.errors import ConfigError, DataValidationError, QueryExecutionError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.operations import (
    apply_relationship,
    validate_entity,
    validate_relationship,
)
from cruxible_core.graph.types import (
    SYSTEM_OWNED_PROPERTIES,
    EntityInstance,
    RelationshipInstance,
)
from cruxible_core.group.types import CandidateMember, CandidateSignal
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.predicate import evaluate_comparison, normalize_comparison_op
from cruxible_core.provider.registry import resolve_provider
from cruxible_core.provider.types import (
    ExecutionTrace,
    ProviderContext,
    ProviderRuntime,
    ResolvedArtifact,
)
from cruxible_core.query.filters import matches_exact_filter
from cruxible_core.read_surface import (
    list_entities as read_list_entities,
)
from cruxible_core.read_surface import (
    list_relationships as read_list_relationships,
)
from cruxible_core.read_surface import (
    run_query as read_run_query,
)
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.workflow.compiler import compile_workflow, load_lock, resolve_lock_path
from cruxible_core.workflow.contracts import query_execution_error, validate_contract_payload
from cruxible_core.workflow.refs import resolve_value
from cruxible_core.workflow.types import (
    ApplyEntitiesPreview,
    ApplyRelationshipsPreview,
    CandidateSet,
    EntitySet,
    RelationshipGroupProposalArtifact,
    RelationshipSet,
    SignalBatch,
    SignalBatchSignal,
    WorkflowExecutionResult,
)

_MAX_DUPLICATE_EXAMPLES = 10


def execute_workflow(
    instance: InstanceProtocol,
    config: CoreConfig,
    workflow_name: str,
    input_payload: dict[str, Any],
    *,
    mode: Literal["run", "preview", "apply"] = "run",
    persist_receipt: bool = True,
    persist_traces: bool = True,
) -> WorkflowExecutionResult:
    """Execute a workflow against the current instance and persist traces/receipts."""
    lock = load_lock(resolve_lock_path(instance))
    plan = compile_workflow(
        config,
        lock,
        workflow_name,
        input_payload,
        config_base_path=instance.get_config_path().parent,
    )
    workflow = config.workflows[workflow_name]
    execution_mode: Literal["run", "preview", "apply"] = mode
    if workflow.canonical and mode == "run":
        execution_mode = "preview"
    head_snapshot_id = instance.get_head_snapshot_id()
    base_graph = instance.load_graph()
    graph = _clone_graph(base_graph) if workflow.canonical else base_graph
    receipt_builder = ReceiptBuilder(
        query_name=workflow_name,
        parameters=plan.input_payload,
        operation_type="workflow",
    )

    step_outputs: dict[str, Any] = {}
    alias_step_ids: dict[str, str] = {}
    step_trace_ids: dict[str, list[str]] = {}
    query_receipt_ids: list[str] = []
    traces: list[ExecutionTrace] = []
    apply_previews: dict[str, Any] = {}

    for compiled_step in plan.steps:
        if compiled_step.kind == "query":
            _execute_query_step(
                instance,
                config,
                graph,
                plan,
                compiled_step,
                step_outputs,
                alias_step_ids,
                query_receipt_ids,
                receipt_builder,
                persist_receipt=persist_receipt,
            )
            continue

        if compiled_step.kind == "provider":
            _execute_provider_step(
                instance,
                config,
                lock,
                plan,
                compiled_step,
                step_outputs,
                alias_step_ids,
                traces,
                step_trace_ids,
                receipt_builder,
                workflow_name=workflow_name,
                persist_traces=persist_traces,
                config_base_path=instance.get_config_path().parent,
            )
            continue

        if compiled_step.kind == "list_entities":
            assert compiled_step.list_entities_spec is not None
            entity_list = _list_entities(
                config,
                graph,
                compiled_step.step_id,
                compiled_step.list_entities_spec,
                plan.input_payload,
                step_outputs,
            )
            step_outputs[compiled_step.as_name or compiled_step.step_id] = entity_list
            if compiled_step.as_name is not None:
                alias_step_ids[compiled_step.as_name] = compiled_step.step_id
            receipt_builder.record_plan_step(
                compiled_step.step_id,
                "list_entities",
                detail={
                    "entity_type": compiled_step.list_entities_spec.entity_type,
                    "item_count": len(entity_list["items"]),
                    "total": entity_list["total"],
                },
            )
            continue

        if compiled_step.kind == "list_relationships":
            assert compiled_step.list_relationships_spec is not None
            relationship_list = _list_relationships(
                graph,
                compiled_step.step_id,
                compiled_step.list_relationships_spec,
                plan.input_payload,
                step_outputs,
            )
            step_outputs[compiled_step.as_name or compiled_step.step_id] = relationship_list
            if compiled_step.as_name is not None:
                alias_step_ids[compiled_step.as_name] = compiled_step.step_id
            receipt_builder.record_plan_step(
                compiled_step.step_id,
                "list_relationships",
                detail={
                    "relationship_type": (
                        compiled_step.list_relationships_spec.relationship_type
                    ),
                    "item_count": len(relationship_list["items"]),
                    "total": relationship_list["total"],
                },
            )
            continue

        if compiled_step.kind == "shape_items":
            assert compiled_step.shape_items_spec is not None
            shaped_items = _shape_items(
                compiled_step.step_id,
                compiled_step.shape_items_spec,
                plan.input_payload,
                step_outputs,
            )
            step_outputs[compiled_step.as_name or compiled_step.step_id] = shaped_items
            if compiled_step.as_name is not None:
                alias_step_ids[compiled_step.as_name] = compiled_step.step_id
            receipt_builder.record_plan_step(
                compiled_step.step_id,
                "shape_items",
                detail={
                    "input_count": shaped_items["input_count"],
                    "output_count": shaped_items["output_count"],
                    "dropped_count": shaped_items["dropped_count"],
                },
            )
            continue

        if compiled_step.kind == "join_items":
            assert compiled_step.join_items_spec is not None
            joined_items = _join_items(
                compiled_step.step_id,
                compiled_step.join_items_spec,
                plan.input_payload,
                step_outputs,
            )
            step_outputs[compiled_step.as_name or compiled_step.step_id] = joined_items
            if compiled_step.as_name is not None:
                alias_step_ids[compiled_step.as_name] = compiled_step.step_id
            receipt_builder.record_plan_step(
                compiled_step.step_id,
                "join_items",
                detail={
                    "left_count": joined_items["left_count"],
                    "right_count": joined_items["right_count"],
                    "skipped_right_count": joined_items["skipped_right_count"],
                    "matched_left_count": joined_items["matched_left_count"],
                    "output_count": joined_items["output_count"],
                },
            )
            continue

        if compiled_step.kind == "filter_items":
            assert compiled_step.filter_items_spec is not None
            filtered_items = _filter_items(
                compiled_step.step_id,
                compiled_step.filter_items_spec,
                plan.input_payload,
                step_outputs,
            )
            step_outputs[compiled_step.as_name or compiled_step.step_id] = filtered_items
            if compiled_step.as_name is not None:
                alias_step_ids[compiled_step.as_name] = compiled_step.step_id
            receipt_builder.record_plan_step(
                compiled_step.step_id,
                "filter_items",
                detail={
                    "input_count": filtered_items["input_count"],
                    "output_count": filtered_items["output_count"],
                    "filtered_count": filtered_items["filtered_count"],
                },
            )
            continue

        if compiled_step.kind == "dedupe_items":
            assert compiled_step.dedupe_items_spec is not None
            deduped_items = _dedupe_items(
                compiled_step.step_id,
                compiled_step.dedupe_items_spec,
                plan.input_payload,
                step_outputs,
            )
            step_outputs[compiled_step.as_name or compiled_step.step_id] = deduped_items
            if compiled_step.as_name is not None:
                alias_step_ids[compiled_step.as_name] = compiled_step.step_id
            receipt_builder.record_plan_step(
                compiled_step.step_id,
                "dedupe_items",
                detail={
                    "input_count": deduped_items["input_count"],
                    "output_count": deduped_items["output_count"],
                    "duplicate_count": deduped_items["duplicate_count"],
                },
            )
            continue

        if compiled_step.kind == "make_candidates":
            assert compiled_step.make_candidates_spec is not None
            candidate_set = _make_candidate_set(
                config,
                compiled_step.step_id,
                compiled_step.make_candidates_spec,
                plan.input_payload,
                step_outputs,
            )
            step_outputs[compiled_step.as_name or compiled_step.step_id] = candidate_set.model_dump(
                mode="python"
            )
            if compiled_step.as_name is not None:
                alias_step_ids[compiled_step.as_name] = compiled_step.step_id
            receipt_builder.record_plan_step(
                compiled_step.step_id,
                "make_candidates",
                detail={
                    "relationship_type": candidate_set.relationship_type,
                    "candidate_count": len(candidate_set.candidates),
                    "item_count": len(
                        _resolve_step_items(
                            compiled_step.make_candidates_spec.items,
                            plan.input_payload,
                            step_outputs,
                        )
                    ),
                },
            )
            continue

        if compiled_step.kind == "map_signals":
            assert compiled_step.map_signals_spec is not None
            signal_batch = _map_signal_batch(
                compiled_step.step_id,
                compiled_step.map_signals_spec,
                plan.input_payload,
                step_outputs,
            )
            step_outputs[compiled_step.as_name or compiled_step.step_id] = signal_batch.model_dump(
                mode="python"
            )
            if compiled_step.as_name is not None:
                alias_step_ids[compiled_step.as_name] = compiled_step.step_id
            receipt_builder.record_plan_step(
                compiled_step.step_id,
                "map_signals",
                detail={
                    "integration": signal_batch.integration,
                    "signal_count": len(signal_batch.signals),
                    "item_count": len(
                        _resolve_step_items(
                            compiled_step.map_signals_spec.items,
                            plan.input_payload,
                            step_outputs,
                        )
                    ),
                },
            )
            continue

        if compiled_step.kind == "propose_relationship_group":
            assert compiled_step.propose_relationship_group_spec is not None
            proposal = _build_relationship_group_proposal(
                compiled_step.step_id,
                compiled_step.propose_relationship_group_spec,
                plan.input_payload,
                step_outputs,
            )
            step_outputs[compiled_step.as_name or compiled_step.step_id] = proposal.model_dump(
                mode="python"
            )
            if compiled_step.as_name is not None:
                alias_step_ids[compiled_step.as_name] = compiled_step.step_id
            receipt_builder.record_plan_step(
                compiled_step.step_id,
                "propose_relationship_group",
                detail={
                    "relationship_type": proposal.relationship_type,
                    "candidates_from": (
                        compiled_step.propose_relationship_group_spec.candidates_from
                    ),
                    "signals_from": (
                        compiled_step.propose_relationship_group_spec.signals_from
                    ),
                    "member_count": len(proposal.members),
                    "integrations_used": proposal.integrations_used,
                },
            )
            continue

        if compiled_step.kind == "make_entities":
            assert compiled_step.make_entities_spec is not None
            entity_set = _make_entity_set(
                config,
                compiled_step.step_id,
                compiled_step.make_entities_spec,
                plan.input_payload,
                step_outputs,
            )
            step_outputs[compiled_step.as_name or compiled_step.step_id] = entity_set.model_dump(
                mode="python"
            )
            if compiled_step.as_name is not None:
                alias_step_ids[compiled_step.as_name] = compiled_step.step_id
            receipt_builder.record_plan_step(
                compiled_step.step_id,
                "make_entities",
                detail={
                    "entity_type": entity_set.entity_type,
                    "entity_count": len(entity_set.entities),
                    "item_count": len(
                        _resolve_step_items(
                            compiled_step.make_entities_spec.items,
                            plan.input_payload,
                            step_outputs,
                        )
                    ),
                    "duplicate_input_count": entity_set.duplicate_input_count,
                    "conflicting_duplicate_count": entity_set.conflicting_duplicate_count,
                },
            )
            continue

        if compiled_step.kind == "make_relationships":
            assert compiled_step.make_relationships_spec is not None
            relationship_set = _make_relationship_set(
                config,
                compiled_step.step_id,
                compiled_step.make_relationships_spec,
                plan.input_payload,
                step_outputs,
            )
            alias = compiled_step.as_name or compiled_step.step_id
            step_outputs[alias] = relationship_set.model_dump(mode="python")
            if compiled_step.as_name is not None:
                alias_step_ids[compiled_step.as_name] = compiled_step.step_id
            receipt_builder.record_plan_step(
                compiled_step.step_id,
                "make_relationships",
                detail={
                    "relationship_type": relationship_set.relationship_type,
                    "relationship_count": len(relationship_set.relationships),
                    "item_count": len(
                        _resolve_step_items(
                            compiled_step.make_relationships_spec.items,
                            plan.input_payload,
                            step_outputs,
                        )
                    ),
                    "duplicate_input_count": relationship_set.duplicate_input_count,
                    "conflicting_duplicate_count": relationship_set.conflicting_duplicate_count,
                },
            )
            continue

        if compiled_step.kind == "apply_entities":
            assert compiled_step.apply_entities_spec is not None
            step_node = receipt_builder.record_plan_step(
                compiled_step.step_id,
                "apply_entities",
                detail={},
            )
            preview = _apply_entity_set(
                instance,
                graph,
                compiled_step.step_id,
                step_outputs[compiled_step.apply_entities_spec.entities_from],
                receipt_builder,
                persist_writes=execution_mode == "apply",
                parent_id=step_node,
            )
            preview_payload = preview.model_dump(mode="python")
            step_outputs[compiled_step.as_name or compiled_step.step_id] = preview_payload
            apply_previews[compiled_step.step_id] = preview_payload
            if compiled_step.as_name is not None:
                alias_step_ids[compiled_step.as_name] = compiled_step.step_id
            receipt_builder.record_validation(True, detail=preview_payload, parent_id=step_node)
            continue

        if compiled_step.kind == "apply_relationships":
            assert compiled_step.apply_relationships_spec is not None
            step_node = receipt_builder.record_plan_step(
                compiled_step.step_id,
                "apply_relationships",
                detail={},
            )
            preview = _apply_relationship_set(
                instance,
                graph,
                workflow_name,
                compiled_step.step_id,
                step_outputs[compiled_step.apply_relationships_spec.relationships_from],
                receipt_builder,
                persist_writes=execution_mode == "apply",
                parent_id=step_node,
            )
            preview_payload = preview.model_dump(mode="python")
            step_outputs[compiled_step.as_name or compiled_step.step_id] = preview_payload
            apply_previews[compiled_step.step_id] = preview_payload
            if compiled_step.as_name is not None:
                alias_step_ids[compiled_step.as_name] = compiled_step.step_id
            receipt_builder.record_validation(True, detail=preview_payload, parent_id=step_node)
            continue

        assert compiled_step.assert_spec is not None
        _execute_assert_step(
            instance,
            compiled_step,
            plan.input_payload,
            step_outputs,
            receipt_builder,
            persist_receipt=persist_receipt,
        )

    output = step_outputs[plan.returns]
    receipt_builder.record_results([{"output": output}])
    receipt = receipt_builder.build(results=[{"output": output}])
    apply_digest = _compute_apply_digest(plan, head_snapshot_id, apply_previews)
    committed_snapshot_id: str | None = None
    receipt.nodes[0].detail.update(
        {
            "mode": execution_mode,
            "config_digest": plan.config_digest,
            "lock_digest": plan.lock_digest,
            "head_snapshot_id": head_snapshot_id,
            "apply_digest": apply_digest,
        }
    )
    receipt.committed = execution_mode == "apply" or not workflow.canonical

    if workflow.canonical and execution_mode == "apply":
        snapshot = instance.commit_graph_snapshot(graph)
        committed_snapshot_id = snapshot.snapshot_id
        receipt.nodes[0].detail["committed_snapshot_id"] = committed_snapshot_id
        receipt.committed = True

    if persist_receipt:
        _persist_receipt(instance, receipt)

    return WorkflowExecutionResult(
        workflow=workflow_name,
        output=output,
        receipt=receipt,
        mode=execution_mode,
        canonical=workflow.canonical,
        apply_digest=apply_digest,
        head_snapshot_id=head_snapshot_id,
        committed_snapshot_id=committed_snapshot_id,
        apply_previews=apply_previews,
        query_receipt_ids=query_receipt_ids,
        traces=traces,
        step_outputs=step_outputs,
        alias_step_ids=alias_step_ids,
        step_trace_ids=step_trace_ids,
    )


def _persist_receipt(instance: InstanceProtocol, receipt) -> None:
    store = instance.get_receipt_store()
    try:
        store.save_receipt(receipt)
    finally:
        store.close()


def _persist_trace(instance: InstanceProtocol, trace: ExecutionTrace) -> None:
    store = instance.get_receipt_store()
    try:
        store.save_trace(trace)
    finally:
        store.close()


def _build_trace(
    *,
    workflow_name: str,
    step_id: str,
    provider_name: str,
    provider_version: str,
    provider_ref: str,
    provider_entrypoint_sha256: str | None,
    runtime: ProviderRuntime,
    deterministic: bool,
    side_effects: bool,
    artifact_name: str | None,
    artifact_sha256: str | None,
    input_payload: dict[str, Any],
    output_payload: dict[str, Any],
    status: str,
    error: str | None,
    started_at: datetime,
    duration_ms: float,
) -> ExecutionTrace:
    return ExecutionTrace(
        workflow_name=workflow_name,
        step_id=step_id,
        provider_name=provider_name,
        provider_version=provider_version,
        provider_ref=provider_ref,
        provider_entrypoint_sha256=provider_entrypoint_sha256,
        runtime=runtime,
        deterministic=deterministic,
        side_effects=side_effects,
        artifact_name=artifact_name,
        artifact_sha256=artifact_sha256,
        input_payload=input_payload,
        output_payload=output_payload,
        status=status,
        error=error,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        duration_ms=round(duration_ms, 3),
    )


def _evaluate_assert(left: Any, op: str, right: Any) -> bool:
    try:
        return evaluate_comparison(left, op, right)
    except ValueError as exc:
        raise ConfigError(f"Unsupported assert op '{op}'") from exc


def _resolve_step_items(
    items_template: Any,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> list[Any]:
    items = resolve_value(items_template, input_payload, step_outputs)
    if not isinstance(items, list):
        raise QueryExecutionError("Built-in workflow step 'items' must resolve to a list")
    return items


def _resolve_limit(
    limit_template: Any,
    step_id: str,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> int | None:
    if limit_template is None:
        return None
    limit_value = resolve_value(limit_template, input_payload, step_outputs)
    if not isinstance(limit_value, int) or limit_value < 1:
        raise QueryExecutionError(
            f"Workflow step '{step_id}' limit must resolve to an integer >= 1"
        )
    return limit_value


def _resolve_property_filter(
    property_filter_template: dict[str, Any],
    step_id: str,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> dict[str, Any]:
    property_filter = resolve_value(property_filter_template, input_payload, step_outputs)
    if not isinstance(property_filter, dict):
        raise QueryExecutionError(
            f"Workflow step '{step_id}' property_filter must resolve to a mapping"
        )
    return property_filter


def _list_entities(
    config: CoreConfig,
    graph: EntityGraph,
    step_id: str,
    spec,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> dict[str, Any]:
    property_filter = _resolve_property_filter(
        spec.property_filter,
        step_id,
        input_payload,
        step_outputs,
    )
    limit = _resolve_limit(spec.limit, step_id, input_payload, step_outputs)
    result = read_list_entities(
        graph,
        spec.entity_type,
        config=config,
        property_filter=property_filter or None,
        limit=limit,
    )
    items = [entity.model_dump(mode="python") for entity in result.items]
    return {
        "items": items,
        "total": result.total,
    }


def _list_relationships(
    graph: EntityGraph,
    step_id: str,
    spec,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> dict[str, Any]:
    property_filter = _resolve_property_filter(
        spec.property_filter,
        step_id,
        input_payload,
        step_outputs,
    )
    limit = _resolve_limit(spec.limit, step_id, input_payload, step_outputs)
    result = read_list_relationships(
        graph,
        relationship_type=spec.relationship_type,
        property_filter=property_filter or None,
        limit=limit,
    )
    return {
        "items": result.items,
        "total": result.total,
    }


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
                f"Workflow step '{step_id}' shape_items rename collision: "
                f"'{target}' already exists"
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
            f"Workflow step '{step_id}' filter_items where reference '{value}' "
            "must use $input only"
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


def _make_candidate_set(
    config: CoreConfig,
    step_id: str,
    spec,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> CandidateSet:
    relationship_type = spec.relationship_type
    rel_schema = config.get_relationship(relationship_type)
    if rel_schema is None:
        raise QueryExecutionError(
            f"Workflow step '{step_id}' references unknown relationship '{relationship_type}'"
        )

    items = _resolve_step_items(spec.items, input_payload, step_outputs)
    seen: set[tuple[str, str, str, str]] = set()
    candidates: list[RelationshipInstance] = []

    for item in items:
        member = RelationshipInstance.model_validate(
            {
                "relationship_type": relationship_type,
                "from_type": resolve_value(
                    spec.from_type,
                    input_payload,
                    step_outputs,
                    item_payload=item,
                    allow_item=True,
                ),
                "from_id": resolve_value(
                    spec.from_id,
                    input_payload,
                    step_outputs,
                    item_payload=item,
                    allow_item=True,
                ),
                "to_type": resolve_value(
                    spec.to_type,
                    input_payload,
                    step_outputs,
                    item_payload=item,
                    allow_item=True,
                ),
                "to_id": resolve_value(
                    spec.to_id,
                    input_payload,
                    step_outputs,
                    item_payload=item,
                    allow_item=True,
                ),
                "properties": resolve_value(
                    spec.properties,
                    input_payload,
                    step_outputs,
                    item_payload=item,
                    allow_item=True,
                ),
            }
        )
        if member.from_type != rel_schema.from_entity or member.to_type != rel_schema.to_entity:
            raise QueryExecutionError(
                f"Workflow step '{step_id}' produced candidate types "
                f"{member.from_type}->{member.to_type} which do not match "
                "relationship "
                f"'{relationship_type}' "
                f"({rel_schema.from_entity}->{rel_schema.to_entity})"
            )
        key = (member.from_type, member.from_id, member.to_type, member.to_id)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(member)

    return CandidateSet(relationship_type=relationship_type, candidates=candidates)


def _map_signal_batch(
    step_id: str,
    spec,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> SignalBatch:
    items = _resolve_step_items(spec.items, input_payload, step_outputs)
    seen_pairs: set[tuple[str, str]] = set()
    signals: list[SignalBatchSignal] = []

    for item in items:
        from_id = str(
            resolve_value(
                spec.from_id,
                input_payload,
                step_outputs,
                item_payload=item,
                allow_item=True,
            )
        )
        to_id = str(
            resolve_value(
                spec.to_id,
                input_payload,
                step_outputs,
                item_payload=item,
                allow_item=True,
            )
        )
        key = (from_id, to_id)
        if key in seen_pairs:
            raise QueryExecutionError(
                f"Workflow step '{step_id}' produced duplicate signal for pair {from_id}->{to_id}"
            )

        evidence = ""
        if spec.evidence is not None:
            resolved_evidence = resolve_value(
                spec.evidence,
                input_payload,
                step_outputs,
                item_payload=item,
                allow_item=True,
            )
            if resolved_evidence is not None:
                evidence = str(resolved_evidence)

        if spec.score is not None:
            score_spec = spec.score
            score_value = resolve_value(
                f"$item.{score_spec.path}",
                input_payload,
                step_outputs,
                item_payload=item,
                allow_item=True,
            )
            if isinstance(score_value, bool) or not isinstance(score_value, (int, float)):
                raise QueryExecutionError(
                    f"Workflow step '{step_id}' score path '{score_spec.path}' "
                    "must resolve to a number"
                )
            numeric_score = float(score_value)
            if numeric_score >= float(score_spec.support_gte):
                signal = "support"
            elif numeric_score >= float(score_spec.unsure_gte):
                signal = "unsure"
            else:
                signal = "contradict"
        else:
            assert spec.enum is not None
            enum_spec = spec.enum
            enum_value = resolve_value(
                f"$item.{enum_spec.path}",
                input_payload,
                step_outputs,
                item_payload=item,
                allow_item=True,
            )
            if not isinstance(enum_value, str):
                raise QueryExecutionError(
                    f"Workflow step '{step_id}' enum path '{enum_spec.path}' "
                    "must resolve to a string"
                )
            if enum_value not in enum_spec.map:
                raise QueryExecutionError(
                    f"Workflow step '{step_id}' enum path '{enum_spec.path}' returned "
                    f"unknown value '{enum_value}'"
                )
            signal = enum_spec.map[enum_value]

        signals.append(
            SignalBatchSignal(
                from_id=from_id,
                to_id=to_id,
                signal=signal,
                evidence=evidence,
            )
        )
        seen_pairs.add(key)

    return SignalBatch(integration=spec.integration, signals=signals)


def _build_relationship_group_proposal(
    step_id: str,
    spec,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> RelationshipGroupProposalArtifact:
    candidate_set = CandidateSet.model_validate(step_outputs[spec.candidates_from])
    relationship_type = spec.relationship_type
    if candidate_set.relationship_type != relationship_type:
        raise QueryExecutionError(
            f"Workflow step '{step_id}' expected candidate relationship '{relationship_type}' "
            f"but received '{candidate_set.relationship_type}'"
        )

    members_by_pair: dict[tuple[str, str], CandidateMember] = {
        (candidate.from_id, candidate.to_id): CandidateMember(
            from_type=candidate.from_type,
            from_id=candidate.from_id,
            to_type=candidate.to_type,
            to_id=candidate.to_id,
            relationship_type=relationship_type,
            properties=candidate.properties,
        )
        for candidate in candidate_set.candidates
    }

    integrations_used: list[str] = []
    for alias in spec.signals_from:
        signal_batch = SignalBatch.model_validate(step_outputs[alias])
        integrations_used.append(signal_batch.integration)
        for signal in signal_batch.signals:
            key = (signal.from_id, signal.to_id)
            if key not in members_by_pair:
                raise QueryExecutionError(
                    f"Workflow step '{step_id}' received signal for unknown candidate pair "
                    f"{signal.from_id}->{signal.to_id}"
                )
            member = members_by_pair[key]
            if any(existing.integration == signal_batch.integration for existing in member.signals):
                raise QueryExecutionError(
                    f"Workflow step '{step_id}' produced duplicate integration "
                    f"'{signal_batch.integration}' for pair {signal.from_id}->{signal.to_id}"
                )
            member.signals.append(
                CandidateSignal(
                    integration=signal_batch.integration,
                    signal=signal.signal,
                    evidence=signal.evidence,
                )
            )

    return RelationshipGroupProposalArtifact.model_validate(
        {
            "relationship_type": relationship_type,
            "members": [member.model_dump(mode="python") for member in members_by_pair.values()],
            "thesis_text": resolve_value(
                spec.thesis_text,
                input_payload,
                step_outputs,
            ),
            "thesis_facts": resolve_value(
                spec.thesis_facts,
                input_payload,
                step_outputs,
            ),
            "pending_refresh_mode": spec.pending_refresh_mode,
            "analysis_state": resolve_value(
                spec.analysis_state,
                input_payload,
                step_outputs,
            ),
            "integrations_used": integrations_used,
            "suggested_priority": resolve_value(
                spec.suggested_priority,
                input_payload,
                step_outputs,
            ),
            "proposed_by": spec.proposed_by,
        }
    )


def _make_entity_set(
    config: CoreConfig,
    step_id: str,
    spec,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> EntitySet:
    if spec.entity_type not in config.entity_types:
        raise QueryExecutionError(
            f"Workflow step '{step_id}' references unknown entity type '{spec.entity_type}'"
        )
    items = _resolve_step_items(spec.items, input_payload, step_outputs)
    seen: dict[str, dict[str, Any]] = {}
    entities: list[EntityInstance] = []
    duplicate_input_count = 0
    conflicting_duplicate_count = 0
    duplicate_examples: list[dict[str, Any]] = []
    for item in items:
        entity_id = str(
            resolve_value(
                spec.entity_id,
                input_payload,
                step_outputs,
                item_payload=item,
                allow_item=True,
            )
        )
        properties = resolve_value(
            spec.properties,
            input_payload,
            step_outputs,
            item_payload=item,
            allow_item=True,
        )
        if entity_id in seen:
            duplicate_input_count += 1
            conflicting = seen[entity_id] != properties
            if conflicting:
                conflicting_duplicate_count += 1
            if len(duplicate_examples) < _MAX_DUPLICATE_EXAMPLES:
                example = {
                    "entity_id": entity_id,
                    "conflicting": conflicting,
                }
                if conflicting:
                    example["first_properties"] = seen[entity_id]
                    example["duplicate_properties"] = properties
                duplicate_examples.append(example)
            continue
        seen[entity_id] = properties
        entities.append(
            EntityInstance(
                entity_type=spec.entity_type,
                entity_id=entity_id,
                properties=properties,
            )
        )
    return EntitySet(
        entity_type=spec.entity_type,
        entities=entities,
        duplicate_input_count=duplicate_input_count,
        conflicting_duplicate_count=conflicting_duplicate_count,
        duplicate_examples=duplicate_examples,
    )


def _make_relationship_set(
    config: CoreConfig,
    step_id: str,
    spec,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> RelationshipSet:
    rel_schema = config.get_relationship(spec.relationship_type)
    if rel_schema is None:
        raise QueryExecutionError(
            f"Workflow step '{step_id}' references unknown relationship '{spec.relationship_type}'"
        )
    items = _resolve_step_items(spec.items, input_payload, step_outputs)
    seen: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    relationships: list[RelationshipInstance] = []
    duplicate_input_count = 0
    conflicting_duplicate_count = 0
    duplicate_examples: list[dict[str, Any]] = []
    for item in items:
        member = RelationshipInstance.model_validate(
            {
                "relationship_type": spec.relationship_type,
                "from_type": resolve_value(
                    spec.from_type,
                    input_payload,
                    step_outputs,
                    item_payload=item,
                    allow_item=True,
                ),
                "from_id": resolve_value(
                    spec.from_id,
                    input_payload,
                    step_outputs,
                    item_payload=item,
                    allow_item=True,
                ),
                "to_type": resolve_value(
                    spec.to_type,
                    input_payload,
                    step_outputs,
                    item_payload=item,
                    allow_item=True,
                ),
                "to_id": resolve_value(
                    spec.to_id,
                    input_payload,
                    step_outputs,
                    item_payload=item,
                    allow_item=True,
                ),
                "properties": resolve_value(
                    spec.properties,
                    input_payload,
                    step_outputs,
                    item_payload=item,
                    allow_item=True,
                ),
            }
        )
        if member.from_type != rel_schema.from_entity or member.to_type != rel_schema.to_entity:
            raise QueryExecutionError(
                f"Workflow step '{step_id}' produced relationship types "
                f"{member.from_type}->{member.to_type} which do not match "
                f"'{spec.relationship_type}' ({rel_schema.from_entity}->{rel_schema.to_entity})"
            )
        key = (
            spec.relationship_type,
            member.from_type,
            member.from_id,
            member.to_type,
            member.to_id,
        )
        if key in seen:
            duplicate_input_count += 1
            conflicting = seen[key] != member.properties
            if conflicting:
                conflicting_duplicate_count += 1
            if len(duplicate_examples) < _MAX_DUPLICATE_EXAMPLES:
                example = {
                    "from_type": member.from_type,
                    "from_id": member.from_id,
                    "to_type": member.to_type,
                    "to_id": member.to_id,
                    "relationship_type": spec.relationship_type,
                    "conflicting": conflicting,
                }
                if conflicting:
                    example["first_properties"] = seen[key]
                    example["duplicate_properties"] = member.properties
                duplicate_examples.append(example)
            continue
        seen[key] = member.properties
        relationships.append(member)
    return RelationshipSet(
        relationship_type=spec.relationship_type,
        relationships=relationships,
        duplicate_input_count=duplicate_input_count,
        conflicting_duplicate_count=conflicting_duplicate_count,
        duplicate_examples=duplicate_examples,
    )


def _apply_entity_set(
    instance: InstanceProtocol,
    graph: EntityGraph,
    step_id: str,
    raw_entity_set: Any,
    receipt_builder: ReceiptBuilder,
    *,
    persist_writes: bool,
    parent_id: str | None,
) -> ApplyEntitiesPreview:
    entity_set = EntitySet.model_validate(raw_entity_set)
    from cruxible_core.service._ownership import check_type_ownership

    check_type_ownership(instance, entity_types=[entity_set.entity_type])
    config = instance.load_config()
    create_count = 0
    update_count = 0
    noop_count = 0
    validated_entities = []
    errors: list[str] = []
    for entity in entity_set.entities:
        try:
            validated = validate_entity(
                config,
                graph,
                entity_set.entity_type,
                entity.entity_id,
                entity.properties,
            )
        except DataValidationError as exc:
            detail = "; ".join(exc.errors) if exc.errors else str(exc)
            errors.append(f"{entity_set.entity_type}:{entity.entity_id}: {detail}")
            continue
        validated_entities.append(validated)

    if errors:
        raise QueryExecutionError(
            f"Workflow step '{step_id}' entity property validation failed: "
            + "; ".join(errors)
        )

    for validated in validated_entities:
        entity = validated.entity
        existing = graph.get_entity(entity_set.entity_type, entity.entity_id)
        if existing is None:
            create_count += 1
            graph.add_entity(entity)
            if persist_writes:
                receipt_builder.record_entity_write(
                    entity_set.entity_type,
                    entity.entity_id,
                    is_update=False,
                    parent_id=parent_id,
                )
            continue
        if _would_update_entity(existing.properties, entity.properties):
            update_count += 1
            graph.update_entity_properties(
                entity_set.entity_type,
                entity.entity_id,
                dict(entity.properties),
            )
            if persist_writes:
                receipt_builder.record_entity_write(
                    entity_set.entity_type,
                    entity.entity_id,
                    is_update=True,
                    parent_id=parent_id,
                )
            continue
        noop_count += 1
    return ApplyEntitiesPreview(
        entity_type=entity_set.entity_type,
        create_count=create_count,
        update_count=update_count,
        noop_count=noop_count,
        duplicate_input_count=entity_set.duplicate_input_count,
        conflicting_duplicate_count=entity_set.conflicting_duplicate_count,
        duplicate_examples=entity_set.duplicate_examples,
    )


def _apply_relationship_set(
    instance: InstanceProtocol,
    graph: EntityGraph,
    workflow_name: str,
    step_id: str,
    raw_relationship_set: Any,
    receipt_builder: ReceiptBuilder,
    *,
    persist_writes: bool,
    parent_id: str | None,
) -> ApplyRelationshipsPreview:
    relationship_set = RelationshipSet.model_validate(raw_relationship_set)
    from cruxible_core.service._ownership import check_type_ownership

    check_type_ownership(instance, relationship_types=[relationship_set.relationship_type])
    config = instance.load_config()
    create_count = 0
    update_count = 0
    noop_count = 0
    validated_relationships = []
    errors: list[str] = []
    for rel in relationship_set.relationships:
        try:
            validated = validate_relationship(
                config,
                graph,
                rel.from_type,
                rel.from_id,
                relationship_set.relationship_type,
                rel.to_type,
                rel.to_id,
                rel.properties,
            )
        except DataValidationError as exc:
            detail = "; ".join(exc.errors) if exc.errors else str(exc)
            errors.append(
                f"{rel.from_type}:{rel.from_id}-[{relationship_set.relationship_type}]->"
                f"{rel.to_type}:{rel.to_id}: {detail}"
            )
            continue
        validated_relationships.append(validated)

    if errors:
        raise QueryExecutionError(
            f"Workflow step '{step_id}' relationship property validation failed: "
            + "; ".join(errors)
        )

    source_ref = f"workflow:{workflow_name}:{step_id}"
    for validated in validated_relationships:
        rel = validated.relationship
        existing = graph.get_relationship(
            rel.from_type,
            rel.from_id,
            rel.to_type,
            rel.to_id,
            relationship_set.relationship_type,
        )
        if existing is None:
            create_count += 1
            apply_relationship(graph, validated, "workflow_apply", source_ref)
            if persist_writes:
                receipt_builder.record_relationship_write(
                    rel.from_type,
                    rel.from_id,
                    rel.to_type,
                    rel.to_id,
                    relationship_set.relationship_type,
                    is_update=False,
                    parent_id=parent_id,
            )
            continue
        existing_domain_properties = {
            key: value
            for key, value in existing.properties.items()
            if key not in SYSTEM_OWNED_PROPERTIES
        }
        if existing_domain_properties != rel.properties:
            update_count += 1
            apply_relationship(graph, validated, "workflow_apply", source_ref)
            if persist_writes:
                receipt_builder.record_relationship_write(
                    rel.from_type,
                    rel.from_id,
                    rel.to_type,
                    rel.to_id,
                    relationship_set.relationship_type,
                    is_update=True,
                    parent_id=parent_id,
                )
            continue
        noop_count += 1
    return ApplyRelationshipsPreview(
        relationship_type=relationship_set.relationship_type,
        create_count=create_count,
        update_count=update_count,
        noop_count=noop_count,
        duplicate_input_count=relationship_set.duplicate_input_count,
        conflicting_duplicate_count=relationship_set.conflicting_duplicate_count,
        duplicate_examples=relationship_set.duplicate_examples,
    )


def _execute_query_step(
    instance: InstanceProtocol,
    config: CoreConfig,
    graph: EntityGraph,
    plan,
    compiled_step,
    step_outputs: dict[str, Any],
    alias_step_ids: dict[str, str],
    query_receipt_ids: list[str],
    receipt_builder: ReceiptBuilder,
    *,
    persist_receipt: bool,
) -> None:
    assert compiled_step.query_name is not None
    step_params = resolve_value(compiled_step.params_template, plan.input_payload, step_outputs)
    query_result = read_run_query(config, graph, compiled_step.query_name, step_params)
    if query_result.receipt is None:
        raise QueryExecutionError(f"Query step '{compiled_step.step_id}' did not produce a receipt")
    if persist_receipt:
        _persist_receipt(instance, query_result.receipt)
    query_receipt_ids.append(query_result.receipt.receipt_id)
    step_outputs[compiled_step.as_name or compiled_step.step_id] = {
        "results": [item.model_dump() for item in query_result.results],
        "receipt_id": query_result.receipt.receipt_id,
        "total_results": query_result.total_results,
        "steps_executed": query_result.steps_executed,
    }
    if compiled_step.as_name is not None:
        alias_step_ids[compiled_step.as_name] = compiled_step.step_id
    receipt_builder.record_plan_step(
        compiled_step.step_id,
        "query",
        detail={
            "query_name": compiled_step.query_name,
            "receipt_id": query_result.receipt.receipt_id,
            "params": step_params,
        },
    )


def _execute_provider_step(
    instance: InstanceProtocol,
    config: CoreConfig,
    lock,
    plan,
    compiled_step,
    step_outputs: dict[str, Any],
    alias_step_ids: dict[str, str],
    traces: list[ExecutionTrace],
    step_trace_ids: dict[str, list[str]],
    receipt_builder: ReceiptBuilder,
    *,
    workflow_name: str,
    persist_traces: bool,
    config_base_path: Path,
) -> None:
    assert compiled_step.provider_name is not None
    provider_schema = config.providers[compiled_step.provider_name]
    locked_provider = lock.providers[compiled_step.provider_name]
    raw_input = resolve_value(compiled_step.input_template, plan.input_payload, step_outputs)
    provider_input = validate_contract_payload(
        config,
        provider_schema.contract_in,
        raw_input,
        subject=f"Provider step '{compiled_step.step_id}' input",
        error_factory=query_execution_error,
    )
    artifact = None
    if locked_provider.artifact is not None:
        locked_artifact = lock.artifacts[locked_provider.artifact]
        local_path = _resolve_local_artifact_path(locked_artifact.uri, config_base_path)
        artifact = ResolvedArtifact(
            name=locked_provider.artifact,
            kind=locked_artifact.kind,
            uri=locked_artifact.uri,
            local_path=str(local_path) if local_path is not None else None,
            sha256=locked_artifact.sha256,
            metadata=locked_artifact.metadata,
        )

    context = ProviderContext(
        workflow_name=workflow_name,
        step_id=compiled_step.step_id,
        provider_name=compiled_step.provider_name,
        provider_version=locked_provider.version,
        provider_config=locked_provider.config,
        deterministic=locked_provider.deterministic,
        artifact=artifact,
    )
    provider_fn = resolve_provider(compiled_step.provider_name, provider_schema)
    started = time.monotonic_ns()
    started_at = datetime.now(timezone.utc)
    status = "success"
    error_message: str | None = None
    try:
        raw_output = provider_fn(provider_input, context)
        if not isinstance(raw_output, dict):
            raise QueryExecutionError(
                f"Provider '{compiled_step.provider_name}' returned non-dict output"
            )
        provider_output = validate_contract_payload(
            config,
            provider_schema.contract_out,
            raw_output,
            subject=f"Provider step '{compiled_step.step_id}' output",
            error_factory=query_execution_error,
        )
    except Exception as exc:
        status = "error"
        error_message = str(exc)
        trace = _build_trace(
            workflow_name=workflow_name,
            step_id=compiled_step.step_id,
            provider_name=compiled_step.provider_name,
            provider_version=locked_provider.version,
            provider_ref=locked_provider.ref,
            provider_entrypoint_sha256=locked_provider.provider_entrypoint_sha256,
            runtime=locked_provider.runtime,
            deterministic=locked_provider.deterministic,
            side_effects=locked_provider.side_effects,
            artifact_name=locked_provider.artifact,
            artifact_sha256=artifact.sha256 if artifact is not None else None,
            input_payload=provider_input,
            output_payload={},
            status=status,
            error=error_message,
            started_at=started_at,
            duration_ms=(time.monotonic_ns() - started) / 1_000_000,
        )
        if persist_traces:
            _persist_trace(instance, trace)
        traces.append(trace)
        step_trace_ids.setdefault(compiled_step.step_id, []).append(trace.trace_id)
        receipt_builder.record_plan_step(
            compiled_step.step_id,
            "provider",
            detail={
                "provider_name": compiled_step.provider_name,
                "trace_id": trace.trace_id,
                "status": status,
            },
        )
        raise QueryExecutionError(error_message or "Provider execution failed") from exc

    trace = _build_trace(
        workflow_name=workflow_name,
        step_id=compiled_step.step_id,
        provider_name=compiled_step.provider_name,
        provider_version=locked_provider.version,
        provider_ref=locked_provider.ref,
        provider_entrypoint_sha256=locked_provider.provider_entrypoint_sha256,
        runtime=locked_provider.runtime,
        deterministic=locked_provider.deterministic,
        side_effects=locked_provider.side_effects,
        artifact_name=locked_provider.artifact,
        artifact_sha256=artifact.sha256 if artifact is not None else None,
        input_payload=provider_input,
        output_payload=provider_output,
        status=status,
        error=error_message,
        started_at=started_at,
        duration_ms=(time.monotonic_ns() - started) / 1_000_000,
    )
    if persist_traces:
        _persist_trace(instance, trace)
    traces.append(trace)
    step_outputs[compiled_step.as_name or compiled_step.step_id] = provider_output
    step_trace_ids.setdefault(compiled_step.step_id, []).append(trace.trace_id)
    if compiled_step.as_name is not None:
        alias_step_ids[compiled_step.as_name] = compiled_step.step_id
    receipt_builder.record_plan_step(
        compiled_step.step_id,
        "provider",
        detail={
            "provider_name": compiled_step.provider_name,
            "provider_version": locked_provider.version,
            "trace_id": trace.trace_id,
        },
    )


def _execute_assert_step(
    instance: InstanceProtocol,
    compiled_step,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
    receipt_builder: ReceiptBuilder,
    *,
    persist_receipt: bool,
) -> None:
    assert compiled_step.assert_spec is not None
    left = resolve_value(compiled_step.assert_spec.left, input_payload, step_outputs)
    right = resolve_value(compiled_step.assert_spec.right, input_payload, step_outputs)
    passed = _evaluate_assert(left, compiled_step.assert_spec.op, right)
    step_node = receipt_builder.record_plan_step(
        compiled_step.step_id,
        "assert",
        detail={
            "op": compiled_step.assert_spec.op,
            "left": left,
            "right": right,
            "message": compiled_step.assert_spec.message,
        },
    )
    receipt_builder.record_validation(
        passed=passed,
        detail={"message": compiled_step.assert_spec.message},
        parent_id=step_node,
    )
    if not passed:
        receipt = receipt_builder.build(results=[{"output": None}])
        if persist_receipt:
            _persist_receipt(instance, receipt)
        raise QueryExecutionError(compiled_step.assert_spec.message)


def _would_update_entity(current: dict[str, Any], new_properties: dict[str, Any]) -> bool:
    return any(current.get(key) != value for key, value in new_properties.items())


def _compute_apply_digest(
    plan: Any,
    head_snapshot_id: str | None,
    apply_previews: dict[str, Any],
) -> str | None:
    if not plan.canonical or not apply_previews:
        return None
    payload = {
        "workflow": plan.workflow,
        "input": plan.input_payload,
        "lock_digest": plan.lock_digest,
        "head_snapshot_id": head_snapshot_id,
        "apply_previews": {key: apply_previews[key] for key in sorted(apply_previews)},
    }
    dumped = json.dumps(payload, sort_keys=True, default=str)
    return f"sha256:{hashlib.sha256(dumped.encode()).hexdigest()}"


def _clone_graph(graph: EntityGraph) -> EntityGraph:
    return EntityGraph.from_dict(graph.to_dict())


def _resolve_local_artifact_path(uri: str, config_base_path: Path) -> Path | None:
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        return Path(parsed.path)
    if parsed.scheme == "":
        path = Path(uri)
        if not path.is_absolute():
            path = (config_base_path / path).resolve()
        return path
    return None
