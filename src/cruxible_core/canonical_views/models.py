"""Dataclass models for canonical config views."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


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
class OntologyEnumView:
    name: str
    values: list[str]
    ordered: bool
    description: str | None
    used_by: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OntologyView:
    entity_count: int
    relationship_count: int
    governed_relationship_count: int
    entity_types: list[OntologyEntityView] = field(default_factory=list)
    relationships: list[OntologyRelationshipView] = field(default_factory=list)
    enums: list[OntologyEnumView] = field(default_factory=list)


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
    mode: str
    entry_point: str | None
    required_params: list[str]
    returns: str
    result_shape: str = "path"
    dedupe: str = "path"
    relationship_state: str = "live"
    allow_relationship_state_override: bool = False
    select: dict[str, Any] | None = None
    order_by: list[dict[str, Any]] = field(default_factory=list)
    limit: int | None = None
    max_paths: int | None = None
    max_paths_per_result: int | None = None
    description: str | None = None
    example_ids: list[str] = field(default_factory=list)
    traversal_summary: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class QueryView:
    query_count: int
    queries: list[QuerySummaryView] = field(default_factory=list)


@dataclass(frozen=True)
class PropertySchemaView:
    name: str
    type: str
    primary_key: bool
    optional: bool
    default: Any | None
    enum_ref: str | None
    enum_values: list[Any] | None
    description: str | None


@dataclass(frozen=True)
class SchemaCatalogTypeView:
    name: str
    kind: str
    description: str | None
    properties: list[PropertySchemaView] = field(default_factory=list)
    from_entity: str | None = None
    to_entity: str | None = None
    mode: str | None = None


@dataclass(frozen=True)
class SchemaCatalogView:
    enum_count: int
    entity_types: list[SchemaCatalogTypeView] = field(default_factory=list)
    relationships: list[SchemaCatalogTypeView] = field(default_factory=list)
    contracts: list[SchemaCatalogTypeView] = field(default_factory=list)


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
