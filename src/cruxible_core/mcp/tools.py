"""MCP tool registrations.

Each tool is a thin wrapper that delegates to handlers.py.
Exceptions propagate to FastMCP, which wraps them as ToolError.
"""

from __future__ import annotations

import asyncio
from functools import wraps
from typing import Any, Callable, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field, TypeAdapter

from cruxible_client import contracts
from cruxible_core import __version__
from cruxible_core.deprecation import (
    GROUP_OVERRIDE,
    LEGACY_OUTCOME_PROFILE,
    LEGACY_OUTCOME_RECORD,
    DeprecationNotice,
    attach_mcp_deprecations,
)
from cruxible_core.mcp import handlers
from cruxible_core.mcp.kit_surface import KitSurface
from cruxible_core.mcp.tool_prompts import tool_description


class _MCPFeedbackResult(contracts.FeedbackResult):
    deprecation_warnings: list[dict[str, str]] = Field(default_factory=list)


class _MCPFeedbackBatchResult(contracts.FeedbackBatchResult):
    deprecation_warnings: list[dict[str, str]] = Field(default_factory=list)


class _MCPOutcomeResult(contracts.OutcomeResult):
    deprecation_warnings: list[dict[str, str]] = Field(default_factory=list)


class _MCPOutcomeProfileResult(contracts.OutcomeProfileResult):
    deprecation_warnings: list[dict[str, str]] = Field(default_factory=list)


def _mcp_deprecation_payload(result: Any, notices: list[DeprecationNotice]) -> dict[str, Any]:
    return attach_mcp_deprecations(result, notices)


def _result_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a union-shaped tool payload in the object-rooted MCP result envelope.

    MCP output schemas must be object-rooted, so union results are nested under
    ``result`` — the same envelope FastMCP generates for union-annotated
    returns. The payload itself is untouched.
    """
    return {"result": payload}


def register_tools(
    server: FastMCP,
    *,
    offload_sync_calls: bool = False,
    kit_surface: KitSurface | None = None,
) -> list[str]:
    """Register all cruxible tools on the FastMCP server.

    Args:
        server: FastMCP server receiving the registrations.
        offload_sync_calls: Run synchronous handlers outside the protocol event loop.
        kit_surface: Loaded config vocabulary named in the descriptions of the
            tools that need it. Descriptions only — schemas never vary by kit.

    Returns:
        List of registered tool names (for permission validation).
    """
    registered: list[str] = []

    def _tool(fn: Callable[..., Any]) -> Callable[..., Any]:
        """Register a tool on the server and track its name."""
        registered_fn = fn
        if offload_sync_calls:

            @wraps(fn)
            async def run_in_worker(*args: Any, **kwargs: Any) -> Any:
                # FastMCP invokes synchronous functions on its protocol event
                # loop; daemon HTTP waits must not starve tools/list.
                return await asyncio.to_thread(fn, *args, **kwargs)

            registered_fn = run_in_worker
        server.tool(description=tool_description(fn.__name__, kit_surface=kit_surface))(
            registered_fn
        )
        registered.append(fn.__name__)
        return fn

    @_tool
    def cruxible_version() -> dict[str, str]:
        """Return the cruxible-core version. Use this to confirm which build is running."""
        return {"version": __version__}

    @_tool
    def cruxible_server_info() -> contracts.ServerInfoResult:
        """Return live daemon metadata such as permission mode, state dir, and instance count."""
        return handlers.handle_server_info()

    @_tool
    def cruxible_init(
        root_dir: str,
        config_path: str | None = None,
        config_yaml: str | None = None,
        data_dir: str | None = None,
        kits: list[str] | None = None,
        bare: bool = False,
    ) -> contracts.InitResult:
        """Create or reload a governed daemon-backed instance.

        Provide `config_path`, `config_yaml`, or an ordered `kits`
        sequence when creating a new instance. Kit init composes the configured
        default base unless `bare=true`. In server mode, `config_path` is read locally and
        uploaded as config content; the daemon stores its own active
        copy. To reload after a restart, omit all three.
        """
        return handlers.handle_init(root_dir, config_path, config_yaml, data_dir, kits, bare)

    @_tool
    def cruxible_validate(
        config_path: str | None = None,
        config_yaml: str | None = None,
    ) -> contracts.ValidateResult:
        """Validate a config file or inline YAML without creating an instance.

        Provide exactly one of `config_path` (path to a YAML file) or
        `config_yaml` (raw YAML string).
        """
        return handlers.handle_validate(config_path, config_yaml)

    @_tool
    def cruxible_state_create_overlay(
        root_dir: str,
        transport_ref: str | None = None,
        state_ref: str | None = None,
        kit: str | None = None,
        no_kit: bool = False,
    ) -> contracts.StateOverlayResult:
        """Create a new governed overlay from a published state release."""
        return handlers.handle_create_state_overlay(
            root_dir=root_dir,
            transport_ref=transport_ref,
            state_ref=state_ref,
            kit=kit,
            no_kit=no_kit,
        )

    @_tool
    def cruxible_lock_workflow(
        instance_id: str,
        force: bool = False,
    ) -> contracts.WorkflowLockResult:
        """Generate the workflow lock file for the current instance config.

        Run this after changing providers, artifacts, or workflow config and
        before planning or executing workflows.
        """
        return handlers.handle_workflow_lock(instance_id, force=force)

    @_tool
    def cruxible_plan_workflow(
        instance_id: str,
        workflow_name: str,
        input_payload: dict[str, Any] | None = None,
    ) -> contracts.WorkflowPlanResult:
        """Compile a configured workflow into a concrete execution plan."""
        return handlers.handle_workflow_plan(
            instance_id,
            workflow_name,
            input_payload=input_payload,
        )

    @_tool
    def cruxible_run_workflow(
        instance_id: str,
        workflow_name: str,
        input_payload: dict[str, Any] | None = None,
        decision_record_id: str | None = None,
    ) -> contracts.WorkflowRunResult:
        """Execute a configured workflow and return receipts, traces, and output.

        Canonical workflows run in preview mode and return an `apply_digest`
        plus the current `head_snapshot_id`. To commit a canonical workflow,
        call `cruxible_apply_workflow` with those values.
        """
        return handlers.handle_workflow_run(
            instance_id,
            workflow_name,
            input_payload=input_payload,
            decision_record_id=decision_record_id,
        )

    @_tool
    def cruxible_apply_workflow(
        instance_id: str,
        workflow_name: str,
        expected_apply_digest: str,
        expected_head_snapshot_id: str | None = None,
        input_payload: dict[str, Any] | None = None,
        decision_record_id: str | None = None,
    ) -> contracts.WorkflowApplyResult:
        """Commit a previously previewed canonical workflow after verifying identity."""
        return handlers.handle_workflow_apply(
            instance_id,
            workflow_name,
            expected_apply_digest=expected_apply_digest,
            expected_head_snapshot_id=expected_head_snapshot_id,
            input_payload=input_payload,
            decision_record_id=decision_record_id,
        )

    @_tool
    def cruxible_test_workflow(
        instance_id: str,
        name: str | None = None,
    ) -> contracts.WorkflowTestResult:
        """Run configured workflow tests for an instance."""
        return handlers.handle_workflow_test(instance_id, name=name)

    @_tool
    def cruxible_query(
        instance_id: str,
        query_name: str,
        params: dict[str, Any] | None = None,
        limit: int | None = None,
        offset: int = 0,
        relationship_state: contracts.QueryVisibilityState | None = None,
        lifecycle_status: contracts.LifecycleStatus | None = None,
        decision_record_id: str | None = None,
        profile: contracts.ReadProfile | None = None,
        layout: contracts.QueryLayout = "rows",
    ) -> dict[str, Any]:
        """Run a named query and return results plus a receipt.

        `params` must include the primary-key field of the query's
        entry_point entity type (e.g. if entry_point is Vehicle and its
        primary key is vehicle_id, pass {"vehicle_id": "V-123"}).
        Use `cruxible_schema` to find primary key fields.

        `receipt_id` is also promoted to top-level for follow-up tools.
        After querying, use `cruxible_receipt` to inspect the traversal
        proof showing exactly how results were derived.

        Use `limit` to cap the number of returned results and omit
        the inline receipt (fetch it later via `cruxible_receipt`).
        Use `offset` with `limit` to request later pages; ordering is
        deterministic per snapshot.

        `profile` shapes item payloads: `compact` (default here) returns
        bounded identity cards with governance markers; pass `standard`
        or `full` when you need provenance or actor context.

        `layout='graph'` replaces per-row `items` with the normalized graph
        transport: `nodes`/`edges` carry each unique entity and physical
        relationship once, `results` preserves row order as index
        references, and `paths` holds step-ref sequences (edge index plus
        traversal-step alias) for path-shaped results. Same information
        without per-row duplication — prefer it for multi-row traversal
        reads where you need the relational context.
        """
        # Returned as a plain dict under the shared {"result": ...} envelope:
        # the payload is a UNION of the rows and graph contract models, and an
        # MCP outputSchema must be object-ROOTED, so the union sits under
        # `result` exactly as FastMCP does for union-annotated returns like
        # cruxible_state_diff. See _publish_union_output_schemas.
        return _result_envelope(
            handlers.handle_query(
                instance_id,
                query_name,
                params,
                limit=limit,
                offset=offset,
                relationship_state=relationship_state,
                lifecycle_status=lifecycle_status,
                decision_record_id=decision_record_id,
                profile=profile,
                layout=layout,
            ).model_dump(mode="json")
        )

    @_tool
    def cruxible_query_inline(
        instance_id: str,
        definition: contracts.InlineQueryDefinition,
        params: dict[str, Any] | None = None,
        limit: int | None = None,
        relationship_state: contracts.QueryVisibilityState | None = None,
        lifecycle_status: contracts.LifecycleStatus | None = None,
        decision_record_id: str | None = None,
        profile: contracts.ReadProfile | None = None,
        layout: contracts.QueryLayout = "rows",
    ) -> dict[str, Any]:
        """Run a bounded inline graph query for read-only agent exploration.

        Inline query definitions use the same JSON shape as configured named
        queries plus a required `name`, but they are not persisted to config.
        Use this for one-off filtering and candidate discovery. Promote repeated
        or workflow-critical queries into config as named queries.

        `profile` shapes item payloads: `compact` (default here) returns
        bounded identity cards with governance markers; pass `standard`
        or `full` when you need provenance or actor context.

        `layout='graph'` replaces per-row `items` with the normalized graph
        transport (`nodes`/`edges` once each, `results` as ordered index
        references, `paths` for path-shaped results), exactly as for
        `cruxible_query`.
        """
        # Object-rooted {"result": ...} envelope over the rows|graph union —
        # see cruxible_query and _publish_union_output_schemas.
        return _result_envelope(
            handlers.handle_query_inline(
                instance_id,
                definition,
                params,
                limit=limit,
                relationship_state=relationship_state,
                lifecycle_status=lifecycle_status,
                decision_record_id=decision_record_id,
                profile=profile,
                layout=layout,
            ).model_dump(mode="json")
        )

    @_tool
    def cruxible_list_queries(
        instance_id: str,
        detail: contracts.QueryListDetail = "summary",
        limit: int | None = None,
        offset: int = 0,
        continuation: str | None = None,
    ) -> dict[str, Any]:
        """List named queries as bounded summaries; `detail='full'` expands every definition.

        When `truncated` is true the response carries a `continuation_token`;
        pass it back as `continuation` (same `detail`) for the next page. A
        409 stale-continuation error means state changed — restart the read.

        The QueryListResult | QueryListDetailResult union is carried under the
        object-rooted `result` envelope required of MCP output schemas — see
        `_publish_union_output_schemas`.
        """
        result = handlers.handle_list_queries(
            instance_id,
            detail=detail,
            limit=limit,
            offset=offset,
            continuation=continuation,
        )
        return _result_envelope(result.model_dump(mode="json"))

    @_tool
    def cruxible_describe_query(
        instance_id: str,
        query_name: str,
    ) -> contracts.NamedQueryInfoResult:
        """Describe one named query with the details needed to invoke it correctly."""
        return handlers.handle_describe_query(instance_id, query_name)

    @_tool
    def cruxible_receipt(
        instance_id: str,
        receipt_id: str,
    ) -> dict[str, Any]:
        """Fetch a stored receipt by `receipt_id` from a previous query."""
        return handlers.handle_receipt(instance_id, receipt_id)

    @_tool
    def cruxible_get_trace(
        instance_id: str,
        trace_id: str,
    ) -> dict[str, Any]:
        """Fetch a provider execution trace by `trace_id`."""
        return handlers.handle_get_trace(instance_id, trace_id)

    @_tool
    def cruxible_list_traces(
        instance_id: str,
        workflow_name: str | None = None,
        provider_name: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> contracts.TraceListResult:
        """List provider execution trace summaries with optional workflow/provider filters."""
        return handlers.handle_list_traces(
            instance_id,
            workflow_name=workflow_name,
            provider_name=provider_name,
            limit=limit,
            offset=offset,
        )

    @_tool
    def cruxible_feedback(
        instance_id: str,
        action: contracts.FeedbackAction,
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
        receipt_id: str | None = None,
        claim_id: str | None = None,
    ) -> _MCPFeedbackResult:
        """Record edge-level feedback by explicit relationship coordinates.

        Rejected edges are excluded from future query results.
        Approved edges are trusted in traversals.

        Use `corrections` with `action="correct"`. When several edges share the
        endpoints, disambiguate with `claim_id` (the stable minted identity,
        preferred) or `edge_key` (per-load). `claim_id` takes precedence, and
        supplying both with disagreeing values is refused rather than silently
        resolved. `applied=False` means the record was saved but the graph edge
        was not updated.

        Deprecated `group_override=True` marks the edge assertion metadata as a
        group override for group resolve; use `force_review`. The edge must
        already exist in the graph.
        """
        result = handlers.handle_feedback(
            instance_id=instance_id,
            receipt_id=receipt_id,
            action=action,
            from_type=from_type,
            from_id=from_id,
            relationship_type=relationship_type,
            to_type=to_type,
            to_id=to_id,
            edge_key=edge_key,
            claim_id=claim_id,
            reason=reason,
            reason_code=reason_code,
            scope_hints=scope_hints,
            corrections=corrections,
            group_override=group_override,
        )
        notices: list[DeprecationNotice] = []
        if group_override:
            notices.append(GROUP_OVERRIDE)
        return _MCPFeedbackResult.model_validate(_mcp_deprecation_payload(result, notices))

    @_tool
    def cruxible_feedback_batch(
        instance_id: str,
        items: list[contracts.FeedbackBatchItemInput],
    ) -> _MCPFeedbackBatchResult:
        """Record batch edge feedback under one top-level mutation receipt."""
        result = handlers.handle_feedback_batch(instance_id, items)
        notices: list[DeprecationNotice] = []
        if any(item.group_override for item in items):
            notices.append(GROUP_OVERRIDE)
        return _MCPFeedbackBatchResult.model_validate(_mcp_deprecation_payload(result, notices))

    @_tool
    def cruxible_feedback_from_query(
        instance_id: str,
        receipt_id: str,
        result_index: int,
        action: contracts.FeedbackAction,
        reason: str = "",
        reason_code: str | None = None,
        scope_hints: dict[str, Any] | None = None,
        corrections: dict[str, Any] | None = None,
        group_override: bool = False,
        path_index: int | None = None,
        path_alias: str | None = None,
    ) -> _MCPFeedbackResult:
        """Record edge feedback from one relationship/path row in a query receipt.

        This adjudicates one existing relationship assertion. It does not
        resolve candidate groups; use group resolution for group theses and
        member-set decisions.
        """
        result = handlers.handle_feedback_from_query(
            instance_id,
            receipt_id=receipt_id,
            result_index=result_index,
            action=action,
            reason=reason,
            reason_code=reason_code,
            scope_hints=scope_hints,
            corrections=corrections,
            group_override=group_override,
            path_index=path_index,
            path_alias=path_alias,
        )
        notices: list[DeprecationNotice] = []
        if group_override:
            notices.append(GROUP_OVERRIDE)
        return _MCPFeedbackResult.model_validate(_mcp_deprecation_payload(result, notices))

    @_tool
    def cruxible_outcome(
        instance_id: str,
        outcome: contracts.OutcomeValue,
        receipt_id: str | None = None,
        anchor_type: contracts.OutcomeAnchorType = "receipt",
        anchor_id: str | None = None,
        outcome_code: str | None = None,
        scope_hints: dict[str, Any] | None = None,
        outcome_profile_key: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> _MCPOutcomeResult:
        """Deprecated outcome recorder; use resolution contracts and attestations."""
        result = handlers.handle_outcome(
            instance_id,
            outcome,
            receipt_id=receipt_id,
            anchor_type=anchor_type,
            anchor_id=anchor_id,
            outcome_code=outcome_code,
            scope_hints=scope_hints,
            outcome_profile_key=outcome_profile_key,
            detail=detail,
        )
        notices = [LEGACY_OUTCOME_RECORD]
        return _MCPOutcomeResult.model_validate(_mcp_deprecation_payload(result, notices))

    @_tool
    def cruxible_list(
        instance_id: str,
        resource_type: contracts.ResourceType,
        entity_type: str | None = None,
        relationship_type: str | None = None,
        query_name: str | None = None,
        receipt_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        property_filter: dict[str, Any] | None = None,
        where: dict[str, dict[str, Any]] | None = None,
        operation_type: str | None = None,
        fields: list[str] | None = None,
        relationship_state: contracts.QueryVisibilityState | None = None,
        lifecycle_status: contracts.LifecycleStatus | None = None,
        profile: contracts.ReadProfile | None = None,
        continuation: str | None = None,
    ) -> contracts.ListResult:
        """List `entities|edges|receipts|feedback|outcomes` with optional filters.

        `entity_type` is required for `resource_type="entities"`.
        `relationship_type` filters edges by type for `resource_type="edges"`.
        `property_filter` filters by exact property matches (AND semantics).
        Applies to `resource_type="entities"` and `resource_type="edges"`.
        `where` filters entity/edge properties with bounded operators such as
        `{"status": {"eq": "active"}}`, `{"title": {"contains": "query"}}`,
        or `{"status": {"in": ["active", "planned"]}}`.
        `fields` projects entity properties for `resource_type="entities"`.
        `operation_type` filters receipts (e.g. "query", "add_entity", "ingest").
        `relationship_state` is the read-visibility selector (`live|accepted|all|
        not-live|pending|reviewable`): for entities it gates by lifecycle, for
        edges by review+lifecycle. Entities default to `live`; edges return all
        stored edges unless a selector is given.
        `lifecycle_status` selects one exact, kind-correct lifecycle state
        (`live|retired|superseded` for entities; `active|inactive|superseded|
        retracted` for edges) without adding visibility-state vocabulary.
        `profile` shapes entity/edge item payloads: `compact` (default here)
        returns bounded identity cards with governance markers; pass `standard`
        or `full` when you need provenance or actor context.

        Edge items include `edge_key`, an unstable per-load disambiguation hint for
        parallel edges on one relationship tuple, for use with `cruxible_feedback`;
        tuple coordinates remain authoritative. Prefer `claim_id` (the stable minted
        identity) where accepted. It takes precedence, and supplying both with
        disagreeing values is refused.

        Pagination loop: when `truncated` is true the response carries a
        `continuation_token` — pass it back as `continuation` with the SAME
        filters to fetch the next page. A 409 stale-continuation error means
        state mutated between pages; restart from the first page.
        """
        return handlers.handle_list(
            instance_id,
            resource_type,
            entity_type=entity_type,
            relationship_type=relationship_type,
            query_name=query_name,
            receipt_id=receipt_id,
            limit=limit,
            offset=offset,
            property_filter=property_filter,
            where=where,
            operation_type=operation_type,
            fields=fields,
            relationship_state=relationship_state,
            lifecycle_status=lifecycle_status,
            profile=profile,
            continuation=continuation,
        )

    @_tool
    def cruxible_evaluate(
        instance_id: str,
        max_findings: int = 100,
        exclude_orphan_types: list[str] | None = None,
        severity_filter: list[contracts.FindingSeverity] | None = None,
        category_filter: list[contracts.FindingCategory] | None = None,
    ) -> contracts.EvaluateResult:
        """Run graph quality checks (orphans, gaps, violations, co-members).

        Checks: orphan entities, coverage gaps, constraint violations,
        candidate opportunities, governed support state, and unreviewed
        co-members (entities sharing an intermediary with a cross-referenced
        entity but lacking a cross-reference edge themselves).

        Use `exclude_orphan_types` to skip reference/taxonomy entity types
        (e.g. ``["PCDBPartType"]``) that are expected to be unconnected.
        Use `severity_filter` and `category_filter` to ask narrow triage
        questions while preserving full pre-filter summary counts.
        """
        return handlers.handle_evaluate(
            instance_id,
            max_findings=max_findings,
            exclude_orphan_types=exclude_orphan_types,
            severity_filter=severity_filter,
            category_filter=category_filter,
        )

    @_tool
    def cruxible_stats(instance_id: str) -> contracts.StatsResult:
        """Return graph counts, relationship counts, and head snapshot metadata."""
        return handlers.handle_stats(instance_id)

    @_tool
    def cruxible_lint(
        instance_id: str,
        max_findings: int = 100,
        analysis_limit: int = 200,
        min_support: int = 5,
        exclude_orphan_types: list[str] | None = None,
    ) -> contracts.LintResult:
        """Run aggregate read-only config, graph, feedback, and outcome checks."""
        return handlers.handle_lint(
            instance_id,
            max_findings=max_findings,
            analysis_limit=analysis_limit,
            min_support=min_support,
            exclude_orphan_types=exclude_orphan_types,
        )

    @_tool
    def cruxible_get_feedback_profile(
        instance_id: str,
        relationship_type: str,
    ) -> contracts.FeedbackProfileResult:
        """Return the configured feedback profile for one relationship type."""
        return handlers.handle_get_feedback_profile(instance_id, relationship_type)

    @_tool
    def cruxible_analyze_feedback(
        instance_id: str,
        relationship_type: str,
        limit: int = 200,
        min_support: int = 5,
        decision_surface_type: str | None = None,
        decision_surface_name: str | None = None,
        property_pairs: list[contracts.PropertyPairInput] | None = None,
    ) -> contracts.AnalyzeFeedbackResult:
        """Analyze structured feedback into deterministic remediation suggestions."""
        return handlers.handle_analyze_feedback(
            instance_id,
            relationship_type,
            limit=limit,
            min_support=min_support,
            decision_surface_type=decision_surface_type,
            decision_surface_name=decision_surface_name,
            property_pairs=property_pairs,
        )

    @_tool
    def cruxible_get_outcome_profile(
        instance_id: str,
        anchor_type: contracts.OutcomeAnchorType,
        relationship_type: str | None = None,
        workflow_name: str | None = None,
        surface_type: str | None = None,
        surface_name: str | None = None,
    ) -> _MCPOutcomeProfileResult:
        """Deprecated outcome profile read; use resolution contract declarations."""
        result = handlers.handle_get_outcome_profile(
            instance_id,
            anchor_type=anchor_type,
            relationship_type=relationship_type,
            workflow_name=workflow_name,
            surface_type=surface_type,
            surface_name=surface_name,
        )
        return _MCPOutcomeProfileResult.model_validate(
            _mcp_deprecation_payload(result, [LEGACY_OUTCOME_PROFILE])
        )

    @_tool
    def cruxible_analyze_outcomes(
        instance_id: str,
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
        return handlers.handle_analyze_outcomes(
            instance_id,
            anchor_type=anchor_type,
            relationship_type=relationship_type,
            workflow_name=workflow_name,
            query_name=query_name,
            surface_type=surface_type,
            surface_name=surface_name,
            limit=limit,
            min_support=min_support,
        )

    @_tool
    def cruxible_schema(instance_id: str) -> dict[str, Any]:
        """Return the active config schema for an instance."""
        return handlers.handle_schema(instance_id)

    @_tool
    def cruxible_sample(
        instance_id: str,
        entity_type: str,
        limit: int = 5,
        fields: list[str] | None = None,
        profile: contracts.ReadProfile | None = None,
    ) -> contracts.SampleResult:
        """Return up to `limit` entities for quick data inspection.

        `profile` shapes item payloads: `compact` (default here) returns
        bounded identity cards; pass `standard` or `full` for full
        property bags and metadata.
        """
        return handlers.handle_sample(instance_id, entity_type, limit, fields, profile=profile)

    @_tool
    def cruxible_inspect_entity(
        instance_id: str,
        entity_type: str,
        entity_id: str,
        direction: str = "both",
        relationship_type: str | None = None,
        limit: int | None = None,
        depth: int | None = None,
        relationship_types: list[str] | None = None,
        target_types: list[str] | None = None,
        state: contracts.QueryVisibilityState | None = None,
        projection: list[str] | None = None,
        max_nodes: int | None = None,
        max_edges: int | None = None,
        profile: contracts.ReadProfile | None = None,
        continuation: str | None = None,
    ) -> dict[str, Any]:
        """THE generic bounded neighborhood read: anchor on one entity, expand outward.

        Answer "everything relevant about X within N hops" in ONE call
        instead of stitching multiple named queries. Anchor -> expand:
        `depth` (1-4) sets the hop horizon; `max_nodes` (default 100, cap
        500) and `max_edges` (default 200, cap 1000) are explicit budgets —
        the response reports `truncated` + `truncation_reasons`
        (node_budget/edge_budget/depth) instead of silently clipping.
        Filters: `relationship_types` (repeatable; unions with the legacy
        `relationship_type`), `target_types` (only expand into/return these
        entity types; the anchor is exempt), `direction`. `state` selects
        relationship visibility exactly like named-query traversal
        (live/accepted/all/not-live/pending/reviewable; default all —
        every stored edge with its review/lifecycle markers, matching the
        inspection contract of the single-hop read and `list edges`).
        An explicit non-`all` state filters exactly like traversal and the
        response reports `edges_hidden_by_state`: edges at the explored
        frontier that passed every other filter but were hidden by state
        alone (no budget consumed; regions behind hidden edges are not
        speculatively counted).
        `projection` (repeatable) trims neighbor properties to the named
        ones; `profile` still shapes metadata. Providing any of these
        returns the expanded nodes/edges shape; a bare call keeps the
        legacy single-hop `neighbors` shape.

        Pagination loop: when the expanded read reports `truncated` on a
        budget it carries a `continuation_token` — pass it back as
        `continuation` with the SAME structural parameters to resume the
        expansion where the budget stopped it. A 409 stale-continuation
        error means state mutated between pages; restart the read.
        """
        # Object-rooted {"result": ...} envelope over the legacy|expanded union
        # — see cruxible_query and _publish_union_output_schemas.
        return _result_envelope(
            handlers.handle_inspect_entity(
                instance_id,
                entity_type,
                entity_id,
                direction=direction,
                relationship_type=relationship_type,
                limit=limit,
                depth=depth,
                relationship_types=relationship_types,
                target_types=target_types,
                state=state,
                projection=projection,
                max_nodes=max_nodes,
                max_edges=max_edges,
                profile=profile,
                continuation=continuation,
            ).model_dump(mode="json")
        )

    @_tool
    def cruxible_inspect_entity_history(
        instance_id: str,
        entity_type: str,
        entity_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> contracts.EntityChangeHistoryResult:
        """Inspect receipt-derived entity property changes for one entity type or entity."""
        return handlers.handle_inspect_entity_history(
            instance_id,
            entity_type,
            entity_id=entity_id,
            limit=limit,
            offset=offset,
        )

    @_tool
    def cruxible_inspect_ontology(
        instance_id: str,
    ) -> contracts.CanonicalViewResult:
        """Return compact entity, property, relationship, and write contracts."""
        return handlers.handle_inspect_view(instance_id, "ontology")

    @_tool
    def cruxible_inspect_workflows(
        instance_id: str,
    ) -> contracts.CanonicalViewResult:
        """Return the structured canonical workflow view for an instance."""
        return handlers.handle_inspect_view(instance_id, "workflows")

    @_tool
    def cruxible_inspect_queries(
        instance_id: str,
    ) -> contracts.CanonicalViewResult:
        """Return the structured canonical query view for an instance."""
        return handlers.handle_inspect_view(instance_id, "queries")

    @_tool
    def cruxible_inspect_governance(
        instance_id: str,
        limit: int = 200,
    ) -> contracts.CanonicalViewResult:
        """Return the structured canonical governance view for an instance."""
        return handlers.handle_inspect_view(instance_id, "governance", limit=limit)

    @_tool
    def cruxible_inspect_overview(
        instance_id: str,
        limit: int = 200,
    ) -> contracts.CanonicalViewResult:
        """Return the structured canonical overview view for an instance."""
        return handlers.handle_inspect_view(instance_id, "overview", limit=limit)

    @_tool
    def cruxible_add_relationship(
        instance_id: str,
        relationships: list[contracts.RelationshipInput],
        dry_run: bool = False,
    ) -> contracts.AddRelationshipResult:
        """Add or update relationships in the graph (upsert).

        Each relationship needs: from_type, from_id, relationship_type, to_type, to_id.
        Optional properties must be declared by the relationship schema.
        Entities must already exist. Re-submitting an existing edge merges
        declared domain properties while preserving relationship metadata.
        Optional evidence_refs, source_evidence, and citation_handles attach
        provenance to the live edge, but do not mark it as group-reviewed
        accepted state.

        For governed judgment relationships, prefer candidate group proposal
        flows so Cruxible can preserve tri-state signal-source evidence
        (support, unsure, contradict) and review history.

        Batch size: practical limit is ~500 relationships per call.
        For bulk loading, use workflow dataflow steps plus apply_relationships.
        """
        return handlers.handle_add_relationship(instance_id, relationships, dry_run=dry_run)

    @_tool
    def cruxible_add_entity(
        instance_id: str,
        entities: list[contracts.EntityInput],
        dry_run: bool = False,
    ) -> contracts.AddEntityResult:
        """Add or update entities in the graph (upsert).

        Each entity needs: entity_type, entity_id.
        Optional properties and metadata dicts. Re-submitting an existing
        entity merges properties and metadata.
        A config-declared identity_hint match keeps the write successful and
        returns a structured identity_warnings entry naming the existing
        entity_id. unique_by and id_pattern declarations are hard validation
        constraints.
        Use for entities from free text or external sources when CSV ingestion
        is not available.
        """
        return handlers.handle_add_entity(instance_id, entities, dry_run=dry_run)

    @_tool
    def cruxible_supersede_claim(
        instance_id: str,
        claim_id: str,
        successor_claim_id: str,
        reason: str,
        evidence_ref: contracts.EvidenceRef | None = None,
    ) -> contracts.ClaimLifecycleResult:
        """Settle a claim as superseded by an existing live same-type claim.

        This is a GRAPH_WRITE adjudication: it requires a reason, records actor
        attribution and a mutation receipt, and writes typed pointers in both
        directions. It links the successor; it never creates one.
        """
        return handlers.handle_supersede_claim(
            instance_id,
            claim_id,
            successor_claim_id,
            reason,
            evidence_ref,
        )

    @_tool
    def cruxible_retract_claim(
        instance_id: str,
        claim_id: str,
        reason: str,
        evidence_ref: contracts.EvidenceRef | None = None,
    ) -> contracts.ClaimLifecycleResult:
        """Settle a claim as retracted without a successor.

        This is a GRAPH_WRITE adjudication with required reason, actor
        attribution, and a mutation receipt. The settled claim remains
        addressable by claim_id for historical reads.
        """
        return handlers.handle_retract_claim(instance_id, claim_id, reason, evidence_ref)

    @_tool
    def cruxible_supersede_entity(
        instance_id: str,
        entity_type: str,
        entity_id: str,
        successor_entity_type: str,
        successor_entity_id: str,
        reason: str,
        evidence_ref: contracts.EvidenceRef | None = None,
    ) -> contracts.EntityLifecycleResult:
        """Settle an entity as superseded by an existing live same-type entity.

        This is a GRAPH_WRITE adjudication with required reason, actor
        attribution, two-way typed pointers, and a mutation receipt. Inbound
        and outbound edges do not migrate to the successor; re-point them
        explicitly when needed.
        """
        return handlers.handle_supersede_entity(
            instance_id,
            entity_type,
            entity_id,
            successor_entity_type,
            successor_entity_id,
            reason,
            evidence_ref,
        )

    @_tool
    def cruxible_retire_entity(
        instance_id: str,
        entity_type: str,
        entity_id: str,
        reason: str,
        evidence_ref: contracts.EvidenceRef | None = None,
    ) -> contracts.EntityLifecycleResult:
        """Settle an entity as retired without a successor or edge cascade.

        This is a GRAPH_WRITE adjudication with required reason, actor
        attribution, and a mutation receipt. The result reports how many
        still-live attached edges the retirement strands.
        """
        return handlers.handle_retire_entity(
            instance_id,
            entity_type,
            entity_id,
            reason,
            evidence_ref,
        )

    @_tool
    def cruxible_batch_direct_write(
        instance_id: str,
        payload: contracts.BatchDirectWritePayload,
        dry_run: bool = False,
    ) -> contracts.BatchDirectWriteResult:
        """Validate or apply a direct batch graph write payload.

        Use this for coherent hard-state slices that contain entities and
        relationships. The payload may define top-level shared_evidence entries
        and reference them from relationships with shared_evidence_keys. Direct
        writes are live/unreviewed state; group approval remains the path for
        accepted review state.

        Set dry_run=true to validate entity properties, relationship endpoints,
        relationship properties, evidence locators, duplicate IDs, and missing
        shared evidence keys without mutating graph state.
        Config-declared identity_hint matches appear in the successful result's
        structured identity_warnings; unique_by and id_pattern violations are
        rejected.
        """
        return handlers.handle_batch_direct_write(
            instance_id,
            payload,
            dry_run=dry_run,
        )

    @_tool
    def cruxible_add_constraint(
        instance_id: str,
        name: str,
        rule: str,
        severity: contracts.ConstraintSeverity = "warning",
        description: str | None = None,
    ) -> contracts.AddConstraintResult:
        """Add a constraint rule to the config. Writes the updated config to YAML.

        Constraints are evaluated by cruxible_evaluate to flag edges that violate them.
        Rule format: RELATIONSHIP.FROM.property <op> RELATIONSHIP.TO.property
        Supported operators: ==, !=, >, >=, <, <=
        Identifiers may contain letters, digits, underscores, and hyphens.

        Example: classified_as.FROM.Category == classified_as.TO.CategoryName
        """
        return handlers.handle_add_constraint(instance_id, name, rule, severity, description)

    @_tool
    def cruxible_add_decision_policy(
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
        """Add a decision policy to the config for query/workflow execution."""
        return handlers.handle_add_decision_policy(
            instance_id,
            name,
            applies_to,
            relationship_type,
            effect,
            match=match,
            description=description,
            rationale=rationale,
            query_name=query_name,
            workflow_name=workflow_name,
            expires_at=expires_at,
        )

    @_tool
    def cruxible_reload_config(
        instance_id: str,
        config_path: str | None = None,
        config_yaml: str | None = None,
        allow_orphans: bool = False,
        config_source_manifest: contracts.ConfigSourceManifest | None = None,
    ) -> contracts.ReloadConfigResult:
        """Reload or replace an instance config after validation."""
        return handlers.handle_reload_config(
            instance_id,
            config_path=config_path,
            config_yaml=config_yaml,
            allow_orphans=allow_orphans,
            config_source_manifest=config_source_manifest,
        )

    @_tool
    def cruxible_config_status(
        instance_id: str,
        current_source_manifest: contracts.ConfigSourceManifest | None = None,
    ) -> contracts.ConfigStatusResult:
        """Report source drift and active materialized-config integrity."""
        return handlers.handle_config_status(
            instance_id,
            current_source_manifest=current_source_manifest,
        )

    @_tool
    def cruxible_propose_workflow(
        instance_id: str,
        workflow_name: str,
        input_payload: dict[str, Any] | None = None,
        decision_record_id: str | None = None,
    ) -> contracts.WorkflowProposeResult:
        """Execute a configured workflow and bridge its output into a governed relationship group.

        Use this when a repeated decision procedure should propose relationship state
        through Cruxible's proposal/review/trust boundary instead of writing edges directly.
        The workflow must be `type: proposal` and return a relationship proposal artifact from a
        `propose_relationship_group` step.
        """
        return handlers.handle_propose_workflow(
            instance_id,
            workflow_name,
            input_payload=input_payload,
            decision_record_id=decision_record_id,
        )

    @_tool
    def cruxible_create_decision_record(
        instance_id: str,
        question: str,
        subject_type: str | None = None,
        subject_id: str | None = None,
    ) -> contracts.DecisionRecordResult:
        """Open a decision record that can collect query and workflow receipts.

        The opener is derived from the authenticated actor context.
        """
        return handlers.handle_create_decision_record(
            instance_id,
            question=question,
            subject_type=subject_type,
            subject_id=subject_id,
        )

    @_tool
    def cruxible_get_decision_record(
        instance_id: str,
        decision_record_id: str,
        include_events: bool = True,
    ) -> contracts.DecisionRecordResult:
        """Fetch one decision record, optionally including its logged events."""
        return handlers.handle_get_decision_record(
            instance_id,
            decision_record_id,
            include_events=include_events,
        )

    @_tool
    def cruxible_list_decision_records(
        instance_id: str,
        status: str | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        decision_class: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> contracts.DecisionRecordListResult:
        """List decision records with lifecycle and subject filters."""
        return handlers.handle_list_decision_records(
            instance_id,
            status=status,
            subject_type=subject_type,
            subject_id=subject_id,
            decision_class=decision_class,
            limit=limit,
            offset=offset,
        )

    @_tool
    def cruxible_list_decision_events(
        instance_id: str,
        decision_record_id: str | None = None,
        receipt_id: str | None = None,
        trace_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> contracts.DecisionEventListResult:
        """List decision-record events by record, receipt, trace, or status."""
        return handlers.handle_list_decision_events(
            instance_id,
            decision_record_id=decision_record_id,
            receipt_id=receipt_id,
            trace_id=trace_id,
            status=status,
            limit=limit,
            offset=offset,
        )

    @_tool
    def cruxible_finalize_decision_record(
        instance_id: str,
        decision_record_id: str,
        final_decision: str,
        decision_class: contracts.DecisionClass,
        rationale: str = "",
    ) -> contracts.DecisionRecordResult:
        """Finalize a decision record with an indexed decision class and rationale."""
        return handlers.handle_finalize_decision_record(
            instance_id,
            decision_record_id,
            final_decision=final_decision,
            decision_class=decision_class,
            rationale=rationale,
        )

    @_tool
    def cruxible_abandon_decision_record(
        instance_id: str,
        decision_record_id: str,
        reason: str = "",
    ) -> contracts.DecisionRecordResult:
        """Abandon an open decision record without finalizing a recommendation."""
        return handlers.handle_abandon_decision_record(
            instance_id,
            decision_record_id,
            reason=reason,
        )

    @_tool
    def cruxible_propose_procedure(
        instance_id: str,
        definition: dict[str, Any],
        supersedes_procedure_id: str | None = None,
        evidence_refs: list[contracts.EvidenceRef] | None = None,
    ) -> dict[str, Any]:
        """Propose a bounded procedure definition for governed review."""
        return handlers.handle_propose_procedure(
            instance_id,
            definition,
            supersedes_procedure_id=supersedes_procedure_id,
            evidence_refs=evidence_refs,
        )

    @_tool
    def cruxible_list_procedures(
        instance_id: str,
        status: Literal["pending", "live", "rejected", "retired", "withdrawn"] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> contracts.ListResult:
        """List governed procedures with lifecycle and run-ledger track records."""
        return handlers.handle_list_procedures(
            instance_id,
            status=status,
            limit=limit,
            offset=offset,
        )

    @_tool
    def cruxible_get_procedure(
        instance_id: str,
        procedure_id: str,
    ) -> dict[str, Any]:
        """Get one procedure definition, input schema, lifecycle fields, and track record."""
        return handlers.handle_get_procedure(instance_id, procedure_id)

    @_tool
    def cruxible_resolve_procedure(
        instance_id: str,
        procedure_id: str,
        action: Literal["accept", "reject"],
        expected_version: int,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Accept or reject one pending procedure after review."""
        return handlers.handle_resolve_procedure(
            instance_id,
            procedure_id,
            action=action,
            expected_version=expected_version,
            reason=reason,
        )

    @_tool
    def cruxible_withdraw_procedure(
        instance_id: str,
        procedure_id: str,
        expected_version: int,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Withdraw your own pending procedure proposal, freeing its name."""
        return handlers.handle_withdraw_procedure(
            instance_id,
            procedure_id,
            expected_version=expected_version,
            reason=reason,
        )

    @_tool
    def cruxible_retire_procedure(
        instance_id: str,
        procedure_id: str,
        expected_version: int,
        reason: str,
    ) -> dict[str, Any]:
        """Retire one live procedure with an attributed reason."""
        return handlers.handle_retire_procedure(
            instance_id,
            procedure_id,
            expected_version=expected_version,
            reason=reason,
        )

    @_tool
    def cruxible_run_procedure(
        instance_id: str,
        procedure_id: str,
        input_payload: dict[str, Any],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Run one live procedure through the generic procedure executor."""
        return handlers.handle_run_procedure(
            instance_id,
            procedure_id,
            input_payload=input_payload,
            dry_run=dry_run,
        )

    @_tool
    def cruxible_list_procedure_runs(
        instance_id: str,
        procedure_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> contracts.ListResult:
        """List procedure runs, including unfinalized started tombstones."""
        return handlers.handle_list_procedure_runs(
            instance_id,
            procedure_id,
            limit=limit,
            offset=offset,
        )

    @_tool
    def cruxible_record_procedure_reading(
        instance_id: str,
        procedure_id: str,
        subject_grain: Literal["procedure_unit", "node", "arm"],
        grade: Literal["contract", "attestation"],
        verdict: Literal["satisfied", "contradicted", "indeterminate"],
        observed_at: str,
        node_id: str | None = None,
        from_node_id: str | None = None,
        arm_label: Literal["on_true", "on_false"] | None = None,
        measurement_name: str | None = None,
        contract_id: str | None = None,
        resolution_id: str | None = None,
        value: Any | None = None,
        run_id: str | None = None,
        episode_ref: str | None = None,
        situation_shape: dict[str, Any] | None = None,
        evidence_refs: list[contracts.EvidenceRef] | None = None,
        note: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Record an explicit contract- or attestation-grade procedure outcome."""
        return handlers.handle_record_procedure_reading(
            instance_id,
            procedure_id,
            subject_grain=subject_grain,
            grade=grade,
            verdict=verdict,
            observed_at=observed_at,
            node_id=node_id,
            from_node_id=from_node_id,
            arm_label=arm_label,
            measurement_name=measurement_name,
            contract_id=contract_id,
            resolution_id=resolution_id,
            value=value,
            run_id=run_id,
            episode_ref=episode_ref,
            situation_shape=situation_shape,
            evidence_refs=evidence_refs,
            note=note,
            idempotency_key=idempotency_key,
        )

    @_tool
    def cruxible_attest(
        instance_id: str,
        relationship_type: str,
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
        stance: contracts.AttestationStance,
        observed_at: str,
        evidence_refs: list[contracts.EvidenceRef] | None = None,
        edge_key: int | None = None,
        claim_id: str | None = None,
        properties: dict[str, Any] | None = None,
        note: str | None = None,
        idempotency_key: str | None = None,
    ) -> contracts.AttestationRecordResult:
        """Record one observation against a tuple-first relationship claim.

        WRITES STATE when the claim does not exist yet: a ``support`` stance on
        an absent tuple CREATES the relationship as a pending (unreviewed)
        claim, using ``properties``. ``contradict`` and ``unsure`` are refused
        on an absent claim. Attesting is therefore not a pure observation on
        the create path — use it only when you mean to assert the claim exists.

        When several edges share the tuple, pass ``claim_id`` (the stable
        identity, preferred) or ``edge_key`` (per-load) to pick one. Passing
        both with disagreeing values is refused, never silently resolved.
        """
        return handlers.handle_attest(
            instance_id,
            relationship_type=relationship_type,
            from_type=from_type,
            from_id=from_id,
            to_type=to_type,
            to_id=to_id,
            stance=stance,
            observed_at=observed_at,
            evidence_refs=evidence_refs,
            edge_key=edge_key,
            claim_id=claim_id,
            properties=properties,
            note=note,
            idempotency_key=idempotency_key,
        )

    @_tool
    def cruxible_list_attestations(
        instance_id: str,
        relationship_type: str | None = None,
        from_type: str | None = None,
        from_id: str | None = None,
        to_type: str | None = None,
        to_id: str | None = None,
        stance: contracts.AttestationStance | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> contracts.ListResult:
        """List immutable observations with current tuple-resolution markers."""
        return handlers.handle_list_attestations(
            instance_id,
            relationship_type=relationship_type,
            from_type=from_type,
            from_id=from_id,
            to_type=to_type,
            to_id=to_id,
            stance=stance,
            limit=limit,
            offset=offset,
        )

    @_tool
    def cruxible_attestation_queue(
        instance_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> contracts.ListResult:
        """List live claims with open current-content contradictions."""
        return handlers.handle_attestation_queue(
            instance_id,
            limit=limit,
            offset=offset,
        )

    @_tool
    def cruxible_resolve_attestation(
        instance_id: str,
        attestation_id: str,
        verdict: contracts.AttestationVerdict,
        note: str | None = None,
        follow_up_receipt_id: str | None = None,
    ) -> contracts.AttestationDispositionResult:
        """Append one reviewer disposition to an immutable attestation."""
        return handlers.handle_resolve_attestation(
            instance_id,
            attestation_id,
            verdict=verdict,
            note=note,
            follow_up_receipt_id=follow_up_receipt_id,
        )

    @_tool
    def cruxible_open_outcome_contract(
        instance_id: str,
        entity_type: str,
        entity_id: str,
        description: str,
        check_at: str,
        expires_at: str,
        measurement: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> contracts.OutcomeContractResult:
        """Declare in advance what result counts as success for one subject."""
        return handlers.handle_open_outcome_contract(
            instance_id,
            entity_type=entity_type,
            entity_id=entity_id,
            description=description,
            check_at=check_at,
            expires_at=expires_at,
            measurement=measurement,
            idempotency_key=idempotency_key,
        )

    @_tool
    def cruxible_resolve_outcome(
        instance_id: str,
        contract_id: str,
        verdict: contracts.ResolutionVerdict,
        observed_at: str,
        evidence_refs: list[contracts.EvidenceRef] | None = None,
        note: str | None = None,
        resolving_query_receipt_id: str | None = None,
        resolving_attestation_ids: list[str] | None = None,
    ) -> contracts.OutcomeResolutionResult:
        """Record what reality said about one activated resolution contract."""
        return handlers.handle_resolve_outcome(
            instance_id,
            contract_id,
            verdict=verdict,
            observed_at=observed_at,
            evidence_refs=evidence_refs,
            note=note,
            resolving_query_receipt_id=resolving_query_receipt_id,
            resolving_attestation_ids=resolving_attestation_ids,
        )

    @_tool
    def cruxible_list_outcome_contracts(
        instance_id: str,
        entity_type: str | None = None,
        entity_id: str | None = None,
        status: contracts.ContractStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> contracts.ListResult:
        """List resolution contracts with status, activation, and standing answer."""
        return handlers.handle_list_outcome_contracts(
            instance_id,
            entity_type=entity_type,
            entity_id=entity_id,
            status=status,
            limit=limit,
            offset=offset,
        )

    @_tool
    def cruxible_outcome_due(
        instance_id: str,
        queue: contracts.ContractQueue = "due",
        limit: int = 100,
        offset: int = 0,
    ) -> contracts.ListResult:
        """List due, overdue, or contradicted outcomes on live subjects."""
        return handlers.handle_outcome_due(
            instance_id,
            queue=queue,
            limit=limit,
            offset=offset,
        )

    @_tool
    def cruxible_dispose_outcome_resolution(
        instance_id: str,
        resolution_id: str,
        verdict: contracts.ResolutionDispositionVerdict,
        note: str | None = None,
    ) -> contracts.OutcomeDispositionResult:
        """Uphold or overturn one recorded outcome; an overturn re-opens it."""
        return handlers.handle_dispose_outcome_resolution(
            instance_id,
            resolution_id,
            verdict=verdict,
            note=note,
        )

    @_tool
    def cruxible_propose_group(
        instance_id: str,
        relationship_type: str,
        members: list[contracts.MemberInput],
        thesis_text: str = "",
        thesis_facts: dict[str, Any] | None = None,
        analysis_state: dict[str, Any] | None = None,
        signal_sources_used: list[str] | None = None,
        suggested_priority: str | None = None,
        expected_pending_version: int | None = None,
    ) -> contracts.ProposeGroupToolResult:
        """Propose a candidate group of edges for batch review.

        Each member carries tri-state signals (support/contradict/unsure) from
        declared signal sources. For direct proposals, optional thesis_facts are
        caller-supplied signature scope stored under agent_scope in Cruxible's
        generated thesis_facts. Signal sources are derived from attached member
        signals. Optional analysis_state remains opaque agent data and is not
        hashed.

        If a prior trusted resolution exists for the same thesis signature and
        all signals meet the auto-resolve policy, the group is approved
        immediately through the normal resolve rail — real edges, a real
        resolution row, a real receipt — and the result carries its
        ``resolution_id``. Auto-resolution creates live edges, so a caller below
        GRAPH_WRITE gets ``pending_review`` plus an
        ``auto_resolve_deferred_reason`` instead. Otherwise the group enters
        pending_review with a Cruxible-derived review_priority.

        A re-propose of the same thesis signature REWRITES the live pending
        group. Pass ``expected_pending_version`` (read from the group you
        computed the delta against) to have a bucket that moved underneath you
        refused instead of overwritten; omit it for an unconditional refresh.
        """
        result = handlers.handle_propose_group(
            instance_id,
            relationship_type,
            members,
            thesis_text=thesis_text,
            thesis_facts=thesis_facts,
            analysis_state=analysis_state,
            signal_sources_used=signal_sources_used,
            suggested_priority=suggested_priority,
            expected_pending_version=expected_pending_version,
        )
        return result

    @_tool
    def cruxible_resolve_group(
        instance_id: str,
        group_id: str,
        action: contracts.GroupAction,
        expected_pending_version: int,
        rationale: str = "",
        stamp_existing: bool = False,
    ) -> contracts.ResolveGroupToolResult:
        """Resolve a candidate group by approving or rejecting it.

        Approve creates edges in the graph for valid members. Members whose
        tuple is already live are skipped with an explanation in
        ``skipped_members``; pass ``stamp_existing=True`` to instead bless each
        surviving pre-existing edge with this group's review status and
        provenance. Reject records the resolution without graph mutation. Both
        persist the resolution for audit and future auto-resolve precedent.
        """
        result = handlers.handle_resolve_group(
            instance_id,
            group_id,
            action,
            rationale=rationale,
            expected_pending_version=expected_pending_version,
            stamp_existing=stamp_existing,
        )
        return result

    @_tool
    def cruxible_update_trust_status(
        instance_id: str,
        resolution_id: str,
        trust_status: contracts.GroupTrustStatus,
        reason: str = "",
    ) -> contracts.UpdateTrustStatusToolResult:
        """Update the trust status on a confirmed approved resolution.

        Trust is thesis-scoped: the latest confirmed approval for a signature
        governs auto-resolve eligibility. Promote ``watch`` to ``trusted`` to
        enable auto-resolve. Set ``invalidated`` to block auto-resolve and
        escalate future proposals to critical priority.
        """
        return handlers.handle_update_trust_status(
            instance_id, resolution_id, trust_status, reason=reason
        )

    @_tool
    def cruxible_get_group(
        instance_id: str,
        group_id: str,
    ) -> contracts.GetGroupToolResult:
        """Get a candidate group by ID, including its members and resolution.

        Returns the group metadata (thesis, status, review_priority) and
        the full list of members with their signals. If the group has been
        resolved, includes the resolution details (action, trust_status,
        rationale).
        """
        return handlers.handle_get_group(instance_id, group_id)

    @_tool
    def cruxible_list_groups(
        instance_id: str,
        relationship_type: str | None = None,
        status: contracts.GroupStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> contracts.ListGroupsToolResult:
        """List candidate groups with optional filters.

        Results are sorted by review_priority descending (critical first).
        Use ``status`` to filter by lifecycle state (pending_review, applying,
        resolved, withdrawn). ``auto_resolved`` is a DEPRECATED read-only
        status: nothing writes it any more, and it is filterable only so an
        operator upgrading from 0.2.x can find the rows it left behind. Use
        ``relationship_type`` to filter by edge type.
        """
        return handlers.handle_list_groups(
            instance_id,
            relationship_type=relationship_type,
            status=status,
            limit=limit,
            offset=offset,
        )

    @_tool
    def cruxible_list_resolutions(
        instance_id: str,
        relationship_type: str | None = None,
        action: contracts.GroupAction | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> contracts.ListResolutionsToolResult:
        """List group resolutions with optional filters.

        Returns stored resolutions including analysis_state (for agent reuse),
        thesis_facts, trust_status, and trust_reason. Use ``action`` to filter
        by approve/reject. Use ``relationship_type`` to scope to a specific
        edge type.
        """
        return handlers.handle_list_resolutions(
            instance_id,
            relationship_type=relationship_type,
            action=action,
            limit=limit,
            offset=offset,
        )

    @_tool
    def cruxible_group_status(
        instance_id: str,
        group_id: str | None = None,
        signature: str | None = None,
    ) -> contracts.GroupBucketStatusToolResult:
        """Show lifecycle status for a signature bucket or concrete group."""
        return handlers.handle_group_status(
            instance_id,
            group_id=group_id,
            signature=signature,
        )

    @_tool
    def cruxible_state_publish(
        instance_id: str,
        transport_ref: str,
        state_id: str,
        release_id: str,
        compatibility: contracts.StateCompatibility,
    ) -> contracts.StatePublishResult:
        """Publish a root state instance as an immutable release bundle."""
        return handlers.handle_state_publish(
            instance_id,
            transport_ref,
            state_id,
            release_id,
            compatibility,
        )

    @_tool
    def cruxible_create_snapshot(
        instance_id: str,
        label: str | None = None,
    ) -> contracts.SnapshotCreateResult:
        """Create an immutable snapshot for the current instance."""
        return handlers.handle_create_snapshot(instance_id, label=label)

    @_tool
    def cruxible_instance_backup(
        instance_id: str,
        artifact_path: str,
        label: str | None = None,
    ) -> contracts.InstanceBackupResult:
        """Write a portable same-identity backup artifact for an instance."""
        return handlers.handle_instance_backup(instance_id, artifact_path, label=label)

    @_tool
    def cruxible_instance_restore(
        artifact_path: str,
        root_dir: str | None = None,
    ) -> contracts.InstanceRestoreResult:
        """Restore a same-identity daemon-backed instance from an artifact."""
        return handlers.handle_instance_restore(artifact_path, root_dir=root_dir)

    @_tool
    def cruxible_instance_relocate(
        instance_id: str,
        to_dir: str,
        remove_source: bool = False,
    ) -> contracts.InstanceRelocateResult:
        """Move a healthy daemon-backed instance to a new directory, preserving identity."""
        return handlers.handle_instance_relocate(
            instance_id,
            to_dir,
            remove_source=remove_source,
        )

    @_tool
    def cruxible_list_snapshots(
        instance_id: str,
        limit: int | None = None,
        offset: int = 0,
    ) -> contracts.SnapshotListResult:
        """List immutable snapshots for the current instance."""
        return handlers.handle_list_snapshots(instance_id, limit=limit, offset=offset)

    @_tool
    def cruxible_register_source_artifact(
        instance_id: str,
        source_path: str,
        source_artifact_id: str | None = None,
        source_kind: contracts.SourceKind = "markdown",
        source_retention: contracts.SourceRetention = "manifest_only",
        original_uri: str | None = None,
        label: str | None = None,
    ) -> contracts.RegisterSourceArtifactResult:
        """Register a local source document for source-backed proposal evidence.

        source_artifact_id is a caller-supplied artifact id so pinned evidence
        locators can reference it deterministically; server-generated when
        omitted. When provided, it must be 3-64 chars of [A-Za-z0-9._-]
        starting with an alphanumeric. Duplicate ids are refused by the service.
        """
        return handlers.handle_register_source_artifact(
            instance_id,
            source_path=source_path,
            source_artifact_id=source_artifact_id,
            source_kind=source_kind,
            source_retention=source_retention,
            original_uri=original_uri,
            label=label,
        )

    @_tool
    def cruxible_dereference_source_evidence(
        instance_id: str,
        source_artifact_id: str,
        artifact_revision_id: str | None = None,
        chunk_id: str | None = None,
        heading_path: list[str] | None = None,
        block_selector: str | None = None,
        expected_content_hash: str | None = None,
    ) -> contracts.DereferenceSourceEvidenceResult:
        """Return source text for a registered source-evidence locator.

        Pass ``artifact_revision_id`` (from the evidence ref you are replaying)
        to read the revision the citation was MADE against. Without it the read
        resolves against the current revision and reports ``revision_unpinned``.
        """
        return handlers.handle_dereference_source_evidence(
            instance_id,
            source_artifact_id=source_artifact_id,
            artifact_revision_id=artifact_revision_id,
            chunk_id=chunk_id,
            heading_path=heading_path,
            block_selector=block_selector,
            expected_content_hash=expected_content_hash,
        )

    @_tool
    def cruxible_clone_snapshot(
        instance_id: str,
        snapshot_id: str,
        root_dir: str,
    ) -> contracts.CloneSnapshotResult:
        """Create a point-in-time clone from an immutable snapshot."""
        return handlers.handle_clone_snapshot(instance_id, snapshot_id, root_dir)

    @_tool
    def cruxible_state_status(instance_id: str) -> contracts.StateStatusResult:
        """Return upstream tracking metadata for a release-backed overlay."""
        return handlers.handle_state_status(instance_id)

    @_tool
    def cruxible_state_diff(
        instance_id: str,
        from_coordinate: str | None = None,
        to_coordinate: str | None = None,
        sections: list[str] | None = None,
        entity_types: list[str] | None = None,
        relationship_types: list[str] | None = None,
        buckets: list[str] | None = None,
        changed_only: bool = False,
        max_items_per_bucket: int | None = None,
        artifact_digest: str | None = None,
    ) -> contracts.StateDiffResult | contracts.StateDiffArtifactResult:
        """Diff two state coordinates: `current`, a `snap_...` id, `upstream`, or `origin`.

        Omit both coordinates for "what the last committed transition did, plus
        anything since" (parent-of-head to current). Pass `artifact_digest` on
        its own to re-read a previously persisted diff artifact by its content
        address instead of computing a new one.
        """
        if artifact_digest is not None:
            return handlers.handle_state_diff_artifact(instance_id, artifact_digest)
        return handlers.handle_state_diff(
            instance_id,
            from_coordinate=from_coordinate,
            to_coordinate=to_coordinate,
            sections=sections,
            entity_types=entity_types,
            relationship_types=relationship_types,
            buckets=buckets,
            changed_only=changed_only,
            max_items_per_bucket=max_items_per_bucket,
        )

    @_tool
    def cruxible_state_pull_preview(
        instance_id: str,
    ) -> contracts.StatePullPreviewResult:
        """Preview pulling a newer upstream release into a release-backed overlay."""
        return handlers.handle_state_pull_preview(instance_id)

    @_tool
    def cruxible_state_pull_apply(
        instance_id: str,
        expected_apply_digest: str,
    ) -> contracts.StatePullApplyResult:
        """Apply a previewed upstream release into a release-backed overlay."""
        return handlers.handle_state_pull_apply(instance_id, expected_apply_digest)

    @_tool
    def cruxible_get_entity(
        instance_id: str,
        entity_type: str,
        entity_id: str,
        profile: contracts.ReadProfile | None = None,
    ) -> contracts.GetEntityResult:
        """Look up a specific entity by type and ID. Returns properties and metadata.

        `profile` shapes the payload: `compact` (default here) returns a
        bounded identity card with governance markers; pass `standard` or
        `full` for the complete property bag and metadata.
        """
        return handlers.handle_get_entity(instance_id, entity_type, entity_id, profile=profile)

    @_tool
    def cruxible_get_relationship(
        instance_id: str,
        from_type: str,
        from_id: str,
        relationship_type: str,
        to_type: str,
        to_id: str,
        edge_key: int | None = None,
    ) -> contracts.GetRelationshipResult:
        """Look up a specific relationship by its endpoints and type. Returns its properties.

        `edge_key` is an unstable per-load hint for parallel-edge disambiguation;
        tuple coordinates remain authoritative. Pass it when multiple same-type
        edges exist between the same endpoints. Without `edge_key`, raises an error
        if ambiguous.
        """
        return handlers.handle_get_relationship(
            instance_id, from_type, from_id, relationship_type, to_type, to_id, edge_key
        )

    @_tool
    def cruxible_relationship_lineage(
        instance_id: str,
        from_type: str,
        from_id: str,
        relationship_type: str,
        to_type: str,
        to_id: str,
        edge_key: int | None = None,
    ) -> contracts.RelationshipLineageResult:
        """Look up a relationship and follow group provenance when available."""
        return handlers.handle_relationship_lineage(
            instance_id,
            from_type,
            from_id,
            relationship_type,
            to_type,
            to_id,
            edge_key,
        )

    _publish_union_output_schemas(server)

    return registered


# Tools whose results are UNIONS of contract models. The MCP spec requires a
# tool outputSchema to be object-ROOTED, so a bare `anyOf` root (what a union
# derives to) is rejected by strict clients. Every union result is therefore
# carried under the same {"result": <union>} envelope FastMCP itself produces
# for union-annotated returns (cruxible_state_diff is the in-tree example).
# These tools still return plain dicts so the payload stays byte-identical to
# the handler's model dump; only the nesting and the published schema change.
_UNION_OUTPUT_TOOLS: dict[str, Any] = {
    "cruxible_query": contracts.QueryToolResult | contracts.QueryGraphToolResult,
    "cruxible_query_inline": contracts.QueryToolResult | contracts.QueryGraphToolResult,
    "cruxible_list_queries": contracts.QueryListResult | contracts.QueryListDetailResult,
    "cruxible_inspect_entity": (
        contracts.InspectEntityResult | contracts.InspectNeighborhoodResult
    ),
}


def union_output_envelope_schema(tool_name: str, union: Any) -> dict[str, Any]:
    """Build the object-rooted ``{"result": <union>}`` schema for a union tool.

    Mirrors the schema FastMCP derives for a union-annotated return (see
    ``cruxible_state_diff``): the union moves under a required ``result``
    property and any ``$defs`` are hoisted to the envelope root so the local
    ``#/$defs/...`` references stay resolvable.
    """
    union_schema = TypeAdapter(union).json_schema()
    defs = union_schema.pop("$defs", None)
    envelope: dict[str, Any] = {
        "properties": {"result": {**union_schema, "title": "Result"}},
        "required": ["result"],
        "title": f"{tool_name}Output",
        "type": "object",
    }
    if defs:
        envelope["$defs"] = defs
    return envelope


def _publish_union_output_schemas(server: FastMCP) -> None:
    """Publish object-rooted union outputSchemas for the dict-returning union tools.

    FastMCP derives outputSchema from the return annotation, so a
    ``dict[str, Any]`` return advertises an unrestricted object. FastMCP
    exposes no hook to attach a custom schema to a dict-returning tool
    (``server.tool()`` only takes ``structured_output``), so the derived
    schema is overridden on the registered tool's metadata after
    registration. Only the ADVERTISED schema changes: the permissive dict
    output model stays in place, so the payload the handler produced is passed
    through verbatim under the envelope's ``result`` key instead of being
    re-validated through a union model.
    """
    for tool_name, union in _UNION_OUTPUT_TOOLS.items():
        tool = server._tool_manager.get_tool(tool_name)
        if tool is None:  # pragma: no cover - registration bug guard
            raise RuntimeError(f"union output tool {tool_name!r} is not registered")
        tool.fn_metadata.output_schema = union_output_envelope_schema(tool_name, union)
        # Tool.output_schema is a cached_property over fn_metadata; drop any
        # cached value so list_tools publishes the override.
        tool.__dict__.pop("output_schema", None)
