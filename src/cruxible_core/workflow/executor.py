"""Workflow execution runtime.

This module is the workflow engine's coordinator. It does not own step-specific
business rules; instead it compiles the workflow, dispatches each compiled step
to the helper that owns that step kind, records receipt nodes, and decides
whether canonical apply previews stay isolated or become committed graph state.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from cruxible_core.config.schema import CoreConfig, WorkflowSchema
from cruxible_core.errors import ConfigError, QueryExecutionError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.procedure.types import ABORT_TARGET
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.receipt.types import Receipt
from cruxible_core.workflow.apply import compute_apply_digest
from cruxible_core.workflow.compiler import compile_workflow, load_lock, resolve_lock_path
from cruxible_core.workflow.contracts import query_execution_error, validate_contract_payload
from cruxible_core.workflow.execution_context import (
    ProcedureExecutionBudget,
    WorkflowExecutionContext,
)
from cruxible_core.workflow.step_handlers import (
    DEFAULT_STEP_HANDLER_REGISTRY,
    PROCEDURE_STEP_HANDLER_REGISTRY,
    procedure_runtime_reference_error,
)
from cruxible_core.workflow.step_helpers import extract_read_metadata
from cruxible_core.workflow.tracing import persist_receipt as persist_workflow_receipt
from cruxible_core.workflow.types import (
    CompiledPlan,
    CompiledPlanStep,
    WorkflowExecutionResult,
    WorkflowLock,
)
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


def execute_procedure_plan(
    instance: InstanceProtocol,
    config: CoreConfig,
    definition: Any,
    plan: CompiledPlan,
    lock: WorkflowLock,
    receipt_builder: ReceiptBuilder,
    budget: ProcedureExecutionBudget,
    *,
    persist_query_receipts: bool = True,
    persist_traces: bool = True,
    actor_context: GovernedActorContext | None = None,
    procedure_id: str | None = None,
    procedure_definition_digest: str | None = None,
    procedure_run_id: str | None = None,
    procedure_dry_run: bool = False,
) -> WorkflowExecutionResult:
    """Execute a compiled procedure through the shared workflow step machinery.

    Receipt and run-record persistence remain service-owned so they can be
    finalized atomically. Provider traces and nested query receipts retain their
    established independent persistence behavior.
    """
    head_snapshot_id = instance.get_head_snapshot_id()
    context = WorkflowExecutionContext(
        instance=instance,
        config=config,
        workflow_name=definition.name,
        workflow=definition,
        lock=lock,
        plan=plan,
        graph=instance.load_graph(),
        receipt_builder=receipt_builder,
        execution_action="run",
        result_mode="run",
        persist_receipt=False,
        persist_query_receipts=persist_query_receipts,
        persist_traces=persist_traces,
        config_base_path=instance.get_config_path().parent,
        head_snapshot_id=head_snapshot_id,
        actor_context=actor_context,
        procedure_budget=budget,
        procedure_id=procedure_id,
        procedure_definition_digest=procedure_definition_digest,
        procedure_run_id=procedure_run_id,
        procedure_dry_run=procedure_dry_run,
    )
    results_recorded = False

    try:
        _walk_procedure_successors(context)
        context.check_procedure_wall_clock()

        output = _resolve_procedure_output(context)
        output = _validate_procedure_output_contract(
            config,
            definition.name,
            definition,
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
        _annotate_workflow_receipt(
            receipt,
            plan=context.plan,
            result_mode="run",
            apply_digest=None,
            read_metadata=read_metadata,
        )
    except Exception as exc:
        failed_receipt = _build_failed_workflow_receipt(
            context.receipt_builder,
            plan=context.plan,
            result_mode="run",
            error=exc,
            results_recorded=results_recorded,
            step_outputs=context.step_outputs,
            query_receipt_ids=context.query_receipt_ids,
        )
        setattr(exc, FAILED_WORKFLOW_RECEIPT_ATTR, failed_receipt)
        raise

    return WorkflowExecutionResult(
        workflow=definition.name,
        output=output,
        receipt=receipt,
        mode="run",
        workflow_type="utility",
        head_snapshot_id=head_snapshot_id,
        query_receipt_ids=context.query_receipt_ids,
        read_metadata=read_metadata,
        traces=context.traces,
        step_outputs=context.step_outputs,
        alias_step_ids=context.alias_step_ids,
        step_trace_ids=context.step_trace_ids,
        procedure_group_proposals=context.procedure_group_proposals,
    )


def _walk_procedure_successors(context: WorkflowExecutionContext) -> None:
    """Execute a compiled procedure by following its successor function.

    LINEARITY IS NOT A SPECIAL CASE. With no declared control edges the
    successor of every step is the next step in list order, so this walk visits
    exactly the sequence the flat loop visited -- the same code path, not a
    branch around it.

    Control edges were resolved to step ids at compile time and R1/R2/R3 have
    already been checked, so the successor function here is a dict lookup. The
    forward-only rule bounds the walk without a visited set: every hop moves
    strictly later in the list.
    """
    steps = context.plan.steps
    if not steps:
        return
    by_id = {step.step_id: step for step in steps}
    next_in_order = {
        step.step_id: steps[index + 1].step_id
        for index, step in enumerate(steps)
        if index + 1 < len(steps)
    }
    current: str | None = steps[0].step_id
    while current is not None:
        compiled_step = by_id[current]
        context.check_procedure_wall_clock()
        PROCEDURE_STEP_HANDLER_REGISTRY.execute(context, compiled_step)
        current = _procedure_successor(context, compiled_step, next_in_order)


def _procedure_successor(
    context: WorkflowExecutionContext,
    compiled_step: CompiledPlanStep,
    next_in_order: dict[str, str],
) -> str | None:
    fallthrough = next_in_order.get(compiled_step.step_id)
    if compiled_step.kind == "guard":
        arm = context.guard_outcomes[compiled_step.step_id]
        target = (
            compiled_step.on_true_step_id if arm == "on_true" else compiled_step.on_false_step_id
        )
        if target == ABORT_TARGET:
            # The sentinel terminates the run with the node's message -- the
            # same outcome, and the same failure shape, as the assert kinds
            # this node supersedes.
            raise QueryExecutionError(
                compiled_step.guard_message or f"guard step '{compiled_step.step_id}' failed"
            )
        return target if target is not None else fallthrough
    if compiled_step.next_step_id is not None:
        return compiled_step.next_step_id
    return fallthrough


def _resolve_procedure_output(context: WorkflowExecutionContext) -> Any:
    """Resolve the declared return key without leaking low-level container errors."""
    reference = context.plan.returns
    try:
        return context.step_outputs[reference]
    except (KeyError, IndexError, AttributeError) as exc:
        step_id = _procedure_return_step_id(context, reference)
        raise procedure_runtime_reference_error(step_id, reference, exc) from exc


def _procedure_return_step_id(
    context: WorkflowExecutionContext,
    reference: str,
) -> str:
    """Map a return alias or legacy ``$steps`` ref to its concrete step id."""
    alias = reference
    if reference.startswith("$steps."):
        alias = reference[len("$steps.") :].split(".", 1)[0].split("[", 1)[0]
    return context.alias_step_ids.get(alias, alias)


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


def _validate_procedure_output_contract(
    config: CoreConfig,
    procedure_name: str,
    definition: Any,
    output: Any,
) -> Any:
    if definition.contract_out is None:
        return output
    if not isinstance(output, dict):
        raise QueryExecutionError(
            f"Procedure '{procedure_name}' output failed contract: expected dict output"
        )
    return validate_contract_payload(
        config,
        definition.contract_out,
        output,
        subject=f"Procedure '{procedure_name}' output",
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
    """Return an isolated graph copy for canonical preview/apply execution.

    ``to_dict`` (networkx ``node_link_data``) only shallow-copies node/edge
    attribute containers, leaving the nested ``properties``/``metadata`` dicts
    shared with the source graph. In-place mutations such as
    ``update_entity_properties`` would then bleed back into the live cached
    graph, so a preview/dry-run could silently mutate live state. ``deepcopy``
    of the serialized payload severs that sharing, matching the isolation the
    direct-write path already builds via ``deepcopy(current_graph.to_dict())``.
    """
    return EntityGraph.from_dict(deepcopy(graph.to_dict()))
