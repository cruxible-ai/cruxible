"""Built-in workflow steps that read data or call providers."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, cast

from cruxible_core.config.schema import AssertSpec, CoreConfig
from cruxible_core.errors import ConfigError, CoreError, QueryExecutionError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.predicate import PredicateValueType, evaluate_typed_comparison
from cruxible_core.provider.registry import resolve_provider
from cruxible_core.provider.types import (
    ExecutionTrace,
    ProviderContext,
    ResolvedArtifact,
)
from cruxible_core.query.engine import execute_query_definition
from cruxible_core.query.enums import QueryVisibilityState
from cruxible_core.query.read_surface import (
    run_query as read_run_query,
)
from cruxible_core.query.types import dump_query_row
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.temporal import utc_now
from cruxible_core.workflow.artifacts import resolve_local_artifact_path
from cruxible_core.workflow.contracts import query_execution_error, validate_contract_payload
from cruxible_core.workflow.refs import resolve_value
from cruxible_core.workflow.step_helpers import (
    attach_query_result_index,
    attach_source_metadata,
    extract_read_metadata,
    source_read_metadata_from_template,
)
from cruxible_core.workflow.tracing import (
    apply_trace_payload_retention,
    build_trace,
    persist_trace,
)
from cruxible_core.workflow.tracing import (
    persist_receipt as persist_workflow_receipt,
)
from cruxible_core.workflow.types import CompiledPlan, CompiledPlanStep, WorkflowLock


def _evaluate_assert(
    left: Any,
    op: str,
    right: Any,
    *,
    value_type: PredicateValueType | None = None,
) -> bool:
    try:
        return evaluate_typed_comparison(left, op, right, value_type=value_type)
    except ValueError as exc:
        raise ConfigError(f"Unsupported assert op '{op}'") from exc


def evaluate_assert_condition(
    spec: AssertSpec,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> tuple[bool, Any, Any]:
    """Resolve and evaluate one assert-shape condition without raising on false."""
    left = resolve_value(spec.left, input_payload, step_outputs)
    right = resolve_value(spec.right, input_payload, step_outputs)
    passed = _evaluate_assert(
        left,
        spec.op,
        right,
        value_type=spec.value_type,
    )
    return passed, left, right


def _resolve_query_relationship_state(
    relationship_state_template: Any,
    step_id: str,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> QueryVisibilityState | None:
    if relationship_state_template is None:
        return None
    relationship_state = resolve_value(
        relationship_state_template,
        input_payload,
        step_outputs,
    )
    if relationship_state not in {"live", "accepted", "pending", "reviewable"}:
        raise QueryExecutionError(
            f"Workflow step '{step_id}' relationship_state must resolve to one of "
            "accepted, live, pending, reviewable"
        )
    return cast(QueryVisibilityState, relationship_state)


def _read_output_metadata(
    *,
    total_results: int,
    returned_results: int,
    limit: int | None = None,
    truncated: bool | None = None,
    limit_truncated: bool = False,
    path_truncated: bool = False,
    truncation_reasons: list[str] | None = None,
    result_shape: str | None = None,
    dedupe: str | None = None,
    relationship_state: str | None = None,
    policy_summary: dict[str, int] | None = None,
    receipt_id: str | None = None,
) -> dict[str, Any]:
    """Build consistent workflow read-step metadata."""
    reasons = list(truncation_reasons or [])
    is_truncated = (
        truncated if truncated is not None else limit_truncated or path_truncated or bool(reasons)
    )
    metadata: dict[str, Any] = {
        "total_results": total_results,
        "returned_results": returned_results,
        "limit": limit,
        "truncated": is_truncated,
        "limit_truncated": limit_truncated,
        "path_truncated": path_truncated,
        "truncation_reasons": reasons,
    }
    if result_shape is not None:
        metadata["result_shape"] = result_shape
    if dedupe is not None:
        metadata["dedupe"] = dedupe
    if relationship_state is not None:
        metadata["relationship_state"] = relationship_state
    if policy_summary is not None:
        metadata["policy_summary"] = dict(policy_summary)
    if receipt_id is not None:
        metadata["receipt_id"] = receipt_id
    return metadata


def execute_query_step(
    instance: InstanceProtocol,
    config: CoreConfig,
    graph: EntityGraph,
    plan: CompiledPlan,
    compiled_step: CompiledPlanStep,
    step_outputs: dict[str, Any],
    alias_step_ids: dict[str, str],
    query_receipt_ids: list[str],
    receipt_builder: ReceiptBuilder,
    *,
    persist_receipt: bool,
) -> None:
    step_params = resolve_value(compiled_step.params_template, plan.input_payload, step_outputs)
    relationship_state = _resolve_query_relationship_state(
        compiled_step.relationship_state_template,
        compiled_step.step_id,
        plan.input_payload,
        step_outputs,
    )
    if compiled_step.inline_query is not None:
        if not isinstance(step_params, dict):
            raise QueryExecutionError(
                f"Workflow query step '{compiled_step.step_id}' params must resolve to a mapping"
            )
        step_params = {**plan.input_payload, **step_params}
        query_name = f"workflow:{plan.workflow}/{compiled_step.step_id}"
        query_result = execute_query_definition(
            config,
            graph,
            query_name,
            compiled_step.inline_query,
            step_params,
            relationship_state=relationship_state,
        )
    else:
        assert compiled_step.query_name is not None
        query_name = compiled_step.query_name
        query_result = read_run_query(
            config,
            graph,
            query_name,
            step_params,
            relationship_state=relationship_state,
        )
    if query_result.receipt is None:
        raise QueryExecutionError(f"Query step '{compiled_step.step_id}' did not produce a receipt")
    if persist_receipt:
        persist_workflow_receipt(instance, query_result.receipt)
    query_receipt_ids.append(query_result.receipt.receipt_id)
    query_metadata = _read_output_metadata(
        total_results=query_result.total_results or len(query_result.results),
        returned_results=len(query_result.results),
        limit=query_result.limit,
        truncated=query_result.truncated,
        limit_truncated=query_result.limit_truncated,
        path_truncated=query_result.path_truncated,
        truncation_reasons=list(query_result.truncation_reasons),
        result_shape=query_result.result_shape,
        dedupe=query_result.dedupe,
        relationship_state=query_result.relationship_state,
        policy_summary=query_result.policy_summary,
        receipt_id=query_result.receipt.receipt_id,
    )
    step_outputs[compiled_step.as_name or compiled_step.step_id] = {
        "results": [
            attach_query_result_index(
                dump_query_row(
                    item,
                    include_source=compiled_step.include_source,
                    mode="json",
                ),
                index,
            )
            for index, item in enumerate(query_result.results)
        ],
        **query_metadata,
        "max_paths": query_result.max_paths,
        "max_paths_per_result": query_result.max_paths_per_result,
        "total_path_count": query_result.total_path_count,
        "retained_path_count": query_result.retained_path_count,
        "steps_executed": query_result.steps_executed,
    }
    if compiled_step.as_name is not None:
        alias_step_ids[compiled_step.as_name] = compiled_step.step_id
    receipt_builder.record_plan_step(
        compiled_step.step_id,
        "query",
        detail={
            "query_name": compiled_step.query_name,
            "inline_query": (
                compiled_step.inline_query.model_dump(mode="python", exclude_none=True)
                if compiled_step.inline_query is not None
                else None
            ),
            "receipt_id": query_result.receipt.receipt_id,
            "params": step_params,
            "relationship_state": relationship_state,
            "include_source": compiled_step.include_source,
            "returned_results": len(query_result.results),
            "total_results": query_result.total_results,
            "truncated": query_result.truncated,
        },
    )


def execute_provider_step(
    instance: InstanceProtocol,
    config: CoreConfig,
    lock: WorkflowLock,
    plan: CompiledPlan,
    compiled_step: CompiledPlanStep,
    step_outputs: dict[str, Any],
    alias_step_ids: dict[str, str],
    traces: list[ExecutionTrace],
    step_trace_ids: dict[str, list[str]],
    receipt_builder: ReceiptBuilder,
    *,
    workflow_name: str,
    persist_traces: bool,
    config_base_path: Path,
    before_provider_invocation: Callable[[], float | None] | None = None,
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
        local_path = resolve_local_artifact_path(locked_artifact.uri, config_base_path)
        artifact = ResolvedArtifact(
            name=locked_provider.artifact,
            kind=locked_artifact.kind,
            uri=locked_artifact.uri,
            local_path=str(local_path) if local_path is not None else None,
            digest=locked_artifact.digest,
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
    timeout_ceiling_s = (
        before_provider_invocation() if before_provider_invocation is not None else None
    )
    if timeout_ceiling_s is None:
        provider_fn = resolve_provider(
            compiled_step.provider_name,
            provider_schema,
            config_base_path=config_base_path,
        )
    else:
        provider_fn = resolve_provider(
            compiled_step.provider_name,
            provider_schema,
            config_base_path=config_base_path,
            timeout_ceiling_s=timeout_ceiling_s,
        )
    started = time.monotonic_ns()
    started_at = utc_now()
    status: Literal["success", "error"] = "success"
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
        source_metadata = source_read_metadata_from_template(
            compiled_step.input_template,
            step_outputs,
        )
        if "items" in provider_output or "results" in provider_output:
            provider_output = attach_source_metadata(provider_output, source_metadata)
    except Exception as exc:
        status = "error"
        error_message = str(exc)
        trace = build_trace(
            workflow_name=workflow_name,
            step_id=compiled_step.step_id,
            provider_name=compiled_step.provider_name,
            provider_version=locked_provider.version,
            provider_ref=locked_provider.ref,
            provider_entrypoint_digest=locked_provider.provider_entrypoint_digest,
            runtime=locked_provider.runtime,
            deterministic=locked_provider.deterministic,
            side_effects=locked_provider.side_effects,
            artifact_name=locked_provider.artifact,
            artifact_digest=artifact.digest if artifact is not None else None,
            input_payload=provider_input,
            output_payload={},
            status=status,
            error=error_message,
            started_at=started_at,
            duration_ms=(time.monotonic_ns() - started) / 1_000_000,
        )
        trace = apply_trace_payload_retention(
            trace,
            retention=config.runtime.trace_payloads,
        )
        if persist_traces:
            trace = persist_trace(instance, trace)
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
        # Preserve typed Cruxible errors (e.g. DataValidationError, ConfigError,
        # CustomerCodeExecutionUnsupportedError) so callers can still branch on
        # the specific failure category; only opaque exceptions are wrapped.
        if isinstance(exc, CoreError):
            raise
        raise QueryExecutionError(error_message or "Provider execution failed") from exc

    trace = build_trace(
        workflow_name=workflow_name,
        step_id=compiled_step.step_id,
        provider_name=compiled_step.provider_name,
        provider_version=locked_provider.version,
        provider_ref=locked_provider.ref,
        provider_entrypoint_digest=locked_provider.provider_entrypoint_digest,
        runtime=locked_provider.runtime,
        deterministic=locked_provider.deterministic,
        side_effects=locked_provider.side_effects,
        artifact_name=locked_provider.artifact,
        artifact_digest=artifact.digest if artifact is not None else None,
        input_payload=provider_input,
        output_payload=provider_output,
        status=status,
        error=error_message,
        started_at=started_at,
        duration_ms=(time.monotonic_ns() - started) / 1_000_000,
    )
    trace = apply_trace_payload_retention(
        trace,
        retention=config.runtime.trace_payloads,
    )
    if persist_traces:
        trace = persist_trace(instance, trace)
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


def execute_assert_step(
    compiled_step: CompiledPlanStep,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
    receipt_builder: ReceiptBuilder,
) -> None:
    assert compiled_step.assert_spec is not None
    passed, left, right = evaluate_assert_condition(
        compiled_step.assert_spec,
        input_payload,
        step_outputs,
    )
    detail = {
        "op": compiled_step.assert_spec.op,
        "left": left,
        "right": right,
        "message": compiled_step.assert_spec.message,
    }
    if compiled_step.assert_spec.value_type is not None:
        detail["value_type"] = compiled_step.assert_spec.value_type
    step_node = receipt_builder.record_plan_step(
        compiled_step.step_id,
        "assert",
        detail=detail,
    )
    receipt_builder.record_validation(
        passed=passed,
        detail={"message": compiled_step.assert_spec.message},
        parent_id=step_node,
    )
    if not passed:
        raise QueryExecutionError(compiled_step.assert_spec.message)


def execute_assert_not_truncated_step(
    compiled_step: CompiledPlanStep,
    step_outputs: dict[str, Any],
    receipt_builder: ReceiptBuilder,
) -> None:
    """Guard that a prior read-derived workflow output is not truncated."""
    assert compiled_step.assert_not_truncated_spec is not None
    source_step = compiled_step.assert_not_truncated_spec.step
    source_output = _get_guard_source_output(compiled_step.step_id, source_step, step_outputs)
    metadata = extract_read_metadata(source_output)
    if not metadata:
        detail = {
            "guard": "assert_not_truncated",
            "step": source_step,
            "metadata_found": False,
        }
        step_node = receipt_builder.record_plan_step(
            compiled_step.step_id,
            "assert_not_truncated",
            detail=detail,
        )
        receipt_builder.record_validation(
            passed=False,
            detail=detail,
            parent_id=step_node,
        )
        raise QueryExecutionError(
            f"assert_not_truncated step '{compiled_step.step_id}' failed for "
            f"'{source_step}': no read metadata found"
        )
    flags = {
        "truncated": bool(metadata.get("truncated")),
        "limit_truncated": bool(metadata.get("limit_truncated")),
        "path_truncated": bool(metadata.get("path_truncated")),
    }
    reasons = [
        reason for reason in metadata.get("truncation_reasons", []) if isinstance(reason, str)
    ]
    active_flags = [name for name, active in flags.items() if active]
    passed = not active_flags
    detail = {
        "guard": "assert_not_truncated",
        "step": source_step,
        "metadata_found": True,
        "flags": flags,
        "truncation_reasons": reasons,
    }
    step_node = receipt_builder.record_plan_step(
        compiled_step.step_id,
        "assert_not_truncated",
        detail=detail,
    )
    receipt_builder.record_validation(
        passed=passed,
        detail=detail,
        parent_id=step_node,
    )
    if not passed:
        flags_text = ", ".join(f"{flag}=true" for flag in active_flags)
        reasons_text = f"; reasons: {', '.join(reasons)}" if reasons else ""
        raise QueryExecutionError(
            f"assert_not_truncated step '{compiled_step.step_id}' failed for "
            f"'{source_step}': {flags_text}{reasons_text}"
        )


def execute_assert_count_step(
    compiled_step: CompiledPlanStep,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
    receipt_builder: ReceiptBuilder,
) -> None:
    """Guard a count from a prior workflow output."""
    assert compiled_step.assert_count_spec is not None
    spec = compiled_step.assert_count_spec
    source_output = _get_guard_source_output(compiled_step.step_id, spec.step, step_outputs)
    actual = _resolve_guard_count(compiled_step.step_id, spec.step, source_output, spec.count)
    expected = resolve_value(spec.value, input_payload, step_outputs)
    if not isinstance(expected, int) or isinstance(expected, bool):
        raise QueryExecutionError(
            f"assert_count step '{compiled_step.step_id}' value must resolve to an integer"
        )
    passed = _evaluate_assert(actual, spec.op, expected)
    message = spec.message or (
        f"assert_count step '{compiled_step.step_id}' failed: "
        f"{spec.step}.{spec.count} {spec.op} {expected}"
    )
    detail = {
        "guard": "assert_count",
        "step": spec.step,
        "count": spec.count,
        "actual": actual,
        "op": spec.op,
        "expected": expected,
        "message": message,
    }
    step_node = receipt_builder.record_plan_step(
        compiled_step.step_id,
        "assert_count",
        detail=detail,
    )
    receipt_builder.record_validation(
        passed=passed,
        detail=detail,
        parent_id=step_node,
    )
    if not passed:
        raise QueryExecutionError(message)


def execute_assert_exists_step(
    compiled_step: CompiledPlanStep,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
    receipt_builder: ReceiptBuilder,
) -> None:
    """Guard that one workflow reference resolves to a present value."""
    assert compiled_step.assert_exists_spec is not None
    spec = compiled_step.assert_exists_spec
    resolved = None
    resolution_error: str | None = None
    try:
        resolved = resolve_value(spec.ref, input_payload, step_outputs)
    except QueryExecutionError as exc:
        resolution_error = str(exc)
    present = resolution_error is None and _value_is_present(resolved)
    message = spec.message or (
        f"assert_exists step '{compiled_step.step_id}' failed: reference '{spec.ref}' is required"
    )
    detail: dict[str, Any] = {
        "guard": "assert_exists",
        "ref": spec.ref,
        "present": present,
        "message": message,
    }
    if resolution_error is not None:
        detail["resolution_error"] = resolution_error
    step_node = receipt_builder.record_plan_step(
        compiled_step.step_id,
        "assert_exists",
        detail=detail,
    )
    receipt_builder.record_validation(
        passed=present,
        detail=detail,
        parent_id=step_node,
    )
    if not present:
        raise QueryExecutionError(message)


def _get_guard_source_output(
    guard_step_id: str,
    source_step: str,
    step_outputs: dict[str, Any],
) -> Any:
    if source_step not in step_outputs:
        raise QueryExecutionError(
            f"Workflow guard step '{guard_step_id}' references unknown step output '{source_step}'"
        )
    return step_outputs[source_step]


def _resolve_guard_count(
    guard_step_id: str,
    source_step: str,
    source_output: Any,
    selector: str,
) -> int:
    if not isinstance(source_output, dict):
        raise QueryExecutionError(
            f"assert_count step '{guard_step_id}' source step '{source_step}' "
            "did not produce an object"
        )
    if selector in {"returned_results", "total_results"}:
        metadata = extract_read_metadata(source_output)
        value = metadata.get(selector)
        if not isinstance(value, int) or isinstance(value, bool):
            raise QueryExecutionError(
                f"assert_count step '{guard_step_id}' could not read count "
                f"'{selector}' from '{source_step}'"
            )
        return value
    if selector in {"items", "results"}:
        collection = source_output.get(selector)
        if not isinstance(collection, list):
            raise QueryExecutionError(
                f"assert_count step '{guard_step_id}' expected '{source_step}.{selector}' "
                "to be a list"
            )
        return len(collection)
    raise QueryExecutionError(
        f"assert_count step '{guard_step_id}' has unsupported count selector '{selector}'"
    )


def _value_is_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and value == "":
        return False
    return True
