"""Canonical rendered views for Cruxible config review surfaces."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import cast

from cruxible_core.canonical_views import (
    build_governance_view,
    build_ontology_view,
    build_overview_view,
    build_provider_contracts_view,
    build_query_view,
    build_schema_catalog_view,
    build_workflow_view,
    render_governed_relationship_table_markdown,
    render_learning_loops_markdown,
    render_mutation_guards_markdown,
    render_ontology_legend_markdown,
    render_ontology_mermaid,
    render_overview_markdown,
    render_provider_contracts_markdown,
    render_quality_rules_markdown,
    render_query_catalog_markdown,
    render_query_map_mermaid,
    render_query_mermaid,
    render_query_mermaid_blocks,
    render_schema_catalog_markdown,
    render_signal_policy_catalog_markdown,
    render_workflow_dependency_mermaid,
    render_workflow_mermaid,
    render_workflow_pipeline_mermaid,
    render_workflow_steps_mermaid,
    render_workflow_steps_mermaid_blocks,
    render_workflow_summary_markdown,
    render_workflow_table_markdown,
)
from cruxible_core.canonical_views.models import OntologyView, OverlayScope, QueryView
from cruxible_core.config.schema import CoreConfig


@dataclass(frozen=True)
class ViewSpec:
    key: str
    title: str
    render: Callable[[CoreConfig], str]
    fenced: bool = True
    render_readme: Callable[[CoreConfig], str] | None = None
    # Overlay-scoped variants, preferred when an OverlayScope is in play.
    render_scoped: Callable[[CoreConfig, OverlayScope], str] | None = None
    render_readme_scoped: Callable[[CoreConfig, OverlayScope], str] | None = None
    # Honest one-line prose emitted instead of an empty diagram/table.
    empty_text: str = "This view has nothing to render for this config."
    empty_text_scoped: str | None = None


class MissingReadmeMarkersError(ValueError):
    """Raised when a README is missing one or more requested marker blocks."""

    def __init__(self, missing_keys: tuple[str, ...]) -> None:
        self.missing_keys = missing_keys
        missing = ", ".join(missing_keys)
        super().__init__(f"Missing README marker block(s): {missing}")


def _as_rendered_text(value: object) -> str:
    return cast(str, value)


def _render_ontology(config: CoreConfig) -> str:
    return _as_rendered_text(render_ontology_mermaid(build_ontology_view(config)))


def _render_ontology_scoped(config: CoreConfig, overlay_scope: OverlayScope) -> str:
    return _as_rendered_text(
        render_ontology_mermaid(build_ontology_view(config, overlay_scope=overlay_scope))
    )


def _render_ontology_readme(config: CoreConfig) -> str:
    return _ontology_readme_block(build_ontology_view(config))


def _render_ontology_readme_scoped(config: CoreConfig, overlay_scope: OverlayScope) -> str:
    return _ontology_readme_block(build_ontology_view(config, overlay_scope=overlay_scope))


def _ontology_readme_block(view: OntologyView) -> str:
    """Fenced ontology diagram plus a generated legend for the styles present."""
    if not view.entity_types and not view.relationships:
        return ""
    block = f"```mermaid\n{render_ontology_mermaid(view)}\n```"
    legend = render_ontology_legend_markdown(view)
    if legend:
        block = f"{block}\n\n{legend}"
    return _as_rendered_text(block)


def resolve_overlay_scope(config_path: Path) -> OverlayScope | None:
    """Layer scope for an extends config: the overlay's own declared names.

    Returns None for single-layer configs. Used by overlay-scoped views so an
    overlay kit's ontology renders its own structure plus ghost seam
    endpoints instead of the entire composed base.
    """
    loader = import_module("cruxible_core.config.loader")
    composer = import_module("cruxible_core.config.composer")
    config = loader.load_config(config_path)
    layers = composer.resolve_config_layers(config, config_path=config_path.resolve())
    if len(layers) < 2:
        return None
    own = layers[-1].config
    return OverlayScope(
        own_entities=frozenset(own.entity_types),
        own_relationships=frozenset(rel.name for rel in own.relationships),
        own_guards=frozenset(guard.name for guard in own.mutation_guards),
        own_queries=frozenset(own.named_queries),
        own_constraints=frozenset(constraint.name for constraint in own.constraints),
        own_checks=frozenset(check.name for check in own.quality_checks),
        own_providers=frozenset(own.providers),
    )


def _render_workflow_story(config: CoreConfig) -> str:
    return _as_rendered_text(render_workflow_mermaid(build_workflow_view(config)))


def _render_workflow_pipeline(config: CoreConfig) -> str:
    return _as_rendered_text(render_workflow_pipeline_mermaid(build_workflow_view(config)))


def _render_workflow_summary(config: CoreConfig) -> str:
    return _as_rendered_text(render_workflow_summary_markdown(build_workflow_view(config)))


def _render_workflow_table(config: CoreConfig) -> str:
    return _as_rendered_text(render_workflow_table_markdown(build_workflow_view(config)))


def _render_governance_table(config: CoreConfig) -> str:
    return _as_rendered_text(render_governed_relationship_table_markdown(config))


def _render_governance_table_scoped(config: CoreConfig, overlay_scope: OverlayScope) -> str:
    return _as_rendered_text(render_governed_relationship_table_markdown(config, overlay_scope))


def _render_mutation_guards(config: CoreConfig) -> str:
    return _as_rendered_text(render_mutation_guards_markdown(config))


def _render_mutation_guards_scoped(config: CoreConfig, overlay_scope: OverlayScope) -> str:
    return _as_rendered_text(render_mutation_guards_markdown(config, overlay_scope))


def _render_signal_policy_catalog(config: CoreConfig) -> str:
    return _as_rendered_text(render_signal_policy_catalog_markdown(config))


def _render_signal_policy_catalog_scoped(config: CoreConfig, overlay_scope: OverlayScope) -> str:
    return _as_rendered_text(render_signal_policy_catalog_markdown(config, overlay_scope))


def _render_quality_rules(config: CoreConfig) -> str:
    return _as_rendered_text(render_quality_rules_markdown(config))


def _render_quality_rules_scoped(config: CoreConfig, overlay_scope: OverlayScope) -> str:
    return _as_rendered_text(render_quality_rules_markdown(config, overlay_scope))


def _render_provider_contracts(config: CoreConfig) -> str:
    return _as_rendered_text(
        render_provider_contracts_markdown(build_provider_contracts_view(config))
    )


def _render_provider_contracts_scoped(config: CoreConfig, overlay_scope: OverlayScope) -> str:
    return _as_rendered_text(
        render_provider_contracts_markdown(build_provider_contracts_view(config, overlay_scope))
    )


def _render_learning_loops(config: CoreConfig) -> str:
    return _as_rendered_text(render_learning_loops_markdown(config))


def _render_workflow_steps(config: CoreConfig) -> str:
    return _as_rendered_text(render_workflow_steps_mermaid(build_workflow_view(config)))


def _render_workflow_steps_readme(config: CoreConfig) -> str:
    blocks = render_workflow_steps_mermaid_blocks(build_workflow_view(config))
    return _render_titled_mermaid_blocks(blocks)


def _render_workflow_dependencies(config: CoreConfig) -> str:
    return _as_rendered_text(render_workflow_dependency_mermaid(build_workflow_view(config)))


def _render_queries(config: CoreConfig) -> str:
    return _as_rendered_text(render_query_mermaid(build_query_view(config, query_infos=[])))


def _render_query_map(config: CoreConfig) -> str:
    return _as_rendered_text(render_query_map_mermaid(build_query_view(config, query_infos=[])))


def _render_query_catalog(config: CoreConfig) -> str:
    return _as_rendered_text(
        render_query_catalog_markdown(build_query_view(config, query_infos=[]))
    )


def _render_query_catalog_scoped(config: CoreConfig, overlay_scope: OverlayScope) -> str:
    view = build_query_view(config, query_infos=[])
    own = [query for query in view.queries if query.name in overlay_scope.own_queries]
    inherited = len(view.queries) - len(own)
    body = _as_rendered_text(
        render_query_catalog_markdown(QueryView(query_count=len(own), queries=own))
    )
    if inherited:
        noun = "query" if inherited == 1 else "queries"
        line = f"Plus {inherited} {noun} inherited from the base kit — see its README."
        body = f"{body}\n\n{line}" if body else line
    return body


def _render_schema_catalog(config: CoreConfig) -> str:
    return _as_rendered_text(render_schema_catalog_markdown(build_schema_catalog_view(config)))


def _render_schema_catalog_scoped(config: CoreConfig, overlay_scope: OverlayScope) -> str:
    return _as_rendered_text(
        render_schema_catalog_markdown(build_schema_catalog_view(config), overlay_scope)
    )


def _render_queries_readme(config: CoreConfig) -> str:
    blocks = render_query_mermaid_blocks(build_query_view(config, query_infos=[]))
    return _render_titled_mermaid_blocks(blocks)


def _render_overview(config: CoreConfig) -> str:
    ontology = build_ontology_view(config)
    workflows = build_workflow_view(config)
    queries = build_query_view(config, query_infos=[])
    governance = build_governance_view(
        config,
        pending_groups=[],
        pending_total=0,
        resolutions=[],
        resolution_total=0,
    )
    return _as_rendered_text(
        render_overview_markdown(
            build_overview_view(
                ontology=ontology,
                workflows=workflows,
                queries=queries,
                governance=governance,
            )
        )
    )


_NO_WORKFLOWS_TEXT = "This kit declares no workflows."
_NO_WORKFLOWS_SCOPED_TEXT = (
    "This layer declares no workflows; composed instances inherit the base kit's."
)
_NO_QUERIES_TEXT = "This kit declares no named queries."
_NO_QUERIES_SCOPED_TEXT = (
    "This layer declares no named queries; composed instances inherit the base kit's."
)

VIEW_SPECS: dict[str, ViewSpec] = {
    "ontology": ViewSpec(
        "ontology",
        "Ontology",
        _render_ontology,
        render_scoped=_render_ontology_scoped,
        render_readme=_render_ontology_readme,
        render_readme_scoped=_render_ontology_readme_scoped,
        empty_text="This kit declares no entity types or relationships.",
        empty_text_scoped=(
            "This layer declares no entity types or relationships of its own; "
            "see the base kit's ontology."
        ),
    ),
    "workflow-story": ViewSpec(
        "workflow-story",
        "Workflow Story",
        _render_workflow_story,
        empty_text=_NO_WORKFLOWS_TEXT,
        empty_text_scoped=_NO_WORKFLOWS_SCOPED_TEXT,
    ),
    "workflow-pipeline": ViewSpec(
        "workflow-pipeline",
        "Workflow Pipeline",
        _render_workflow_pipeline,
        empty_text=_NO_WORKFLOWS_TEXT,
        empty_text_scoped=_NO_WORKFLOWS_SCOPED_TEXT,
    ),
    "workflow-summary": ViewSpec(
        "workflow-summary",
        "Workflow Summary",
        _render_workflow_summary,
        fenced=False,
        empty_text=_NO_WORKFLOWS_TEXT,
        empty_text_scoped=_NO_WORKFLOWS_SCOPED_TEXT,
    ),
    "workflow-table": ViewSpec(
        "workflow-table",
        "Workflow Summary",
        _render_workflow_table,
        fenced=False,
        empty_text=_NO_WORKFLOWS_TEXT,
        empty_text_scoped=_NO_WORKFLOWS_SCOPED_TEXT,
    ),
    "provider-contracts": ViewSpec(
        "provider-contracts",
        "Provider Contracts",
        _render_provider_contracts,
        fenced=False,
        render_scoped=_render_provider_contracts_scoped,
        empty_text=(
            "This kit declares no providers; state is written directly by operators and agents."
        ),
        empty_text_scoped=(
            "This layer declares no providers; state is written directly by "
            "operators and agents, and any base-kit providers are documented "
            "in the base kit's README."
        ),
    ),
    "governance-table": ViewSpec(
        "governance-table",
        "Governed Relationships",
        _render_governance_table,
        fenced=False,
        render_scoped=_render_governance_table_scoped,
        empty_text="This kit declares no governed relationships.",
        empty_text_scoped=(
            "This layer declares no governed relationships of its own; "
            "the base kit's governance applies unchanged."
        ),
    ),
    "mutation-guards": ViewSpec(
        "mutation-guards",
        "Mutation Guards",
        _render_mutation_guards,
        fenced=False,
        render_scoped=_render_mutation_guards_scoped,
    ),
    "signal-policy-catalog": ViewSpec(
        "signal-policy-catalog",
        "Signal Policy Catalog",
        _render_signal_policy_catalog,
        fenced=False,
        render_scoped=_render_signal_policy_catalog_scoped,
        empty_text="No configured proposal signal sources.",
        empty_text_scoped=(
            "This layer's own relationships use no proposal signal sources; "
            "see the base kit's catalog."
        ),
    ),
    "workflow-steps": ViewSpec(
        "workflow-steps",
        "Workflow Steps",
        _render_workflow_steps,
        render_readme=_render_workflow_steps_readme,
        empty_text=_NO_WORKFLOWS_TEXT,
        empty_text_scoped=_NO_WORKFLOWS_SCOPED_TEXT,
    ),
    "workflow-dependencies": ViewSpec(
        "workflow-dependencies",
        "Workflow Dependencies",
        _render_workflow_dependencies,
        empty_text=_NO_WORKFLOWS_TEXT,
        empty_text_scoped=_NO_WORKFLOWS_SCOPED_TEXT,
    ),
    "queries": ViewSpec(
        "queries",
        "Query Surface",
        _render_queries,
        render_readme=_render_queries_readme,
        empty_text=_NO_QUERIES_TEXT,
        empty_text_scoped=_NO_QUERIES_SCOPED_TEXT,
    ),
    "query-map": ViewSpec(
        "query-map",
        "Query Map",
        _render_query_map,
        empty_text=_NO_QUERIES_TEXT,
        empty_text_scoped=_NO_QUERIES_SCOPED_TEXT,
    ),
    "query-catalog": ViewSpec(
        "query-catalog",
        "Query Catalog",
        _render_query_catalog,
        fenced=False,
        render_scoped=_render_query_catalog_scoped,
        empty_text=_NO_QUERIES_TEXT,
        empty_text_scoped=_NO_QUERIES_SCOPED_TEXT,
    ),
    "schema-catalog": ViewSpec(
        "schema-catalog",
        "Schema Catalog",
        _render_schema_catalog,
        fenced=False,
        render_scoped=_render_schema_catalog_scoped,
        empty_text="This kit declares no entity types.",
        empty_text_scoped=(
            "This layer declares no entity types of its own; see the base kit's schema catalog."
        ),
    ),
    "quality-rules": ViewSpec(
        "quality-rules",
        "Quality Rules",
        _render_quality_rules,
        fenced=False,
        render_scoped=_render_quality_rules_scoped,
    ),
    "learning-loops": ViewSpec(
        "learning-loops",
        "Learning Loops",
        _render_learning_loops,
        fenced=False,
    ),
    "overview": ViewSpec("overview", "Config Overview", _render_overview, fenced=False),
}
# query-map stays registered for explicit opt-in but is cut from the default
# README/document order (its arrows restate the query catalog's entry/return
# columns as an unscoped wall).
DEFAULT_VIEW_ORDER = (
    "ontology",
    "schema-catalog",
    "workflow-pipeline",
    "workflow-summary",
    "provider-contracts",
    "governance-table",
    "mutation-guards",
    "signal-policy-catalog",
    "query-catalog",
    "quality-rules",
    "learning-loops",
)
BEGIN_MARKER = "<!-- CRUXIBLE:BEGIN {key} -->"
END_MARKER = "<!-- CRUXIBLE:END {key} -->"


def available_view_keys() -> tuple[str, ...]:
    """Return supported single-view keys."""
    return tuple(VIEW_SPECS)


def selected_view_keys(view: str) -> tuple[str, ...]:
    """Resolve a public view selector into concrete view keys."""
    if view == "all":
        return DEFAULT_VIEW_ORDER
    if view not in VIEW_SPECS:
        choices = ", ".join(("all", *available_view_keys()))
        raise ValueError(f"Unknown config view '{view}'. Expected one of: {choices}")
    return (view,)


def load_config_for_rendering(config_path: Path, *, runtime: bool = False) -> CoreConfig:
    """Load a config path and compose any declared layers for rendering."""
    loader = import_module("cruxible_core.config.loader")
    composer = import_module("cruxible_core.config.composer")
    config = loader.load_config(config_path)
    return cast(
        CoreConfig,
        composer.compose_config_sequence(
            composer.resolve_config_layers(config, config_path=config_path.resolve()),
            runtime=runtime,
        ),
    )


def render_config_views(
    config: CoreConfig,
    *,
    view: str = "all",
    source: str | Path | None = None,
    bare: bool = False,
    overlay_scope: OverlayScope | None = None,
) -> str:
    """Render one or more config views as Markdown/Mermaid text."""
    selected_keys = selected_view_keys(view)
    sections = [
        _render_section(
            spec=VIEW_SPECS[key],
            config=config,
            bare=bare and view != "all",
            overlay_scope=overlay_scope,
        )
        for key in selected_keys
    ]

    if view == "all" and not bare:
        header = "# Cruxible Config Diagrams"
        if source is not None:
            header = f"{header}\n\nSource: `{source}`"
        sections.insert(0, header)

    return "\n\n".join(sections)


def render_readme_update(
    readme_text: str,
    config: CoreConfig,
    selected_keys: tuple[str, ...],
    overlay_scope: OverlayScope | None = None,
) -> str:
    """Render updated README text by replacing existing CRUXIBLE marker blocks."""
    updated = readme_text
    missing_keys: list[str] = []
    for key in selected_keys:
        begin = BEGIN_MARKER.format(key=key)
        end = END_MARKER.format(key=key)
        block = _render_readme_block(
            spec=VIEW_SPECS[key], config=config, overlay_scope=overlay_scope
        )
        pattern = re.compile(
            rf"{re.escape(begin)}\n.*?{re.escape(end)}",
            flags=re.DOTALL,
        )
        replacement = f"{begin}\n{block}\n{end}"
        updated, replacement_count = pattern.subn(replacement, updated)
        if replacement_count == 0:
            missing_keys.append(key)

    if missing_keys:
        raise MissingReadmeMarkersError(tuple(missing_keys))

    return updated


def update_readme_file(
    readme_path: Path,
    config: CoreConfig,
    selected_keys: tuple[str, ...],
    overlay_scope: OverlayScope | None = None,
) -> None:
    """Replace CRUXIBLE marker blocks in a README file."""
    readme_path.write_text(
        render_readme_update(readme_path.read_text(), config, selected_keys, overlay_scope)
    )


def _empty_view_text(spec: ViewSpec, overlay_scope: OverlayScope | None) -> str:
    if overlay_scope is not None and spec.empty_text_scoped is not None:
        return spec.empty_text_scoped
    return spec.empty_text


def _mermaid_body_is_empty(body: str) -> bool:
    """True when a Mermaid body is scaffold-only (no nodes or edges)."""
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("flowchart", "classDef", "linkStyle", "%%")):
            continue
        return False
    return True


def _markdown_body_is_empty(body: str) -> bool:
    """True for a blank body or a lone table header with no data rows."""
    lines = [line for line in body.splitlines() if line.strip()]
    if not lines:
        return True
    return len(lines) == 2 and all(line.lstrip().startswith("|") for line in lines)


def _view_body_is_empty(body: str, *, fenced: bool) -> bool:
    if fenced:
        return _mermaid_body_is_empty(body)
    return _markdown_body_is_empty(body)


def _render_readme_block(
    *, spec: ViewSpec, config: CoreConfig, overlay_scope: OverlayScope | None = None
) -> str:
    if overlay_scope is not None and spec.render_readme_scoped is not None:
        body = spec.render_readme_scoped(config, overlay_scope)
        return body if body.strip() else _empty_view_text(spec, overlay_scope)
    if spec.render_readme is not None:
        body = spec.render_readme(config)
        return body if body.strip() else _empty_view_text(spec, overlay_scope)
    if overlay_scope is not None and spec.render_scoped is not None:
        body = spec.render_scoped(config, overlay_scope)
    else:
        body = spec.render(config)
    if _view_body_is_empty(body, fenced=spec.fenced):
        return _empty_view_text(spec, overlay_scope)
    if not spec.fenced:
        return body
    return f"```mermaid\n{body}\n```"


def _render_titled_mermaid_blocks(blocks: list[tuple[str, str]]) -> str:
    sections: list[str] = []
    for title, mermaid in blocks:
        sections.append(f"### {title}\n\n```mermaid\n{mermaid}\n```")
    return "\n\n".join(sections)


def _render_section(
    *,
    spec: ViewSpec,
    config: CoreConfig,
    bare: bool,
    overlay_scope: OverlayScope | None = None,
) -> str:
    if overlay_scope is not None and spec.render_scoped is not None:
        body = spec.render_scoped(config, overlay_scope)
    else:
        body = spec.render(config)
    fenced = spec.fenced
    if _view_body_is_empty(body, fenced=fenced):
        body = _empty_view_text(spec, overlay_scope)
        fenced = False
    if bare:
        return body
    if not fenced:
        return body
    return f"## {spec.title}\n\n```mermaid\n{body}\n```"
