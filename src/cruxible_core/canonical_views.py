"""Lightweight canonical views for kit/system comprehension.

These views are read-only projections over config plus current state. They are
intentionally small: enough to standardize how kits are explained without
committing the product to a heavyweight UI layer.
"""

from __future__ import annotations

import importlib
import inspect
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from cruxible_core.config.schema import CoreConfig, ProviderSchema, WorkflowStepSchema
from cruxible_core.group.types import CandidateGroup, GroupResolution
from cruxible_core.mermaid import (
    MermaidLegendItem,
    render_mermaid_legend,
)
from cruxible_core.mermaid import (
    escape_mermaid_label as _shared_escape_mermaid_label,
)
from cruxible_core.mermaid import (
    mermaid_id as _shared_mermaid_id,
)


@dataclass(frozen=True)
class OntologyEntityView:
    name: str
    primary_key: str | None
    property_count: int
    description: str | None


@dataclass(frozen=True)
class OntologyRelationshipView:
    name: str
    from_entity: str
    to_entity: str
    mode: str
    cardinality: str
    reverse_name: str | None
    description: str | None
    instance_count: int | None = None


@dataclass(frozen=True)
class OntologyView:
    entity_count: int
    relationship_count: int
    governed_relationship_count: int
    entity_types: list[OntologyEntityView] = field(default_factory=list)
    relationships: list[OntologyRelationshipView] = field(default_factory=list)


@dataclass(frozen=True)
class WorkflowStepSummaryView:
    id: str
    kind: str
    detail: str
    output: str | None = None


@dataclass(frozen=True)
class WorkflowProviderSummaryView:
    name: str
    kind: str
    runtime: str
    ref: str
    version: str
    deterministic: bool
    artifact: str | None = None


@dataclass(frozen=True)
class WorkflowSummaryView:
    name: str
    mode: str
    step_count: int
    queries: list[str] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    provider_details: list[WorkflowProviderSummaryView] = field(default_factory=list)
    consumes_relationships: list[str] = field(default_factory=list)
    proposes_relationships: list[str] = field(default_factory=list)
    applies_relationships: list[str] = field(default_factory=list)
    steps: list[WorkflowStepSummaryView] = field(default_factory=list)


@dataclass(frozen=True)
class WorkflowDependencyView:
    source_workflow: str
    target_workflow: str
    via_relationships: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WorkflowView:
    workflow_count: int
    workflows: list[WorkflowSummaryView] = field(default_factory=list)
    dependencies: list[WorkflowDependencyView] = field(default_factory=list)


@dataclass(frozen=True)
class QuerySummaryView:
    name: str
    entry_point: str
    required_params: list[str]
    returns: str
    description: str | None
    example_ids: list[str]
    traversal_summary: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class QueryView:
    query_count: int
    queries: list[QuerySummaryView] = field(default_factory=list)


@dataclass(frozen=True)
class GovernanceRelationshipView:
    relationship_type: str
    auto_resolve_when: str
    prior_trust_policy: str
    pending_group_count: int
    pending_tuple_count: int
    approved_resolution_count: int
    latest_trust_status: str | None


@dataclass(frozen=True)
class PendingBucketView:
    group_id: str
    relationship_type: str
    review_priority: str
    member_count: int
    signature: str
    thesis_text: str


@dataclass(frozen=True)
class GovernanceView:
    governed_relationship_count: int
    pending_group_count: int
    total_pending_groups: int
    approved_resolution_count: int
    total_resolutions: int
    pending_truncated: bool
    resolutions_truncated: bool
    relationships: list[GovernanceRelationshipView] = field(default_factory=list)
    pending_buckets: list[PendingBucketView] = field(default_factory=list)


@dataclass(frozen=True)
class OverviewView:
    ontology: OntologyView
    workflows: WorkflowView
    queries: QueryView
    governance: GovernanceView


def canonical_view_payload(view: Any) -> dict[str, Any]:
    """Serialize a canonical view dataclass tree into JSON-safe dictionaries."""
    return asdict(view)


def build_ontology_view(
    config: CoreConfig,
    *,
    relationship_counts: dict[str, int] | None = None,
) -> OntologyView:
    """Build an ontology view from config and optional live edge counts."""
    entity_views = [
        OntologyEntityView(
            name=name,
            primary_key=schema.get_primary_key(),
            property_count=len(schema.properties),
            description=schema.description,
        )
        for name, schema in sorted(config.entity_types.items())
    ]
    rel_views = [
        OntologyRelationshipView(
            name=rel.name,
            from_entity=rel.from_entity,
            to_entity=rel.to_entity,
            mode="governed" if rel.matching is not None else "deterministic",
            cardinality=rel.cardinality,
            reverse_name=rel.reverse_name,
            description=rel.description,
            instance_count=(relationship_counts or {}).get(rel.name),
        )
        for rel in sorted(config.relationships, key=lambda item: item.name)
    ]
    governed_count = sum(1 for rel in rel_views if rel.mode == "governed")
    return OntologyView(
        entity_count=len(entity_views),
        relationship_count=len(rel_views),
        governed_relationship_count=governed_count,
        entity_types=entity_views,
        relationships=rel_views,
    )


def build_workflow_view(config: CoreConfig) -> WorkflowView:
    """Build a workflow view with inferred relationship dependencies."""
    produced_by_workflow: dict[str, set[str]] = {}
    consumed_by_workflow: dict[str, set[str]] = {}
    workflows: list[WorkflowSummaryView] = []

    for workflow_name, workflow in sorted(config.workflows.items()):
        alias_to_relationship: dict[str, str] = {}
        queries: list[str] = []
        providers: list[str] = []
        steps: list[WorkflowStepSummaryView] = []
        consumes: set[str] = set()
        proposes: set[str] = set()
        applies: set[str] = set()

        for step in workflow.steps:
            step_kind = _workflow_step_kind(step)
            steps.append(_workflow_step_summary(step, step_kind))
            if step_kind == "query" and step.query is not None:
                queries.append(step.query)
                query = config.named_queries.get(step.query)
                if query is not None:
                    for traversal_step in query.traversal:
                        consumes.update(traversal_step.relationship_types)
            elif step_kind == "provider" and step.provider is not None:
                providers.append(step.provider)
            elif step_kind == "list_relationships" and step.list_relationships is not None:
                consumes.add(step.list_relationships.relationship_type)
            elif step_kind == "make_relationships" and step.make_relationships is not None:
                alias = step.as_ or step.id
                alias_to_relationship[alias] = step.make_relationships.relationship_type
            elif (
                step_kind == "propose_relationship_group"
                and step.propose_relationship_group is not None
            ):
                proposes.add(step.propose_relationship_group.relationship_type)
            elif step_kind == "apply_relationships" and step.apply_relationships is not None:
                relationship_type = alias_to_relationship.get(
                    step.apply_relationships.relationships_from
                )
                if relationship_type:
                    applies.add(relationship_type)

        produced_relationships = sorted(proposes | applies)
        consumed_relationships = sorted(consumes)
        produced_by_workflow[workflow_name] = set(produced_relationships)
        consumed_by_workflow[workflow_name] = set(consumed_relationships)
        workflows.append(
            WorkflowSummaryView(
                name=workflow_name,
                mode=_workflow_mode(workflow.canonical, proposes, applies),
                step_count=len(workflow.steps),
                queries=sorted(set(queries)),
                providers=sorted(set(providers)),
                provider_details=_workflow_provider_summaries(
                    sorted(set(providers)),
                    config,
                ),
                consumes_relationships=consumed_relationships,
                proposes_relationships=sorted(proposes),
                applies_relationships=sorted(applies),
                steps=steps,
            )
        )

    dependencies: list[WorkflowDependencyView] = []
    for source_name, source_relationships in produced_by_workflow.items():
        if not source_relationships:
            continue
        for target_name, target_relationships in consumed_by_workflow.items():
            if source_name == target_name:
                continue
            overlap = sorted(source_relationships & target_relationships)
            if overlap:
                dependencies.append(
                    WorkflowDependencyView(
                        source_workflow=source_name,
                        target_workflow=target_name,
                        via_relationships=overlap,
                    )
                )

    dependencies.sort(key=lambda item: (item.source_workflow, item.target_workflow))
    return WorkflowView(
        workflow_count=len(workflows),
        workflows=workflows,
        dependencies=dependencies,
    )


def build_query_view(
    config: CoreConfig,
    *,
    query_infos: list[dict[str, Any]],
) -> QueryView:
    """Build a query view from config plus discovered param metadata."""
    info_by_name = {item["name"]: item for item in query_infos}
    queries: list[QuerySummaryView] = []
    for name, query in sorted(config.named_queries.items()):
        info = info_by_name.get(name, {})
        traversal_summary = [
            _format_traversal_summary(step.relationship_types, step.direction, step.max_depth)
            for step in query.traversal
        ]
        queries.append(
            QuerySummaryView(
                name=name,
                entry_point=query.entry_point,
                required_params=list(info.get("required_params", [])),
                returns=info.get("returns", query.returns),
                description=info.get("description", query.description),
                example_ids=list(info.get("example_ids", [])),
                traversal_summary=traversal_summary,
            )
        )
    return QueryView(query_count=len(queries), queries=queries)


def build_governance_view(
    config: CoreConfig,
    *,
    pending_groups: list[CandidateGroup],
    pending_total: int,
    resolutions: list[GroupResolution],
    resolution_total: int,
) -> GovernanceView:
    """Build a governance summary over governed relationships plus live queue state."""
    governed = {
        rel.name: rel.matching
        for rel in config.relationships
        if rel.matching is not None
    }

    pending_by_relationship: dict[str, list[CandidateGroup]] = {}
    for group in pending_groups:
        pending_by_relationship.setdefault(group.relationship_type, []).append(group)

    approved_by_relationship: dict[str, list[GroupResolution]] = {}
    for resolution in resolutions:
        if resolution.action != "approve":
            continue
        approved_by_relationship.setdefault(resolution.relationship_type, []).append(resolution)

    relationship_rows: list[GovernanceRelationshipView] = []
    for relationship_name, matching in sorted(governed.items()):
        pending = pending_by_relationship.get(relationship_name, [])
        approved = approved_by_relationship.get(relationship_name, [])
        latest = approved[0] if approved else None
        relationship_rows.append(
            GovernanceRelationshipView(
                relationship_type=relationship_name,
                auto_resolve_when=matching.auto_resolve_when,
                prior_trust_policy=matching.auto_resolve_requires_prior_trust,
                pending_group_count=len(pending),
                pending_tuple_count=sum(group.member_count for group in pending),
                approved_resolution_count=len(approved),
                latest_trust_status=latest.trust_status if latest is not None else None,
            )
        )

    pending_rows = [
        PendingBucketView(
            group_id=group.group_id,
            relationship_type=group.relationship_type,
            review_priority=group.review_priority,
            member_count=group.member_count,
            signature=group.signature,
            thesis_text=group.thesis_text,
        )
        for group in pending_groups
    ]

    approved_resolution_count = sum(
        1 for resolution in resolutions if resolution.action == "approve"
    )
    return GovernanceView(
        governed_relationship_count=len(governed),
        pending_group_count=len(pending_groups),
        total_pending_groups=pending_total,
        approved_resolution_count=approved_resolution_count,
        total_resolutions=resolution_total,
        pending_truncated=pending_total > len(pending_groups),
        resolutions_truncated=resolution_total > len(resolutions),
        relationships=relationship_rows,
        pending_buckets=pending_rows,
    )


def build_overview_view(
    *,
    ontology: OntologyView,
    workflows: WorkflowView,
    queries: QueryView,
    governance: GovernanceView,
) -> OverviewView:
    """Compose the four canonical primitives into one overview view."""
    return OverviewView(
        ontology=ontology,
        workflows=workflows,
        queries=queries,
        governance=governance,
    )


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
    return "\n".join(lines)


def render_ontology_mermaid(view: OntologyView) -> str:
    """Render the ontology view as a Mermaid flowchart."""
    deterministic_relationships = [
        relationship
        for relationship in view.relationships
        if relationship.mode == "deterministic"
    ]
    governed_relationships = [
        relationship for relationship in view.relationships if relationship.mode == "governed"
    ]
    deterministic_entities = _relationship_entity_names(deterministic_relationships)
    governed_entities = _relationship_entity_names(governed_relationships)
    canonical_nodes: list[str] = []
    governed_nodes: list[str] = []
    deterministic_edge_indexes: list[int] = []
    governed_edge_indexes: list[int] = []
    edge_index = 0

    lines = [
        "flowchart LR",
        "  classDef canonicalEntity fill:#4a90d9,stroke:#2c5f8a,color:#fff",
        "  classDef governedEntity fill:#e67e22,stroke:#a0521c,color:#fff",
        "",
    ]
    for entity in view.entity_types:
        node_id = _mermaid_id(f"entity_{entity.name}")
        label = _escape_mermaid_label(_humanize_label(entity.name))
        lines.append(f'  {node_id}["{label}"]')
        if entity.name in governed_entities and entity.name not in deterministic_entities:
            governed_nodes.append(node_id)
        else:
            canonical_nodes.append(node_id)

    if canonical_nodes:
        lines.append(f"  class {','.join(canonical_nodes)} canonicalEntity")
    if governed_nodes:
        lines.append(f"  class {','.join(governed_nodes)} governedEntity")

    if deterministic_relationships:
        lines.extend(["", "  %% Deterministic canonical relationships"])
    for relationship in deterministic_relationships:
        src = _mermaid_id(f"entity_{relationship.from_entity}")
        dst = _mermaid_id(f"entity_{relationship.to_entity}")
        label = _escape_mermaid_label(_humanize_label(relationship.name))
        lines.append(f'  {src} -- "{label}" --> {dst}')
        deterministic_edge_indexes.append(edge_index)
        edge_index += 1

    if governed_relationships:
        lines.extend(["", "  %% Governed proposal/review relationships"])
    for relationship in governed_relationships:
        src = _mermaid_id(f"entity_{relationship.from_entity}")
        dst = _mermaid_id(f"entity_{relationship.to_entity}")
        label = _escape_mermaid_label(_humanize_label(relationship.name))
        lines.append(f'  {src} -. "{label}" .-> {dst}')
        governed_edge_indexes.append(edge_index)
        edge_index += 1

    if deterministic_edge_indexes:
        indexes = _format_mermaid_edge_indexes(deterministic_edge_indexes)
        lines.append(f"  linkStyle {indexes} stroke:#2c5f8a,stroke-width:2px")
    if governed_edge_indexes:
        indexes = _format_mermaid_edge_indexes(governed_edge_indexes)
        lines.append(f"  linkStyle {indexes} stroke:#e74c3c,stroke-width:2px")
    return "\n".join(lines)


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


def render_workflow_mermaid(view: WorkflowView) -> str:
    """Render the workflow view as a human-facing Mermaid stage story."""
    return render_workflow_story_mermaid(view)


def render_workflow_story_mermaid(view: WorkflowView) -> str:
    """Render workflows as a linear Mermaid stage story."""
    lines = ["flowchart TD"]
    order = _workflow_story_order(view)
    for workflow in order:
        node_id = _mermaid_id(f"workflow_{workflow.name}")
        label = _escape_mermaid_label(_workflow_story_label(workflow))
        lines.append(f'  {node_id}["{label}"]')

    for source, target in zip(order, order[1:]):
        src = _mermaid_id(f"workflow_{source.name}")
        dst = _mermaid_id(f"workflow_{target.name}")
        lines.append(f"  {src} --> {dst}")

    return "\n".join(lines)


def render_workflow_pipeline_mermaid(view: WorkflowView) -> str:
    """Render workflows as a compact, human-facing pipeline."""
    lines = [
        "flowchart LR",
        "  classDef canonicalWorkflow fill:#4a90d9,stroke:#2c5f8a,color:#fff",
        "  classDef governedWorkflow fill:#e67e22,stroke:#a0521c,color:#fff",
        "",
    ]
    order = [workflow for workflow in _workflow_story_order(view) if workflow.mode != "utility"]
    if not order:
        order = _workflow_story_order(view)
    canonical_nodes: list[str] = []
    governed_nodes: list[str] = []
    for index, workflow in enumerate(order, start=1):
        node_id = _mermaid_id(f"workflow_pipeline_{workflow.name}")
        label = _escape_mermaid_label(_workflow_pipeline_label(index, workflow))
        lines.append(f'  {node_id}["{label}"]')
        if workflow.mode == "canonical":
            canonical_nodes.append(node_id)
        else:
            governed_nodes.append(node_id)

    for source, target in zip(order, order[1:]):
        src = _mermaid_id(f"workflow_pipeline_{source.name}")
        dst = _mermaid_id(f"workflow_pipeline_{target.name}")
        lines.append(f"  {src} --> {dst}")

    if canonical_nodes:
        lines.append(f"  class {','.join(canonical_nodes)} canonicalWorkflow")
    if governed_nodes:
        lines.append(f"  class {','.join(governed_nodes)} governedWorkflow")

    return "\n".join(lines)


def render_workflow_summary_markdown(view: WorkflowView) -> str:
    """Render a readable workflow summary without wide Markdown tables."""
    lines: list[str] = []
    for index, workflow in enumerate(_workflow_story_order(view), start=1):
        if lines:
            lines.append("")
        lines.extend(
            [
                f"### {index}. {_humanize_label(workflow.name)}",
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


def render_workflow_dependency_mermaid(view: WorkflowView) -> str:
    """Render the workflow view as a Mermaid dependency graph."""
    lines = ["flowchart TD"]
    for workflow in view.workflows:
        node_id = _mermaid_id(f"workflow_{workflow.name}")
        label = _escape_mermaid_label(
            f"{_humanize_label(workflow.name)}\n{_humanize_label(workflow.mode)}"
        )
        lines.append(f'  {node_id}["{label}"]')
    if view.dependencies:
        for dependency in view.dependencies:
            src = _mermaid_id(f"workflow_{dependency.source_workflow}")
            dst = _mermaid_id(f"workflow_{dependency.target_workflow}")
            label = _escape_mermaid_label(_humanize_list(dependency.via_relationships))
            lines.append(f'  {src} -- "{label}" --> {dst}')
    return "\n".join(lines)


def render_workflow_steps_mermaid(view: WorkflowView) -> str:
    """Render each workflow as a linear sequence of its declared steps."""
    lines = ["flowchart TD"]
    for workflow in view.workflows:
        subgraph_id = _mermaid_id(f"workflow_steps_{workflow.name}")
        subgraph_label = _escape_mermaid_label(
            f"{_humanize_label(workflow.name)} ({_humanize_label(workflow.mode)})"
        )
        lines.append(f'  subgraph {subgraph_id}["{subgraph_label}"]')
        previous_id: str | None = None
        for index, step in enumerate(workflow.steps, start=1):
            node_id = _mermaid_id(f"{workflow.name}_{index}_{step.id}")
            label = _escape_mermaid_label(_workflow_step_label(index, step))
            lines.append(f'    {node_id}["{label}"]')
            if previous_id is not None:
                lines.append(f"    {previous_id} --> {node_id}")
            previous_id = node_id
        lines.append("  end")
    return "\n".join(lines)


def render_workflow_steps_mermaid_blocks(
    view: WorkflowView,
) -> list[tuple[str, str]]:
    """Render workflow steps as one Mermaid graph per workflow."""
    return [
        (_humanize_label(workflow.name), _render_single_workflow_steps_mermaid(workflow))
        for workflow in view.workflows
    ]


def render_query_markdown(view: QueryView) -> str:
    """Render the query view as compact Markdown."""
    lines = [
        "# Query View",
        "",
        f"- Named queries: {view.query_count}",
        "",
        _markdown_table(
            ("Query", "Entry", "Params", "Returns", "Traversal", "Examples"),
            [
                (
                    item.name,
                    item.entry_point,
                    ", ".join(item.required_params),
                    item.returns,
                    " -> ".join(item.traversal_summary),
                    ", ".join(item.example_ids),
                )
                for item in view.queries
            ],
        ),
    ]
    return "\n".join(lines)


def render_query_mermaid(view: QueryView) -> str:
    """Render the query view as a Mermaid flowchart."""
    lines = ["flowchart TD"]
    for query in view.queries:
        lines.extend(_query_mermaid_lines(query))
    return "\n".join(lines)


def render_query_mermaid_blocks(view: QueryView) -> list[tuple[str, str]]:
    """Render named queries as one Mermaid graph per query."""
    return [
        (_humanize_label(query.name), "\n".join(["flowchart TD", *_query_mermaid_lines(query)]))
        for query in view.queries
    ]


def render_query_map_mermaid(view: QueryView) -> str:
    """Render a compact map of named query entry and return types."""
    edges: set[tuple[str, str]] = set()
    entities: set[str] = set()
    for query in view.queries:
        source = query.entry_point
        target = _query_return_entity(query.returns)
        entities.update((source, target))
        edges.add((source, target))

    lines = [
        "flowchart LR",
        "  classDef queryEntity fill:#ecfdf5,stroke:#047857,color:#064e3b",
        "",
    ]
    for entity in sorted(entities):
        node_id = _mermaid_id(f"query_entity_{entity}")
        label = _escape_mermaid_label(_humanize_label(entity))
        lines.append(f'  {node_id}["{label}"]')

    if entities:
        node_ids = ",".join(_mermaid_id(f"query_entity_{entity}") for entity in sorted(entities))
        lines.append(f"  class {node_ids} queryEntity")

    for source, target in sorted(edges):
        src = _mermaid_id(f"query_entity_{source}")
        dst = _mermaid_id(f"query_entity_{target}")
        lines.append(f"  {src} --> {dst}")

    return "\n".join(lines)


def render_governed_relationship_table_markdown(config: CoreConfig) -> str:
    """Render governed relationship policies from config structure."""
    governed_relationships = [
        relationship
        for relationship in sorted(config.relationships, key=lambda item: item.name)
        if relationship.matching is not None
    ]
    creation_paths = _governed_relationship_creation_paths(config)
    rows: list[tuple[str, ...]] = []
    for relationship in governed_relationships:
        matching = relationship.matching
        if matching is None:
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
                _humanize_label(relationship.name),
                f"{_humanize_label(relationship.from_entity)} -> "
                f"{_humanize_label(relationship.to_entity)}",
                _creation_path_label(creation_paths.get(relationship.name, [])),
                _humanize_list_or_dash(sorted(matching.integrations)),
                _matching_policy_label(
                    matching.auto_resolve_when,
                    matching.auto_resolve_requires_prior_trust,
                ),
                _decision_policy_label(policies),
                _feedback_profile_label(feedback_profile),
                _humanize_list_or_dash(outcomes),
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


def render_integration_catalog_markdown(config: CoreConfig) -> str:
    """Render configured signal integrations and governed relationship usage."""
    if not config.integrations:
        return "No configured integrations."

    used_by: dict[str, set[str]] = {name: set() for name in config.integrations}
    for relationship in config.relationships:
        if relationship.matching is None:
            continue
        for integration_name in relationship.matching.integrations:
            used_by.setdefault(integration_name, set()).add(relationship.name)

    rows: list[tuple[str, ...]] = [
        (
            f"`{name}`",
            integration.kind,
            _humanize_list_or_dash(sorted(used_by.get(name, set()))),
            integration.notes.strip() or "-",
        )
        for name, integration in sorted(config.integrations.items())
    ]
    return _markdown_table(("Integration", "Kind", "Used By", "Notes"), rows)


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
                        _humanize_label(constraint.severity),
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
                        _humanize_label(check.kind),
                        _quality_check_target_label(check),
                        _humanize_label(check.severity),
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
                f"### {_humanize_label(entry_point)}",
                "",
                _markdown_table(
                    ("Query", "Returns", "Traversal", "Purpose"),
                    [
                        (
                            _humanize_label(query.name),
                            _humanize_label(query.returns),
                            " -> ".join(
                                _humanize_traversal_summary(step)
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
                    ("Query", "Params", "Returns", "Traversal"),
                    [
                        (
                            query.name,
                            ", ".join(query.required_params),
                            query.returns,
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


def _workflow_step_kind(step: WorkflowStepSchema) -> str:
    if step.query is not None:
        return "query"
    if step.provider is not None:
        return "provider"
    if step.assert_spec is not None:
        return "assert"
    if step.list_entities is not None:
        return "list_entities"
    if step.list_relationships is not None:
        return "list_relationships"
    if step.make_candidates is not None:
        return "make_candidates"
    if step.map_signals is not None:
        return "map_signals"
    if step.propose_relationship_group is not None:
        return "propose_relationship_group"
    if step.make_entities is not None:
        return "make_entities"
    if step.make_relationships is not None:
        return "make_relationships"
    if step.apply_entities is not None:
        return "apply_entities"
    if step.apply_relationships is not None:
        return "apply_relationships"
    return "unknown"


def _workflow_step_summary(
    step: WorkflowStepSchema,
    step_kind: str,
) -> WorkflowStepSummaryView:
    detail = ""
    if step_kind == "query" and step.query is not None:
        detail = step.query
    elif step_kind == "provider" and step.provider is not None:
        detail = step.provider
    elif step_kind == "list_entities" and step.list_entities is not None:
        detail = step.list_entities.entity_type
    elif step_kind == "list_relationships" and step.list_relationships is not None:
        detail = step.list_relationships.relationship_type
    elif step_kind == "make_candidates" and step.make_candidates is not None:
        detail = step.make_candidates.relationship_type
    elif step_kind == "map_signals" and step.map_signals is not None:
        detail = step.map_signals.integration
    elif (
        step_kind == "propose_relationship_group"
        and step.propose_relationship_group is not None
    ):
        detail = step.propose_relationship_group.relationship_type
    elif step_kind == "make_entities" and step.make_entities is not None:
        detail = step.make_entities.entity_type
    elif step_kind == "make_relationships" and step.make_relationships is not None:
        detail = step.make_relationships.relationship_type
    elif step_kind == "apply_entities" and step.apply_entities is not None:
        detail = step.apply_entities.entities_from
    elif step_kind == "apply_relationships" and step.apply_relationships is not None:
        detail = step.apply_relationships.relationships_from
    elif step_kind == "assert" and step.assert_spec is not None:
        detail = f"{step.assert_spec.left} {step.assert_spec.op} {step.assert_spec.right}"

    return WorkflowStepSummaryView(
        id=step.id,
        kind=step_kind,
        detail=detail,
        output=step.as_,
    )


def _workflow_provider_summaries(
    provider_names: list[str],
    config: CoreConfig,
) -> list[WorkflowProviderSummaryView]:
    summaries: list[WorkflowProviderSummaryView] = []
    for provider_name in provider_names:
        provider = config.providers.get(provider_name)
        if provider is None:
            continue
        summaries.append(_workflow_provider_summary(provider_name, provider))
    return summaries


def _workflow_provider_summary(
    name: str,
    provider: ProviderSchema,
) -> WorkflowProviderSummaryView:
    return WorkflowProviderSummaryView(
        name=name,
        kind=provider.kind,
        runtime=provider.runtime,
        ref=provider.ref,
        version=provider.version,
        deterministic=provider.deterministic,
        artifact=provider.artifact,
    )


def _workflow_mode(
    canonical: bool,
    proposes: set[str],
    applies: set[str],
) -> str:
    if canonical:
        return "canonical"
    if proposes or applies:
        return "governed"
    return "utility"


def _workflow_story_order(view: WorkflowView) -> list[WorkflowSummaryView]:
    workflows_by_name = {workflow.name: workflow for workflow in view.workflows}
    adjacency: dict[str, set[str]] = {workflow.name: set() for workflow in view.workflows}
    indegree: dict[str, int] = {workflow.name: 0 for workflow in view.workflows}
    for dependency in view.dependencies:
        if (
            dependency.source_workflow not in workflows_by_name
            or dependency.target_workflow not in workflows_by_name
        ):
            continue
        if dependency.target_workflow in adjacency[dependency.source_workflow]:
            continue
        adjacency[dependency.source_workflow].add(dependency.target_workflow)
        indegree[dependency.target_workflow] += 1

    ready = sorted(
        (name for name, count in indegree.items() if count == 0),
        key=lambda name: _workflow_story_sort_key(workflows_by_name[name]),
    )
    ordered_names: list[str] = []
    while ready:
        name = ready.pop(0)
        ordered_names.append(name)
        for target in sorted(
            adjacency[name],
            key=lambda item: _workflow_story_sort_key(workflows_by_name[item]),
        ):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort(key=lambda item: _workflow_story_sort_key(workflows_by_name[item]))

    if len(ordered_names) != len(view.workflows):
        ordered = set(ordered_names)
        ordered_names.extend(
            workflow.name
            for workflow in sorted(view.workflows, key=_workflow_story_sort_key)
            if workflow.name not in ordered
        )

    return [workflows_by_name[name] for name in ordered_names]


def _workflow_story_sort_key(workflow: WorkflowSummaryView) -> tuple[int, str]:
    order = {"canonical": 0, "governed": 1, "utility": 2}
    return (order.get(workflow.mode, 3), workflow.name)


def _workflow_story_label(workflow: WorkflowSummaryView) -> str:
    if workflow.applies_relationships:
        detail = "Loads: " + _humanize_list(workflow.applies_relationships)
    elif workflow.proposes_relationships:
        detail = "Proposes: " + _humanize_list(workflow.proposes_relationships)
    elif workflow.providers:
        detail = "Providers: " + _humanize_list(workflow.providers)
    else:
        detail = _humanize_label(workflow.mode)
    return f"{_humanize_label(workflow.name)}\n{detail}"


def _workflow_pipeline_label(index: int, workflow: WorkflowSummaryView) -> str:
    summary = _workflow_pipeline_summary(workflow)
    if workflow.mode == "canonical":
        detail = "Canonical"
    elif workflow.mode == "governed":
        detail = "Governed proposal"
    else:
        detail = "Utility"
    return f"{index}. {summary}\n{detail}"


def _workflow_pipeline_summary(workflow: WorkflowSummaryView) -> str:
    writes = workflow.proposes_relationships + workflow.applies_relationships
    if workflow.mode == "canonical":
        return "Seed canonical state"
    if not writes:
        return _humanize_label(workflow.name)

    relationship = writes[0]
    if relationship == "incident_impacts_supplier":
        return "Assess supplier impact"
    if relationship.startswith("incident_impacts_"):
        entity = _humanize_label(relationship.removeprefix("incident_impacts_"))
        return f"Cascade to {_pluralize_label(entity).lower()}"
    if relationship == "shipment_at_risk":
        return "Flag at-risk shipments"
    return _humanize_label(relationship)


def _pluralize_label(value: str) -> str:
    if value.endswith("y"):
        return f"{value[:-1]}ies"
    if value.endswith("s"):
        return value
    return f"{value}s"


def _workflow_table_role(workflow: WorkflowSummaryView) -> str:
    if workflow.mode == "canonical":
        return "Canonical seed"
    if workflow.mode == "governed":
        return "Governed proposal"
    if workflow.mode == "utility":
        return "Utility"
    return _humanize_label(workflow.mode)


def _workflow_table_input_context(workflow: WorkflowSummaryView) -> str:
    queries = _workflow_step_details(workflow, {"query"})
    entities = _workflow_step_details(workflow, {"list_entities"})
    relationships = _workflow_step_details(workflow, {"list_relationships"})
    context = _format_surface_groups(
        (
            ("Entity context", entities),
            ("Relationship context", relationships),
            ("Named queries", queries),
        )
    )
    if context == "-":
        if workflow.mode == "canonical":
            return "None (seeds canonical state)"
        return "None"
    return context


def _workflow_table_result(workflow: WorkflowSummaryView) -> str:
    entities = _workflow_step_details(workflow, {"make_entities"})
    if workflow.mode == "utility":
        return _format_surface_groups((("Provider output", workflow.providers),))

    if workflow.mode == "canonical":
        relationships = sorted(
            set(workflow.proposes_relationships + workflow.applies_relationships)
        )
        if not relationships:
            relationships = _workflow_step_details(workflow, {"make_relationships"})
        return _format_surface_groups(
            (
                ("Canonical entities", entities),
                ("Canonical relationships", relationships),
            )
        )

    proposed_relationships = sorted(workflow.proposes_relationships)
    applied_relationships = sorted(workflow.applies_relationships)
    fallback_relationships: list[str] = []
    if not proposed_relationships and not applied_relationships:
        fallback_relationships = _workflow_step_details(workflow, {"make_relationships"})
    return _format_surface_groups(
        (
            ("Created entities", entities),
            ("Proposed relationships", proposed_relationships),
            ("Applied relationships", applied_relationships),
            ("Relationships", fallback_relationships),
        )
    )


def _workflow_table_providers(workflow: WorkflowSummaryView) -> str:
    if not workflow.provider_details:
        return _humanize_list_or_dash(workflow.providers)
    return "\n".join(_workflow_provider_label(provider) for provider in workflow.provider_details)


def _workflow_provider_source_bullets(workflow: WorkflowSummaryView) -> list[str]:
    if not workflow.provider_details:
        return _markdown_bullets(_humanize_list_or_dash(workflow.providers))

    lines: list[str] = []
    for provider in workflow.provider_details:
        labels = [
            _workflow_provider_descriptor(provider),
            f"source: `{_provider_source_label(provider)}`",
        ]
        if provider.artifact is not None:
            labels.append(f"artifact: {_humanize_label(provider.artifact)}")
        elif not provider.deterministic:
            labels.append("non-deterministic")
        lines.append(f"- {'; '.join(labels)}")
    return lines


def _workflow_provider_label(provider: WorkflowProviderSummaryView) -> str:
    descriptor = _workflow_provider_descriptor(provider)
    source = _provider_source_label(provider)
    labels = [descriptor, source]
    if provider.artifact is not None:
        labels.append(f"Artifact: {_humanize_label(provider.artifact)}")
    elif not provider.deterministic:
        labels.append("Non-deterministic")
    return "\n".join(labels)


def _workflow_provider_descriptor(provider: WorkflowProviderSummaryView) -> str:
    return (
        f"{_humanize_label(provider.name)} "
        f"({_humanize_label(provider.runtime)} {_humanize_label(provider.kind)}, "
        f"v{provider.version})"
    )


def _provider_source_label(provider: WorkflowProviderSummaryView) -> str:
    if provider.runtime == "python":
        module_name, separator, attr_name = provider.ref.rpartition(".")
        if separator:
            source_path = _provider_source_path(provider.ref, module_name, attr_name)
            if source_path is not None:
                return f"{source_path}::{attr_name}"
            path = module_name.replace(".", "/")
            if module_name.startswith(("cruxible_core.", "cruxible_kits.")):
                path = f"src/{path}"
            return f"{path}.py::{attr_name}"
    return provider.ref


def _provider_source_path(ref: str, module_name: str, attr_name: str) -> str | None:
    try:
        module = importlib.import_module(module_name)
        candidate = getattr(module, attr_name)
        source_path = inspect.getsourcefile(candidate) or inspect.getfile(candidate)
    except Exception:
        return None

    try:
        repo_root = Path(__file__).resolve().parents[2]
        return str(Path(source_path).resolve().relative_to(repo_root))
    except ValueError:
        return source_path


def _workflow_step_details(
    workflow: WorkflowSummaryView,
    kinds: set[str],
) -> list[str]:
    return sorted({step.detail for step in workflow.steps if step.kind in kinds and step.detail})


def _format_surface_groups(groups: tuple[tuple[str, list[str]], ...]) -> str:
    lines = [
        f"{label}: {_humanize_list(values)}"
        for label, values in groups
        if values
    ]
    if not lines:
        return "-"
    return "\n".join(lines)


def _markdown_bullets(value: str) -> list[str]:
    return [f"- {line}" for line in value.splitlines()]


def _workflow_step_label(index: int, step: WorkflowStepSummaryView) -> str:
    prefix = f"{index}. {_humanize_label(step.id)}"
    detail = (
        f"{_humanize_label(step.kind)}: {_humanize_label(step.detail)}"
        if step.detail
        else _humanize_label(step.kind)
    )
    if step.output:
        detail = f"{detail}\nAs: {_humanize_label(step.output)}"
    return f"{prefix}\n{detail}"


def _render_single_workflow_steps_mermaid(workflow: WorkflowSummaryView) -> str:
    lines = ["flowchart TD"]
    previous_id: str | None = None
    for index, step in enumerate(workflow.steps, start=1):
        node_id = _mermaid_id(f"{workflow.name}_{index}_{step.id}")
        label = _escape_mermaid_label(_workflow_step_label(index, step))
        lines.append(f'  {node_id}["{label}"]')
        if previous_id is not None:
            lines.append(f"  {previous_id} --> {node_id}")
        previous_id = node_id
    return "\n".join(lines)


def _query_mermaid_lines(query: QuerySummaryView) -> list[str]:
    query_id = _mermaid_id(f"query_{query.name}")
    entry_id = _mermaid_id(f"query_{query.name}_entry")
    return_id = _mermaid_id(f"query_{query.name}_return")
    query_label = _escape_mermaid_label(_humanize_label(query.name))
    entry_label = _escape_mermaid_label(_humanize_label(query.entry_point))
    return_label = _escape_mermaid_label(_humanize_label(query.returns))
    lines = [
        f'  {query_id}["{query_label}"]',
        f'  {entry_id}["Entry: {entry_label}"]',
        f"  {query_id} --> {entry_id}",
    ]
    previous_id = entry_id
    for index, step in enumerate(query.traversal_summary):
        step_id = _mermaid_id(f"query_{query.name}_step_{index}")
        step_label = _escape_mermaid_label(_humanize_traversal_summary(step))
        lines.append(f'  {step_id}["{step_label}"]')
        lines.append(f"  {previous_id} --> {step_id}")
        previous_id = step_id
    lines.append(f'  {return_id}["Returns: {return_label}"]')
    lines.append(f"  {previous_id} --> {return_id}")
    return lines


def _format_traversal_summary(
    relationships: list[str],
    direction: str,
    max_depth: int,
) -> str:
    rels = "|".join(relationships)
    if max_depth > 1:
        return f"{rels} ({direction}, depth={max_depth})"
    return f"{rels} ({direction})"


def _humanize_list(values: list[str]) -> str:
    return ", ".join(_humanize_label(value) for value in values)


def _humanize_list_or_dash(values: list[str]) -> str:
    if not values:
        return "-"
    return _humanize_list(values)


def _governed_relationship_creation_paths(config: CoreConfig) -> dict[str, list[str]]:
    paths: dict[str, set[str]] = {}
    for workflow_name, workflow in sorted(config.workflows.items()):
        for step in workflow.steps:
            if step.propose_relationship_group is None:
                continue
            relationship_type = step.propose_relationship_group.relationship_type
            paths.setdefault(relationship_type, set()).add(workflow_name)
    return {
        relationship_type: sorted(workflow_names)
        for relationship_type, workflow_names in paths.items()
    }


def _creation_path_label(workflow_names: list[str]) -> str:
    if not workflow_names:
        return "Agent/manual group propose"
    return f"Workflow: {_humanize_list(sorted(workflow_names))}"


def _matching_policy_label(auto_resolve_when: str, prior_trust_policy: str) -> str:
    return (
        f"{_humanize_label(auto_resolve_when)}; "
        f"prior trust: {_humanize_label(prior_trust_policy)}"
    )


def _decision_policy_label(policies: list[Any]) -> str:
    if not policies:
        return "Trust-gated auto-resolve"
    return "; ".join(
        f"{_humanize_label(policy.effect)}: {_humanize_label(policy.name)}"
        for policy in sorted(policies, key=lambda item: item.name)
    )


def _feedback_profile_label(profile: Any | None) -> str:
    if profile is None:
        return "-"
    count = len(profile.reason_codes)
    if count == 1:
        return "1 reason code"
    return f"{count} reason codes"


def _quality_check_target_label(check: Any) -> str:
    kind = getattr(check, "kind", "")
    if kind in {"property", "json_content"}:
        if check.target == "entity":
            return f"{_humanize_label(check.entity_type)}.{check.property}"
        return f"{_humanize_label(check.relationship_type)}.{check.property}"
    if kind == "uniqueness":
        return _humanize_label(check.entity_type)
    if kind == "bounds":
        if check.target == "entity_count":
            return f"{_humanize_label(check.entity_type)} count"
        return f"{_humanize_label(check.relationship_type)} count"
    if kind == "cardinality":
        direction = "out" if check.direction == "outgoing" else "in"
        return (
            f"{_humanize_label(check.entity_type)} -> "
            f"{_humanize_label(check.relationship_type)} ({direction})"
        )
    return "-"


def _quality_check_rule_label(check: Any) -> str:
    kind = getattr(check, "kind", "")
    if kind == "property":
        details = _quality_check_optional_details(
            (
                ("type", check.expected_type),
                ("pattern", check.pattern),
            )
        )
        return _join_label_parts((_humanize_label(check.rule), details))
    if kind == "json_content":
        details = _quality_check_optional_details(
            (
                ("keys", ", ".join(check.keys)),
                ("match", check.match),
            )
        )
        return _join_label_parts((_humanize_label(check.rule), details))
    if kind == "uniqueness":
        return f"Unique on {_code_list(check.properties)}"
    if kind in {"bounds", "cardinality"}:
        return _bounds_rule_label(check.min_count, check.max_count)
    return "-"


def _quality_check_optional_details(values: tuple[tuple[str, str | None], ...]) -> str:
    details = [f"{label}: `{value}`" for label, value in values if value]
    return "; ".join(details)


def _join_label_parts(values: tuple[str, ...]) -> str:
    return "; ".join(value for value in values if value)


def _bounds_rule_label(min_count: int | None, max_count: int | None) -> str:
    parts: list[str] = []
    if min_count is not None:
        parts.append(f"min `{min_count}`")
    if max_count is not None:
        parts.append(f"max `{max_count}`")
    return ", ".join(parts) if parts else "-"


def _code_list(values: list[str]) -> str:
    if not values:
        return "-"
    return ", ".join(f"`{value}`" for value in values)


def _profile_code_bullets(
    codes: Any,
) -> list[str]:
    lines = [
        f"  - `{code}` (`{hint}`): {description}"
        for code, hint, description in codes
    ]
    if lines:
        return lines
    return ["  - None configured."]


def _scope_key_bullets(scope_keys: dict[str, str]) -> list[str]:
    if not scope_keys:
        return ["  - None configured."]
    return [
        f"  - `{key}`: `{path}`"
        for key, path in sorted(scope_keys.items())
    ]


def _render_outcome_profile_group(
    title: str,
    config: CoreConfig,
    anchor_type: str,
) -> list[str]:
    profiles = [
        (name, profile)
        for name, profile in sorted(config.outcome_profiles.items())
        if profile.anchor_type == anchor_type
    ]
    lines = [f"#### {title}", ""]
    if not profiles:
        lines.append(f"No configured {title.lower()} outcome profiles.")
        return lines

    for index, (profile_name, profile) in enumerate(profiles):
        if index > 0:
            lines.append("")
        lines.extend(
            [
                f"##### `{profile_name}`",
                f"- Version: `{profile.version}`",
                f"- Target: {_outcome_profile_target(profile)}",
                "- Outcome codes:",
            ]
        )
        lines.extend(
            _profile_code_bullets(
                (
                    (code, outcome.remediation_hint, outcome.description)
                    for code, outcome in sorted(profile.outcome_codes.items())
                )
            )
        )
        lines.extend(["- Scope keys:", *_scope_key_bullets(profile.scope_keys)])
    return lines


def _outcome_profile_target(profile: Any) -> str:
    if profile.anchor_type == "resolution":
        return f"Relationship `{profile.relationship_type}`"
    return f"{_humanize_label(profile.surface_type)} `{profile.surface_name}`"


def _query_return_entity(value: str) -> str:
    stripped = value.strip().strip('"')
    match = re.fullmatch(r"list\[(.+)\]", stripped, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return stripped


def _relationship_entity_names(
    relationships: list[OntologyRelationshipView],
) -> set[str]:
    entity_names: set[str] = set()
    for relationship in relationships:
        entity_names.add(relationship.from_entity)
        entity_names.add(relationship.to_entity)
    return entity_names


def _format_mermaid_edge_indexes(indexes: list[int]) -> str:
    return ",".join(str(index) for index in indexes)


def _humanize_label(value: str) -> str:
    value = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value)
    value = value.replace("_", " ").replace("-", " ").strip()
    return value.title()


def _humanize_traversal_summary(value: str) -> str:
    relationships, separator, suffix = value.partition(" (")
    relationship_label = " | ".join(
        _humanize_label(relationship) for relationship in relationships.split("|")
    )
    if not separator:
        return relationship_label

    suffix = suffix.rstrip(")")
    parts = suffix.split(", ")
    direction = _humanize_label(parts[0]) if parts else ""
    details = ", ".join(parts[1:])
    if details:
        return f"{relationship_label} ({direction}, {details})"
    return f"{relationship_label} ({direction})"


def _markdown_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    header_row = "| " + " | ".join(headers) + " |"
    divider = "| " + " | ".join("---" for _ in headers) + " |"
    body = [
        "| " + " | ".join(_escape_markdown_cell(cell) for cell in row) + " |"
        for row in rows
    ]
    return "\n".join([header_row, divider, *body])


def _escape_markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", "<br>")


def _escape_mermaid_label(value: str) -> str:
    return str(_shared_escape_mermaid_label(value))


def _mermaid_id(raw: str) -> str:
    return str(_shared_mermaid_id(raw))


def _group_queries_by_entry(
    queries: list[QuerySummaryView],
) -> list[tuple[str, list[QuerySummaryView]]]:
    grouped: dict[str, list[QuerySummaryView]] = {}
    for query in queries:
        grouped.setdefault(query.entry_point, []).append(query)
    return [
        (entry_point, sorted(items, key=lambda item: item.name))
        for entry_point, items in sorted(grouped.items())
    ]


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
