"""Cross-reference validation for CoreConfig.

The Pydantic schema validates structure. This module validates semantics:
relationships reference valid entity types, named queries reference valid
relationships, ingestion mappings reference valid entity/relationship types, and
workflow/provider declarations resolve correctly.
"""

from __future__ import annotations

import re
from typing import Any

from cruxible_core.config.constraint_rules import parse_constraint_rule
from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import ConfigError
from cruxible_core.graph.types import SYSTEM_OWNED_PROPERTIES


def validate_config(config: CoreConfig) -> list[str]:
    """Run all cross-reference validations on a CoreConfig.

    Returns a list of warning strings. Raises ConfigError for hard errors.
    """
    errors: list[str] = []
    warnings: list[str] = []

    _validate_relationships(config, errors)
    _validate_named_queries(config, errors)
    _validate_constraints(config, errors, warnings)
    _validate_feedback_profiles(config, errors)
    _validate_outcome_profiles(config, errors)
    _validate_ingestion(config, errors)
    _validate_primary_keys(config, errors)
    _validate_matching_integrations(config, errors)
    _validate_kind(config, errors)
    _validate_provider_artifacts(config, errors)
    _validate_quality_checks(config, errors)
    _validate_decision_policies(config, errors)
    _validate_workflows(config, errors)
    _validate_tests(config, errors)

    if errors:
        raise ConfigError(
            f"Config has {len(errors)} cross-reference error(s)",
            errors=errors,
        )

    return warnings


def _validate_relationships(config: CoreConfig, errors: list[str]) -> None:
    """Check that relationship from/to reference valid entity types."""
    entity_names = set(config.entity_types.keys())
    canonical_names = [rel.name for rel in config.relationships]
    canonical_name_set = set(canonical_names)
    seen_names: set[str] = set()
    seen_reverse_names: set[str] = set()

    for rel in config.relationships:
        if rel.from_entity not in entity_names:
            errors.append(
                f"Relationship '{rel.name}': 'from' entity type "
                f"'{rel.from_entity}' not defined in entity_types"
            )
        if rel.to_entity not in entity_names:
            errors.append(
                f"Relationship '{rel.name}': 'to' entity type "
                f"'{rel.to_entity}' not defined in entity_types"
            )
        if rel.name in seen_names:
            errors.append(f"Duplicate relationship name: '{rel.name}'")
        seen_names.add(rel.name)
        if rel.reverse_name is None:
            continue
        if rel.reverse_name in seen_reverse_names:
            errors.append(f"Duplicate relationship reverse_name: '{rel.reverse_name}'")
        if rel.reverse_name in canonical_name_set:
            errors.append(
                f"Relationship '{rel.name}': reverse_name '{rel.reverse_name}' "
                "collides with a canonical relationship name"
            )
        seen_reverse_names.add(rel.reverse_name)


def _validate_named_queries(config: CoreConfig, errors: list[str]) -> None:
    """Check that named queries reference valid entity types and relationships."""
    entity_names = set(config.entity_types.keys())

    for query_name, query in config.named_queries.items():
        if query.entry_point not in entity_names:
            errors.append(
                f"Named query '{query_name}': entry_point "
                f"'{query.entry_point}' not defined in entity_types"
            )

        for i, step in enumerate(query.traversal):
            for rel_name in step.relationship_types:
                resolved = config.resolve_relationship_reference(rel_name)
                if resolved is None:
                    errors.append(
                        f"Named query '{query_name}' step {i}: relationship "
                        f"'{rel_name}' not defined in relationships"
                    )
                    continue

                rel, is_reverse = resolved
                effective_direction = (
                    _flip_direction(step.direction) if is_reverse else step.direction
                )
                if step.filter:
                    allowed_edge_props = set(rel.properties) | SYSTEM_OWNED_PROPERTIES
                    for prop_name in step.filter:
                        if prop_name not in allowed_edge_props:
                            errors.append(
                                f"Named query '{query_name}' step {i}: filter references "
                                f"unknown property '{prop_name}' on relationship '{rel.name}'"
                            )
                if step.target_filter:
                    target_types = _target_entity_types_for_step(rel, effective_direction)
                    for target_type in target_types:
                        entity = config.get_entity_type(target_type)
                        if entity is None:
                            continue
                        for prop_name in step.target_filter:
                            if prop_name not in entity.properties:
                                errors.append(
                                    f"Named query '{query_name}' step {i}: target_filter "
                                    f"references unknown property '{prop_name}' on "
                                    f"entity type '{target_type}'"
                                )


def _flip_direction(direction: str) -> str:
    if direction == "incoming":
        return "outgoing"
    if direction == "outgoing":
        return "incoming"
    return direction


def _target_entity_types_for_step(rel: Any, direction: str) -> set[str]:
    if direction == "outgoing":
        return {rel.to_entity}
    if direction == "incoming":
        return {rel.from_entity}
    return {rel.from_entity, rel.to_entity}


def _validate_constraints(
    config: CoreConfig,
    errors: list[str],
    warnings: list[str],
) -> None:
    """Check parseable constraints against relationship and property references."""
    relationships = {rel.name: rel for rel in config.relationships}

    for constraint in config.constraints:
        parsed = parse_constraint_rule(constraint.rule)
        if not parsed:
            warnings.append(
                f"Constraint '{constraint.name}': could not verify rule references against schema"
            )
            continue

        rel_name = parsed.relationship
        from_prop = parsed.from_property
        to_prop = parsed.to_property
        rel = relationships.get(rel_name)
        if rel is None:
            errors.append(
                f"Constraint '{constraint.name}': relationship '{rel_name}' not defined"
            )
            continue

        from_entity = config.get_entity_type(rel.from_entity)
        to_entity = config.get_entity_type(rel.to_entity)
        if from_entity is None or to_entity is None:
            continue

        if from_prop not in from_entity.properties:
            errors.append(
                f"Constraint '{constraint.name}': property '{from_prop}' not found on "
                f"FROM entity type '{rel.from_entity}' for relationship '{rel_name}'"
            )
        if to_prop not in to_entity.properties:
            errors.append(
                f"Constraint '{constraint.name}': property '{to_prop}' not found on "
                f"TO entity type '{rel.to_entity}' for relationship '{rel_name}'"
            )


def _validate_feedback_profiles(config: CoreConfig, errors: list[str]) -> None:
    """Validate relationship-scoped feedback profile references."""
    for relationship_type, profile in config.feedback_profiles.items():
        rel = config.get_relationship(relationship_type)
        if rel is None:
            errors.append(
                f"Feedback profile '{relationship_type}': relationship not defined in relationships"
            )
            continue

        from_entity = config.get_entity_type(rel.from_entity)
        to_entity = config.get_entity_type(rel.to_entity)
        if from_entity is None or to_entity is None:
            continue

        for scope_key, path in profile.scope_keys.items():
            scope, _, prop_name = path.partition(".")
            if scope == "FROM":
                if prop_name not in from_entity.properties:
                    errors.append(
                        f"Feedback profile '{relationship_type}': scope key '{scope_key}' "
                        f"references unknown FROM property '{prop_name}'"
                    )
            elif scope == "TO":
                if prop_name not in to_entity.properties:
                    errors.append(
                        f"Feedback profile '{relationship_type}': scope key '{scope_key}' "
                        f"references unknown TO property '{prop_name}'"
                    )
            elif prop_name not in rel.properties:
                errors.append(
                    f"Feedback profile '{relationship_type}': scope key '{scope_key}' "
                    f"references unknown EDGE property '{prop_name}'"
                )


def _validate_outcome_profiles(config: CoreConfig, errors: list[str]) -> None:
    """Validate anchor-scoped outcome profile references."""
    resolution_wildcards: set[str] = set()
    resolution_exact: set[tuple[str, str]] = set()
    receipt_profiles: set[tuple[str, str]] = set()

    resolution_field_sets = {
        "RESOLUTION": {
            "resolution_id",
            "relationship_type",
            "action",
            "trust_status",
            "resolved_by",
        },
        "GROUP": {"group_signature"},
        "WORKFLOW": {"name", "receipt_id", "trace_ids"},
    }
    receipt_field_sets = {
        "RECEIPT": {"receipt_id", "operation_type"},
        "SURFACE": {"type", "name"},
        "TRACESET": {"trace_ids", "provider_names", "trace_count"},
    }

    for profile_key, profile in config.outcome_profiles.items():
        if profile.anchor_type == "resolution":
            if profile.relationship_type is None:
                errors.append(
                    f"Outcome profile '{profile_key}': resolution profiles require "
                    "relationship_type"
                )
                continue
            rel = config.get_relationship(profile.relationship_type)
            if rel is None:
                errors.append(
                    f"Outcome profile '{profile_key}': relationship_type "
                    f"'{profile.relationship_type}' not defined in relationships"
                )
            if profile.workflow_name is None:
                if profile.relationship_type in resolution_wildcards:
                    errors.append(
                        f"Outcome profile '{profile_key}': duplicate wildcard resolution profile "
                        f"for relationship_type '{profile.relationship_type}'"
                    )
                resolution_wildcards.add(profile.relationship_type)
            else:
                workflow = config.workflows.get(profile.workflow_name)
                if workflow is None:
                    errors.append(
                        f"Outcome profile '{profile_key}': workflow_name "
                        f"'{profile.workflow_name}' not found in workflows"
                    )
                elif workflow.canonical:
                    errors.append(
                        f"Outcome profile '{profile_key}': workflow_name "
                        f"'{profile.workflow_name}' must be non-canonical"
                    )
                elif not _workflow_returns_relationship_proposal(workflow):
                    errors.append(
                        f"Outcome profile '{profile_key}': workflow_name "
                        f"'{profile.workflow_name}' must return a proposal-bearing alias "
                        "produced by propose_relationship_group"
                    )

                combo = (profile.relationship_type, profile.workflow_name)
                if combo in resolution_exact:
                    errors.append(
                        f"Outcome profile '{profile_key}': duplicate resolution profile for "
                        f"relationship_type '{profile.relationship_type}' and workflow_name "
                        f"'{profile.workflow_name}'"
                    )
                resolution_exact.add(combo)

            for scope_key, path in profile.scope_keys.items():
                prefix, _, field_name = path.partition(".")
                if prefix == "THESIS":
                    continue
                allowed = resolution_field_sets.get(prefix)
                if allowed is None or field_name not in allowed:
                    allowed_str = ", ".join(sorted(allowed or set()))
                    errors.append(
                        f"Outcome profile '{profile_key}': scope key '{scope_key}' references "
                        f"unsupported path '{path}'. Allowed {prefix} fields: {allowed_str}"
                    )
        else:
            if profile.surface_type is None or profile.surface_name is None:
                errors.append(
                    f"Outcome profile '{profile_key}': receipt profiles require "
                    "surface_type and surface_name"
                )
                continue
            if profile.surface_type == "query":
                if profile.surface_name not in config.named_queries:
                    errors.append(
                        f"Outcome profile '{profile_key}': surface_name "
                        f"'{profile.surface_name}' not found in named_queries"
                    )
            elif profile.surface_type == "workflow":
                if profile.surface_name not in config.workflows:
                    errors.append(
                        f"Outcome profile '{profile_key}': surface_name "
                        f"'{profile.surface_name}' not found in workflows"
                    )

            combo = (profile.surface_type, profile.surface_name)
            if combo in receipt_profiles:
                errors.append(
                    f"Outcome profile '{profile_key}': duplicate receipt profile for "
                    f"surface_type '{profile.surface_type}' and surface_name "
                    f"'{profile.surface_name}'"
                )
            receipt_profiles.add(combo)

            for scope_key, path in profile.scope_keys.items():
                prefix, _, field_name = path.partition(".")
                allowed = receipt_field_sets.get(prefix)
                if allowed is None or field_name not in allowed:
                    allowed_str = ", ".join(sorted(allowed or set()))
                    errors.append(
                        f"Outcome profile '{profile_key}': scope key '{scope_key}' references "
                        f"unsupported path '{path}'. Allowed {prefix} fields: {allowed_str}"
                    )


def _validate_decision_policies(config: CoreConfig, errors: list[str]) -> None:
    """Validate decision policy references and match selectors."""
    query_names = set(config.named_queries.keys())
    seen_names: set[str] = set()

    for policy in config.decision_policies:
        if policy.name in seen_names:
            errors.append(f"Duplicate decision policy name: '{policy.name}'")
            continue
        seen_names.add(policy.name)

        rel = config.get_relationship(policy.relationship_type)
        if rel is None:
            errors.append(
                f"Decision policy '{policy.name}': relationship_type "
                f"'{policy.relationship_type}' not defined in relationships"
            )
            continue

        if policy.applies_to == "query":
            if policy.query_name is None:
                errors.append(
                    f"Decision policy '{policy.name}': query policies require query_name"
                )
                continue
            if policy.query_name not in query_names:
                errors.append(
                    f"Decision policy '{policy.name}': query_name "
                    f"'{policy.query_name}' not found in named_queries"
                )
        else:
            if policy.workflow_name is None:
                errors.append(
                    f"Decision policy '{policy.name}': workflow policies require workflow_name"
                )
                continue
            workflow = config.workflows.get(policy.workflow_name)
            if workflow is None:
                errors.append(
                    f"Decision policy '{policy.name}': workflow_name "
                    f"'{policy.workflow_name}' not found in workflows"
                )
            elif workflow.canonical:
                errors.append(
                    f"Decision policy '{policy.name}': workflow_name "
                    f"'{policy.workflow_name}' must be a non-canonical proposal workflow"
                )
            elif not _workflow_returns_relationship_proposal(workflow):
                errors.append(
                    f"Decision policy '{policy.name}': workflow_name "
                    f"'{policy.workflow_name}' must return a proposal-bearing alias "
                    "produced by propose_relationship_group"
                )

        from_entity = config.get_entity_type(rel.from_entity)
        to_entity = config.get_entity_type(rel.to_entity)
        if from_entity is None or to_entity is None:
            continue

        for prop_name in policy.match.from_match:
            if prop_name not in from_entity.properties:
                errors.append(
                    f"Decision policy '{policy.name}': match.from references unknown "
                    f"property '{prop_name}' on '{rel.from_entity}'"
                )
        for prop_name in policy.match.to:
            if prop_name not in to_entity.properties:
                errors.append(
                    f"Decision policy '{policy.name}': match.to references unknown "
                    f"property '{prop_name}' on '{rel.to_entity}'"
                )
        for prop_name in policy.match.edge:
            if prop_name not in rel.properties:
                errors.append(
                    f"Decision policy '{policy.name}': match.edge references unknown "
                    f"property '{prop_name}' on relationship '{rel.name}'"
                )


def _validate_ingestion(config: CoreConfig, errors: list[str]) -> None:
    """Check that ingestion mappings reference valid entity/relationship types."""
    entity_names = set(config.entity_types.keys())
    relationships = {rel.name: rel for rel in config.relationships}

    for mapping_name, mapping in config.ingestion.items():
        if mapping.is_entity:
            if mapping.entity_type not in entity_names:
                errors.append(
                    f"Ingestion mapping '{mapping_name}': entity_type "
                    f"'{mapping.entity_type}' not defined in entity_types"
                )
                continue
            assert mapping.entity_type is not None
            entity = config.entity_types[mapping.entity_type]
            allowed_targets = set(entity.properties) | {mapping.id_column or ""}
            allowed_targets.update(SYSTEM_OWNED_PROPERTIES)
            for source, target in mapping.column_map.items():
                if target not in allowed_targets:
                    errors.append(
                        f"Ingestion mapping '{mapping_name}': column_map target "
                        f"'{target}' from source '{source}' is not a property on "
                        f"entity type '{mapping.entity_type}'"
                    )
        elif mapping.is_relationship:
            rel = relationships.get(mapping.relationship_type or "")
            if rel is None:
                errors.append(
                    f"Ingestion mapping '{mapping_name}': relationship_type "
                    f"'{mapping.relationship_type}' not defined in relationships"
                )
                continue
            mapped_from_column = mapping.column_map.get(
                mapping.from_column or "", mapping.from_column or ""
            )
            mapped_to_column = mapping.column_map.get(
                mapping.to_column or "", mapping.to_column or ""
            )
            allowed_targets = set(rel.properties) | {
                mapping.from_column or "",
                mapping.to_column or "",
                mapped_from_column,
                mapped_to_column,
            }
            allowed_targets.update(SYSTEM_OWNED_PROPERTIES)
            for source, target in mapping.column_map.items():
                if target not in allowed_targets:
                    errors.append(
                        f"Ingestion mapping '{mapping_name}': column_map target "
                        f"'{target}' from source '{source}' is not a property on "
                        f"relationship '{mapping.relationship_type}'"
                    )


def _validate_matching_integrations(config: CoreConfig, errors: list[str]) -> None:
    """Strict mixed mode: non-empty global registry requires all matching keys to resolve."""
    registry = config.integrations
    if not registry:
        return  # Empty registry = open mode, bare labels allowed

    for rel in config.relationships:
        if rel.matching is None:
            continue
        for key in rel.matching.integrations:
            if key not in registry:
                errors.append(
                    f"Integration '{key}' in matching.integrations for "
                    f"relationship '{rel.name}' not found in global "
                    f"integrations registry"
                )


def _validate_primary_keys(config: CoreConfig, errors: list[str]) -> None:
    """Error if entity types are missing primary keys."""
    for name, entity in config.entity_types.items():
        if entity.get_primary_key() is None:
            errors.append(
                f"Entity type '{name}': no property has primary_key: true — "
                f"set primary_key: true on the ID property (e.g. "
                f"properties: {{id: {{type: string, primary_key: true}}}})"
            )


def _validate_provider_artifacts(config: CoreConfig, errors: list[str]) -> None:
    """Validate contracts, artifacts, and providers."""
    contract_names = set(config.contracts.keys())
    artifact_names = set(config.artifacts.keys())

    for artifact_name, artifact in config.artifacts.items():
        if artifact.sha256 is None or not artifact.sha256.strip():
            errors.append(f"Artifact '{artifact_name}' is missing required sha256")

    for provider_name, provider in config.providers.items():
        if provider.contract_in not in contract_names:
            errors.append(
                "Provider "
                f"'{provider_name}': contract_in '{provider.contract_in}' "
                "not found in contracts"
            )
        if provider.contract_out not in contract_names:
            errors.append(
                "Provider "
                f"'{provider_name}': contract_out '{provider.contract_out}' "
                "not found in contracts"
            )
        if provider.artifact is not None and provider.artifact not in artifact_names:
            errors.append(
                f"Provider '{provider_name}': artifact '{provider.artifact}' not found in artifacts"
            )


def _validate_quality_checks(config: CoreConfig, errors: list[str]) -> None:
    """Validate config-defined graph quality checks."""
    entity_names = set(config.entity_types.keys())
    relationship_names = {rel.name for rel in config.relationships}
    seen_names: set[str] = set()

    for check in config.quality_checks:
        if check.name in seen_names:
            errors.append(f"Duplicate quality check name: '{check.name}'")
            continue
        seen_names.add(check.name)

        kind = getattr(check, "kind", "")

        if kind == "property":
            if check.target == "entity":
                if check.entity_type not in entity_names:
                    errors.append(
                        f"Quality check '{check.name}': entity_type "
                        f"'{check.entity_type}' not defined in entity_types"
                    )
                    continue
                if check.property not in config.entity_types[check.entity_type].properties:
                    errors.append(
                        f"Quality check '{check.name}': property '{check.property}' "
                        f"not found on entity type '{check.entity_type}'"
                    )
            else:
                if check.relationship_type not in relationship_names:
                    errors.append(
                        f"Quality check '{check.name}': relationship_type "
                        f"'{check.relationship_type}' not defined in relationships"
                    )
                    continue
                rel = config.get_relationship(check.relationship_type)
                assert rel is not None
                if check.property not in rel.properties:
                    errors.append(
                        f"Quality check '{check.name}': property '{check.property}' "
                        f"not found on relationship '{check.relationship_type}'"
                    )
            if check.rule == "pattern":
                try:
                    assert check.pattern is not None
                    re.compile(check.pattern)
                except re.error as exc:
                    errors.append(
                        f"Quality check '{check.name}': invalid regex pattern "
                        f"'{check.pattern}': {exc}"
                    )

        elif kind == "json_content":
            if check.target == "entity":
                if check.entity_type not in entity_names:
                    errors.append(
                        f"Quality check '{check.name}': entity_type "
                        f"'{check.entity_type}' not defined in entity_types"
                    )
                    continue
                prop = config.entity_types[check.entity_type].properties.get(check.property)
            else:
                if check.relationship_type not in relationship_names:
                    errors.append(
                        f"Quality check '{check.name}': relationship_type "
                        f"'{check.relationship_type}' not defined in relationships"
                    )
                    continue
                rel = config.get_relationship(check.relationship_type)
                assert rel is not None
                prop = rel.properties.get(check.property)
            if prop is None:
                errors.append(
                    f"Quality check '{check.name}': property '{check.property}' not found"
                )
                continue
            if prop.type != "json":
                errors.append(
                    f"Quality check '{check.name}': json_content requires property "
                    f"'{check.property}' to have type 'json'"
                )

        elif kind == "uniqueness":
            if check.entity_type not in entity_names:
                errors.append(
                    f"Quality check '{check.name}': entity_type "
                    f"'{check.entity_type}' not defined in entity_types"
                )
                continue
            entity_props = config.entity_types[check.entity_type].properties
            for prop_name in check.properties:
                if prop_name not in entity_props:
                    errors.append(
                        f"Quality check '{check.name}': property '{prop_name}' "
                        f"not found on entity type '{check.entity_type}'"
                    )

        elif kind == "bounds":
            if check.target == "entity_count":
                if check.entity_type not in entity_names:
                    errors.append(
                        f"Quality check '{check.name}': entity_type "
                        f"'{check.entity_type}' not defined in entity_types"
                    )
            elif check.relationship_type not in relationship_names:
                errors.append(
                    f"Quality check '{check.name}': relationship_type "
                    f"'{check.relationship_type}' not defined in relationships"
                )

        elif kind == "cardinality":
            if check.entity_type not in entity_names:
                errors.append(
                    f"Quality check '{check.name}': entity_type "
                    f"'{check.entity_type}' not defined in entity_types"
                )
                continue
            rel = config.get_relationship(check.relationship_type)
            if rel is None:
                errors.append(
                    f"Quality check '{check.name}': relationship_type "
                    f"'{check.relationship_type}' not defined in relationships"
                )
                continue
            if check.direction == "outgoing" and rel.from_entity != check.entity_type:
                errors.append(
                    f"Quality check '{check.name}': outgoing cardinality on "
                    f"'{check.relationship_type}' requires entity_type '{rel.from_entity}', "
                    f"not '{check.entity_type}'"
                )
            if check.direction == "incoming" and rel.to_entity != check.entity_type:
                errors.append(
                    f"Quality check '{check.name}': incoming cardinality on "
                    f"'{check.relationship_type}' requires entity_type '{rel.to_entity}', "
                    f"not '{check.entity_type}'"
                )


def _validate_kind(config: CoreConfig, errors: list[str]) -> None:
    """Validate top-level kind gating for built world-model features."""
    if config.kind != "ontology":
        return

    if config.ingestion:
        errors.append("Config kind 'ontology' may not define ingestion mappings")
    if config.contracts:
        errors.append("Config kind 'ontology' may not define contracts")
    if config.artifacts:
        errors.append("Config kind 'ontology' may not define artifacts")
    if config.providers:
        errors.append("Config kind 'ontology' may not define providers")
    if config.workflows:
        errors.append("Config kind 'ontology' may not define workflows")
    if config.tests:
        errors.append("Config kind 'ontology' may not define workflow tests")


def _validate_workflows(config: CoreConfig, errors: list[str]) -> None:
    """Validate workflow/provider/query references and reference syntax."""
    contract_names = set(config.contracts.keys())
    provider_names = set(config.providers.keys())
    query_names = set(config.named_queries.keys())
    entity_names = set(config.entity_types.keys())
    relationship_names = {rel.name for rel in config.relationships}
    integration_names = set(config.integrations.keys())

    for workflow_name, workflow in config.workflows.items():
        if workflow.contract_in not in contract_names:
            errors.append(
                "Workflow "
                f"'{workflow_name}': contract_in '{workflow.contract_in}' "
                "not found in contracts"
            )

        produced_aliases: set[str] = set()
        step_ids: set[str] = set()
        uses_apply_steps = False
        providers_used: set[str] = set()

        for step in workflow.steps:
            if step.id in step_ids:
                errors.append(f"Workflow '{workflow_name}': duplicate step id '{step.id}'")
                continue
            step_ids.add(step.id)

            if step.query is not None:
                if step.query not in query_names:
                    errors.append(
                        "Workflow "
                        f"'{workflow_name}' step '{step.id}': query '{step.query}' "
                        "not found in named_queries"
                    )
                for ref in _iter_refs(step.params):
                    _validate_workflow_ref(
                        workflow_name,
                        step.id,
                        ref,
                        produced_aliases,
                        errors,
                    )
                if step.as_ is not None:
                    produced_aliases.add(step.as_)
                continue

            if step.provider is not None:
                if step.provider not in provider_names:
                    errors.append(
                        "Workflow "
                        f"'{workflow_name}' step '{step.id}': provider "
                        f"'{step.provider}' not found in providers"
                    )
                else:
                    providers_used.add(step.provider)
                for ref in _iter_refs(step.input):
                    _validate_workflow_ref(
                        workflow_name,
                        step.id,
                        ref,
                        produced_aliases,
                        errors,
                    )
                if step.as_ is not None:
                    produced_aliases.add(step.as_)
                continue

            if step.list_entities is not None:
                if step.list_entities.entity_type not in entity_names:
                    errors.append(
                        "Workflow "
                        f"'{workflow_name}' step '{step.id}': list_entities entity_type "
                        f"'{step.list_entities.entity_type}' not found in entity_types"
                    )
                for ref in _iter_refs(
                    [
                        step.list_entities.property_filter,
                        step.list_entities.limit,
                    ]
                ):
                    _validate_workflow_ref(
                        workflow_name,
                        step.id,
                        ref,
                        produced_aliases,
                        errors,
                    )
                if step.as_ is not None:
                    produced_aliases.add(step.as_)
                continue

            if step.list_relationships is not None:
                if step.list_relationships.relationship_type not in relationship_names:
                    errors.append(
                        "Workflow "
                        f"'{workflow_name}' step '{step.id}': list_relationships "
                        f"relationship_type '{step.list_relationships.relationship_type}' "
                        "not found in relationships"
                    )
                for ref in _iter_refs(
                    [
                        step.list_relationships.property_filter,
                        step.list_relationships.limit,
                    ]
                ):
                    _validate_workflow_ref(
                        workflow_name,
                        step.id,
                        ref,
                        produced_aliases,
                        errors,
                    )
                if step.as_ is not None:
                    produced_aliases.add(step.as_)
                continue

            if step.make_candidates is not None:
                if step.make_candidates.relationship_type not in relationship_names:
                    errors.append(
                        "Workflow "
                        f"'{workflow_name}' step '{step.id}': make_candidates relationship_type "
                        f"'{step.make_candidates.relationship_type}' not found in relationships"
                    )
                for ref in _iter_refs(
                    [
                        step.make_candidates.items,
                        step.make_candidates.from_type,
                        step.make_candidates.from_id,
                        step.make_candidates.to_type,
                        step.make_candidates.to_id,
                        step.make_candidates.properties,
                    ]
                ):
                    _validate_workflow_ref(
                        workflow_name,
                        step.id,
                        ref,
                        produced_aliases,
                        errors,
                        allow_item=True,
                    )
                if step.as_ is not None:
                    produced_aliases.add(step.as_)
                continue

            if step.map_signals is not None:
                if step.map_signals.integration not in integration_names:
                    errors.append(
                        "Workflow "
                        f"'{workflow_name}' step '{step.id}': map_signals integration "
                        f"'{step.map_signals.integration}' not found in integrations"
                    )
                for ref in _iter_refs(
                    [
                        step.map_signals.items,
                        step.map_signals.from_id,
                        step.map_signals.to_id,
                        step.map_signals.evidence,
                    ]
                ):
                    _validate_workflow_ref(
                        workflow_name,
                        step.id,
                        ref,
                        produced_aliases,
                        errors,
                        allow_item=True,
                    )
                if step.as_ is not None:
                    produced_aliases.add(step.as_)
                continue

            if step.propose_relationship_group is not None:
                if step.propose_relationship_group.relationship_type not in relationship_names:
                    errors.append(
                        "Workflow "
                        f"'{workflow_name}' step '{step.id}': propose_relationship_group "
                        f"relationship_type '{step.propose_relationship_group.relationship_type}' "
                        "not found in relationships"
                    )
                if step.propose_relationship_group.candidates_from not in produced_aliases:
                    errors.append(
                        "Workflow "
                        f"'{workflow_name}' step '{step.id}': candidates_from alias "
                        f"'{step.propose_relationship_group.candidates_from}' "
                        "is unknown or future"
                    )
                for alias in step.propose_relationship_group.signals_from:
                    if alias not in produced_aliases:
                        errors.append(
                            "Workflow "
                            f"'{workflow_name}' step '{step.id}': signals_from alias "
                            f"'{alias}' is unknown or future"
                        )
                for ref in _iter_refs(
                    [
                        step.propose_relationship_group.thesis_text,
                        step.propose_relationship_group.thesis_facts,
                        step.propose_relationship_group.analysis_state,
                        step.propose_relationship_group.suggested_priority,
                    ]
                ):
                    _validate_workflow_ref(
                        workflow_name,
                        step.id,
                        ref,
                        produced_aliases,
                        errors,
                    )
                if not step.propose_relationship_group.thesis_facts:
                    errors.append(
                        "Workflow "
                        f"'{workflow_name}' step '{step.id}': propose_relationship_group "
                        "requires non-empty thesis_facts"
                    )
                if step.as_ is not None:
                    produced_aliases.add(step.as_)
                continue

            if step.make_entities is not None:
                if step.make_entities.entity_type not in entity_names:
                    errors.append(
                        "Workflow "
                        f"'{workflow_name}' step '{step.id}': make_entities entity_type "
                        f"'{step.make_entities.entity_type}' not found in entity_types"
                    )
                for ref in _iter_refs(
                    [
                        step.make_entities.items,
                        step.make_entities.entity_id,
                        step.make_entities.properties,
                    ]
                ):
                    _validate_workflow_ref(
                        workflow_name,
                        step.id,
                        ref,
                        produced_aliases,
                        errors,
                        allow_item=True,
                    )
                if step.as_ is not None:
                    produced_aliases.add(step.as_)
                continue

            if step.make_relationships is not None:
                if step.make_relationships.relationship_type not in relationship_names:
                    errors.append(
                        "Workflow "
                        f"'{workflow_name}' step '{step.id}': make_relationships "
                        f"relationship_type '{step.make_relationships.relationship_type}' "
                        "not found in relationships"
                    )
                for ref in _iter_refs(
                    [
                        step.make_relationships.items,
                        step.make_relationships.from_type,
                        step.make_relationships.from_id,
                        step.make_relationships.to_type,
                        step.make_relationships.to_id,
                        step.make_relationships.properties,
                    ]
                ):
                    _validate_workflow_ref(
                        workflow_name,
                        step.id,
                        ref,
                        produced_aliases,
                        errors,
                        allow_item=True,
                    )
                if step.as_ is not None:
                    produced_aliases.add(step.as_)
                continue

            if step.apply_entities is not None:
                uses_apply_steps = True
                if step.apply_entities.entities_from not in produced_aliases:
                    errors.append(
                        "Workflow "
                        f"'{workflow_name}' step '{step.id}': entities_from alias "
                        f"'{step.apply_entities.entities_from}' is unknown or future"
                    )
                if step.as_ is not None:
                    produced_aliases.add(step.as_)
                continue

            if step.apply_relationships is not None:
                uses_apply_steps = True
                if step.apply_relationships.relationships_from not in produced_aliases:
                    errors.append(
                        "Workflow "
                        f"'{workflow_name}' step '{step.id}': relationships_from alias "
                        f"'{step.apply_relationships.relationships_from}' is unknown or future"
                    )
                if step.as_ is not None:
                    produced_aliases.add(step.as_)
                continue

            assert step.assert_spec is not None
            for ref in _iter_refs([step.assert_spec.left, step.assert_spec.right]):
                _validate_workflow_ref(
                    workflow_name,
                    step.id,
                    ref,
                    produced_aliases,
                    errors,
                )

        if workflow.returns not in produced_aliases:
            errors.append(
                "Workflow "
                f"'{workflow_name}': returns alias '{workflow.returns}' "
                "not produced by any prior step"
            )

        if uses_apply_steps and not workflow.canonical:
            errors.append(
                f"Workflow '{workflow_name}': apply_* steps require canonical: true"
            )
        if workflow.canonical and not uses_apply_steps:
            errors.append(
                f"Workflow '{workflow_name}': canonical workflows require at least one apply_* step"
            )
        if workflow.purpose == "decision_support" and uses_apply_steps:
            errors.append(
                f"Workflow '{workflow_name}': decision_support workflows must not use apply_* steps"
            )
        if workflow.purpose == "proposal" and not _workflow_returns_relationship_proposal(
            workflow
        ):
            errors.append(
                f"Workflow '{workflow_name}': proposal workflows must return a "
                "proposal-bearing alias produced by propose_relationship_group"
            )

        if workflow.canonical:
            for provider_name in providers_used:
                provider = config.providers[provider_name]
                if provider.runtime != "python":
                    errors.append(
                        f"Workflow '{workflow_name}': canonical provider '{provider_name}' "
                        "must use runtime 'python'"
                    )
                if not provider.deterministic:
                    errors.append(
                        f"Workflow '{workflow_name}': canonical provider '{provider_name}' "
                        "must be deterministic"
                    )
                if provider.side_effects:
                    errors.append(
                        f"Workflow '{workflow_name}': canonical provider '{provider_name}' "
                        "must not declare side_effects"
                    )
                if provider.artifact is None:
                    errors.append(
                        f"Workflow '{workflow_name}': canonical provider '{provider_name}' "
                        "must declare an artifact bundle"
                    )


def _workflow_returns_relationship_proposal(workflow: Any) -> bool:
    """Return True when a workflow returns a built-in relationship proposal artifact."""
    for step in workflow.steps:
        if step.as_ == workflow.returns and step.propose_relationship_group is not None:
            return True
    return False


def _validate_tests(config: CoreConfig, errors: list[str]) -> None:
    """Validate workflow test declarations."""
    workflow_names = set(config.workflows.keys())

    seen: set[str] = set()
    for test in config.tests:
        if test.name in seen:
            errors.append(f"Duplicate test name: '{test.name}'")
        seen.add(test.name)
        if test.workflow not in workflow_names:
            errors.append(f"Test '{test.name}': workflow '{test.workflow}' not found in workflows")


def _iter_refs(value: Any) -> list[str]:
    """Collect workflow reference strings from nested data."""
    refs: list[str] = []

    if isinstance(value, str):
        if value.startswith("$"):
            refs.append(value)
        return refs

    if isinstance(value, dict):
        for item in value.values():
            refs.extend(_iter_refs(item))
        return refs

    if isinstance(value, list):
        for item in value:
            refs.extend(_iter_refs(item))

    return refs


def _validate_workflow_ref(
    workflow_name: str,
    step_id: str,
    ref: str,
    produced_aliases: set[str],
    errors: list[str],
    *,
    allow_item: bool = False,
) -> None:
    """Validate a single workflow input/step reference."""
    if ref == "$input":
        return
    if ref.startswith("$input."):
        return
    if ref == "$item" or ref.startswith("$item."):
        if allow_item:
            return
        errors.append(f"Workflow '{workflow_name}' step '{step_id}': unsupported reference '{ref}'")
        return
    if ref == "$steps":
        errors.append(f"Workflow '{workflow_name}' step '{step_id}': invalid reference '{ref}'")
        return
    if not ref.startswith("$steps."):
        errors.append(f"Workflow '{workflow_name}' step '{step_id}': unsupported reference '{ref}'")
        return

    alias = ref[len("$steps.") :].split(".", 1)[0]
    if not alias:
        errors.append(f"Workflow '{workflow_name}' step '{step_id}': invalid reference '{ref}'")
        return
    if alias not in produced_aliases:
        errors.append(
            "Workflow "
            f"'{workflow_name}' step '{step_id}': reference '{ref}' points "
            f"to unknown or future step alias '{alias}'"
        )
