"""Built-in workflow steps that read data or call providers."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from cruxible_core.config.schema import CoreConfig, ListEntitiesSpec, ListRelationshipsSpec
from cruxible_core.errors import ConfigError, QueryExecutionError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.predicate import evaluate_comparison
from cruxible_core.provider.registry import resolve_provider
from cruxible_core.provider.types import (
    ExecutionTrace,
    ProviderContext,
    ResolvedArtifact,
)
from cruxible_core.query.read_surface import (
    list_entities as read_list_entities,
)
from cruxible_core.query.read_surface import (
    list_relationships as read_list_relationships,
)
from cruxible_core.query.read_surface import (
    run_query as read_run_query,
)
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.workflow.contracts import query_execution_error, validate_contract_payload
from cruxible_core.workflow.refs import resolve_value
from cruxible_core.workflow.tracing import _build_trace, _persist_receipt, _persist_trace
from cruxible_core.workflow.types import CompiledPlan, CompiledPlanStep, WorkflowLock


def _evaluate_assert(left: Any, op: str, right: Any) -> bool:
    try:
        return evaluate_comparison(left, op, right)
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


def _list_entities(
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
    return {
        "items": items,
        "total": result.total,
    }


def _list_relationships(
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
    return {
        "items": result.items,
        "total": result.total,
    }


def _execute_query_step(
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
    provider_fn = resolve_provider(
        compiled_step.provider_name,
        provider_schema,
        config_base_path=config_base_path,
    )
    started = time.monotonic_ns()
    started_at = datetime.now(timezone.utc)
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
    compiled_step: CompiledPlanStep,
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
