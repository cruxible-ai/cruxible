"""Client-facing MCP tool descriptions.

Style rule for every description:
- Start with "Use when" so non-coding clients can choose tools by intent.
- Prefer domain words a kit user sees in Cruxible: config, query, receipt,
  workflow, review, group, state, source evidence.
- Avoid implementation terms that are not useful for tool choice.
"""

from __future__ import annotations

from cruxible_core.errors import ConfigError

TOOL_PROMPT_STYLE_RULE = (
    'Tool descriptions must start with "Use when", name the user intent first, '
    "and avoid implementation details that do not help a non-coding client choose a tool."
)

TOOL_DESCRIPTIONS: dict[str, str] = {
    "cruxible_version": (
        "Use when you need to confirm which cruxible-core build this MCP server is running."
    ),
    "cruxible_server_info": (
        "Use when you need live daemon details such as state directory, version, "
        "and how many instances are loaded."
    ),
    "cruxible_init": (
        "Use when you need to create a governed instance from a config or reconnect "
        "to an existing instance after a daemon restart."
    ),
    "cruxible_validate": (
        "Use when you need to check whether a Cruxible config is valid before "
        "creating or reloading an instance."
    ),
    "cruxible_state_create_overlay": (
        "Use when you need a local overlay instance based on a published upstream state release."
    ),
    "cruxible_lock_workflow": (
        "Use when workflow inputs, providers, or artifacts changed and you need "
        "to refresh the workflow lock before running it."
    ),
    "cruxible_plan_workflow": (
        "Use when you need to preview the concrete steps a configured workflow "
        "would run without executing those steps."
    ),
    "cruxible_run_workflow": (
        "Use when you need to execute a configured workflow and receive its output, "
        "receipts, traces, and apply instructions if it is a preview."
    ),
    "cruxible_apply_workflow": (
        "Use when a workflow preview returned an apply digest and you are ready "
        "to commit that exact workflow result."
    ),
    "cruxible_test_workflow": (
        "Use when you need to run workflow tests declared by the active config."
    ),
    "cruxible_query": (
        "Use when you need to run a named query from the active config and receive "
        "matching items plus a receipt."
    ),
    "cruxible_query_inline": (
        "Use when you need a one-off bounded graph query without adding it to the config."
    ),
    "cruxible_list_queries": (
        "Use when you need to discover the named queries available in the active config."
    ),
    "cruxible_describe_query": (
        "Use when you need the purpose, parameters, and result shape for one named query."
    ),
    "cruxible_receipt": (
        "Use when you need to inspect the proof record for a previous query, write, "
        "workflow, feedback, or outcome."
    ),
    "cruxible_get_trace": (
        "Use when you need the execution trace for one provider or workflow step."
    ),
    "cruxible_list_traces": (
        "Use when you need to browse execution traces by workflow, provider, or page."
    ),
    "cruxible_feedback": (
        "Use when a person reviewed a specific relationship from a receipt and you "
        "need to record support, rejection, or a correction."
    ),
    "cruxible_feedback_batch": (
        "Use when you need to record several relationship feedback decisions from "
        "the same review session."
    ),
    "cruxible_feedback_from_query": (
        "Use when a query result path identifies the relationship that needs feedback."
    ),
    "cruxible_outcome": (
        "Use when you need to record what happened after a decision, query, workflow, "
        "or reviewed relationship."
    ),
    "cruxible_list": (
        "Use when you need a paged list of entities, relationships, receipts, feedback, "
        "or outcomes with optional filters."
    ),
    "cruxible_evaluate": (
        "Use when you need graph quality findings such as orphaned entities, "
        "coverage gaps, constraint issues, or candidate opportunities."
    ),
    "cruxible_stats": (
        "Use when you need quick counts of entity and relationship types in an instance."
    ),
    "cruxible_lint": (
        "Use when you need a combined quality report for config, graph state, feedback, "
        "and outcome coverage."
    ),
    "cruxible_get_feedback_profile": (
        "Use when you need the allowed feedback codes and guidance for a relationship type."
    ),
    "cruxible_analyze_feedback": (
        "Use when you need patterns from recorded feedback, such as common corrections "
        "or recurring review issues."
    ),
    "cruxible_get_outcome_profile": (
        "Use when you need the allowed outcome codes and guidance for a decision surface."
    ),
    "cruxible_analyze_outcomes": (
        "Use when you need patterns from recorded outcomes for a query, workflow, "
        "relationship, or decision surface."
    ),
    "cruxible_schema": (
        "Use when you need the active entity types, relationships, queries, workflows, "
        "and governance settings."
    ),
    "cruxible_sample": (
        "Use when you need example entities of one type before writing a query or review."
    ),
    "cruxible_inspect_entity": (
        "Use when you need one entity plus nearby relationships and related entities."
    ),
    "cruxible_inspect_entity_history": (
        "Use when you need receipt-derived status transitions for one entity type or entity."
    ),
    "cruxible_inspect_ontology": (
        "Use when you need a compact overview of entity types, relationships, and rules."
    ),
    "cruxible_inspect_workflows": (
        "Use when you need to understand the workflows declared by the active config."
    ),
    "cruxible_inspect_queries": (
        "Use when you need to understand configured queries and their parameters."
    ),
    "cruxible_inspect_governance": (
        "Use when you need to review feedback, outcome, group, and policy settings."
    ),
    "cruxible_inspect_overview": ("Use when you need a single high-level summary of the instance."),
    "cruxible_render_wiki": (
        "Use when you need Markdown documentation generated from the current graph and config."
    ),
    "cruxible_add_relationship": (
        "Use when you need to add or update a small number of explicit relationships "
        "and the endpoint entities already exist."
    ),
    "cruxible_add_entity": (
        "Use when you need to add or update a small number of explicit entities."
    ),
    "cruxible_batch_direct_write": (
        "Use when you need to validate or apply one coherent batch of explicit "
        "entities and relationships; set dry_run first."
    ),
    "cruxible_add_constraint": (
        "Use when you need to add a graph quality rule that future evaluations should check."
    ),
    "cruxible_add_decision_policy": (
        "Use when you need to record a policy that affects how a decision surface "
        "should be handled."
    ),
    "cruxible_reload_config": (
        "Use when you need to replace or reload the active config for an instance."
    ),
    "cruxible_propose_workflow": (
        "Use when a workflow proposes reviewable relationship changes instead of "
        "writing them directly."
    ),
    "cruxible_create_decision_record": (
        "Use when you need to open a tracked decision before gathering evidence, "
        "running workflows, or recording outcomes."
    ),
    "cruxible_get_decision_record": (
        "Use when you need the current state and optional event history for one decision."
    ),
    "cruxible_list_decision_records": (
        "Use when you need to find decision records by status, subject, class, or page."
    ),
    "cruxible_list_decision_events": (
        "Use when you need the event timeline for decisions, optionally filtered by receipt."
    ),
    "cruxible_finalize_decision_record": (
        "Use when a tracked decision has a final answer and rationale."
    ),
    "cruxible_abandon_decision_record": (
        "Use when a tracked decision should be closed without a final decision."
    ),
    "cruxible_propose_group": (
        "Use when you need to create a review group for candidate relationship changes."
    ),
    "cruxible_resolve_group": (
        "Use when a reviewer approves, rejects, or otherwise resolves a pending group."
    ),
    "cruxible_update_trust_status": (
        "Use when you need to mark a prior group resolution as trusted, invalidated, "
        "or otherwise updated."
    ),
    "cruxible_get_group": (
        "Use when you need the details and members for one candidate relationship group."
    ),
    "cruxible_list_groups": (
        "Use when you need to find candidate relationship groups by type, status, or page."
    ),
    "cruxible_list_resolutions": (
        "Use when you need to review past group decisions by relationship type or action."
    ),
    "cruxible_group_status": (
        "Use when you need the latest status for a group or for a known group signature."
    ),
    "cruxible_state_publish": (
        "Use when you need to publish the current instance state as an immutable release."
    ),
    "cruxible_create_snapshot": (
        "Use when you need to mark the current state with a named snapshot."
    ),
    "cruxible_list_snapshots": ("Use when you need to browse available snapshots for an instance."),
    "cruxible_register_source_artifact": (
        "Use when you need to register a source document so relationship evidence can "
        "cite stable chunks from it."
    ),
    "cruxible_dereference_source_evidence": (
        "Use when you need to read back a registered source evidence chunk and verify "
        "its expected content hash."
    ),
    "cruxible_clone_snapshot": (
        "Use when you need a new local instance created from an existing snapshot."
    ),
    "cruxible_state_status": (
        "Use when you need to see whether an overlay is connected to an upstream state "
        "and whether pulls are available."
    ),
    "cruxible_state_pull_preview": (
        "Use when you need to preview upstream state changes before applying them."
    ),
    "cruxible_state_pull_apply": (
        "Use when a pull preview returned an apply digest and you are ready to apply it."
    ),
    "cruxible_get_entity": ("Use when you need to fetch one entity by type and ID."),
    "cruxible_get_relationship": (
        "Use when you need to fetch one relationship by endpoints and relationship type."
    ),
    "cruxible_relationship_lineage": (
        "Use when you need the provenance, review state, feedback, and receipts for "
        "one relationship."
    ),
}


def tool_description(tool_name: str) -> str:
    """Return the reviewed MCP description for *tool_name*."""
    try:
        return TOOL_DESCRIPTIONS[tool_name]
    except KeyError as exc:
        raise ConfigError(f"MCP tool '{tool_name}' is missing a prompt description") from exc


__all__ = [
    "TOOL_DESCRIPTIONS",
    "TOOL_PROMPT_STYLE_RULE",
    "tool_description",
]
