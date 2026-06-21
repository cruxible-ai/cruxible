"""Markdown renderers for canonical config views."""

from __future__ import annotations

from cruxible_core.canonical_views.labels import (
    humanize_label,
    humanize_list_or_dash,
    humanize_traversal_summary,
)
from cruxible_core.canonical_views.mermaid import (
    render_ontology_mermaid,
    render_workflow_mermaid,
)
from cruxible_core.canonical_views.mermaid_utils import MermaidLegendItem, render_mermaid_legend
from cruxible_core.canonical_views.models import (
    GovernanceView,
    OntologyEnumView,
    OntologyView,
    OverviewView,
    PropertySchemaView,
    QuerySummaryView,
    QueryView,
    SchemaCatalogTypeView,
    SchemaCatalogView,
    WorkflowView,
)
from cruxible_core.canonical_views.policy_labels import (
    _creation_path_label,
    _decision_policy_label,
    _feedback_profile_label,
    _governed_relationship_creation_paths,
    _matching_policy_label,
    _profile_code_bullets,
    _quality_check_rule_label,
    _quality_check_target_label,
    _render_outcome_profile_group,
    _scope_key_bullets,
)
from cruxible_core.canonical_views.workflow_labels import (
    _markdown_bullets,
    _workflow_provider_source_bullets,
    _workflow_story_order,
    _workflow_table_input_context,
    _workflow_table_result,
    _workflow_table_role,
)
from cruxible_core.config.schema import CoreConfig


def render_ontology_markdown(view: OntologyView) -> str:
    """Render the ontology view as compact Markdown."""
    lines = [
        "# Ontology View",
        "",
        f"- Entity types: {view.entity_count}",
        f"- Relationships: {view.relationship_count}",
        f"- Governed relationships: {view.governed_relationship_count}",
        "",
        "## Entity Types",
        "",
        _markdown_table(
            ("Entity", "Primary Key", "Properties", "Description"),
            [
                (
                    item.name,
                    item.primary_key or "",
                    str(item.property_count),
                    item.description or "",
                )
                for item in view.entity_types
            ],
        ),
        "",
        "## Relationships",
        "",
        _markdown_table(
            ("Relationship", "From", "To", "Mode", "Cardinality", "Instances"),
            [
                (
                    item.name,
                    item.from_entity,
                    item.to_entity,
                    item.mode,
                    item.cardinality,
                    "" if item.instance_count is None else str(item.instance_count),
                )
                for item in view.relationships
            ],
        ),
    ]
    lines.extend(_render_enum_vocabularies(view.enums))
    return "\n".join(lines)


def _render_enum_vocabularies(enums: list[OntologyEnumView]) -> list[str]:
    """Render the full enum vocabulary block so values need not be sampled.

    Lists every allowed value in declaration order and flags ordered enums, so an
    agent never has to read the config file to learn the complete vocabulary.
    """
    lines = ["", "## Enum Vocabularies", ""]
    if not enums:
        lines.append("No configured enums.")
        return lines
    lines.append(
        "Complete allowed values for enum-typed properties. "
        "Ordered enums list values low to high."
    )
    lines.append("")
    lines.append(
        _markdown_table(
            ("Enum", "Ordered", "Values", "Used By", "Description"),
            [
                (
                    f"`{item.name}`",
                    "low_to_high" if item.ordered else "-",
                    ", ".join(item.values),
                    ", ".join(item.used_by) or "-",
                    item.description.strip() if item.description else "-",
                )
                for item in enums
            ],
        )
    )
    return lines


def render_workflow_markdown(view: WorkflowView) -> str:
    """Render the workflow view as compact Markdown."""
    lines = [
        "# Workflow View",
        "",
        f"- Workflows: {view.workflow_count}",
        "",
        _markdown_table(
            (
                "Workflow",
                "Mode",
                "Steps",
                "Queries",
                "Providers",
                "Produces",
                "Consumes",
            ),
            [
                (
                    item.name,
                    item.mode,
                    str(item.step_count),
                    ", ".join(item.queries),
                    ", ".join(item.providers),
                    ", ".join(item.proposes_relationships + item.applies_relationships),
                    ", ".join(item.consumes_relationships),
                )
                for item in view.workflows
            ],
        ),
    ]

    if view.dependencies:
        lines.extend(
            [
                "",
                "## Inferred Dependencies",
                "",
                _markdown_table(
                    ("From", "To", "Via"),
                    [
                        (
                            item.source_workflow,
                            item.target_workflow,
                            ", ".join(item.via_relationships),
                        )
                        for item in view.dependencies
                    ],
                ),
            ]
        )

    return "\n".join(lines)


def render_workflow_summary_markdown(view: WorkflowView) -> str:
    """Render a readable workflow summary without wide Markdown tables."""
    lines: list[str] = []
    for index, workflow in enumerate(_workflow_story_order(view), start=1):
        if lines:
            lines.append("")
        lines.extend(
            [
                f"### {index}. {humanize_label(workflow.name)}",
                "",
                f"**Role:** {_workflow_table_role(workflow)}",
                "",
                "**Input context**",
                *_markdown_bullets(_workflow_table_input_context(workflow)),
                "",
                "**Result**",
                *_markdown_bullets(_workflow_table_result(workflow)),
                "",
                "**Provider source**",
                *_workflow_provider_source_bullets(workflow),
            ]
        )
    return "\n".join(lines)


def render_workflow_table_markdown(view: WorkflowView) -> str:
    """Backward-compatible alias for the old workflow-table view key."""
    return render_workflow_summary_markdown(view)


def render_query_markdown(view: QueryView) -> str:
    """Render the query view as compact Markdown."""
    lines = [
        "# Query View",
        "",
        f"- Named queries: {view.query_count}",
        "",
        _markdown_table(
            ("Query", "Mode", "Entry", "Params", "Returns", "State", "Traversal", "Examples"),
            [
                (
                    item.name,
                    item.mode,
                    query_entry_label(item.entry_point),
                    ", ".join(item.required_params),
                    item.returns,
                    item.relationship_state,
                    " -> ".join(item.traversal_summary),
                    ", ".join(item.example_ids),
                )
                for item in view.queries
            ],
        ),
    ]
    return "\n".join(lines)


def render_governed_relationship_table_markdown(config: CoreConfig) -> str:
    """Render governed relationship policies from config structure."""
    governed_relationships = [
        relationship
        for relationship in sorted(config.relationships, key=lambda item: item.name)
        if relationship.proposal_policy is not None
    ]
    creation_paths = _governed_relationship_creation_paths(config)
    rows: list[tuple[str, ...]] = []
    for relationship in governed_relationships:
        proposal_policy = relationship.proposal_policy
        if proposal_policy is None:
            continue
        policies = [
            policy
            for policy in config.decision_policies
            if policy.relationship_type == relationship.name
        ]
        outcomes = [
            name
            for name, profile in sorted(config.outcome_profiles.items())
            if profile.relationship_type == relationship.name
        ]
        feedback_profile = config.feedback_profiles.get(relationship.name)
        rows.append(
            (
                humanize_label(relationship.name),
                f"{humanize_label(relationship.from_entity)} -> "
                f"{humanize_label(relationship.to_entity)}",
                _creation_path_label(creation_paths.get(relationship.name, [])),
                humanize_list_or_dash(sorted(proposal_policy.signals)),
                _matching_policy_label(
                    proposal_policy.auto_resolve_when,
                    proposal_policy.auto_resolve_requires_prior_trust,
                ),
                _decision_policy_label(policies),
                _feedback_profile_label(feedback_profile),
                humanize_list_or_dash(outcomes),
            )
        )
    return _markdown_table(
        (
            "Relationship",
            "Scope",
            "Creation Path",
            "Signals",
            "Auto-resolve Gate",
            "Review Policy",
            "Feedback",
            "Outcomes",
        ),
        rows,
    )


def render_signal_policy_catalog_markdown(config: CoreConfig) -> str:
    """Render governed proposal signal-source policies from relationship config."""
    used_by: dict[str, set[str]] = {}
    policy_rows: dict[str, tuple[str, str, str]] = {}
    for relationship in config.relationships:
        if relationship.proposal_policy is None:
            continue
        for source_name, policy in relationship.proposal_policy.signals.items():
            used_by.setdefault(source_name, set()).add(relationship.name)
            policy_rows.setdefault(
                source_name,
                (
                    policy.role,
                    "yes" if policy.always_review_on_unsure else "no",
                    policy.note.strip() or "-",
                ),
            )

    if not policy_rows:
        return "No configured proposal signal sources."

    rows: list[tuple[str, ...]] = [
        (
            f"`{name}`",
            role,
            always_review,
            humanize_list_or_dash(sorted(used_by.get(name, set()))),
            note,
        )
        for name, (role, always_review, note) in sorted(policy_rows.items())
    ]
    return _markdown_table(
        ("Signal Source", "Role", "Review Unsure", "Used By", "Notes"),
        rows,
    )


def render_quality_rules_markdown(config: CoreConfig) -> str:
    """Render configured constraints and graph quality checks."""
    lines = ["### Constraints", ""]
    if config.constraints:
        lines.append(
            _markdown_table(
                ("Name", "Severity", "Rule", "Description"),
                [
                    (
                        f"`{constraint.name}`",
                        humanize_label(constraint.severity),
                        constraint.rule,
                        constraint.description or "-",
                    )
                    for constraint in sorted(config.constraints, key=lambda item: item.name)
                ],
            )
        )
    else:
        lines.append("No configured constraints.")

    lines.extend(["", "### Quality Checks", ""])
    if config.quality_checks:
        lines.append(
            _markdown_table(
                ("Name", "Kind", "Target", "Severity", "Rule"),
                [
                    (
                        f"`{check.name}`",
                        humanize_label(check.kind),
                        _quality_check_target_label(check),
                        humanize_label(check.severity),
                        _quality_check_rule_label(check),
                    )
                    for check in sorted(config.quality_checks, key=lambda item: item.name)
                ],
            )
        )
    else:
        lines.append("No configured quality checks.")
    return "\n".join(lines)


def render_learning_loops_markdown(config: CoreConfig) -> str:
    """Render feedback and outcome profile vocabularies."""
    lines = ["### Feedback Profiles (Loop 1)", ""]
    if config.feedback_profiles:
        for profile_name, profile in sorted(config.feedback_profiles.items()):
            if len(lines) > 2:
                lines.append("")
            lines.extend(
                [
                    f"#### `{profile_name}`",
                    f"- Version: `{profile.version}`",
                    "- Reason codes:",
                ]
            )
            lines.extend(
                _profile_code_bullets(
                    (
                        (code, reason.remediation_hint, reason.description)
                        for code, reason in sorted(profile.reason_codes.items())
                    )
                )
            )
            lines.extend(["- Scope keys:", *_scope_key_bullets(profile.scope_keys)])
    else:
        lines.append("No configured feedback profiles.")

    lines.extend(["", "### Outcome Profiles (Loop 2)", ""])
    lines.extend(_render_outcome_profile_group("Resolution-Anchored", config, "resolution"))
    lines.append("")
    lines.extend(_render_outcome_profile_group("Receipt-Anchored", config, "receipt"))
    return "\n".join(lines)


def render_query_catalog_markdown(view: QueryView) -> str:
    """Render named queries as grouped, human-readable catalog tables."""
    lines: list[str] = []
    for entry_point, queries in _group_queries_by_entry(view.queries):
        if lines:
            lines.append("")
        lines.extend(
            [
                f"### {humanize_label(entry_point)}",
                "",
                _markdown_table(
                    ("Query", "Mode", "Returns", "State", "Traversal", "Purpose"),
                    [
                        (
                            humanize_label(query.name),
                            query.mode,
                            humanize_label(query.returns),
                            query.relationship_state,
                            " -> ".join(
                                humanize_traversal_summary(step)
                                for step in query.traversal_summary
                            ),
                            query.description.strip() if query.description else "",
                        )
                        for query in queries
                    ],
                ),
            ]
        )
    return "\n".join(lines)


def render_schema_catalog_markdown(view: SchemaCatalogView) -> str:
    """Render entity, relationship, and contract property schemas."""
    lines: list[str] = []
    lines.extend(_render_schema_catalog_group("Entity Types", view.entity_types))
    lines.append("")
    lines.extend(_render_schema_catalog_group("Relationships", view.relationships))
    lines.append("")
    lines.extend(_render_schema_catalog_group("Contracts", view.contracts))
    return "\n".join(lines)


def render_governance_markdown(view: GovernanceView) -> str:
    """Render the governance view as compact Markdown."""
    lines = [
        "# Governance View",
        "",
        f"- Governed relationships: {view.governed_relationship_count}",
        f"- Pending buckets shown: {view.pending_group_count}",
        f"- Pending buckets total: {view.total_pending_groups}",
        f"- Approved resolutions shown: {view.approved_resolution_count}",
        f"- Resolutions total: {view.total_resolutions}",
    ]
    if view.pending_truncated or view.resolutions_truncated:
        lines.append("- Note: results are truncated to the requested fetch limit.")

    lines.extend(
        [
            "",
            "## Relationship Policies",
            "",
            _markdown_table(
                (
                    "Relationship",
                    "Auto-resolve",
                    "Prior Trust",
                    "Pending Groups",
                    "Pending Tuples",
                    "Approved Resolutions",
                    "Latest Trust",
                ),
                [
                    (
                        item.relationship_type,
                        item.auto_resolve_when,
                        item.prior_trust_policy,
                        str(item.pending_group_count),
                        str(item.pending_tuple_count),
                        str(item.approved_resolution_count),
                        item.latest_trust_status or "",
                    )
                    for item in view.relationships
                ],
            ),
        ]
    )
    if view.pending_buckets:
        lines.extend(
            [
                "",
                "## Pending Buckets",
                "",
                _markdown_table(
                    ("Group ID", "Relationship", "Priority", "Members", "Signature", "Thesis"),
                    [
                        (
                            item.group_id,
                            item.relationship_type,
                            item.review_priority,
                            str(item.member_count),
                            item.signature,
                            item.thesis_text,
                        )
                        for item in view.pending_buckets
                    ],
                ),
            ]
        )
    return "\n".join(lines)


def render_overview_markdown(view: OverviewView) -> str:
    """Render a readable generated overview from the canonical views."""
    deterministic = [rel for rel in view.ontology.relationships if rel.mode == "deterministic"]
    governed = [rel for rel in view.ontology.relationships if rel.mode == "governed"]
    query_groups = _group_queries_by_entry(view.queries.queries)

    lines = [
        "# Config Overview",
        "",
        (
            "This page is generated from the canonical ontology, workflow, query, "
            "and governance views."
        ),
        "",
        "## At A Glance",
        "",
        f"- Entity types: {view.ontology.entity_count}",
        f"- Relationship types: {view.ontology.relationship_count}",
        f"- Governed relationship types: {view.ontology.governed_relationship_count}",
        f"- Workflows: {view.workflows.workflow_count}",
        f"- Named queries: {view.queries.query_count}",
        f"- Pending buckets: {view.governance.total_pending_groups}",
        f"- Approved resolutions: {view.governance.total_resolutions}",
        "",
        "## Entity Types",
        "",
        _markdown_table(
            ("Entity", "Primary Key", "Properties", "Description"),
            [
                (
                    entity.name,
                    entity.primary_key or "",
                    str(entity.property_count),
                    entity.description or "",
                )
                for entity in view.ontology.entity_types
            ],
        ),
        *_render_enum_vocabularies(view.ontology.enums),
        "",
        "## Relationship Map",
        "",
        "```mermaid",
        render_ontology_mermaid(view.ontology),
        "```",
        "",
        *render_mermaid_legend(
            (
                MermaidLegendItem(
                    "Blue entity node",
                    "Entity type that participates in deterministic state.",
                ),
                MermaidLegendItem(
                    "Orange entity node",
                    "Entity type that only appears in governed relationships.",
                ),
                MermaidLegendItem("Solid blue edge", "Deterministic relationship."),
                MermaidLegendItem("Dashed red edge", "Governed relationship."),
            )
        ),
        "",
        "### Deterministic Relationships",
        "",
        _markdown_table(
            ("Relationship", "From", "To", "Instances"),
            [
                (
                    rel.name,
                    rel.from_entity,
                    rel.to_entity,
                    "" if rel.instance_count is None else str(rel.instance_count),
                )
                for rel in deterministic
            ],
        ),
        "",
        "### Governed Relationships",
        "",
        _markdown_table(
            ("Relationship", "From", "To", "Approved", "Pending", "Latest Trust"),
            [
                (
                    rel.name,
                    rel.from_entity,
                    rel.to_entity,
                    str(_governed_resolution_count(view.governance, rel.name)),
                    str(_governed_pending_count(view.governance, rel.name)),
                    _governed_latest_trust(view.governance, rel.name) or "",
                )
                for rel in governed
            ],
        ),
        "",
        "## Workflow Chain",
        "",
        "```mermaid",
        render_workflow_mermaid(view.workflows),
        "```",
        "",
        _markdown_table(
            ("Workflow", "Mode", "Produces", "Consumes"),
            [
                (
                    workflow.name,
                    workflow.mode,
                    ", ".join(workflow.proposes_relationships + workflow.applies_relationships),
                    ", ".join(workflow.consumes_relationships),
                )
                for workflow in view.workflows.workflows
            ],
        ),
        "",
        "## Query Surface",
        "",
        (
            "Queries are grouped by entry point so the surface reads like "
            "starting perspectives into the graph."
        ),
    ]

    for entry_point, queries_for_entry in query_groups:
        lines.extend(
            [
                "",
                f"### {entry_point}",
                "",
                _markdown_table(
                    ("Query", "Mode", "Params", "Returns", "State", "Traversal"),
                    [
                        (
                            query.name,
                            query.mode,
                            ", ".join(query.required_params),
                            query.returns,
                            query.relationship_state,
                            " -> ".join(query.traversal_summary),
                        )
                        for query in queries_for_entry
                    ],
                ),
            ]
        )

    lines.extend(
        [
            "",
            "## Governance State",
            "",
            _markdown_table(
                (
                    "Relationship",
                    "Auto-resolve",
                    "Prior Trust",
                    "Pending Groups",
                    "Approved Resolutions",
                    "Latest Trust",
                ),
                [
                    (
                        rel.relationship_type,
                        rel.auto_resolve_when,
                        rel.prior_trust_policy,
                        str(rel.pending_group_count),
                        str(rel.approved_resolution_count),
                        rel.latest_trust_status or "",
                    )
                    for rel in view.governance.relationships
                ],
            ),
        ]
    )

    if view.governance.pending_buckets:
        lines.extend(
            [
                "",
                "### Pending Buckets",
                "",
                _markdown_table(
                    ("Group ID", "Relationship", "Priority", "Members", "Thesis"),
                    [
                        (
                            bucket.group_id,
                            bucket.relationship_type,
                            bucket.review_priority,
                            str(bucket.member_count),
                            bucket.thesis_text,
                        )
                        for bucket in view.governance.pending_buckets
                    ],
                ),
            ]
        )

    return "\n".join(lines)


def _render_schema_catalog_group(
    title: str,
    entries: list[SchemaCatalogTypeView],
) -> list[str]:
    lines = [f"### {title}", ""]
    if not entries:
        lines.append("No configured entries.")
        return lines
    for entry in entries:
        lines.append(f"#### `{entry.name}`")
        if entry.kind == "relationship" and entry.from_entity and entry.to_entity:
            mode = f" ({entry.mode})" if entry.mode else ""
            lines.append(f"- Scope: `{entry.from_entity}` -> `{entry.to_entity}`{mode}")
        if entry.description:
            lines.append(f"- Description: {entry.description.strip()}")
        if entry.properties:
            lines.append(
                _markdown_table(
                    ("Property", "Type", "Flags", "Enum", "Default", "Description"),
                    [_property_schema_row(prop) for prop in entry.properties],
                )
            )
        else:
            lines.append("No declared properties.")
        lines.append("")
    if lines[-1] == "":
        lines.pop()
    return lines


def _property_schema_row(prop: PropertySchemaView) -> tuple[str, ...]:
    flags: list[str] = []
    if prop.primary_key:
        flags.append("primary key")
    if prop.optional:
        flags.append("optional")
    enum_label = "-"
    if prop.enum_ref is not None:
        values = ", ".join(str(value) for value in prop.enum_values or [])
        enum_label = f"`{prop.enum_ref}`"
        if values:
            enum_label = f"{enum_label}: {values}"
    elif prop.enum_values is not None:
        enum_label = ", ".join(str(value) for value in prop.enum_values)
    return (
        f"`{prop.name}`",
        prop.type,
        ", ".join(flags) or "-",
        enum_label,
        "-" if prop.default is None else f"`{prop.default}`",
        prop.description or "-",
    )


def _markdown_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    header_row = "| " + " | ".join(headers) + " |"
    divider = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(_escape_markdown_cell(cell) for cell in row) + " |" for row in rows]
    return "\n".join([header_row, divider, *body])


def _escape_markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", "<br>")


def _group_queries_by_entry(
    queries: list[QuerySummaryView],
) -> list[tuple[str, list[QuerySummaryView]]]:
    grouped: dict[str, list[QuerySummaryView]] = {}
    for query in queries:
        grouped.setdefault(query_entry_label(query.entry_point), []).append(query)
    return [
        (entry_point, sorted(items, key=lambda item: item.name))
        for entry_point, items in sorted(grouped.items())
    ]


def query_entry_label(entry_point: str | None) -> str:
    """Return the display label for a named-query entry point."""
    return entry_point if entry_point is not None else "Collection query"


def _governed_resolution_count(view: GovernanceView, relationship_name: str) -> int:
    for item in view.relationships:
        if item.relationship_type == relationship_name:
            return item.approved_resolution_count
    return 0


def _governed_pending_count(view: GovernanceView, relationship_name: str) -> int:
    for item in view.relationships:
        if item.relationship_type == relationship_name:
            return item.pending_group_count
    return 0


def _governed_latest_trust(view: GovernanceView, relationship_name: str) -> str | None:
    for item in view.relationships:
        if item.relationship_type == relationship_name:
            return item.latest_trust_status
    return None
