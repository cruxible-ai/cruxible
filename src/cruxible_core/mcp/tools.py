"""MCP tool registrations.

Each tool is a thin wrapper that delegates to handlers.py.
Exceptions propagate to FastMCP, which wraps them as ToolError.
"""

from __future__ import annotations

from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

from cruxible_client import contracts
from cruxible_core import __version__
from cruxible_core.mcp import handlers
from cruxible_core.mcp.tool_prompts import tool_description


def register_tools(server: FastMCP) -> list[str]:
    """Register all cruxible tools on the FastMCP server.

    Returns:
        List of registered tool names (for permission validation).
    """
    registered: list[str] = []

    def _tool(fn: Callable[..., Any]) -> Callable[..., Any]:
        """Register a tool on the server and track its name."""
        server.tool(description=tool_description(fn.__name__))(fn)
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
        kit: str | None = None,
    ) -> contracts.InitResult:
        """Create or reload a governed daemon-backed instance.

        Provide `config_path` or `config_yaml` when creating a new
        instance. In server mode, `config_path` is read locally and
        uploaded as config content; the daemon stores its own active
        copy. To reload after a restart, omit both.
        """
        return handlers.handle_init(root_dir, config_path, config_yaml, data_dir, kit)

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
        decision_record_id: str | None = None,
    ) -> contracts.QueryToolResult:
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
        """
        return handlers.handle_query(
            instance_id,
            query_name,
            params,
            limit=limit,
            offset=offset,
            relationship_state=relationship_state,
            decision_record_id=decision_record_id,
        )

    @_tool
    def cruxible_query_inline(
        instance_id: str,
        definition: contracts.InlineQueryDefinition,
        params: dict[str, Any] | None = None,
        limit: int | None = None,
        relationship_state: contracts.QueryVisibilityState | None = None,
        decision_record_id: str | None = None,
    ) -> contracts.QueryToolResult:
        """Run a bounded inline graph query for read-only agent exploration.

        Inline query definitions use the same JSON shape as configured named
        queries plus a required `name`, but they are not persisted to config.
        Use this for one-off filtering and candidate discovery. Promote repeated
        or workflow-critical queries into config as named queries.
        """
        return handlers.handle_query_inline(
            instance_id,
            definition,
            params,
            limit=limit,
            relationship_state=relationship_state,
            decision_record_id=decision_record_id,
        )

    @_tool
    def cruxible_list_queries(
        instance_id: str,
        limit: int | None = None,
        offset: int = 0,
    ) -> contracts.QueryListResult:
        """List named queries with their entry points, required params, and example IDs."""
        return handlers.handle_list_queries(instance_id, limit=limit, offset=offset)

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
        receipt_id: str | None = None,
    ) -> contracts.FeedbackResult:
        """Record edge-level feedback by explicit relationship coordinates.

        ``source`` identifies who produced this feedback:
        ``"human"`` for human review, ``"agent"`` for AI agent review.

        Rejected edges are excluded from future query results.
        Approved edges are trusted in traversals.

        Use `corrections` with `action="correct"` and set `edge_key` only
        when disambiguation is needed. `applied=False` means the record was
        saved but the graph edge was not updated.

        Set `group_override=True` to mark the edge assertion metadata as a
        group override for group resolve. The edge must already exist in the
        graph.
        """
        return handlers.handle_feedback(
            instance_id=instance_id,
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
        )

    @_tool
    def cruxible_feedback_batch(
        instance_id: str,
        items: list[contracts.FeedbackBatchItemInput],
        source: contracts.FeedbackSource = "human",
    ) -> contracts.FeedbackBatchResult:
        """Record batch edge feedback under one top-level mutation receipt."""
        return handlers.handle_feedback_batch(instance_id, items, source=source)

    @_tool
    def cruxible_feedback_from_query(
        instance_id: str,
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
        """Record edge feedback from one relationship/path row in a query receipt.

        This adjudicates one existing relationship assertion. It does not
        resolve candidate groups; use group resolution for group theses and
        member-set decisions.
        """
        return handlers.handle_feedback_from_query(
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
        )

    @_tool
    def cruxible_outcome(
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
        """Record a structured outcome for a receipt or proposal resolution."""
        return handlers.handle_outcome(
            instance_id,
            outcome,
            receipt_id=receipt_id,
            anchor_type=anchor_type,
            anchor_id=anchor_id,
            source=source,
            outcome_code=outcome_code,
            scope_hints=scope_hints,
            outcome_profile_key=outcome_profile_key,
            detail=detail,
        )

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

        Edge items include `edge_key` for use with `cruxible_feedback` when
        multiple edges exist between the same endpoints.
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
    ) -> contracts.OutcomeProfileResult:
        """Return the configured outcome profile for one anchor context."""
        return handlers.handle_get_outcome_profile(
            instance_id,
            anchor_type=anchor_type,
            relationship_type=relationship_type,
            workflow_name=workflow_name,
            surface_type=surface_type,
            surface_name=surface_name,
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
    ) -> contracts.SampleResult:
        """Return up to `limit` entities for quick data inspection."""
        return handlers.handle_sample(instance_id, entity_type, limit, fields)

    @_tool
    def cruxible_inspect_entity(
        instance_id: str,
        entity_type: str,
        entity_id: str,
        direction: str = "both",
        relationship_type: str | None = None,
        limit: int | None = None,
    ) -> contracts.InspectEntityResult:
        """Inspect one entity and its immediate incoming/outgoing neighbors."""
        return handlers.handle_inspect_entity(
            instance_id,
            entity_type,
            entity_id,
            direction=direction,
            relationship_type=relationship_type,
            limit=limit,
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
        """Return the structured canonical ontology view for an instance."""
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
    def cruxible_render_wiki(
        instance_id: str,
        focus: list[str] | None = None,
        include_types: list[str] | None = None,
        scope: str | None = None,
        max_per_type: int = 50,
        all_subjects: bool = False,
    ) -> contracts.WikiRenderResult:
        """Render local wiki pages and return path/content payloads."""
        return handlers.handle_render_wiki(
            instance_id,
            focus=focus,
            include_types=include_types,
            scope=scope,
            max_per_type=max_per_type,
            all_subjects=all_subjects,
        )

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
        Optional evidence_refs and source_evidence attach provenance to the live
        edge, but do not mark it as group-reviewed accepted state.

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
        Use for entities from free text or external sources when CSV ingestion
        is not available.
        """
        return handlers.handle_add_entity(instance_id, entities, dry_run=dry_run)

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
    ) -> contracts.ReloadConfigResult:
        """Reload or replace an instance config after validation."""
        return handlers.handle_reload_config(
            instance_id,
            config_path=config_path,
            config_yaml=config_yaml,
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
        opened_by: str = "human",
    ) -> contracts.DecisionRecordResult:
        """Open a decision record that can collect query and workflow receipts."""
        return handlers.handle_create_decision_record(
            instance_id,
            question=question,
            subject_type=subject_type,
            subject_id=subject_id,
            opened_by=opened_by,
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
    def cruxible_propose_group(
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
        """Propose a candidate group of edges for batch review.

        Each member carries tri-state signals (support/contradict/unsure) from
        declared signal sources. For direct proposals, optional thesis_facts are
        caller-supplied signature scope stored under agent_scope in Cruxible's
        generated thesis_facts. Signal sources are derived from attached member
        signals. Optional analysis_state remains opaque agent data and is not
        hashed.

        If a prior trusted resolution exists for the same thesis signature and
        all signals meet the auto-resolve policy, the group is auto-resolved.
        Otherwise it enters pending_review with a Cruxible-derived review_priority.
        """
        return handlers.handle_propose_group(
            instance_id,
            relationship_type,
            members,
            thesis_text=thesis_text,
            thesis_facts=thesis_facts,
            analysis_state=analysis_state,
            signal_sources_used=signal_sources_used,
            proposed_by=proposed_by,
            suggested_priority=suggested_priority,
        )

    @_tool
    def cruxible_resolve_group(
        instance_id: str,
        group_id: str,
        action: contracts.GroupAction,
        expected_pending_version: int,
        rationale: str = "",
        resolved_by: contracts.GroupResolvedBy = "human",
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
        return handlers.handle_resolve_group(
            instance_id,
            group_id,
            action,
            rationale=rationale,
            resolved_by=resolved_by,
            expected_pending_version=expected_pending_version,
            stamp_existing=stamp_existing,
        )

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
        Use ``status`` to filter by lifecycle state (pending_review,
        auto_resolved, applying, resolved). Use ``relationship_type``
        to filter by edge type.
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
        source_kind: contracts.SourceKind = "markdown",
        source_retention: contracts.SourceRetention = "manifest_only",
        original_uri: str | None = None,
        label: str | None = None,
    ) -> contracts.RegisterSourceArtifactResult:
        """Register a local source document for source-backed proposal evidence."""
        return handlers.handle_register_source_artifact(
            instance_id,
            source_path=source_path,
            source_kind=source_kind,
            source_retention=source_retention,
            original_uri=original_uri,
            label=label,
        )

    @_tool
    def cruxible_dereference_source_evidence(
        instance_id: str,
        source_artifact_id: str,
        chunk_id: str | None = None,
        heading_path: list[str] | None = None,
        block_selector: str | None = None,
        expected_content_hash: str | None = None,
    ) -> contracts.DereferenceSourceEvidenceResult:
        """Return source text for a registered source-evidence locator."""
        return handlers.handle_dereference_source_evidence(
            instance_id,
            source_artifact_id=source_artifact_id,
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
    ) -> contracts.GetEntityResult:
        """Look up a specific entity by type and ID. Returns properties and metadata."""
        return handlers.handle_get_entity(instance_id, entity_type, entity_id)

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

        If multiple same-type edges exist between the same endpoints, pass edge_key
        to select a specific one. Without edge_key, raises an error if ambiguous.
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

    return registered
