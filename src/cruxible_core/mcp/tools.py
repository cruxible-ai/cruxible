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


def register_tools(server: FastMCP) -> list[str]:
    """Register all cruxible tools on the FastMCP server.

    Returns:
        List of registered tool names (for permission validation).
    """
    registered: list[str] = []

    def _tool(fn: Callable[..., Any]) -> Callable[..., Any]:
        """Register a tool on the server and track its name."""
        server.tool()(fn)
        registered.append(fn.__name__)
        return fn

    @_tool
    def cruxible_version() -> dict[str, str]:
        """Return the cruxible-core version. Use this to confirm which build is running."""
        return {"version": __version__}

    @_tool
    def cruxible_server_info() -> contracts.ServerInfoResult:
        """Return live daemon metadata such as agent mode, state dir, and instance count."""
        return handlers.handle_server_info()

    @_tool
    def cruxible_init(
        root_dir: str,
        config_path: str | None = None,
        config_yaml: str | None = None,
        data_dir: str | None = None,
    ) -> contracts.InitResult:
        """Create or reload a governed daemon-backed instance.

        Provide `config_path` or `config_yaml` when creating a new
        instance. In server mode, `config_path` is read locally and
        uploaded as config content; the daemon stores its own active
        copy. To reload after a restart, omit both.
        """
        return handlers.handle_init(root_dir, config_path, config_yaml, data_dir)

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
    def cruxible_world_fork(
        root_dir: str,
        transport_ref: str | None = None,
        world_ref: str | None = None,
        kit: str | None = None,
        no_kit: bool = False,
    ) -> contracts.WorldForkResult:
        """Create a new governed fork from a published world release."""
        return handlers.handle_world_fork(
            root_dir=root_dir,
            transport_ref=transport_ref,
            world_ref=world_ref,
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
        )

    @_tool
    def cruxible_apply_workflow(
        instance_id: str,
        workflow_name: str,
        expected_apply_digest: str,
        expected_head_snapshot_id: str | None = None,
        input_payload: dict[str, Any] | None = None,
    ) -> contracts.WorkflowApplyResult:
        """Apply a canonical workflow after verifying the preview identity."""
        return handlers.handle_workflow_apply(
            instance_id,
            workflow_name,
            expected_apply_digest=expected_apply_digest,
            expected_head_snapshot_id=expected_head_snapshot_id,
            input_payload=input_payload,
        )

    @_tool
    def cruxible_ingest(
        instance_id: str,
        mapping_name: str,
        file_path: str | None = None,
        data_csv: str | None = None,
        data_json: str | list[dict[str, Any]] | None = None,
        data_ndjson: str | None = None,
        upload_id: str | None = None,
    ) -> contracts.IngestResult:
        """Ingest data through a legacy ingestion mapping.

        Deprecated for new configs: prefer workflow-based deterministic loading
        via `cruxible_lock_workflow`, `cruxible_run_workflow`, and
        `cruxible_apply_workflow`.

        For deterministic relationships only (explicit in source data).
        For inferred relationships (matching, classification), use
        ``cruxible_add_relationship`` instead.

        Provide exactly one data source:
        - ``file_path``: path to a CSV, JSON, or NDJSON (.jsonl/.ndjson) file
          on disk. Files with ``.json`` extension containing NDJSON content
          are auto-detected.
        - ``data_csv``: inline CSV string
        - ``data_json``: inline JSON array of row objects
          (e.g. ``[{"id": "1", "name": "x"}, ...]``)
        - ``data_ndjson``: inline NDJSON string (one JSON object per line)
        - ``upload_id``: reserved for cloud mode (not supported locally)

        Ingest entity mappings before relationship mappings.
        Re-ingesting existing relationships updates provided properties;
        omitted properties are preserved.

        For large relationship sets (10K+ edges), CSV file ingestion is
        recommended — it streams rows and avoids MCP payload size limits.
        """
        return handlers.handle_ingest(
            instance_id, mapping_name, file_path, data_csv, data_json, data_ndjson, upload_id
        )

    @_tool
    def cruxible_query(
        instance_id: str,
        query_name: str,
        params: dict[str, Any] | None = None,
        limit: int | None = None,
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
        """
        return handlers.handle_query(instance_id, query_name, params, limit=limit)

    @_tool
    def cruxible_list_queries(
        instance_id: str,
    ) -> contracts.QueryListResult:
        """List named queries with their entry points, required params, and example IDs."""
        return handlers.handle_list_queries(instance_id)

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
    def cruxible_feedback(
        instance_id: str,
        receipt_id: str,
        action: contracts.FeedbackAction,
        source: contracts.FeedbackSource,
        from_type: str,
        from_id: str,
        relationship: str,
        to_type: str,
        to_id: str,
        edge_key: int | None = None,
        reason: str = "",
        reason_code: str | None = None,
        scope_hints: dict[str, Any] | None = None,
        corrections: dict[str, Any] | None = None,
        group_override: bool = False,
    ) -> contracts.FeedbackResult:
        """Record edge-level feedback tied to a receipt.

        ``source`` identifies who produced this feedback:
        ``"human"`` for human review, ``"agent"`` for AI agent review.

        Rejected edges are excluded from future query results.
        Approved edges are trusted in traversals.

        Use `corrections` with `action="correct"` and set `edge_key` only
        when disambiguation is needed. `applied=False` means the record was
        saved but the graph edge was not updated.

        Set `group_override=True` to stamp the edge with a group_override
        property, marking it as pre-approved for group resolve. The edge
        must already exist in the graph.
        """
        return handlers.handle_feedback(
            instance_id,
            receipt_id,
            action,
            source,
            from_type,
            from_id,
            relationship,
            to_type,
            to_id,
            edge_key,
            reason,
            reason_code,
            scope_hints,
            corrections,
            group_override,
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
        property_filter: dict[str, Any] | None = None,
        operation_type: str | None = None,
    ) -> contracts.ListResult:
        """List `entities|edges|receipts|feedback|outcomes` with optional filters.

        `entity_type` is required for `resource_type="entities"`.
        `relationship_type` filters edges by type for `resource_type="edges"`.
        `property_filter` filters by exact property matches (AND semantics).
        Applies to `resource_type="entities"` and `resource_type="edges"`.
        `operation_type` filters receipts (e.g. "query", "add_entity", "ingest").

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
            property_filter=property_filter,
            operation_type=operation_type,
        )

    @_tool
    def cruxible_find_candidates(
        instance_id: str,
        relationship_type: str,
        strategy: contracts.CandidateStrategy,
        match_rules: list[dict[str, str]] | None = None,
        via_relationship: str | None = None,
        min_overlap: float = 0.5,
        min_confidence: float = 0.5,
        limit: int = 20,
        min_distinct_neighbors: int = 2,
    ) -> contracts.CandidatesResult:
        """Find missing-relationship candidates.

        `strategy="property_match"` requires `match_rules`.
        Each rule: `{from_property, to_property, operator}`.
        Operators: `equals` (type-strict), `iequals` (case-insensitive),
        `contains` (substring, forces brute-force scan).
        `strategy="shared_neighbors"` requires `via_relationship`.
        `min_distinct_neighbors` (default 2) skips pairs where both entities
        have fewer than this many neighbors — filters degenerate cases.
        """
        return handlers.handle_find_candidates(
            instance_id,
            relationship_type,
            strategy,
            match_rules=match_rules,
            via_relationship=via_relationship,
            min_overlap=min_overlap,
            min_confidence=min_confidence,
            limit=limit,
            min_distinct_neighbors=min_distinct_neighbors,
        )

    @_tool
    def cruxible_evaluate(
        instance_id: str,
        max_findings: int = 100,
        exclude_orphan_types: list[str] | None = None,
    ) -> contracts.EvaluateResult:
        """Run graph quality checks (orphans, gaps, violations, co-members).

        Checks: orphan entities, coverage gaps, constraint violations,
        candidate opportunities, governed support state, and unreviewed
        co-members (entities sharing an intermediary with a cross-referenced
        entity but lacking a cross-reference edge themselves).

        Use `exclude_orphan_types` to skip reference/taxonomy entity types
        (e.g. ``["PCDBPartType"]``) that are expected to be unconnected.
        """
        return handlers.handle_evaluate(
            instance_id,
            max_findings=max_findings,
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
    ) -> contracts.SampleResult:
        """Return up to `limit` entities for quick data inspection."""
        return handlers.handle_sample(instance_id, entity_type, limit)

    @_tool
    def cruxible_add_relationship(
        instance_id: str,
        relationships: list[contracts.RelationshipInput],
    ) -> contracts.AddRelationshipResult:
        """Add or update relationships in the graph (upsert).

        Each relationship needs: from_type, from_id, relationship, to_type, to_id.
        Optional properties must be declared by the relationship schema.
        Entities must already exist. Re-submitting an existing edge merges
        declared domain properties while preserving system review metadata.

        For governed judgment relationships, prefer candidate group proposal
        flows so Cruxible can preserve tri-state integration signals
        (support, unsure, contradict) and review history.

        Batch size: practical limit is ~500 relationships per call.
        For bulk ingestion of 10K+ relationships, use ``cruxible_ingest``
        with CSV files instead.
        """
        return handlers.handle_add_relationship(instance_id, relationships)

    @_tool
    def cruxible_add_entity(
        instance_id: str,
        entities: list[contracts.EntityInput],
    ) -> contracts.AddEntityResult:
        """Add or update entities in the graph (upsert).

        Each entity needs: entity_type, entity_id.
        Optional properties dict. Re-submitting an existing entity replaces
        all its properties (full overwrite, not merge).
        Use for entities from free text or external sources when CSV ingestion
        is not available.
        """
        return handlers.handle_add_entity(instance_id, entities)

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
    def cruxible_propose_workflow(
        instance_id: str,
        workflow_name: str,
        input_payload: dict[str, Any] | None = None,
    ) -> contracts.WorkflowProposeResult:
        """Execute a configured workflow and bridge its output into a governed relationship group.

        Use this when a repeated decision procedure should propose relationship state
        through Cruxible's proposal/review/trust boundary instead of writing edges directly.
        The workflow must return a relationship proposal artifact from a
        `propose_relationship_group` step.
        """
        return handlers.handle_propose_workflow(
            instance_id,
            workflow_name,
            input_payload=input_payload,
        )

    @_tool
    def cruxible_propose_group(
        instance_id: str,
        relationship_type: str,
        members: list[contracts.MemberInput],
        thesis_text: str = "",
        thesis_facts: dict[str, Any] | None = None,
        analysis_state: dict[str, Any] | None = None,
        integrations_used: list[str] | None = None,
        proposed_by: contracts.GroupProposedBy = "agent",
        suggested_priority: str | None = None,
    ) -> contracts.ProposeGroupToolResult:
        """Propose a candidate group of edges for batch review.

        Each member carries tri-state signals (support/contradict/unsure) from
        declared integrations. The group carries a thesis (structured facts that
        get hashed into a deterministic signature) and optional analysis_state
        (opaque agent data, NOT hashed).

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
            integrations_used=integrations_used,
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
    ) -> contracts.ResolveGroupToolResult:
        """Resolve a candidate group by approving or rejecting it.

        Approve creates edges in the graph for valid members (skipping members
        whose edges already exist). Reject records the resolution without
        graph mutation. Both persist the resolution for audit and future
        auto-resolve precedent.
        """
        return handlers.handle_resolve_group(
            instance_id,
            group_id,
            action,
            rationale=rationale,
            resolved_by=resolved_by,
            expected_pending_version=expected_pending_version,
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
        )

    @_tool
    def cruxible_list_resolutions(
        instance_id: str,
        relationship_type: str | None = None,
        action: contracts.GroupAction | None = None,
        limit: int = 50,
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
    def cruxible_world_publish(
        instance_id: str,
        transport_ref: str,
        world_id: str,
        release_id: str,
        compatibility: contracts.WorldCompatibility,
    ) -> contracts.WorldPublishResult:
        """Publish a root world-model instance as an immutable release bundle."""
        return handlers.handle_world_publish(
            instance_id,
            transport_ref,
            world_id,
            release_id,
            compatibility,
        )

    @_tool
    def cruxible_world_status(instance_id: str) -> contracts.WorldStatusResult:
        """Return upstream tracking metadata for a release-backed fork."""
        return handlers.handle_world_status(instance_id)

    @_tool
    def cruxible_world_pull_preview(
        instance_id: str,
    ) -> contracts.WorldPullPreviewResult:
        """Preview pulling a newer upstream release into a release-backed fork."""
        return handlers.handle_world_pull_preview(instance_id)

    @_tool
    def cruxible_world_pull_apply(
        instance_id: str,
        expected_apply_digest: str,
    ) -> contracts.WorldPullApplyResult:
        """Apply a previewed upstream release into a release-backed fork."""
        return handlers.handle_world_pull_apply(instance_id, expected_apply_digest)

    @_tool
    def cruxible_get_entity(
        instance_id: str,
        entity_type: str,
        entity_id: str,
    ) -> contracts.GetEntityResult:
        """Look up a specific entity by type and ID. Returns its properties."""
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

    return registered
