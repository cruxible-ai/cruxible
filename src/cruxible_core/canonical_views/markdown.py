"""Markdown renderers for canonical config views."""

from __future__ import annotations

from typing import Any

from cruxible_core.canonical_views.labels import (
    humanize_label,
    humanize_list_or_dash,
    humanize_traversal_summary,
)
from cruxible_core.canonical_views.mermaid import (
    render_ontology_mermaid,
    render_workflow_mermaid,
)
from cruxible_core.canonical_views.mermaid_utils import (
    MermaidLegendItem,
    render_mermaid_inline_legend,
    render_mermaid_legend,
)
from cruxible_core.canonical_views.models import (
    GovernanceView,
    OntologyEnumView,
    OntologyView,
    OverlayScope,
    OverviewView,
    PropertySchemaView,
    ProviderCallView,
    ProviderContractsView,
    ProviderOutputFieldView,
    ProviderOutputShapeView,
    QuerySummaryView,
    QueryView,
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
        "Complete allowed values for enum-typed properties. Ordered enums list values low to high."
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
    """Render the workflow-table view (same content as the workflow summary)."""
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


def render_governed_relationship_table_markdown(
    config: CoreConfig,
    overlay_scope: OverlayScope | None = None,
) -> str:
    """Render governed relationship policies from config structure.

    With ``overlay_scope``, only the rendered layer's own governed
    relationships appear (overlay READMEs stop restating the base).
    """
    governed_relationships = [
        relationship
        for relationship in sorted(config.relationships, key=lambda item: item.name)
        if relationship.proposal_policy is not None
        and (overlay_scope is None or relationship.name in overlay_scope.own_relationships)
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
                (
                    "Proposal only (direct write refused)"
                    if relationship.write_policy == "proposal_only"
                    else _creation_path_label(creation_paths.get(relationship.name, []))
                ),
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


def _guard_fires_on_label(guard: Any) -> str:
    if guard.entity_type is not None:
        values = guard.new_value if isinstance(guard.new_value, list) else [guard.new_value]
        rendered = ", ".join(str(value) for value in values)
        return f"`{guard.entity_type}.{guard.property}` -> `{rendered}`"
    return f"writes to `{guard.relationship_type}`"


def _guard_requirement_label(guard: Any) -> str:
    condition = guard.condition
    kind = condition.type
    if kind == "query":
        bounds = []
        if condition.min_count is not None:
            bounds.append(f">= {condition.min_count}")
        if condition.max_count is not None:
            bounds.append(f"<= {condition.max_count}")
        return f"query `{condition.query_name}` returns {' and '.join(bounds)} result(s)"
    if kind == "actor":
        actors = ", ".join(condition.allowed_actor_ids)
        return f"authenticated actor in: {actors}"
    if kind == "co_write":
        requires = condition.requires
        kind_note = f"(kind={requires.kind}) " if requires.kind else ""
        return (
            f"same write creates `{requires.entity_type}` {kind_note}"
            f"linked via `{requires.via_relationship}`"
        )
    if kind == "evidence":
        return f">= {condition.min_count} source evidence ref(s)"
    return kind


def render_mutation_guards_markdown(
    config: CoreConfig,
    overlay_scope: OverlayScope | None = None,
) -> str:
    """Render mutation guards as a table: the write-time gates of this config.

    Guards are the hardest promises a kit makes (e.g. an eval-gated promotion
    or a review-gated close), so they get a first-class generated block
    instead of living only in authored prose.
    """
    guards = list(config.mutation_guards)
    if overlay_scope is not None:
        guards = [guard for guard in guards if guard.name in overlay_scope.own_guards]
    if not guards:
        return "No mutation guards declared."
    rows = [
        (
            f"`{guard.name}`",
            _guard_fires_on_label(guard),
            _guard_requirement_label(guard),
            guard.message or "",
        )
        for guard in sorted(guards, key=lambda item: item.name)
    ]
    return _markdown_table(
        ("Guard", "Fires On", "Refused Unless", "Message"),
        rows,
    )


def render_signal_policy_catalog_markdown(
    config: CoreConfig,
    overlay_scope: OverlayScope | None = None,
) -> str:
    """Render governed proposal signal-source policies from relationship config.

    With ``overlay_scope``, only signal sources used by the rendered layer's
    own relationships appear, and base users collapse to a count.
    """
    own_used_by: dict[str, set[str]] = {}
    base_used_by: dict[str, set[str]] = {}
    policy_rows: dict[str, tuple[str, str, str, str]] = {}
    for relationship in config.relationships:
        if relationship.proposal_policy is None:
            continue
        is_own = overlay_scope is None or relationship.name in overlay_scope.own_relationships
        for source_name, policy in relationship.proposal_policy.signals.items():
            if is_own:
                own_used_by.setdefault(source_name, set()).add(relationship.name)
                policy_rows.setdefault(
                    source_name,
                    (
                        policy.role,
                        "yes" if policy.always_review_on_unsure else "no",
                        "yes" if policy.require_evidence_on_support else "no",
                        policy.note.strip() or "-",
                    ),
                )
            else:
                base_used_by.setdefault(source_name, set()).add(relationship.name)

    if not policy_rows:
        if overlay_scope is not None:
            return ""
        return "No configured proposal signal sources."

    rows: list[tuple[str, ...]] = [
        (
            f"`{name}`",
            role,
            always_review,
            require_evidence,
            _signal_used_by_label(
                sorted(own_used_by.get(name, set())),
                len(base_used_by.get(name, set())),
            ),
            note,
        )
        for name, (role, always_review, require_evidence, note) in sorted(policy_rows.items())
    ]
    return _markdown_table(
        ("Signal Source", "Role", "Review Unsure", "Evidence on Support", "Used By", "Notes"),
        rows,
    )


def _signal_used_by_label(own_relationships: list[str], base_count: int) -> str:
    label = humanize_list_or_dash(own_relationships)
    if base_count:
        suffix = f"+ {base_count} base relationship{'s' if base_count != 1 else ''}"
        label = f"{label}, {suffix}" if own_relationships else suffix
    return label


def render_quality_rules_markdown(
    config: CoreConfig,
    overlay_scope: OverlayScope | None = None,
) -> str:
    """Render configured constraints and graph quality checks.

    With ``overlay_scope``, only the rendered layer's own rules appear plus a
    single inherited-count line.
    """
    constraints = sorted(config.constraints, key=lambda item: item.name)
    checks = sorted(config.quality_checks, key=lambda item: item.name)
    inherited_constraints = 0
    inherited_checks = 0
    if overlay_scope is not None:
        own_constraints = [c for c in constraints if c.name in overlay_scope.own_constraints]
        own_checks = [c for c in checks if c.name in overlay_scope.own_checks]
        inherited_constraints = len(constraints) - len(own_constraints)
        inherited_checks = len(checks) - len(own_checks)
        constraints, checks = own_constraints, own_checks

    lines = ["### Constraints", ""]
    if constraints:
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
                    for constraint in constraints
                ],
            )
        )
    else:
        lines.append("No configured constraints.")

    lines.extend(["", "### Quality Checks", ""])
    if checks:
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
                    for check in checks
                ],
            )
        )
    else:
        lines.append("No configured quality checks.")

    inherited_line = _inherited_rules_line(inherited_constraints, inherited_checks)
    if inherited_line:
        lines.extend(["", inherited_line])
    return "\n".join(lines)


def _inherited_rules_line(constraint_count: int, check_count: int) -> str | None:
    parts: list[str] = []
    if constraint_count:
        parts.append(f"{constraint_count} constraint{'s' if constraint_count != 1 else ''}")
    if check_count:
        parts.append(f"{check_count} quality check{'s' if check_count != 1 else ''}")
    if not parts:
        return None
    return f"Plus {' and '.join(parts)} inherited from the base kit — see its README."


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
                                humanize_traversal_summary(step) for step in query.traversal_summary
                            ),
                            query.description.strip() if query.description else "",
                        )
                        for query in queries
                    ],
                ),
            ]
        )
    return "\n".join(lines)


def render_schema_catalog_markdown(
    view: SchemaCatalogView,
    overlay_scope: OverlayScope | None = None,
) -> str:
    """Render the compact schema catalog: one row per entity type, plus the
    enum vocabularies those types actually use.

    With ``overlay_scope``, only the rendered layer's own entity types appear
    (standalone kits render every type).
    """
    entities = view.entity_types
    if overlay_scope is not None:
        entities = [entity for entity in entities if entity.name in overlay_scope.own_entities]
    if not entities:
        return ""
    lines = [
        _markdown_table(
            ("Entity", "Properties", "Description"),
            [
                (
                    f"`{entity.name}`",
                    ", ".join(_compact_property_label(prop) for prop in entity.properties) or "-",
                    entity.description.strip() if entity.description else "-",
                )
                for entity in entities
            ],
        )
    ]
    used_enums: dict[str, list[Any]] = {}
    for entity in entities:
        for prop in entity.properties:
            if prop.enum_ref is not None and prop.enum_ref not in used_enums:
                used_enums[prop.enum_ref] = prop.enum_values or []
    if used_enums:
        lines.extend(
            [
                "",
                "### Enums",
                "",
                _markdown_table(
                    ("Enum", "Values"),
                    [
                        (f"`{name}`", ", ".join(str(value) for value in values))
                        for name, values in sorted(used_enums.items())
                    ],
                ),
            ]
        )
    return "\n".join(lines)


def _compact_property_label(prop: PropertySchemaView) -> str:
    type_label = prop.enum_ref or ("enum" if prop.enum_values is not None else prop.type)
    label = f"{prop.name}: {type_label}"
    if prop.optional:
        label = f"{label}?"
    if prop.primary_key:
        label = f"{label} (pk)"
    return f"`{label}`"


def render_provider_contracts_markdown(view: ProviderContractsView) -> str:
    """Render the swap-the-data provider manual as compact Markdown.

    Optimized for an agent replacing seed data provider by provider: each
    entry states what the provider reads, what each calling step feeds it,
    and the exact row keys downstream make_* steps demand from its output.
    """
    if not view.providers:
        return ""
    lines: list[str] = []
    for provider in view.providers:
        if lines:
            lines.append("")
        badge = "deterministic" if provider.deterministic else "non-deterministic"
        lines.extend(
            [
                f"### `{provider.name}` ({badge})",
                "",
                f"- Ref: `{provider.ref}`",
            ]
        )
        if provider.artifact:
            artifact_label = f"- Reads artifact: `{provider.artifact}`"
            if provider.artifact_uri:
                artifact_label = f"{artifact_label} (`{provider.artifact_uri}`)"
            lines.append(artifact_label)
        if provider.description:
            lines.append(f"- Purpose: {' '.join(provider.description.split())}")
        if not provider.calls:
            lines.append("- Not called by any workflow step.")
        for call in provider.calls:
            lines.extend(_provider_call_lines(call))
    return "\n".join(lines)


def _provider_call_lines(call: ProviderCallView) -> list[str]:
    lines = ["", f"Called by workflow `{call.workflow}`, step `{call.step_id}`:", ""]
    if call.inputs:
        lines.extend(f"- Input `{item.name}` <- {item.source}" for item in call.inputs)
    else:
        lines.append("- Input: none (empty payload).")
    for shape in call.output_shapes:
        lines.append(_provider_output_shape_line(shape))
    return lines


def _provider_output_shape_line(shape: ProviderOutputShapeView) -> str:
    keys = ", ".join(_provider_output_field_label(item) for item in shape.fields) or "none"
    line = (
        f"- Output rows -> `{shape.kind}` step `{shape.step_id}` "
        f"(`{shape.target_type}`): required row keys: {keys}."
    )
    if shape.auto_properties:
        line = f"{line} `properties: auto` — rows must carry every key; null for unset optionals."
    return line


def _provider_output_field_label(item: ProviderOutputFieldView) -> str:
    if item.key is None:
        return f"{item.role} from `{item.expr}`"
    if item.role is not None:
        return f"`{item.key}` ({item.role})"
    if item.target is not None:
        return f"`{item.key}` -> `{item.target}`"
    return f"`{item.key}`"


def render_ontology_legend_markdown(view: OntologyView) -> str:
    """One-line legend for the ontology diagram, listing only styles present."""
    deterministic = [rel for rel in view.relationships if rel.mode == "deterministic"]
    governed = [rel for rel in view.relationships if rel.mode == "governed"]
    deterministic_entities = {rel.from_entity for rel in deterministic} | {
        rel.to_entity for rel in deterministic
    }
    governed_entities = {rel.from_entity for rel in governed} | {rel.to_entity for rel in governed}
    has_base = False
    has_canonical = False
    has_governed_entity = False
    for entity in view.entity_types:
        if entity.origin == "base":
            has_base = True
        elif entity.name in governed_entities and entity.name not in deterministic_entities:
            has_governed_entity = True
        else:
            has_canonical = True

    items: list[MermaidLegendItem] = []
    if has_canonical:
        items.append(MermaidLegendItem("blue node", "canonical entity (deterministic writes)"))
    if has_governed_entity:
        items.append(
            MermaidLegendItem("orange node", "governed entity (enters via proposal/review)")
        )
    if has_base:
        items.append(
            MermaidLegendItem("dashed grey node", "base-kit entity shown for seam context")
        )
    if deterministic:
        items.append(MermaidLegendItem("solid edge", "deterministic relationship"))
    if governed:
        items.append(MermaidLegendItem("dotted edge", "governed relationship"))
    return "\n".join(render_mermaid_inline_legend(items))


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
