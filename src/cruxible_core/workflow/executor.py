"""Workflow execution runtime.

This module is the workflow engine's coordinator. It does not own step-specific
business rules; instead it compiles the workflow, dispatches each compiled step
to the helper that owns that step kind, records receipt nodes, and decides
whether canonical apply previews stay isolated or become committed graph state.
"""

from __future__ import annotations

from typing import Any

from cruxible_core.config.schema import CoreConfig, WorkflowSchema
from cruxible_core.errors import ConfigError, QueryExecutionError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.provider.types import ExecutionTrace
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.receipt.types import Receipt
from cruxible_core.workflow.apply import (
    apply_entity_set,
    apply_relationship_set,
    compute_apply_digest,
    make_entity_set,
    make_relationship_set,
)
from cruxible_core.workflow.compiler import compile_workflow, load_lock, resolve_lock_path
from cruxible_core.workflow.contracts import query_execution_error, validate_contract_payload
from cruxible_core.workflow.io import (
    execute_assert_count_step,
    execute_assert_exists_step,
    execute_assert_not_truncated_step,
    execute_assert_step,
    execute_provider_step,
    execute_query_step,
    list_entities_step,
    list_relationships_step,
)
from cruxible_core.workflow.proposals import (
    build_relationship_group_proposal,
    make_candidate_set,
    map_signal_batch,
    signal_mapping_snapshot,
)
from cruxible_core.workflow.step_helpers import (
    extract_read_metadata,
    resolve_step_items,
)
from cruxible_core.workflow.tracing import persist_receipt as persist_workflow_receipt
from cruxible_core.workflow.transforms import (
    aggregate_items,
    dedupe_items,
    filter_items,
    join_items,
    shape_items,
)
from cruxible_core.workflow.types import WorkflowExecutionResult
from cruxible_core.workflow_execution_types import (
    WorkflowExecutionAction,
    WorkflowResultMode,
)

_WORKFLOW_READ_COUNT_KEYS = (
    "total_results",
    "returned_results",
    "total",
    "input_count",
    "output_count",
    "filtered_count",
    "group_count",
    "dropped_count",
    "duplicate_count",
    "left_count",
    "right_count",
    "matched_left_count",
    "skipped_right_count",
)

_TRUNCATION_REASON_ORDER = (
    "limit",
    "max_paths",
    "max_paths_per_result",
)


def execute_workflow(
    instance: InstanceProtocol,
    config: CoreConfig,
    workflow_name: str,
    input_payload: dict[str, Any],
    *,
    mode: WorkflowExecutionAction = "run",
    persist_receipt: bool = True,
    persist_query_receipts: bool | None = None,
    persist_traces: bool = True,
) -> WorkflowExecutionResult:
    """Execute one compiled workflow plan and return its full runtime result.

    The executor is below the service/CLI/MCP surfaces. Callers pass the already
    loaded config plus an input payload, and this function loads the workflow
    lock, compiles a plan, dispatches the compiled steps in order, and returns
    both the public output and internal audit material such as traces, receipt
    nodes, apply previews, alias-to-step mappings, and query receipt ids.

    Canonical workflows are special: user-facing ``run`` calls are translated by
    the service layer into preview execution before they reach this function. A
    preview executes against a cloned graph so it can calculate the apply digest
    and write previews without mutating live state; an apply is the service
    replay path after preview identity has already been verified. Utility,
    proposal, and decision-support workflows execute against the live graph but
    may only use the executor ``run`` action.
    """
    lock = load_lock(resolve_lock_path(instance))
    if persist_query_receipts is None:
        persist_query_receipts = persist_receipt
    plan = compile_workflow(
        config,
        lock,
        workflow_name,
        input_payload,
        config_base_path=instance.get_config_path().parent,
    )
    workflow = config.workflows[workflow_name]
    workflow_type = workflow.type
    execution_action: WorkflowExecutionAction = mode
    result_mode: WorkflowResultMode
    if workflow_type == "canonical":
        if execution_action == "run":
            raise ConfigError(
                "Canonical workflows use preview-first execution; direct executor "
                "calls must pass mode='preview'. Commits must go through the "
                "workflow apply service after preview verification."
            )
        result_mode = execution_action
    else:
        if execution_action != "run":
            raise ConfigError(
                f"{workflow_type} workflows only support executor action 'run'"
            )
        result_mode = "proposal" if workflow_type == "proposal" else "run"
    head_snapshot_id = instance.get_head_snapshot_id()
    base_graph = instance.load_graph()
    graph = _clone_graph(base_graph) if workflow_type == "canonical" else base_graph
    receipt_builder = ReceiptBuilder(
        query_name=workflow_name,
        parameters=plan.input_payload,
        operation_type="workflow",
        head_snapshot_id=head_snapshot_id,
        workflow_mode=result_mode,
    )

    step_outputs: dict[str, Any] = {}
    alias_step_ids: dict[str, str] = {}
    step_trace_ids: dict[str, list[str]] = {}
    query_receipt_ids: list[str] = []
    traces: list[ExecutionTrace] = []
    apply_previews: dict[str, Any] = {}
    results_recorded = False
    committed_snapshot_id: str | None = None

    try:
        for compiled_step in plan.steps:
            if compiled_step.kind == "query":
                execute_query_step(
                    instance,
                    config,
                    graph,
                    plan,
                    compiled_step,
                    step_outputs,
                    alias_step_ids,
                    query_receipt_ids,
                    receipt_builder,
                    persist_receipt=persist_query_receipts,
                )
                continue

            if compiled_step.kind == "provider":
                execute_provider_step(
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
                entity_list = list_entities_step(
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
                relationship_list = list_relationships_step(
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
                shaped_items = shape_items(
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
                joined_items = join_items(
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
                filtered_items = filter_items(
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

            if compiled_step.kind == "aggregate_items":
                assert compiled_step.aggregate_items_spec is not None
                aggregated_items = aggregate_items(
                    compiled_step.step_id,
                    compiled_step.aggregate_items_spec,
                    plan.input_payload,
                    step_outputs,
                )
                step_outputs[compiled_step.as_name or compiled_step.step_id] = (
                    aggregated_items
                )
                if compiled_step.as_name is not None:
                    alias_step_ids[compiled_step.as_name] = compiled_step.step_id
                receipt_builder.record_plan_step(
                    compiled_step.step_id,
                    "aggregate_items",
                    detail={
                        "input_count": aggregated_items["input_count"],
                        "group_count": aggregated_items["group_count"],
                        "output_count": aggregated_items["output_count"],
                        "measures": {
                            name: measure.operation
                            for name, measure in (
                                compiled_step.aggregate_items_spec.measures.items()
                            )
                        },
                        "source_metadata": aggregated_items.get("source_metadata", {}),
                    },
                )
                continue

            if compiled_step.kind == "dedupe_items":
                assert compiled_step.dedupe_items_spec is not None
                deduped_items = dedupe_items(
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
                candidate_set = make_candidate_set(
                    config,
                    compiled_step.step_id,
                    compiled_step.make_candidates_spec,
                    plan.input_payload,
                    step_outputs,
                )
                step_outputs[compiled_step.as_name or compiled_step.step_id] = (
                    candidate_set.model_dump(mode="python")
                )
                if compiled_step.as_name is not None:
                    alias_step_ids[compiled_step.as_name] = compiled_step.step_id
                receipt_builder.record_plan_step(
                    compiled_step.step_id,
                    "make_candidates",
                    detail={
                        "relationship_type": candidate_set.relationship_type,
                        "candidate_count": len(candidate_set.candidates),
                        "duplicate_input_count": candidate_set.duplicate_input_count,
                        "conflicting_duplicate_count": candidate_set.conflicting_duplicate_count,
                        "duplicate_examples": candidate_set.duplicate_examples,
                        "item_count": len(
                            resolve_step_items(
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
                signal_batch = map_signal_batch(
                    compiled_step.step_id,
                    compiled_step.map_signals_spec,
                    plan.input_payload,
                    step_outputs,
                )
                step_outputs[compiled_step.as_name or compiled_step.step_id] = (
                    signal_batch.model_dump(mode="python")
                )
                if compiled_step.as_name is not None:
                    alias_step_ids[compiled_step.as_name] = compiled_step.step_id
                receipt_builder.record_plan_step(
                    compiled_step.step_id,
                    "map_signals",
                    detail={
                        "signal_source": signal_batch.signal_source,
                        "signal_count": len(signal_batch.signals),
                        "mapping": signal_mapping_snapshot(compiled_step.map_signals_spec),
                        "item_count": len(
                            resolve_step_items(
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
                proposal = build_relationship_group_proposal(
                    compiled_step.step_id,
                    compiled_step.propose_relationship_group_spec,
                    plan.input_payload,
                    step_outputs,
                )
                step_outputs[compiled_step.as_name or compiled_step.step_id] = (
                    proposal.model_dump(mode="python")
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
                        "signal_sources_used": proposal.signal_sources_used,
                    },
                )
                continue

            if compiled_step.kind == "make_entities":
                assert compiled_step.make_entities_spec is not None
                entity_set = make_entity_set(
                    config,
                    compiled_step.step_id,
                    compiled_step.make_entities_spec,
                    plan.input_payload,
                    step_outputs,
                )
                step_outputs[compiled_step.as_name or compiled_step.step_id] = (
                    entity_set.model_dump(mode="python")
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
                            resolve_step_items(
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
                relationship_set = make_relationship_set(
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
                            resolve_step_items(
                                compiled_step.make_relationships_spec.items,
                                plan.input_payload,
                                step_outputs,
                            )
                        ),
                        "duplicate_input_count": relationship_set.duplicate_input_count,
                        "conflicting_duplicate_count": (
                            relationship_set.conflicting_duplicate_count
                        ),
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
                entity_preview = apply_entity_set(
                    instance,
                    graph,
                    compiled_step.step_id,
                    step_outputs[compiled_step.apply_entities_spec.entities_from],
                    receipt_builder,
                    persist_writes=execution_action == "apply",
                    parent_id=step_node,
                )
                preview_payload = entity_preview.model_dump(mode="python")
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
                relationship_preview = apply_relationship_set(
                    instance,
                    graph,
                    workflow_name,
                    compiled_step.step_id,
                    step_outputs[compiled_step.apply_relationships_spec.relationships_from],
                    receipt_builder,
                    persist_writes=execution_action == "apply",
                    parent_id=step_node,
                )
                preview_payload = relationship_preview.model_dump(mode="python")
                step_outputs[compiled_step.as_name or compiled_step.step_id] = preview_payload
                apply_previews[compiled_step.step_id] = preview_payload
                if compiled_step.as_name is not None:
                    alias_step_ids[compiled_step.as_name] = compiled_step.step_id
                receipt_builder.record_validation(True, detail=preview_payload, parent_id=step_node)
                continue

            if compiled_step.kind == "assert":
                execute_assert_step(
                    compiled_step,
                    plan.input_payload,
                    step_outputs,
                    receipt_builder,
                )
                continue
            if compiled_step.kind == "assert_not_truncated":
                execute_assert_not_truncated_step(
                    compiled_step,
                    step_outputs,
                    receipt_builder,
                )
                continue
            if compiled_step.kind == "assert_count":
                execute_assert_count_step(
                    compiled_step,
                    plan.input_payload,
                    step_outputs,
                    receipt_builder,
                )
                continue
            assert compiled_step.kind == "assert_exists"
            execute_assert_exists_step(
                compiled_step,
                plan.input_payload,
                step_outputs,
                receipt_builder,
            )

        output = step_outputs[plan.returns]
        output = _validate_workflow_output_contract(
            config,
            workflow_name,
            workflow,
            output,
        )
        read_metadata = _aggregate_workflow_read_metadata(
            plan,
            step_outputs,
            query_receipt_ids,
        )
        success_results = [{"output": output}]
        receipt_builder.record_results(success_results)
        results_recorded = True
        receipt = receipt_builder.build(results=success_results)
        apply_digest = compute_apply_digest(plan, head_snapshot_id, apply_previews)
        _annotate_workflow_receipt(
            receipt,
            plan=plan,
            result_mode=result_mode,
            apply_digest=apply_digest,
            read_metadata=read_metadata,
        )

        if workflow_type == "canonical" and execution_action == "apply":
            snapshot = instance.commit_graph_snapshot(graph)
            committed_snapshot_id = snapshot.snapshot_id
            receipt.nodes[0].detail["committed_snapshot_id"] = committed_snapshot_id
            receipt.committed = True
    except Exception as exc:
        failed_receipt = _build_failed_workflow_receipt(
            receipt_builder,
            plan=plan,
            result_mode=result_mode,
            error=exc,
            results_recorded=results_recorded,
            step_outputs=step_outputs,
            query_receipt_ids=query_receipt_ids,
        )
        if persist_receipt:
            persist_workflow_receipt(instance, failed_receipt)
        raise

    if persist_receipt:
        persist_workflow_receipt(instance, receipt)

    return WorkflowExecutionResult(
        workflow=workflow_name,
        output=output,
        receipt=receipt,
        mode=result_mode,
        workflow_type=workflow_type,
        apply_digest=apply_digest,
        head_snapshot_id=head_snapshot_id,
        committed_snapshot_id=committed_snapshot_id,
        apply_previews=apply_previews,
        query_receipt_ids=query_receipt_ids,
        read_metadata=read_metadata,
        traces=traces,
        step_outputs=step_outputs,
        alias_step_ids=alias_step_ids,
        step_trace_ids=step_trace_ids,
    )


def _validate_workflow_output_contract(
    config: CoreConfig,
    workflow_name: str,
    workflow: WorkflowSchema,
    output: Any,
) -> Any:
    """Validate final workflow output against optional workflow contract_out."""
    if workflow.contract_out is None:
        return output
    if not isinstance(output, dict):
        raise QueryExecutionError(
            f"Workflow '{workflow_name}' output failed contract: expected dict output"
        )
    return validate_contract_payload(
        config,
        workflow.contract_out,
        output,
        subject=f"Workflow '{workflow_name}' output",
        error_factory=query_execution_error,
    )


def _annotate_workflow_receipt(
    receipt: Receipt,
    *,
    plan: Any,
    result_mode: WorkflowResultMode,
    apply_digest: str | None,
    read_metadata: dict[str, Any] | None = None,
    error: BaseException | None = None,
) -> None:
    """Attach workflow-level result metadata to the receipt root."""
    receipt.nodes[0].detail.update(
        {
            "mode": result_mode,
            "config_digest": plan.config_digest,
            "lock_digest": plan.lock_digest,
            "apply_digest": apply_digest,
            "read_metadata": read_metadata or _empty_workflow_read_metadata([]),
        }
    )
    if error is not None:
        receipt.nodes[0].detail.update(
            {
                "error": str(error),
                "error_type": type(error).__name__,
            }
        )


def _build_failed_workflow_receipt(
    receipt_builder: ReceiptBuilder,
    *,
    plan: Any,
    result_mode: WorkflowResultMode,
    error: BaseException,
    results_recorded: bool,
    step_outputs: dict[str, Any] | None = None,
    query_receipt_ids: list[str] | None = None,
) -> Receipt:
    """Finalize an uncommitted workflow receipt for execution failures."""
    failure_results = [{"output": None, "error": str(error)}]
    if not results_recorded:
        receipt_builder.record_results(failure_results)
    receipt = receipt_builder.build(results=failure_results)
    read_metadata = (
        _aggregate_workflow_read_metadata(plan, step_outputs, query_receipt_ids or [])
        if step_outputs is not None
        else _empty_workflow_read_metadata(query_receipt_ids or [])
    )
    _annotate_workflow_receipt(
        receipt,
        plan=plan,
        result_mode=result_mode,
        apply_digest=None,
        read_metadata=read_metadata,
        error=error,
    )
    return receipt


def _aggregate_workflow_read_metadata(
    plan: Any,
    step_outputs: dict[str, Any],
    query_receipt_ids: list[str],
) -> dict[str, Any]:
    read_steps: list[dict[str, Any]] = []
    for compiled_step in plan.steps:
        output_key = compiled_step.as_name or compiled_step.step_id
        if output_key not in step_outputs:
            continue
        output = step_outputs[output_key]
        if not isinstance(output, dict):
            continue
        metadata = extract_read_metadata(output)
        if not metadata:
            continue
        summary: dict[str, Any] = {
            "step_id": compiled_step.step_id,
            "kind": compiled_step.kind,
            "metadata": metadata,
            "counts": _workflow_read_step_counts(output),
        }
        if compiled_step.as_name is not None:
            summary["alias"] = compiled_step.as_name
        if isinstance(metadata.get("source_step"), str):
            summary["source_step"] = metadata["source_step"]
        if isinstance(metadata.get("source_ref"), str):
            summary["source_ref"] = metadata["source_ref"]
        read_steps.append(summary)

    reasons = _ordered_truncation_reasons(
        reason
        for summary in read_steps
        for reason in summary["metadata"].get("truncation_reasons", [])
        if isinstance(reason, str)
    )
    return {
        "read_steps": read_steps,
        "step_counts": {
            summary["step_id"]: summary["counts"]
            for summary in read_steps
            if summary["counts"]
        },
        "any_read_truncated": any(
            bool(summary["metadata"].get("truncated")) for summary in read_steps
        ),
        "any_query_truncated": any(
            summary["kind"] == "query" and bool(summary["metadata"].get("truncated"))
            for summary in read_steps
        ),
        "truncation_reasons": reasons,
        "query_receipt_ids": list(query_receipt_ids),
    }


def _workflow_read_step_counts(output: dict[str, Any]) -> dict[str, Any]:
    return {
        key: output[key]
        for key in _WORKFLOW_READ_COUNT_KEYS
        if key in output
    }


def _ordered_truncation_reasons(reasons: Any) -> list[str]:
    unique = set(reasons)
    ordered = [reason for reason in _TRUNCATION_REASON_ORDER if reason in unique]
    ordered.extend(sorted(unique.difference(_TRUNCATION_REASON_ORDER)))
    return ordered


def _empty_workflow_read_metadata(query_receipt_ids: list[str]) -> dict[str, Any]:
    return {
        "read_steps": [],
        "step_counts": {},
        "any_read_truncated": False,
        "any_query_truncated": False,
        "truncation_reasons": [],
        "query_receipt_ids": list(query_receipt_ids),
    }


def _clone_graph(graph: EntityGraph) -> EntityGraph:
    """Return an isolated graph copy for canonical preview/apply execution."""
    return EntityGraph.from_dict(graph.to_dict())
