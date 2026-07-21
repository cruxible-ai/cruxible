"""Canonical view builders and renderers."""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "GovernanceRelationshipView",
    "GovernanceView",
    "OntologyEntityView",
    "OntologyEnumView",
    "OntologyRelationshipView",
    "OntologyView",
    "OverviewView",
    "PendingBucketView",
    "PropertySchemaView",
    "ProviderCallView",
    "ProviderContractView",
    "ProviderContractsView",
    "ProviderInputFieldView",
    "ProviderOutputFieldView",
    "ProviderOutputShapeView",
    "QuerySummaryView",
    "QueryView",
    "SchemaCatalogTypeView",
    "SchemaCatalogView",
    "WorkflowDependencyView",
    "WorkflowProviderSummaryView",
    "WorkflowStepSummaryView",
    "WorkflowSummaryView",
    "WorkflowView",
    "build_governance_view",
    "build_ontology_view",
    "build_overview_view",
    "build_provider_contracts_view",
    "build_query_view",
    "build_schema_catalog_view",
    "build_workflow_view",
    "canonical_view_payload",
    "render_gates_markdown",
    "render_governance_markdown",
    "render_governed_relationship_table_markdown",
    "render_mutation_guards_markdown",
    "render_learning_loops_markdown",
    "render_ontology_legend_markdown",
    "render_ontology_markdown",
    "render_ontology_mermaid",
    "render_overview_markdown",
    "render_provider_contracts_markdown",
    "render_quality_rules_markdown",
    "render_query_catalog_markdown",
    "render_query_map_mermaid",
    "render_query_markdown",
    "render_query_mermaid",
    "render_query_mermaid_blocks",
    "render_schema_catalog_markdown",
    "render_signal_policy_catalog_markdown",
    "render_workflow_dependency_mermaid",
    "render_workflow_markdown",
    "render_workflow_mermaid",
    "render_workflow_pipeline_mermaid",
    "render_workflow_steps_mermaid",
    "render_workflow_steps_mermaid_blocks",
    "render_workflow_summary_markdown",
    "render_workflow_table_markdown",
]

_BUILDERS = {
    "build_governance_view",
    "build_ontology_view",
    "build_overview_view",
    "build_provider_contracts_view",
    "build_query_view",
    "build_schema_catalog_view",
    "build_workflow_view",
}
_MARKDOWN = {name for name in __all__ if name.startswith("render_") and "mermaid" not in name}
_MERMAID = {name for name in __all__ if "mermaid" in name}
_MODELS = set(__all__) - _BUILDERS - _MARKDOWN - _MERMAID


def __getattr__(name: str) -> Any:
    """Load only the canonical-view layer that owns the requested symbol."""
    if name in _BUILDERS:
        module_name = "builders"
    elif name in _MARKDOWN:
        module_name = "markdown"
    elif name in _MERMAID:
        module_name = "mermaid"
    elif name in _MODELS:
        module_name = "models"
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f"{__name__}.{module_name}")
    value = getattr(module, name)
    globals()[name] = value
    return value
