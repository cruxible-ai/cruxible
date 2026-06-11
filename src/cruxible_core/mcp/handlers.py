"""Handler implementations for MCP tools.

Public MCP handlers can delegate to a governed server when server mode is
configured. In local mode, they forward to the shared runtime API.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import yaml

from cruxible_client import CruxibleClient, contracts
from cruxible_core.config.composer import compose_config_sequence, resolve_config_layers
from cruxible_core.config.loader import load_config
from cruxible_core.errors import ConfigError
from cruxible_core.runtime import api
from cruxible_core.runtime.instance import CruxibleInstance  # noqa: F401
from cruxible_core.runtime.instance_manager import (
    InstanceManager,
)
from cruxible_core.runtime.instance_manager import (
    get_manager as runtime_get_manager,
)
from cruxible_core.server.config import get_runtime_bearer_token, resolve_server_settings

_client_cache: CruxibleClient | None = None
_client_cache_key: tuple[str | None, str | None, str | None] | None = None
ResultT = TypeVar("ResultT")


def get_manager() -> InstanceManager:
    """Return the process-global instance manager."""
    return runtime_get_manager()


def reset_client_cache() -> None:
    """Clear cached client state. Used by tests."""
    global _client_cache, _client_cache_key
    if _client_cache is not None:
        _client_cache.close()
    _client_cache = None
    _client_cache_key = None


def _get_client() -> CruxibleClient | None:
    """Return a configured HTTP client in server mode."""
    global _client_cache, _client_cache_key

    settings = resolve_server_settings()
    if not settings.enabled:
        reset_client_cache()
        return None

    token = get_runtime_bearer_token()
    cache_key = (settings.server_url, settings.server_socket, token)
    if _client_cache is None or _client_cache_key != cache_key:
        reset_client_cache()
        _client_cache = CruxibleClient(
            base_url=settings.server_url,
            socket_path=settings.server_socket,
            token=token,
        )
        _client_cache_key = cache_key
    return _client_cache


def _dispatch_remote_or_local(
    remote_call: Callable[[CruxibleClient], ResultT],
    local_call: Callable[[], ResultT],
    *,
    allow_local: bool = True,
    operation_name: str | None = None,
) -> ResultT:
    """Route a handler to the configured HTTP client when server mode is enabled."""
    client = _get_client()
    if client is not None:
        return remote_call(client)
    if not allow_local:
        raise ConfigError(
            f"Local mutation disabled for {operation_name or 'this operation'}; configure a server."
        )
    return local_call()


def _required_pending_version(expected_pending_version: int | None) -> int:
    if expected_pending_version is None:
        raise ConfigError("expected_pending_version is required when resolving via server mode")
    return expected_pending_version


def _config_yaml_for_upload(config_path: str, *, root_dir: str | None = None) -> str:
    """Read a config file and compose overlays before uploading to the daemon."""
    path = Path(config_path)
    if not path.is_absolute() and root_dir is not None:
        path = Path(root_dir) / path
    config = load_config(path)
    composed = compose_config_sequence(
        resolve_config_layers(config, config_path=path.resolve()),
    )
    composed_data = composed.model_dump(mode="python", by_alias=True, exclude_none=True)
    return yaml.safe_dump(composed_data, default_flow_style=False, sort_keys=False)


def handle_init(
    root_dir: str,
    config_path: str | None = None,
    config_yaml: str | None = None,
    data_dir: str | None = None,
    kit: str | None = None,
) -> contracts.InitResult:
    """Initialize a new cruxible instance, or reload an existing one."""
    uploaded_yaml = config_yaml
    if uploaded_yaml is None and config_path is not None:
        uploaded_yaml = _config_yaml_for_upload(config_path, root_dir=root_dir)

    def _remote_init(client: CruxibleClient) -> contracts.InitResult:
        return client.init(
            root_dir=root_dir,
            config_path=None,
            config_yaml=uploaded_yaml,
            data_dir=data_dir,
            kit=kit,
        )

    return _dispatch_remote_or_local(
        _remote_init,
        lambda: api.init_local(root_dir, config_path, config_yaml, data_dir, kit),
        allow_local=False,
        operation_name="cruxible_init",
    )


def handle_validate(
    config_path: str | None = None,
    config_yaml: str | None = None,
) -> contracts.ValidateResult:
    """Validate a config file or inline YAML string."""
    uploaded_yaml = config_yaml
    if uploaded_yaml is None and config_path is not None:
        uploaded_yaml = _config_yaml_for_upload(config_path)
    return _dispatch_remote_or_local(
        lambda client: client.validate(config_path=None, config_yaml=uploaded_yaml),
        lambda: api.validate(config_path, config_yaml),
    )


def handle_server_info() -> contracts.ServerInfoResult:
    """Return live daemon metadata such as agent mode and state dir."""
    return _dispatch_remote_or_local(
        lambda client: client.server_info(),
        api.server_info,
    )


def handle_create_state_overlay(
    root_dir: str,
    transport_ref: str | None = None,
    state_ref: str | None = None,
    kit: str | None = None,
    no_kit: bool = False,
) -> contracts.StateOverlayResult:
    """Create a new governed overlay from a published state release."""
    return _dispatch_remote_or_local(
        lambda client: client.create_state_overlay(
            root_dir=root_dir,
            transport_ref=transport_ref,
            state_ref=state_ref,
            kit=kit,
            no_kit=no_kit,
        ),
        lambda: api.create_state_overlay_local(
            transport_ref,
            state_ref,
            kit,
            no_kit,
            root_dir,
        ),
        allow_local=False,
        operation_name="cruxible_state_create_overlay",
    )


def handle_workflow_lock(instance_id: str, force: bool = False) -> contracts.WorkflowLockResult:
    """Generate a workflow lock file for an instance."""
    return _dispatch_remote_or_local(
        lambda client: client.workflow_lock(instance_id, force=force),
        lambda: api.workflow_lock(instance_id, force=force),
    )


def handle_workflow_plan(
    instance_id: str,
    workflow_name: str,
    input_payload: dict[str, Any] | None = None,
) -> contracts.WorkflowPlanResult:
    """Compile a configured workflow plan."""
    return _dispatch_remote_or_local(
        lambda client: client.workflow_plan(
            instance_id,
            workflow_name=workflow_name,
            input_payload=input_payload or {},
        ),
        lambda: api.workflow_plan(
            instance_id,
            workflow_name,
            input_payload,
        ),
    )


def handle_workflow_run(
    instance_id: str,
    workflow_name: str,
    input_payload: dict[str, Any] | None = None,
    decision_record_id: str | None = None,
) -> contracts.WorkflowRunResult:
    """Execute a configured workflow."""
    decision_kwargs = (
        {"decision_record_id": decision_record_id} if decision_record_id is not None else {}
    )
    return _dispatch_remote_or_local(
        lambda client: client.workflow_run(
            instance_id,
            workflow_name=workflow_name,
            input_payload=input_payload or {},
            **decision_kwargs,
        ),
        lambda: api.workflow_run(
            instance_id,
            workflow_name,
            input_payload,
            decision_record_id=decision_record_id,
            surface="mcp",
        ),
        allow_local=False,
        operation_name="cruxible_run_workflow",
    )


def handle_workflow_apply(
    instance_id: str,
    workflow_name: str,
    *,
    expected_apply_digest: str,
    expected_head_snapshot_id: str | None = None,
    input_payload: dict[str, Any] | None = None,
    decision_record_id: str | None = None,
) -> contracts.WorkflowApplyResult:
    """Commit a previously previewed canonical workflow after verifying identity."""
    decision_kwargs = (
        {"decision_record_id": decision_record_id} if decision_record_id is not None else {}
    )
    return _dispatch_remote_or_local(
        lambda client: client.workflow_apply(
            instance_id,
            workflow_name=workflow_name,
            expected_apply_digest=expected_apply_digest,
            expected_head_snapshot_id=expected_head_snapshot_id,
            input_payload=input_payload or {},
            **decision_kwargs,
        ),
        lambda: api.workflow_apply(
            instance_id,
            workflow_name,
            expected_apply_digest,
            expected_head_snapshot_id,
            input_payload,
            decision_record_id=decision_record_id,
            surface="mcp",
        ),
        allow_local=False,
        operation_name="cruxible_apply_workflow",
    )


def handle_workflow_test(
    instance_id: str,
    name: str | None = None,
) -> contracts.WorkflowTestResult:
    """Run configured workflow tests for an instance."""
    return _dispatch_remote_or_local(
        lambda client: client.workflow_test(instance_id, name=name),
        lambda: api.workflow_test(instance_id, name),
        allow_local=False,
        operation_name="cruxible_test_workflow",
    )


def handle_propose_workflow(
    instance_id: str,
    workflow_name: str,
    input_payload: dict[str, Any] | None = None,
    decision_record_id: str | None = None,
) -> contracts.WorkflowProposeResult:
    """Execute a workflow and create a governed relationship proposal."""
    decision_kwargs = (
        {"decision_record_id": decision_record_id} if decision_record_id is not None else {}
    )
    return _dispatch_remote_or_local(
        lambda client: client.propose_workflow(
            instance_id,
            workflow_name=workflow_name,
            input_payload=input_payload or {},
            **decision_kwargs,
        ),
        lambda: api.propose_workflow(
            instance_id,
            workflow_name,
            input_payload,
            decision_record_id=decision_record_id,
            surface="mcp",
        ),
        allow_local=False,
        operation_name="cruxible_propose_workflow",
    )


def handle_query(
    instance_id: str,
    query_name: str,
    params: dict[str, Any] | None = None,
    limit: int | None = None,
    offset: int = 0,
    relationship_state: contracts.QueryRelationshipState | None = None,
    decision_record_id: str | None = None,
) -> contracts.QueryToolResult:
    """Execute a named query."""
    return _dispatch_remote_or_local(
        lambda client: _client_query(
            client,
            instance_id=instance_id,
            query_name=query_name,
            params=params,
            limit=limit,
            offset=offset,
            relationship_state=relationship_state,
            decision_record_id=decision_record_id,
        ),
        lambda: api.query(
            instance_id,
            query_name,
            params,
            limit=limit,
            offset=offset,
            relationship_state=relationship_state,
            decision_record_id=decision_record_id,
            surface="mcp",
        ),
    )


def handle_query_inline(
    instance_id: str,
    definition: contracts.InlineQueryDefinition,
    params: dict[str, Any] | None = None,
    limit: int | None = None,
    relationship_state: contracts.QueryRelationshipState | None = None,
    decision_record_id: str | None = None,
) -> contracts.QueryToolResult:
    """Execute a bounded inline query definition without persisting it to config."""
    return _dispatch_remote_or_local(
        lambda client: client.query_inline(
            instance_id,
            definition,
            params,
            limit=limit,
            relationship_state=relationship_state,
            decision_record_id=decision_record_id,
        ),
        lambda: api.query_inline(
            instance_id,
            definition,
            params,
            limit=limit,
            relationship_state=relationship_state,
            decision_record_id=decision_record_id,
            surface="mcp",
        ),
    )


def _client_query(
    client: CruxibleClient,
    *,
    instance_id: str,
    query_name: str,
    params: dict[str, Any] | None,
    limit: int | None,
    offset: int,
    relationship_state: contracts.QueryRelationshipState | None,
    decision_record_id: str | None,
) -> contracts.QueryToolResult:
    if relationship_state is None and decision_record_id is None:
        return client.query(instance_id, query_name, params, limit=limit, offset=offset)
    if relationship_state is None:
        return client.query(
            instance_id,
            query_name,
            params,
            limit=limit,
            offset=offset,
            decision_record_id=decision_record_id,
        )
    if decision_record_id is None:
        return client.query(
            instance_id,
            query_name,
            params,
            limit=limit,
            offset=offset,
            relationship_state=relationship_state,
        )
    return client.query(
        instance_id,
        query_name,
        params,
        limit=limit,
        offset=offset,
        relationship_state=relationship_state,
        decision_record_id=decision_record_id,
    )


def handle_list_queries(
    instance_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> contracts.QueryListResult:
    """List named-query definitions for an instance."""
    return _dispatch_remote_or_local(
        lambda client: client.list_queries(instance_id, limit=limit, offset=offset),
        lambda: api.list_queries(instance_id, limit=limit, offset=offset),
    )


def handle_create_decision_record(
    instance_id: str,
    question: str,
    subject_type: str | None = None,
    subject_id: str | None = None,
    opened_by: str = "human",
) -> contracts.DecisionRecordResult:
    return _dispatch_remote_or_local(
        lambda client: client.create_decision_record(
            instance_id,
            question=question,
            subject_type=subject_type,
            subject_id=subject_id,
            opened_by=opened_by,
        ),
        lambda: api.create_decision_record(
            instance_id,
            question=question,
            subject_type=subject_type,
            subject_id=subject_id,
            opened_by=opened_by,
        ),
        allow_local=False,
        operation_name="cruxible_create_decision_record",
    )


def handle_get_decision_record(
    instance_id: str,
    decision_record_id: str,
    include_events: bool = True,
) -> contracts.DecisionRecordResult:
    return _dispatch_remote_or_local(
        lambda client: client.get_decision_record(
            instance_id,
            decision_record_id,
            include_events=include_events,
        ),
        lambda: api.get_decision_record(
            instance_id,
            decision_record_id,
            include_events=include_events,
        ),
    )


def handle_list_decision_records(
    instance_id: str,
    status: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    decision_class: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> contracts.DecisionRecordListResult:
    return _dispatch_remote_or_local(
        lambda client: client.list_decision_records(
            instance_id,
            status=status,
            subject_type=subject_type,
            subject_id=subject_id,
            decision_class=decision_class,
            limit=limit,
            offset=offset,
        ),
        lambda: api.list_decision_records(
            instance_id,
            status=status,
            subject_type=subject_type,
            subject_id=subject_id,
            decision_class=decision_class,
            limit=limit,
            offset=offset,
        ),
    )


def handle_list_decision_events(
    instance_id: str,
    decision_record_id: str | None = None,
    receipt_id: str | None = None,
    trace_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> contracts.DecisionEventListResult:
    return _dispatch_remote_or_local(
        lambda client: client.list_decision_events(
            instance_id,
            decision_record_id=decision_record_id,
            receipt_id=receipt_id,
            trace_id=trace_id,
            status=status,
            limit=limit,
            offset=offset,
        ),
        lambda: api.list_decision_events(
            instance_id,
            decision_record_id=decision_record_id,
            receipt_id=receipt_id,
            trace_id=trace_id,
            status=status,
            limit=limit,
            offset=offset,
        ),
    )


def handle_finalize_decision_record(
    instance_id: str,
    decision_record_id: str,
    final_decision: str,
    decision_class: contracts.DecisionClass,
    rationale: str = "",
) -> contracts.DecisionRecordResult:
    return _dispatch_remote_or_local(
        lambda client: client.finalize_decision_record(
            instance_id,
            decision_record_id,
            final_decision=final_decision,
            decision_class=decision_class,
            rationale=rationale,
        ),
        lambda: api.finalize_decision_record(
            instance_id,
            decision_record_id,
            final_decision=final_decision,
            decision_class=decision_class,
            rationale=rationale,
        ),
        allow_local=False,
        operation_name="cruxible_finalize_decision_record",
    )


def handle_abandon_decision_record(
    instance_id: str,
    decision_record_id: str,
    reason: str = "",
) -> contracts.DecisionRecordResult:
    return _dispatch_remote_or_local(
        lambda client: client.abandon_decision_record(
            instance_id,
            decision_record_id,
            reason=reason,
        ),
        lambda: api.abandon_decision_record(
            instance_id,
            decision_record_id,
            reason=reason,
        ),
        allow_local=False,
        operation_name="cruxible_abandon_decision_record",
    )


def handle_describe_query(
    instance_id: str,
    query_name: str,
) -> contracts.NamedQueryInfoResult:
    """Describe one named-query surface for an instance."""
    return _dispatch_remote_or_local(
        lambda client: client.describe_query(instance_id, query_name),
        lambda: api.describe_query(instance_id, query_name),
    )


def handle_receipt(instance_id: str, receipt_id: str) -> dict[str, Any]:
    """Retrieve a stored receipt by ID."""
    return _dispatch_remote_or_local(
        lambda client: client.receipt(instance_id, receipt_id),
        lambda: api.receipt(instance_id, receipt_id),
    )


def handle_get_trace(instance_id: str, trace_id: str) -> dict[str, Any]:
    """Retrieve a stored provider execution trace by ID."""
    return _dispatch_remote_or_local(
        lambda client: client.get_trace(instance_id, trace_id),
        lambda: api.get_trace(instance_id, trace_id),
    )


def handle_list_traces(
    instance_id: str,
    workflow_name: str | None = None,
    provider_name: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> contracts.TraceListResult:
    """List stored provider execution trace summaries."""
    return _dispatch_remote_or_local(
        lambda client: client.list_traces(
            instance_id,
            workflow_name=workflow_name,
            provider_name=provider_name,
            limit=limit,
            offset=offset,
        ),
        lambda: api.list_traces(
            instance_id,
            workflow_name=workflow_name,
            provider_name=provider_name,
            limit=limit,
            offset=offset,
        ),
    )


def handle_feedback(
    instance_id: str,
    receipt_id: str,
    action: contracts.FeedbackAction,
    source: contracts.FeedbackSource,
    from_type: str,
    from_id: str,
    relationship_type: str,
    to_type: str,
    to_id: str,
    edge_key: int | None = None,
    reason: str = "",
    reason_code: str | None = None,
    scope_hints: dict[str, Any] | None = None,
    corrections: dict[str, Any] | None = None,
    group_override: bool = False,
) -> contracts.FeedbackResult:
    """Record feedback on an edge."""
    return _dispatch_remote_or_local(
        lambda client: client.feedback(
            instance_id,
            receipt_id=receipt_id,
            action=action,
            source=source,
            from_type=from_type,
            from_id=from_id,
            relationship_type=relationship_type,
            to_type=to_type,
            to_id=to_id,
            edge_key=edge_key,
            reason=reason,
            reason_code=reason_code,
            scope_hints=scope_hints,
            corrections=corrections,
            group_override=group_override,
        ),
        lambda: api.feedback(
            instance_id,
            receipt_id,
            action,
            source,
            from_type,
            from_id,
            relationship_type,
            to_type,
            to_id,
            edge_key=edge_key,
            reason=reason,
            reason_code=reason_code,
            scope_hints=scope_hints,
            corrections=corrections,
            group_override=group_override,
        ),
        allow_local=False,
        operation_name="cruxible_feedback",
    )


def handle_get_feedback_profile(
    instance_id: str,
    relationship_type: str,
) -> contracts.FeedbackProfileResult:
    """Get a focused feedback profile for one relationship type."""
    return _dispatch_remote_or_local(
        lambda client: client.get_feedback_profile(instance_id, relationship_type),
        lambda: api.get_feedback_profile(instance_id, relationship_type),
    )


def handle_analyze_feedback(
    instance_id: str,
    relationship_type: str,
    limit: int = 200,
    min_support: int = 5,
    decision_surface_type: str | None = None,
    decision_surface_name: str | None = None,
    property_pairs: list[contracts.PropertyPairInput] | None = None,
) -> contracts.AnalyzeFeedbackResult:
    """Analyze structured feedback into deterministic remediation suggestions."""
    return _dispatch_remote_or_local(
        lambda client: client.analyze_feedback(
            instance_id,
            relationship_type=relationship_type,
            limit=limit,
            min_support=min_support,
            decision_surface_type=decision_surface_type,
            decision_surface_name=decision_surface_name,
            property_pairs=property_pairs,
        ),
        lambda: api.analyze_feedback(
            instance_id,
            relationship_type,
            limit=limit,
            min_support=min_support,
            decision_surface_type=decision_surface_type,
            decision_surface_name=decision_surface_name,
            property_pairs=property_pairs,
        ),
    )


def handle_get_outcome_profile(
    instance_id: str,
    *,
    anchor_type: contracts.OutcomeAnchorType,
    relationship_type: str | None = None,
    workflow_name: str | None = None,
    surface_type: str | None = None,
    surface_name: str | None = None,
) -> contracts.OutcomeProfileResult:
    """Get a focused outcome profile for one anchor context."""
    return _dispatch_remote_or_local(
        lambda client: client.get_outcome_profile(
            instance_id,
            anchor_type=anchor_type,
            relationship_type=relationship_type,
            workflow_name=workflow_name,
            surface_type=surface_type,
            surface_name=surface_name,
        ),
        lambda: api.get_outcome_profile(
            instance_id,
            anchor_type=anchor_type,
            relationship_type=relationship_type,
            workflow_name=workflow_name,
            surface_type=surface_type,
            surface_name=surface_name,
        ),
    )


def handle_analyze_outcomes(
    instance_id: str,
    *,
    anchor_type: contracts.OutcomeAnchorType,
    relationship_type: str | None = None,
    workflow_name: str | None = None,
    query_name: str | None = None,
    surface_type: str | None = None,
    surface_name: str | None = None,
    limit: int = 200,
    min_support: int = 5,
) -> contracts.AnalyzeOutcomesResult:
    """Analyze structured outcomes into trust and debugging suggestions."""
    return _dispatch_remote_or_local(
        lambda client: client.analyze_outcomes(
            instance_id,
            anchor_type=anchor_type,
            relationship_type=relationship_type,
            workflow_name=workflow_name,
            query_name=query_name,
            surface_type=surface_type,
            surface_name=surface_name,
            limit=limit,
            min_support=min_support,
        ),
        lambda: api.analyze_outcomes(
            instance_id,
            anchor_type=anchor_type,
            relationship_type=relationship_type,
            workflow_name=workflow_name,
            query_name=query_name,
            surface_type=surface_type,
            surface_name=surface_name,
            limit=limit,
            min_support=min_support,
        ),
    )


def handle_feedback_batch(
    instance_id: str,
    items: list[contracts.FeedbackBatchItemInput],
    *,
    source: contracts.FeedbackSource,
) -> contracts.FeedbackBatchResult:
    """Record batch edge feedback tied to prior receipts."""
    return _dispatch_remote_or_local(
        lambda client: client.feedback_batch(instance_id, items=items, source=source),
        lambda: api.feedback_batch(instance_id, items, source=source),
        allow_local=False,
        operation_name="cruxible_feedback_batch",
    )


def handle_feedback_from_query(
    instance_id: str,
    *,
    receipt_id: str,
    result_index: int,
    action: contracts.FeedbackAction,
    source: contracts.FeedbackSource = "human",
    reason: str = "",
    reason_code: str | None = None,
    scope_hints: dict[str, Any] | None = None,
    corrections: dict[str, Any] | None = None,
    group_override: bool = False,
    path_index: int | None = None,
    path_alias: str | None = None,
) -> contracts.FeedbackResult:
    """Record edge feedback by selecting relationship evidence from a query receipt."""
    return _dispatch_remote_or_local(
        lambda client: client.feedback_from_query(
            instance_id,
            receipt_id=receipt_id,
            result_index=result_index,
            action=action,
            source=source,
            reason=reason,
            reason_code=reason_code,
            scope_hints=scope_hints,
            corrections=corrections,
            group_override=group_override,
            path_index=path_index,
            path_alias=path_alias,
        ),
        lambda: api.feedback_from_query(
            instance_id,
            receipt_id=receipt_id,
            result_index=result_index,
            action=action,
            source=source,
            reason=reason,
            reason_code=reason_code,
            scope_hints=scope_hints,
            corrections=corrections,
            group_override=group_override,
            path_index=path_index,
            path_alias=path_alias,
        ),
        allow_local=False,
        operation_name="cruxible_feedback_from_query",
    )


def handle_outcome(
    instance_id: str,
    outcome: contracts.OutcomeValue,
    receipt_id: str | None = None,
    anchor_type: contracts.OutcomeAnchorType = "receipt",
    anchor_id: str | None = None,
    source: contracts.FeedbackSource = "human",
    outcome_code: str | None = None,
    scope_hints: dict[str, Any] | None = None,
    outcome_profile_key: str | None = None,
    detail: dict[str, Any] | None = None,
) -> contracts.OutcomeResult:
    """Record a structured outcome for a prior receipt or proposal resolution."""
    return _dispatch_remote_or_local(
        lambda client: client.outcome(
            instance_id,
            receipt_id=receipt_id,
            outcome=outcome,
            anchor_type=anchor_type,
            anchor_id=anchor_id,
            source=source,
            outcome_code=outcome_code,
            scope_hints=scope_hints,
            outcome_profile_key=outcome_profile_key,
            detail=detail,
        ),
        lambda: api.outcome(
            instance_id,
            receipt_id,
            outcome,
            anchor_type=anchor_type,
            anchor_id=anchor_id,
            source=source,
            outcome_code=outcome_code,
            scope_hints=scope_hints,
            outcome_profile_key=outcome_profile_key,
            detail=detail,
        ),
        allow_local=False,
        operation_name="cruxible_outcome",
    )


def handle_list(
    instance_id: str,
    resource_type: contracts.ResourceType,
    entity_type: str | None = None,
    relationship_type: str | None = None,
    query_name: str | None = None,
    receipt_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    property_filter: dict[str, Any] | None = None,
    operation_type: str | None = None,
) -> contracts.ListResult:
    """List entities, edges, receipts, feedback, or outcomes."""
    return _dispatch_remote_or_local(
        lambda client: client.list(
            instance_id,
            resource_type=resource_type,
            entity_type=entity_type,
            relationship_type=relationship_type,
            query_name=query_name,
            receipt_id=receipt_id,
            limit=limit,
            offset=offset,
            property_filter=property_filter,
            operation_type=operation_type,
        ),
        lambda: api.list_resources(
            instance_id,
            resource_type,
            entity_type=entity_type,
            relationship_type=relationship_type,
            query_name=query_name,
            receipt_id=receipt_id,
            limit=limit,
            offset=offset,
            property_filter=property_filter,
            operation_type=operation_type,
        ),
    )


def handle_evaluate(
    instance_id: str,
    max_findings: int = 100,
    exclude_orphan_types: list[str] | None = None,
) -> contracts.EvaluateResult:
    """Evaluate graph quality."""
    return _dispatch_remote_or_local(
        lambda client: client.evaluate(
            instance_id,
            max_findings=max_findings,
            exclude_orphan_types=exclude_orphan_types,
        ),
        lambda: api.evaluate(
            instance_id,
            max_findings=max_findings,
            exclude_orphan_types=exclude_orphan_types,
        ),
    )


def handle_stats(instance_id: str) -> contracts.StatsResult:
    """Return graph counts and head snapshot metadata."""
    return _dispatch_remote_or_local(
        lambda client: client.stats(instance_id),
        lambda: api.stats(instance_id),
    )


def handle_lint(
    instance_id: str,
    max_findings: int = 100,
    analysis_limit: int = 200,
    min_support: int = 5,
    exclude_orphan_types: list[str] | None = None,
) -> contracts.LintResult:
    """Run aggregate read-only lint checks."""
    return _dispatch_remote_or_local(
        lambda client: client.lint(
            instance_id,
            max_findings=max_findings,
            analysis_limit=analysis_limit,
            min_support=min_support,
            exclude_orphan_types=exclude_orphan_types,
        ),
        lambda: api.lint(
            instance_id,
            max_findings=max_findings,
            analysis_limit=analysis_limit,
            min_support=min_support,
            exclude_orphan_types=exclude_orphan_types,
        ),
    )


def handle_schema(instance_id: str) -> dict[str, Any]:
    """Get config schema details."""
    return _dispatch_remote_or_local(
        lambda client: client.schema(instance_id),
        lambda: api.schema(instance_id),
    )


def handle_sample(
    instance_id: str,
    entity_type: str,
    limit: int = 5,
) -> contracts.SampleResult:
    """Sample entities of a given type."""
    return _dispatch_remote_or_local(
        lambda client: client.sample(instance_id, entity_type, limit=limit),
        lambda: api.sample(instance_id, entity_type, limit=limit),
    )


def handle_inspect_entity(
    instance_id: str,
    entity_type: str,
    entity_id: str,
    *,
    direction: str = "both",
    relationship_type: str | None = None,
    limit: int | None = None,
) -> contracts.InspectEntityResult:
    """Inspect one entity and its immediate neighbors."""
    return _dispatch_remote_or_local(
        lambda client: client.inspect_entity(
            instance_id,
            entity_type,
            entity_id,
            direction=direction,
            relationship_type=relationship_type,
            limit=limit,
        ),
        lambda: api.inspect_entity(
            instance_id,
            entity_type,
            entity_id,
            direction=direction,
            relationship_type=relationship_type,
            limit=limit,
        ),
    )


def handle_inspect_view(
    instance_id: str,
    view: str,
    *,
    limit: int = 200,
) -> contracts.CanonicalViewResult:
    """Build a canonical structured inspect view."""
    return _dispatch_remote_or_local(
        lambda client: client.inspect_view(instance_id, view, limit=limit),
        lambda: api.inspect_view(instance_id, view, limit=limit),
    )


def handle_render_wiki(
    instance_id: str,
    *,
    focus: list[str] | None = None,
    include_types: list[str] | None = None,
    scope: str | None = None,
    max_per_type: int = 50,
    all_subjects: bool = False,
) -> contracts.WikiRenderResult:
    """Render wiki pages for a governed instance."""
    return _dispatch_remote_or_local(
        lambda client: client.render_wiki(
            instance_id,
            focus=focus,
            include_types=include_types,
            scope=scope,
            max_per_type=max_per_type,
            all_subjects=all_subjects,
        ),
        lambda: api.render_wiki(
            instance_id,
            focus=focus,
            include_types=include_types,
            scope=scope,
            max_per_type=max_per_type,
            all_subjects=all_subjects,
        ),
    )


def handle_reload_config(
    instance_id: str,
    *,
    config_path: str | None = None,
    config_yaml: str | None = None,
) -> contracts.ReloadConfigResult:
    """Reload or replace an instance config."""
    uploaded_yaml = config_yaml
    if uploaded_yaml is None and config_path is not None:
        uploaded_yaml = _config_yaml_for_upload(config_path)
    return _dispatch_remote_or_local(
        lambda client: client.reload_config(
            instance_id,
            config_path=None,
            config_yaml=uploaded_yaml,
        ),
        lambda: api.reload_config(
            instance_id,
            config_path=config_path,
            config_yaml=config_yaml,
        ),
        allow_local=False,
        operation_name="cruxible_reload_config",
    )


def handle_add_relationship(
    instance_id: str,
    relationships: list[contracts.RelationshipInput],
    *,
    dry_run: bool = False,
) -> contracts.AddRelationshipResult:
    """Add or update one or more relationships in the graph (upsert)."""
    return _dispatch_remote_or_local(
        lambda client: client.add_relationships(instance_id, relationships, dry_run=dry_run),
        lambda: api.add_relationships(instance_id, relationships, dry_run=dry_run),
        allow_local=False,
        operation_name="cruxible_add_relationship",
    )


def handle_add_entity(
    instance_id: str,
    entities: list[contracts.EntityInput],
    *,
    dry_run: bool = False,
) -> contracts.AddEntityResult:
    """Add or update one or more entities in the graph (upsert)."""
    return _dispatch_remote_or_local(
        lambda client: client.add_entities(instance_id, entities, dry_run=dry_run),
        lambda: api.add_entities(instance_id, entities, dry_run=dry_run),
        allow_local=False,
        operation_name="cruxible_add_entity",
    )


def handle_batch_direct_write(
    instance_id: str,
    payload: contracts.BatchDirectWritePayload,
    *,
    dry_run: bool = False,
) -> contracts.BatchDirectWriteResult:
    """Validate or apply one direct entity/relationship write payload."""
    return _dispatch_remote_or_local(
        lambda client: client.batch_direct_write(
            instance_id,
            payload,
            dry_run=dry_run,
        ),
        lambda: api.batch_direct_write(
            instance_id,
            payload,
            dry_run=dry_run,
        ),
        allow_local=False,
        operation_name="cruxible_batch_direct_write",
    )


def handle_add_constraint(
    instance_id: str,
    name: str,
    rule: str,
    severity: contracts.ConstraintSeverity = "warning",
    description: str | None = None,
) -> contracts.AddConstraintResult:
    """Add a constraint rule to the config and write back to YAML."""
    return _dispatch_remote_or_local(
        lambda client: client.add_constraint(
            instance_id,
            name=name,
            rule=rule,
            severity=severity,
            description=description,
        ),
        lambda: api.add_constraint(
            instance_id,
            name,
            rule,
            severity,
            description,
        ),
        allow_local=False,
        operation_name="cruxible_add_constraint",
    )


def handle_add_decision_policy(
    instance_id: str,
    name: str,
    applies_to: contracts.DecisionPolicyAppliesTo,
    relationship_type: str,
    effect: contracts.DecisionPolicyEffect,
    match: contracts.DecisionPolicyMatchInput | None = None,
    description: str | None = None,
    rationale: str = "",
    query_name: str | None = None,
    workflow_name: str | None = None,
    expires_at: str | None = None,
) -> contracts.AddDecisionPolicyResult:
    """Add a decision policy to the config and write back to YAML."""
    return _dispatch_remote_or_local(
        lambda client: client.add_decision_policy(
            instance_id,
            name=name,
            applies_to=applies_to,
            relationship_type=relationship_type,
            effect=effect,
            match=match,
            description=description,
            rationale=rationale,
            query_name=query_name,
            workflow_name=workflow_name,
            expires_at=expires_at,
        ),
        lambda: api.add_decision_policy(
            instance_id,
            name=name,
            applies_to=applies_to,
            relationship_type=relationship_type,
            effect=effect,
            match=match,
            description=description,
            rationale=rationale,
            query_name=query_name,
            workflow_name=workflow_name,
            expires_at=expires_at,
        ),
        allow_local=False,
        operation_name="cruxible_add_decision_policy",
    )


def handle_get_entity(
    instance_id: str,
    entity_type: str,
    entity_id: str,
) -> contracts.GetEntityResult:
    """Look up a specific entity by type and ID."""
    return _dispatch_remote_or_local(
        lambda client: client.get_entity(instance_id, entity_type, entity_id),
        lambda: api.get_entity(instance_id, entity_type, entity_id),
    )


def handle_get_relationship(
    instance_id: str,
    from_type: str,
    from_id: str,
    relationship_type: str,
    to_type: str,
    to_id: str,
    edge_key: int | None = None,
) -> contracts.GetRelationshipResult:
    """Look up a specific relationship by its endpoints and type."""
    return _dispatch_remote_or_local(
        lambda client: client.get_relationship(
            instance_id,
            from_type=from_type,
            from_id=from_id,
            relationship_type=relationship_type,
            to_type=to_type,
            to_id=to_id,
            edge_key=edge_key,
        ),
        lambda: api.get_relationship(
            instance_id,
            from_type,
            from_id,
            relationship_type,
            to_type,
            to_id,
            edge_key=edge_key,
        ),
    )


def handle_relationship_lineage(
    instance_id: str,
    from_type: str,
    from_id: str,
    relationship_type: str,
    to_type: str,
    to_id: str,
    edge_key: int | None = None,
) -> contracts.RelationshipLineageResult:
    """Look up a relationship and follow group provenance when available."""
    return _dispatch_remote_or_local(
        lambda client: client.get_relationship_lineage(
            instance_id,
            from_type=from_type,
            from_id=from_id,
            relationship_type=relationship_type,
            to_type=to_type,
            to_id=to_id,
            edge_key=edge_key,
        ),
        lambda: api.get_relationship_lineage(
            instance_id,
            from_type,
            from_id,
            relationship_type,
            to_type,
            to_id,
            edge_key=edge_key,
        ),
    )


def handle_propose_group(
    instance_id: str,
    relationship_type: str,
    members: list[contracts.MemberInput],
    thesis_text: str = "",
    thesis_facts: dict[str, Any] | None = None,
    analysis_state: dict[str, Any] | None = None,
    signal_sources_used: list[str] | None = None,
    proposed_by: contracts.GroupProposedBy = "agent",
    suggested_priority: str | None = None,
) -> contracts.ProposeGroupToolResult:
    """Propose a candidate group for batch edge review."""
    return _dispatch_remote_or_local(
        lambda client: client.propose_group(
            instance_id,
            relationship_type=relationship_type,
            members=members,
            thesis_text=thesis_text,
            thesis_facts=thesis_facts,
            analysis_state=analysis_state,
            signal_sources_used=signal_sources_used,
            proposed_by=proposed_by,
            suggested_priority=suggested_priority,
        ),
        lambda: api.propose_group(
            instance_id,
            relationship_type,
            members,
            thesis_text=thesis_text,
            thesis_facts=thesis_facts,
            analysis_state=analysis_state,
            signal_sources_used=signal_sources_used,
            proposed_by=proposed_by,
            suggested_priority=suggested_priority,
        ),
        allow_local=False,
        operation_name="cruxible_propose_group",
    )


def handle_resolve_group(
    instance_id: str,
    group_id: str,
    action: contracts.GroupAction,
    rationale: str = "",
    resolved_by: contracts.GroupResolvedBy = "human",
    expected_pending_version: int | None = None,
) -> contracts.ResolveGroupToolResult:
    """Resolve a candidate group (approve or reject)."""
    return _dispatch_remote_or_local(
        lambda client: client.resolve_group(
            instance_id,
            group_id,
            action=action,
            rationale=rationale,
            resolved_by=resolved_by,
            expected_pending_version=_required_pending_version(expected_pending_version),
        ),
        lambda: api.resolve_group(
            instance_id,
            group_id,
            action,
            rationale=rationale,
            resolved_by=resolved_by,
            expected_pending_version=expected_pending_version,
        ),
        allow_local=False,
        operation_name="cruxible_resolve_group",
    )


def handle_update_trust_status(
    instance_id: str,
    resolution_id: str,
    trust_status: contracts.GroupTrustStatus,
    reason: str = "",
) -> contracts.UpdateTrustStatusToolResult:
    """Update trust status on a resolution."""
    return _dispatch_remote_or_local(
        lambda client: client.update_trust_status(
            instance_id,
            resolution_id,
            trust_status=trust_status,
            reason=reason,
        ),
        lambda: api.update_trust_status(
            instance_id,
            resolution_id,
            trust_status,
            reason,
        ),
        allow_local=False,
        operation_name="cruxible_update_trust_status",
    )


def handle_get_group(
    instance_id: str,
    group_id: str,
) -> contracts.GetGroupToolResult:
    """Get a candidate group with its members."""
    return _dispatch_remote_or_local(
        lambda client: client.get_group(instance_id, group_id),
        lambda: api.get_group(instance_id, group_id),
    )


def handle_group_status(
    instance_id: str,
    *,
    group_id: str | None = None,
    signature: str | None = None,
) -> contracts.GroupBucketStatusToolResult:
    """Get signature-bucket lifecycle status."""
    return _dispatch_remote_or_local(
        lambda client: client.get_group_status(
            instance_id,
            group_id=group_id,
            signature=signature,
        ),
        lambda: api.get_group_status(
            instance_id,
            group_id=group_id,
            signature=signature,
        ),
    )


def handle_list_groups(
    instance_id: str,
    relationship_type: str | None = None,
    status: contracts.GroupStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> contracts.ListGroupsToolResult:
    """List candidate groups with optional filters."""
    return _dispatch_remote_or_local(
        lambda client: client.list_groups(
            instance_id,
            relationship_type=relationship_type,
            status=status,
            limit=limit,
            offset=offset,
        ),
        lambda: api.list_groups(
            instance_id,
            relationship_type,
            status,
            limit,
        ),
    )


def handle_list_resolutions(
    instance_id: str,
    relationship_type: str | None = None,
    action: contracts.GroupAction | None = None,
    limit: int = 50,
    offset: int = 0,
) -> contracts.ListResolutionsToolResult:
    """List group resolutions with optional filters."""
    return _dispatch_remote_or_local(
        lambda client: client.list_resolutions(
            instance_id,
            relationship_type=relationship_type,
            action=action,
            limit=limit,
            offset=offset,
        ),
        lambda: api.list_resolutions(
            instance_id,
            relationship_type,
            action,
            limit,
        ),
    )


def handle_state_publish(
    instance_id: str,
    transport_ref: str,
    state_id: str,
    release_id: str,
    compatibility: contracts.StateCompatibility,
) -> contracts.StatePublishResult:
    """Publish a root state instance to a transport ref."""
    return _dispatch_remote_or_local(
        lambda client: client.state_publish(
            instance_id,
            transport_ref=transport_ref,
            state_id=state_id,
            release_id=release_id,
            compatibility=compatibility,
        ),
        lambda: api.state_publish(
            instance_id,
            transport_ref,
            state_id,
            release_id,
            compatibility,
        ),
        allow_local=False,
        operation_name="cruxible_state_publish",
    )


def handle_create_snapshot(
    instance_id: str,
    label: str | None = None,
) -> contracts.SnapshotCreateResult:
    """Create an immutable snapshot for an instance."""
    return _dispatch_remote_or_local(
        lambda client: client.create_snapshot(instance_id, label=label),
        lambda: api.create_snapshot(instance_id, label),
        allow_local=False,
        operation_name="cruxible_create_snapshot",
    )


def handle_list_snapshots(
    instance_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> contracts.SnapshotListResult:
    """List snapshots for an instance."""
    return _dispatch_remote_or_local(
        lambda client: client.list_snapshots(instance_id, limit=limit, offset=offset),
        lambda: api.list_snapshots(instance_id, limit=limit, offset=offset),
    )


def handle_register_source_artifact(
    instance_id: str,
    *,
    source_path: str,
    source_kind: contracts.SourceKind = "markdown",
    source_retention: contracts.SourceRetention = "manifest_only",
    original_uri: str | None = None,
    label: str | None = None,
) -> contracts.RegisterSourceArtifactResult:
    """Register a source artifact for source-backed proposal evidence."""
    return _dispatch_remote_or_local(
        lambda client: client.register_source_artifact(
            instance_id,
            source_path=source_path,
            source_kind=source_kind,
            source_retention=source_retention,
            original_uri=original_uri,
            label=label,
        ),
        lambda: api.register_source_artifact(
            instance_id,
            source_path=source_path,
            source_kind=source_kind,
            source_retention=source_retention,
            original_uri=original_uri,
            label=label,
        ),
        allow_local=False,
        operation_name="cruxible_register_source_artifact",
    )


def handle_dereference_source_evidence(
    instance_id: str,
    *,
    source_artifact_id: str,
    chunk_id: str | None = None,
    heading_path: list[str] | None = None,
    block_selector: str | None = None,
    expected_content_hash: str | None = None,
) -> contracts.DereferenceSourceEvidenceResult:
    """Dereference source-backed proposal evidence."""
    return _dispatch_remote_or_local(
        lambda client: client.dereference_source_evidence(
            instance_id,
            source_artifact_id=source_artifact_id,
            chunk_id=chunk_id,
            heading_path=heading_path,
            block_selector=block_selector,
            expected_content_hash=expected_content_hash,
        ),
        lambda: api.dereference_source_evidence(
            instance_id,
            source_artifact_id=source_artifact_id,
            chunk_id=chunk_id,
            heading_path=heading_path,
            block_selector=block_selector,
            expected_content_hash=expected_content_hash,
        ),
    )


def handle_clone_snapshot(
    instance_id: str,
    snapshot_id: str,
    root_dir: str,
) -> contracts.CloneSnapshotResult:
    """Create a point-in-time clone from a snapshot."""
    return _dispatch_remote_or_local(
        lambda client: client.clone_snapshot(
            instance_id,
            snapshot_id=snapshot_id,
            root_dir=root_dir,
        ),
        lambda: api.clone_snapshot_governed(instance_id, snapshot_id, root_dir),
        allow_local=False,
        operation_name="cruxible_clone_snapshot",
    )


def handle_state_status(instance_id: str) -> contracts.StateStatusResult:
    """Read upstream tracking metadata for a release-backed overlay."""
    return _dispatch_remote_or_local(
        lambda client: client.state_status(instance_id),
        lambda: api.state_status(instance_id),
    )


def handle_state_pull_preview(instance_id: str) -> contracts.StatePullPreviewResult:
    """Preview pulling a new upstream release into a local overlay."""
    return _dispatch_remote_or_local(
        lambda client: client.state_pull_preview(instance_id),
        lambda: api.state_pull_preview(instance_id),
    )


def handle_state_pull_apply(
    instance_id: str,
    expected_apply_digest: str,
) -> contracts.StatePullApplyResult:
    """Apply a previewed upstream release into a local overlay."""
    return _dispatch_remote_or_local(
        lambda client: client.state_pull_apply(
            instance_id,
            expected_apply_digest=expected_apply_digest,
        ),
        lambda: api.state_pull_apply(
            instance_id,
            expected_apply_digest,
        ),
        allow_local=False,
        operation_name="cruxible_state_pull_apply",
    )
