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
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.receipt.types import Receipt
from cruxible_core.workflow.apply import compute_apply_digest
from cruxible_core.workflow.compiler import compile_workflow, load_lock, resolve_lock_path
from cruxible_core.workflow.contracts import query_execution_error, validate_contract_payload
from cruxible_core.workflow.execution_context import WorkflowExecutionContext
from cruxible_core.workflow.step_handlers import DEFAULT_STEP_HANDLER_REGISTRY
from cruxible_core.workflow.step_helpers import extract_read_metadata
from cruxible_core.workflow.tracing import persist_receipt as persist_workflow_receipt
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
FAILED_WORKFLOW_RECEIPT_ATTR = "_cruxible_failed_workflow_receipt"
FAILED_WORKFLOW_RECEIPT_PERSISTED_ATTR = "_cruxible_failed_workflow_receipt_persisted"


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
    actor_context: GovernedActorContext | None = None,
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
    config_base_path = instance.get_config_path().parent
    plan = compile_workflow(
        config,
        lock,
        workflow_name,
        input_payload,
        config_base_path=config_base_path,
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
            raise ConfigError(f"{workflow_type} workflows only support executor action 'run'")
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

    context = WorkflowExecutionContext(
        instance=instance,
        config=config,
        workflow_name=workflow_name,
        workflow=workflow,
        lock=lock,
        plan=plan,
        graph=graph,
        receipt_builder=receipt_builder,
        execution_action=execution_action,
        result_mode=result_mode,
        persist_receipt=persist_receipt,
        persist_query_receipts=persist_query_receipts,
        persist_traces=persist_traces,
        config_base_path=config_base_path,
        head_snapshot_id=head_snapshot_id,
        actor_context=actor_context,
    )
    results_recorded = False
    committed_snapshot_id: str | None = None

    try:
        for compiled_step in context.plan.steps:
            DEFAULT_STEP_HANDLER_REGISTRY.execute(context, compiled_step)

        output = context.step_outputs[context.plan.returns]
        output = _validate_workflow_output_contract(
            config,
            workflow_name,
            workflow,
            output,
        )
        read_metadata = _aggregate_workflow_read_metadata(
            context.plan,
            context.step_outputs,
            context.query_receipt_ids,
        )
        success_results = [{"output": output}]
        context.receipt_builder.record_results(success_results)
        results_recorded = True
        receipt = context.receipt_builder.build(results=success_results)
        apply_digest = compute_apply_digest(
            context.plan,
            context.head_snapshot_id,
            context.apply_previews,
        )
        _annotate_workflow_receipt(
            receipt,
            plan=context.plan,
            result_mode=context.result_mode,
            apply_digest=apply_digest,
            read_metadata=read_metadata,
        )

        if workflow_type == "canonical" and context.execution_action == "apply":
            snapshot = instance.commit_graph_snapshot(
                context.graph,
                entities=list(context.applied_entities.values()),
                relationships=list(context.applied_relationships.values()),
                actor_context=context.actor_context,
            )
            committed_snapshot_id = snapshot.snapshot_id
            receipt.nodes[0].detail["committed_snapshot_id"] = committed_snapshot_id
            receipt.committed = True
    except Exception as exc:
        failed_receipt = _build_failed_workflow_receipt(
            context.receipt_builder,
            plan=context.plan,
            result_mode=context.result_mode,
            error=exc,
            results_recorded=results_recorded,
            step_outputs=context.step_outputs,
            query_receipt_ids=context.query_receipt_ids,
        )
        setattr(exc, FAILED_WORKFLOW_RECEIPT_ATTR, failed_receipt)
        defer_failed_receipt = (
            workflow_type == "canonical"
            and execution_action == "apply"
            and getattr(instance, "_active_uow", None) is not None
        )
        if persist_receipt and not defer_failed_receipt:
            persist_workflow_receipt(instance, failed_receipt)
            setattr(exc, FAILED_WORKFLOW_RECEIPT_PERSISTED_ATTR, True)
        raise

    if persist_receipt:
        persist_workflow_receipt(instance, receipt)

    return WorkflowExecutionResult(
        workflow=workflow_name,
        output=output,
        receipt=receipt,
        mode=context.result_mode,
        workflow_type=workflow_type,
        apply_digest=apply_digest,
        head_snapshot_id=context.head_snapshot_id,
        committed_snapshot_id=committed_snapshot_id,
        apply_previews=context.apply_previews,
        query_receipt_ids=context.query_receipt_ids,
        read_metadata=read_metadata,
        traces=context.traces,
        step_outputs=context.step_outputs,
        alias_step_ids=context.alias_step_ids,
        step_trace_ids=context.step_trace_ids,
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
            summary["step_id"]: summary["counts"] for summary in read_steps if summary["counts"]
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
    return {key: output[key] for key in _WORKFLOW_READ_COUNT_KEYS if key in output}


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
