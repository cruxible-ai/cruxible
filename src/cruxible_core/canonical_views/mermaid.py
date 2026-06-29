"""Mermaid renderers for canonical config views."""

from __future__ import annotations

from cruxible_core.canonical_views.labels import (
    humanize_label,
    humanize_list,
    humanize_traversal_summary,
    query_return_entity,
)
from cruxible_core.canonical_views.mermaid_utils import (
    escape_mermaid_label as _shared_escape_mermaid_label,
)
from cruxible_core.canonical_views.mermaid_utils import (
    mermaid_id as _shared_mermaid_id,
)
from cruxible_core.canonical_views.models import (
    OntologyRelationshipView,
    OntologyView,
    QuerySummaryView,
    QueryView,
    WorkflowSummaryView,
    WorkflowView,
)
from cruxible_core.canonical_views.workflow_labels import (
    _workflow_pipeline_label,
    _workflow_step_label,
    _workflow_story_label,
    _workflow_story_order,
)


def render_ontology_mermaid(view: OntologyView) -> str:
    """Render the ontology view as a Mermaid flowchart."""
    deterministic_relationships = [
        relationship for relationship in view.relationships if relationship.mode == "deterministic"
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
        label = _escape_mermaid_label(humanize_label(entity.name))
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
        label = _escape_mermaid_label(humanize_label(relationship.name))
        lines.append(f'  {src} -- "{label}" --> {dst}')
        deterministic_edge_indexes.append(edge_index)
        edge_index += 1

    if governed_relationships:
        lines.extend(["", "  %% Governed proposal/review relationships"])
    for relationship in governed_relationships:
        src = _mermaid_id(f"entity_{relationship.from_entity}")
        dst = _mermaid_id(f"entity_{relationship.to_entity}")
        label = _escape_mermaid_label(humanize_label(relationship.name))
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


def render_workflow_dependency_mermaid(view: WorkflowView) -> str:
    """Render the workflow view as a Mermaid dependency graph."""
    lines = ["flowchart TD"]
    for workflow in view.workflows:
        node_id = _mermaid_id(f"workflow_{workflow.name}")
        label = _escape_mermaid_label(
            f"{humanize_label(workflow.name)}\n{_workflow_mode_label(workflow.mode)}"
        )
        lines.append(f'  {node_id}["{label}"]')
    if view.dependencies:
        for dependency in view.dependencies:
            src = _mermaid_id(f"workflow_{dependency.source_workflow}")
            dst = _mermaid_id(f"workflow_{dependency.target_workflow}")
            label = _escape_mermaid_label(humanize_list(dependency.via_relationships))
            lines.append(f'  {src} -- "{label}" --> {dst}')
    return "\n".join(lines)


def render_workflow_steps_mermaid(view: WorkflowView) -> str:
    """Render each workflow as a linear sequence of its declared steps."""
    lines = ["flowchart TD"]
    for workflow in view.workflows:
        subgraph_id = _mermaid_id(f"workflow_steps_{workflow.name}")
        subgraph_label = _escape_mermaid_label(
            f"{humanize_label(workflow.name)} ({_workflow_mode_label(workflow.mode)})"
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
        (humanize_label(workflow.name), _render_single_workflow_steps_mermaid(workflow))
        for workflow in view.workflows
    ]


def render_query_mermaid(view: QueryView) -> str:
    """Render the query view as a Mermaid flowchart."""
    lines = ["flowchart TD"]
    for query in view.queries:
        lines.extend(_query_mermaid_lines(query))
    return "\n".join(lines)


def render_query_mermaid_blocks(view: QueryView) -> list[tuple[str, str]]:
    """Render named queries as one Mermaid graph per query."""
    return [
        (humanize_label(query.name), "\n".join(["flowchart TD", *_query_mermaid_lines(query)]))
        for query in view.queries
    ]


def render_query_map_mermaid(view: QueryView) -> str:
    """Render a compact map of named query entry and return types."""
    edges: set[tuple[str, str]] = set()
    entities: set[str] = set()
    for query in view.queries:
        source = _query_entry_label(query.entry_point)
        target = query_return_entity(query.returns)
        entities.update((source, target))
        edges.add((source, target))

    lines = [
        "flowchart LR",
        "  classDef queryEntity fill:#ecfdf5,stroke:#047857,color:#064e3b",
        "",
    ]
    for entity in sorted(entities):
        node_id = _mermaid_id(f"query_entity_{entity}")
        label = _escape_mermaid_label(humanize_label(entity))
        lines.append(f'  {node_id}["{label}"]')

    if entities:
        node_ids = ",".join(_mermaid_id(f"query_entity_{entity}") for entity in sorted(entities))
        lines.append(f"  class {node_ids} queryEntity")

    for source, target in sorted(edges):
        src = _mermaid_id(f"query_entity_{source}")
        dst = _mermaid_id(f"query_entity_{target}")
        lines.append(f"  {src} --> {dst}")

    return "\n".join(lines)


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
    query_label = _escape_mermaid_label(f"{humanize_label(query.name)} ({query.mode})")
    entry_label = _escape_mermaid_label(humanize_label(_query_entry_label(query.entry_point)))
    return_label = _escape_mermaid_label(humanize_label(query.returns))
    lines = [
        f'  {query_id}["{query_label}"]',
        f'  {entry_id}["Entry: {entry_label}"]',
        f"  {query_id} --> {entry_id}",
    ]
    previous_id = entry_id
    for index, step in enumerate(query.traversal_summary):
        step_id = _mermaid_id(f"query_{query.name}_step_{index}")
        step_label = _escape_mermaid_label(humanize_traversal_summary(step))
        lines.append(f'  {step_id}["{step_label}"]')
        lines.append(f"  {previous_id} --> {step_id}")
        previous_id = step_id
    lines.append(f'  {return_id}["Returns: {return_label}"]')
    lines.append(f"  {previous_id} --> {return_id}")
    return lines


def _query_entry_label(entry_point: str | None) -> str:
    return entry_point if entry_point is not None else "Collection query"


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


def _escape_mermaid_label(value: str) -> str:
    return str(_shared_escape_mermaid_label(value))


def _workflow_mode_label(mode: str) -> str:
    if mode == "proposal":
        return "Governed proposal"
    if mode == "decision_support":
        return "Decision support"
    return humanize_label(mode)


def _mermaid_id(raw: str) -> str:
    return str(_shared_mermaid_id(raw))
