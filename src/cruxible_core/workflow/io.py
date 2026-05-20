"""Built-in workflow steps that read data or call providers."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Literal, cast

from cruxible_core.config.schema import CoreConfig, ListEntitiesSpec, ListRelationshipsSpec
from cruxible_core.errors import ConfigError, QueryExecutionError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.predicate import PredicateValueType, evaluate_typed_comparison
from cruxible_core.provider.registry import resolve_provider
from cruxible_core.provider.types import (
    ExecutionTrace,
    ProviderContext,
    ResolvedArtifact,
)
from cruxible_core.query.enums import QueryRelationshipState
from cruxible_core.query.read_surface import (
    list_entities as read_list_entities,
)
from cruxible_core.query.read_surface import (
    list_relationships as read_list_relationships,
)
from cruxible_core.query.read_surface import (
    run_query as read_run_query,
)
from cruxible_core.query.types import dump_query_row
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.temporal import utc_now
from cruxible_core.workflow.artifacts import resolve_local_artifact_path
from cruxible_core.workflow.contracts import query_execution_error, validate_contract_payload
from cruxible_core.workflow.refs import resolve_value
from cruxible_core.workflow.tracing import (
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


def _resolve_query_relationship_state(
    relationship_state_template: Any,
    step_id: str,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> QueryRelationshipState | None:
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
    return cast(QueryRelationshipState, relationship_state)


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
        truncated
        if truncated is not None
        else limit_truncated or path_truncated or bool(reasons)
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


def list_entities_step(
    config: CoreConfig,
    graph: EntityGraph,
    step_id: str,
    spec: ListEntitiesSpec,
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
    limit_truncated = limit is not None and len(items) < result.total
    return {
        "items": items,
        "total": result.total,
        **_read_output_metadata(
            total_results=result.total,
            returned_results=len(items),
            limit=limit,
            limit_truncated=limit_truncated,
            path_truncated=False,
            truncation_reasons=["limit"] if limit_truncated else [],
        ),
    }


def list_relationships_step(
    graph: EntityGraph,
    step_id: str,
    spec: ListRelationshipsSpec,
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
    limit_truncated = limit is not None and len(result.items) < result.total
    return {
        "items": result.items,
        "total": result.total,
        **_read_output_metadata(
            total_results=result.total,
            returned_results=len(result.items),
            limit=limit,
            limit_truncated=limit_truncated,
            path_truncated=False,
            truncation_reasons=["limit"] if limit_truncated else [],
        ),
    }


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
    assert compiled_step.query_name is not None
    step_params = resolve_value(compiled_step.params_template, plan.input_payload, step_outputs)
    relationship_state = _resolve_query_relationship_state(
        compiled_step.relationship_state_template,
        compiled_step.step_id,
        plan.input_payload,
        step_outputs,
    )
    query_result = read_run_query(
        config,
        graph,
        compiled_step.query_name,
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
            dump_query_row(item, include_source=compiled_step.include_source)
            for item in query_result.results
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
            "receipt_id": query_result.receipt.receipt_id,
            "params": step_params,
            "relationship_state": relationship_state,
            "include_source": compiled_step.include_source,
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
    provider_fn = resolve_provider(
        compiled_step.provider_name,
        provider_schema,
        config_base_path=config_base_path,
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
    except Exception as exc:
        status = "error"
        error_message = str(exc)
        trace = build_trace(
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
            persist_trace(instance, trace)
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

    trace = build_trace(
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
        persist_trace(instance, trace)
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
    left = resolve_value(compiled_step.assert_spec.left, input_payload, step_outputs)
    right = resolve_value(compiled_step.assert_spec.right, input_payload, step_outputs)
    passed = _evaluate_assert(
        left,
        compiled_step.assert_spec.op,
        right,
        value_type=compiled_step.assert_spec.value_type,
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
