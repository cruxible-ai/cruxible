"""Builders for canonical config views."""

from __future__ import annotations

from typing import Any

from cruxible_core.canonical_views.models import (
    GovernanceRelationshipView,
    GovernanceView,
    OntologyEntityView,
    OntologyRelationshipView,
    OntologyView,
    OverviewView,
    PendingBucketView,
    PropertySchemaView,
    QuerySummaryView,
    QueryView,
    SchemaCatalogTypeView,
    SchemaCatalogView,
    WorkflowDependencyView,
    WorkflowProviderSummaryView,
    WorkflowStepSummaryView,
    WorkflowSummaryView,
    WorkflowView,
)
from cruxible_core.config.property_validation import enum_values
from cruxible_core.config.schema import CoreConfig, ProviderSchema, WorkflowStepSchema
from cruxible_core.group.types import CandidateGroup, GroupResolution


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
            mode="governed" if rel.proposal_policy is not None else "deterministic",
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
                if isinstance(step.query, str):
                    queries.append(step.query)
                    query = config.named_queries.get(step.query)
                else:
                    query = step.query
                if query is not None:
                    for traversal_step in query.traversal:
                        consumes.update(traversal_step.relationship_types)
            elif step_kind == "provider" and step.provider is not None:
                providers.append(step.provider)
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
                mode=_workflow_mode(workflow.type, proposes, applies),
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
                mode=info.get("mode", query.mode),
                entry_point=query.entry_point,
                required_params=list(info.get("required_params", [])),
                returns=info.get("returns", query.returns),
                result_shape=info.get("result_shape", query.result_shape),
                dedupe=info.get("dedupe", query.dedupe),
                relationship_state=info.get("relationship_state", query.relationship_state),
                allow_relationship_state_override=info.get(
                    "allow_relationship_state_override",
                    query.allow_relationship_state_override,
                ),
                select=info.get("select", query.select),
                order_by=list(
                    info.get(
                        "order_by",
                        [
                            order.model_dump(mode="json", exclude_none=True)
                            for order in query.order_by
                        ],
                    )
                ),
                limit=info.get("limit", query.limit),
                max_paths=info.get("max_paths", query.max_paths),
                max_paths_per_result=info.get(
                    "max_paths_per_result",
                    query.max_paths_per_result,
                ),
                description=info.get("description", query.description),
                example_ids=list(info.get("example_ids", [])),
                traversal_summary=traversal_summary,
            )
        )
    return QueryView(query_count=len(queries), queries=queries)


def build_schema_catalog_view(config: CoreConfig) -> SchemaCatalogView:
    """Build a property-schema catalog for README/wiki reference rendering."""
    entity_types = [
        SchemaCatalogTypeView(
            name=name,
            kind="entity",
            description=schema.description,
            properties=[
                _property_schema_view(config, prop_name, prop)
                for prop_name, prop in schema.properties.items()
            ],
        )
        for name, schema in sorted(config.entity_types.items())
    ]
    relationships = [
        SchemaCatalogTypeView(
            name=relationship.name,
            kind="relationship",
            description=relationship.description,
            from_entity=relationship.from_entity,
            to_entity=relationship.to_entity,
            mode="governed" if relationship.proposal_policy is not None else "deterministic",
            properties=[
                _property_schema_view(config, prop_name, prop)
                for prop_name, prop in relationship.properties.items()
            ],
        )
        for relationship in sorted(config.relationships, key=lambda item: item.name)
    ]
    contracts = [
        SchemaCatalogTypeView(
            name=name,
            kind="contract",
            description=contract.description,
            properties=[
                _property_schema_view(config, field_name, field_schema)
                for field_name, field_schema in contract.fields.items()
            ],
        )
        for name, contract in sorted(config.contracts.items())
    ]
    return SchemaCatalogView(
        enum_count=len(config.enums),
        entity_types=entity_types,
        relationships=relationships,
        contracts=contracts,
    )


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
        rel.name: rel.proposal_policy
        for rel in config.relationships
        if rel.proposal_policy is not None
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


def _workflow_step_kind(step: WorkflowStepSchema) -> str:
    if step.query is not None:
        return "query"
    if step.provider is not None:
        return "provider"
    if step.assert_spec is not None:
        return "assert"
    if step.shape_items is not None:
        return "shape_items"
    if step.join_items is not None:
        return "join_items"
    if step.filter_items is not None:
        return "filter_items"
    if step.dedupe_items is not None:
        return "dedupe_items"
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
    if step.apply_all is not None:
        return "apply_all"
    return "unknown"


def _workflow_step_summary(
    step: WorkflowStepSchema,
    step_kind: str,
) -> WorkflowStepSummaryView:
    detail = ""
    if step_kind == "query" and step.query is not None:
        detail = step.query if isinstance(step.query, str) else step.query.returns
    elif step_kind == "provider" and step.provider is not None:
        detail = step.provider
    elif step_kind == "shape_items" and step.shape_items is not None:
        detail = f"{len(step.shape_items.fields)} fields"
    elif step_kind == "join_items" and step.join_items is not None:
        detail = step.join_items.join_type
    elif step_kind == "filter_items" and step.filter_items is not None:
        detail = f"{len(step.filter_items.where)} filters"
    elif step_kind == "dedupe_items" and step.dedupe_items is not None:
        detail = step.dedupe_items.strategy
    elif step_kind == "make_candidates" and step.make_candidates is not None:
        detail = step.make_candidates.relationship_type
    elif step_kind == "map_signals" and step.map_signals is not None:
        detail = step.map_signals.signal_source
    elif step_kind == "propose_relationship_group" and step.propose_relationship_group is not None:
        detail = step.propose_relationship_group.relationship_type
    elif step_kind == "make_entities" and step.make_entities is not None:
        detail = step.make_entities.entity_type
    elif step_kind == "make_relationships" and step.make_relationships is not None:
        detail = step.make_relationships.relationship_type
    elif step_kind == "apply_entities" and step.apply_entities is not None:
        detail = step.apply_entities.entities_from
    elif step_kind == "apply_relationships" and step.apply_relationships is not None:
        detail = step.apply_relationships.relationships_from
    elif step_kind == "apply_all" and step.apply_all is not None:
        detail = (
            f"{len(step.apply_all.entities_from)} entity set(s), "
            f"{len(step.apply_all.relationships_from)} relationship set(s)"
        )
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
    workflow_type: str,
    proposes: set[str],
    applies: set[str],
) -> str:
    if workflow_type in {"canonical", "proposal", "decision_support"}:
        return workflow_type
    if proposes or applies:
        return "governed"
    return "utility"


def _format_traversal_summary(
    relationships: list[str],
    direction: str,
    max_depth: int,
) -> str:
    rels = "|".join(relationships)
    if max_depth > 1:
        return f"{rels} ({direction}, depth={max_depth})"
    return f"{rels} ({direction})"


def _property_schema_view(config: CoreConfig, name: str, prop: Any) -> PropertySchemaView:
    return PropertySchemaView(
        name=name,
        type=prop.type,
        primary_key=prop.primary_key,
        optional=prop.optional,
        default=prop.default,
        enum_ref=prop.enum_ref,
        enum_values=enum_values(config, prop),
        description=prop.description,
    )
