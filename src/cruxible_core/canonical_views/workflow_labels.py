"""Workflow-specific labels and ordering for canonical views."""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

from cruxible_core.canonical_views.labels import (
    humanize_label,
    humanize_list,
    humanize_list_or_dash,
)
from cruxible_core.canonical_views.models import (
    WorkflowProviderSummaryView,
    WorkflowStepSummaryView,
    WorkflowSummaryView,
    WorkflowView,
)


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
    order = {"canonical": 0, "proposal": 1, "governed": 1, "decision_support": 2, "utility": 3}
    return (order.get(workflow.mode, 3), workflow.name)


def _workflow_story_label(workflow: WorkflowSummaryView) -> str:
    if workflow.applies_relationships:
        detail = "Loads: " + humanize_list(workflow.applies_relationships)
    elif workflow.proposes_relationships:
        detail = "Proposes: " + humanize_list(workflow.proposes_relationships)
    elif workflow.providers:
        detail = "Providers: " + humanize_list(workflow.providers)
    else:
        detail = humanize_label(workflow.mode)
    return f"{humanize_label(workflow.name)}\n{detail}"


def _workflow_pipeline_label(index: int, workflow: WorkflowSummaryView) -> str:
    summary = _workflow_pipeline_summary(workflow)
    if workflow.mode == "canonical":
        detail = "Canonical"
    elif workflow.mode in {"proposal", "governed"}:
        detail = "Governed proposal"
    elif workflow.mode == "decision_support":
        detail = "Decision support"
    else:
        detail = "Utility"
    return f"{index}. {summary}\n{detail}"


def _workflow_pipeline_summary(workflow: WorkflowSummaryView) -> str:
    writes = workflow.proposes_relationships + workflow.applies_relationships
    if workflow.mode == "canonical":
        return "Seed canonical state"
    if not writes:
        return humanize_label(workflow.name)

    relationship = writes[0]
    return humanize_label(relationship)


def _workflow_table_role(workflow: WorkflowSummaryView) -> str:
    if workflow.mode == "canonical":
        return "Canonical seed"
    if workflow.mode in {"proposal", "governed"}:
        return "Governed proposal"
    if workflow.mode == "decision_support":
        return "Decision support"
    if workflow.mode == "utility":
        return "Utility"
    return humanize_label(workflow.mode)


def _workflow_table_input_context(workflow: WorkflowSummaryView) -> str:
    queries = _workflow_step_details(workflow, {"query"})
    context = _format_surface_groups(
        (
            ("Query context", queries),
        )
    )
    if context == "-":
        if workflow.mode == "canonical":
            return "None (seeds canonical state)"
        return "None"
    return context


def _workflow_table_result(workflow: WorkflowSummaryView) -> str:
    entities = _workflow_step_details(workflow, {"make_entities"})
    if workflow.mode in {"utility", "decision_support"}:
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
        return humanize_list_or_dash(workflow.providers)
    return "\n".join(_workflow_provider_label(provider) for provider in workflow.provider_details)


def _workflow_provider_source_bullets(workflow: WorkflowSummaryView) -> list[str]:
    if not workflow.provider_details:
        return _markdown_bullets(humanize_list_or_dash(workflow.providers))

    lines: list[str] = []
    for provider in workflow.provider_details:
        labels = [
            _workflow_provider_descriptor(provider),
            f"source: `{_provider_source_label(provider)}`",
        ]
        if provider.artifact is not None:
            labels.append(f"artifact: {humanize_label(provider.artifact)}")
        elif not provider.deterministic:
            labels.append("non-deterministic")
        lines.append(f"- {'; '.join(labels)}")
    return lines


def _workflow_provider_label(provider: WorkflowProviderSummaryView) -> str:
    descriptor = _workflow_provider_descriptor(provider)
    source = _provider_source_label(provider)
    labels = [descriptor, source]
    if provider.artifact is not None:
        labels.append(f"Artifact: {humanize_label(provider.artifact)}")
    elif not provider.deterministic:
        labels.append("Non-deterministic")
    return "\n".join(labels)


def _workflow_provider_descriptor(provider: WorkflowProviderSummaryView) -> str:
    return (
        f"{humanize_label(provider.name)} "
        f"({humanize_label(provider.runtime)} {humanize_label(provider.kind)}, "
        f"v{provider.version})"
    )


def _provider_source_label(provider: WorkflowProviderSummaryView) -> str:
    if provider.ref.startswith("kit://"):
        return provider.ref
    if provider.runtime == "python":
        module_name, separator, attr_name = provider.ref.rpartition(".")
        if separator:
            source_path = _provider_source_path(provider.ref, module_name, attr_name)
            if source_path is not None:
                return f"{source_path}::{attr_name}"
            path = module_name.replace(".", "/")
            if module_name.startswith("cruxible_core."):
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
        repo_root = Path(__file__).resolve().parents[3]
        return str(Path(source_path).resolve().relative_to(repo_root))
    except ValueError:
        return source_path


def _workflow_step_details(
    workflow: WorkflowSummaryView,
    kinds: set[str],
) -> list[str]:
    return sorted({step.detail for step in workflow.steps if step.kind in kinds and step.detail})


def _format_surface_groups(groups: tuple[tuple[str, list[str]], ...]) -> str:
    lines = [f"{label}: {humanize_list(values)}" for label, values in groups if values]
    if not lines:
        return "-"
    return "\n".join(lines)


def _markdown_bullets(value: str) -> list[str]:
    return [f"- {line}" for line in value.splitlines()]


def _workflow_step_label(index: int, step: WorkflowStepSummaryView) -> str:
    prefix = f"{index}. {humanize_label(step.id)}"
    detail = (
        f"{humanize_label(step.kind)}: {humanize_label(step.detail)}"
        if step.detail
        else humanize_label(step.kind)
    )
    if step.output:
        detail = f"{detail}\nAs: {humanize_label(step.output)}"
    return f"{prefix}\n{detail}"
