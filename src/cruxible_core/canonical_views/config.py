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
    build_query_view,
    build_schema_catalog_view,
    build_workflow_view,
    render_governed_relationship_table_markdown,
    render_learning_loops_markdown,
    render_ontology_mermaid,
    render_overview_markdown,
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
from cruxible_core.config.schema import CoreConfig


@dataclass(frozen=True)
class ViewSpec:
    key: str
    title: str
    render: Callable[[CoreConfig], str]
    fenced: bool = True
    render_readme: Callable[[CoreConfig], str] | None = None


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


def _render_signal_policy_catalog(config: CoreConfig) -> str:
    return _as_rendered_text(render_signal_policy_catalog_markdown(config))


def _render_quality_rules(config: CoreConfig) -> str:
    return _as_rendered_text(render_quality_rules_markdown(config))


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


def _render_schema_catalog(config: CoreConfig) -> str:
    return _as_rendered_text(render_schema_catalog_markdown(build_schema_catalog_view(config)))


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


VIEW_SPECS: dict[str, ViewSpec] = {
    "ontology": ViewSpec("ontology", "Ontology", _render_ontology),
    "workflow-story": ViewSpec(
        "workflow-story",
        "Workflow Story",
        _render_workflow_story,
    ),
    "workflow-pipeline": ViewSpec(
        "workflow-pipeline",
        "Workflow Pipeline",
        _render_workflow_pipeline,
    ),
    "workflow-summary": ViewSpec(
        "workflow-summary",
        "Workflow Summary",
        _render_workflow_summary,
        fenced=False,
    ),
    "workflow-table": ViewSpec(
        "workflow-table",
        "Workflow Summary",
        _render_workflow_table,
        fenced=False,
    ),
    "governance-table": ViewSpec(
        "governance-table",
        "Governed Relationships",
        _render_governance_table,
        fenced=False,
    ),
    "signal-policy-catalog": ViewSpec(
        "signal-policy-catalog",
        "Signal Policy Catalog",
        _render_signal_policy_catalog,
        fenced=False,
    ),
    "workflow-steps": ViewSpec(
        "workflow-steps",
        "Workflow Steps",
        _render_workflow_steps,
        render_readme=_render_workflow_steps_readme,
    ),
    "workflow-dependencies": ViewSpec(
        "workflow-dependencies",
        "Workflow Dependencies",
        _render_workflow_dependencies,
    ),
    "queries": ViewSpec(
        "queries",
        "Query Surface",
        _render_queries,
        render_readme=_render_queries_readme,
    ),
    "query-map": ViewSpec("query-map", "Query Map", _render_query_map),
    "query-catalog": ViewSpec(
        "query-catalog",
        "Query Catalog",
        _render_query_catalog,
        fenced=False,
    ),
    "schema-catalog": ViewSpec(
        "schema-catalog",
        "Schema Catalog",
        _render_schema_catalog,
        fenced=False,
    ),
    "quality-rules": ViewSpec(
        "quality-rules",
        "Quality Rules",
        _render_quality_rules,
        fenced=False,
    ),
    "learning-loops": ViewSpec(
        "learning-loops",
        "Learning Loops",
        _render_learning_loops,
        fenced=False,
    ),
    "overview": ViewSpec("overview", "Config Overview", _render_overview, fenced=False),
}
DEFAULT_VIEW_ORDER = (
    "ontology",
    "workflow-pipeline",
    "workflow-summary",
    "governance-table",
    "signal-policy-catalog",
    "query-map",
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
) -> str:
    """Render one or more config views as Markdown/Mermaid text."""
    selected_keys = selected_view_keys(view)
    sections = [
        _render_section(
            spec=VIEW_SPECS[key],
            config=config,
            bare=bare and view != "all",
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
) -> str:
    """Render updated README text by replacing existing CRUXIBLE marker blocks."""
    updated = readme_text
    missing_keys: list[str] = []
    for key in selected_keys:
        begin = BEGIN_MARKER.format(key=key)
        end = END_MARKER.format(key=key)
        block = _render_readme_block(spec=VIEW_SPECS[key], config=config)
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
) -> None:
    """Replace CRUXIBLE marker blocks in a README file."""
    readme_path.write_text(render_readme_update(readme_path.read_text(), config, selected_keys))


def _render_readme_block(*, spec: ViewSpec, config: CoreConfig) -> str:
    if spec.render_readme is not None:
        return spec.render_readme(config)
    body = spec.render(config)
    if not spec.fenced:
        return body
    return f"```mermaid\n{body}\n```"


def _render_titled_mermaid_blocks(blocks: list[tuple[str, str]]) -> str:
    sections: list[str] = []
    for title, mermaid in blocks:
        sections.append(f"### {title}\n\n```mermaid\n{mermaid}\n```")
    return "\n\n".join(sections)


def _render_section(*, spec: ViewSpec, config: CoreConfig, bare: bool) -> str:
    body = spec.render(config)
    if bare:
        return body
    if not spec.fenced:
        return body
    return f"## {spec.title}\n\n```mermaid\n{body}\n```"
