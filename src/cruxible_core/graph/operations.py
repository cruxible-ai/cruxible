"""Two-phase shared helpers for entity and relationship validation/application.

Phase 1 (validate): Pure functions that check inputs against config/graph,
returning a validated result or raising DataValidationError. No graph mutation.

Phase 2 (apply): Functions that mutate the graph using a validated result.

MCP handlers use validate in batch loops (collect errors, then apply all if
no errors — preserving batch atomicity). CLI validates and applies one at a time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cruxible_core.config.property_validation import validate_property_payload
from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import DataValidationError, DirectWriteRefusedError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.assertion_state import (
    RelationshipAssertion,
    RelationshipLifecycleState,
    RelationshipReviewState,
)
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.provenance import (
    backfill_provenance_on_touch,
    make_provenance,
)
from cruxible_core.graph.types import (
    EntityInstance,
    RelationshipInstance,
    RelationshipMetadata,
)
from cruxible_core.temporal import utc_now


@dataclass
class ValidatedEntity:
    """Result of validate_entity — ready to apply."""

    entity: EntityInstance
    is_update: bool


@dataclass
class ValidatedRelationship:
    """Result of validate_relationship — ready to apply."""

    relationship: RelationshipInstance
    is_update: bool


def validate_entity(
    config: CoreConfig,
    graph: EntityGraph,
    entity_type: str,
    entity_id: str,
    properties: dict[str, Any] | None = None,
    *,
    metadata: dict[str, Any] | None = None,
) -> ValidatedEntity:
    """Validate an entity against config and graph state.

    Raises DataValidationError on failure.
    """
    if entity_type not in config.entity_types:
        raise DataValidationError(f"type '{entity_type}' not found in config")
    if not entity_id.strip():
        raise DataValidationError("entity_id must not be empty")

    is_update = graph.has_entity(entity_type, entity_id)
    entity_schema = config.entity_types[entity_type]
    validation = validate_property_payload(
        config,
        entity_schema.properties,
        properties or {},
        require_required=not is_update,
        primary_key_name=entity_schema.get_primary_key(),
        entity_id=entity_id,
    )
    if validation.errors:
        raise DataValidationError(
            f"Entity '{entity_type}:{entity_id}' property validation failed",
            errors=validation.errors,
        )
    entity = EntityInstance(
        entity_type=entity_type,
        entity_id=entity_id,
        properties=validation.properties,
        metadata=dict(metadata or {}),
    )
    return ValidatedEntity(entity=entity, is_update=is_update)


def validate_relationship(
    config: CoreConfig,
    graph: EntityGraph,
    from_type: str,
    from_id: str,
    relationship: str,
    to_type: str,
    to_id: str,
    properties: dict[str, Any] | None = None,
) -> ValidatedRelationship:
    """Validate a relationship against config and graph state.

    Handles property schema checks, direction checks, and endpoint existence checks.

    Raises DataValidationError on failure.
    """
    props = dict(properties) if properties else {}

    # Validate relationship type exists in config
    rel_schema = config.get_relationship(relationship)
    if rel_schema is None:
        raise DataValidationError(f"relationship '{relationship}' not found in config")

    # Validate endpoint types match config direction
    if from_type != rel_schema.from_entity:
        raise DataValidationError(
            f"from_type '{from_type}' does not match "
            f"relationship '{relationship}' "
            f"which expects '{rel_schema.from_entity}'"
        )
    if to_type != rel_schema.to_entity:
        raise DataValidationError(
            f"to_type '{to_type}' does not match "
            f"relationship '{relationship}' "
            f"which expects '{rel_schema.to_entity}'"
        )

    # Validate source entity exists
    if graph.get_entity(from_type, from_id) is None:
        raise DataValidationError(f"entity {from_type}:{from_id} not found")

    # Validate target entity exists
    if graph.get_entity(to_type, to_id) is None:
        raise DataValidationError(f"entity {to_type}:{to_id} not found")

    existing_rel = graph.get_relationship(from_type, from_id, to_type, to_id, relationship)
    is_update = existing_rel is not None
    validation_source = dict(existing_rel.properties) if existing_rel is not None else {}
    validation_source.update(props)
    validation = validate_property_payload(
        config,
        rel_schema.properties,
        validation_source,
        require_required=True,
    )
    if validation.errors:
        raise DataValidationError(
            f"Relationship '{relationship}' property validation failed",
            errors=validation.errors,
        )

    rel = RelationshipInstance(
        relationship_type=relationship,
        from_type=from_type,
        from_id=from_id,
        to_type=to_type,
        to_id=to_id,
        properties=validation.properties,
    )
    return ValidatedRelationship(relationship=rel, is_update=is_update)


def apply_entity(
    graph: EntityGraph,
    validated: ValidatedEntity,
    *,
    config: CoreConfig,
    source: str,
) -> None:
    """Apply a validated entity to the graph (add or update).

    The single entity chokepoint. Every direct entity write funnels here, so the
    ``refuse_direct_writes`` governance check lives here: a write whose ``source``
    is NOT a governed verb (``workflow_apply`` / ``group_resolve``) is refused
    when the entity type resolves to ``proposal_only``. Entities have no pending
    staging path, so a refused direct add is refused outright. The decision is
    resolved INSIDE the chokepoint (callers pass ``config`` + ``source``) so it
    stays a single funnel — a pre-resolved bool would re-scatter governance
    across call sites and let a future verb slip through.
    """
    # Deferred import: service/__init__ -> ... -> graph.operations, so a
    # top-level import would be circular. Importing the resolver module here
    # breaks that cycle.
    from cruxible_core.service.direct_write_policy import (
        TOKEN_MINT_SOURCE,
        effective_entity_write_policy,
        is_governed_source,
    )

    entity_type = validated.entity.entity_type
    policy = effective_entity_write_policy(config, entity_type)
    if policy == "mint_only" and source != TOKEN_MINT_SOURCE:
        # mint_only is exclusive to token_mint: refuse EVERY other source,
        # including the governed verbs that proposal_only would have admitted.
        raise DirectWriteRefusedError("entity", entity_type, source)
    if not is_governed_source(source) and policy == "proposal_only":
        raise DirectWriteRefusedError("entity", entity_type, source)

    if validated.is_update:
        graph.update_entity_properties(
            validated.entity.entity_type,
            validated.entity.entity_id,
            dict(validated.entity.properties),
        )
        metadata_updates = validated.entity.metadata.to_metadata_dict()
        if metadata_updates:
            graph.update_entity_metadata(
                validated.entity.entity_type,
                validated.entity.entity_id,
                metadata_updates,
            )
    else:
        graph.add_entity(validated.entity)


def _initial_assertion(
    source: str,
    source_ref: str,
    actor_context: GovernedActorContext | None,
) -> RelationshipAssertion:
    if source == "group_resolve":
        # A group-resolved edge is born approved-by-group. Stamp the resolving
        # actor identity onto the review state where it is available, mirroring
        # the blessing of pre-existing edges (see _blessed_metadata_for_existing)
        # so newly written and pre-existing group members carry the same actor
        # context. actor_context stays None on the auth-off local path.
        return RelationshipAssertion(
            review=RelationshipReviewState(
                status="approved",
                source="group",
                updated_at=utc_now(),
                updated_by=source_ref,
                actor_context=actor_context,
            )
        )
    return RelationshipAssertion()


def _pending_assertion(
    actor_context: GovernedActorContext | None,
) -> RelationshipAssertion:
    return RelationshipAssertion(
        review=RelationshipReviewState(
            status="pending",
            source="agent",
            updated_at=utc_now(),
            updated_by="relationship:add_pending",
            actor_context=actor_context,
        )
    )


def apply_relationship(
    graph: EntityGraph,
    validated: ValidatedRelationship,
    source: str,
    source_ref: str,
    *,
    config: CoreConfig,
    receipt_id: str | None = None,
    resolution_id: str | None = None,
    actor_context: GovernedActorContext | None = None,
    pending: bool = False,
    lifecycle: RelationshipLifecycleState | None = None,
) -> None:
    """Apply a validated relationship to the graph (add or update).

    New edges get metadata provenance stamped via make_provenance(source, source_ref)
    and a default assertion, including the creating receipt_id / resolution_id /
    actor_context when supplied. Updated edges preserve existing metadata while
    stamping provenance modification fields when provenance exists; creation-time
    correlation fields are never rewritten.

    ``lifecycle`` is the typed, review-SAFE lifecycle write channel. When supplied,
    it sets ONLY ``assertion.lifecycle`` -- the review axis (``assertion.review``)
    and ``group_override`` are left exactly as computed for the add path or as
    found on the existing edge for the update path. Because ``lifecycle`` is typed
    as :class:`RelationshipLifecycleState` (which has no ``review`` /
    ``group_override`` fields), a lifecycle write is structurally incapable of
    self-approving/rejecting an edge or flipping the group override.

    The single relationship chokepoint, so the ``refuse_direct_writes`` governance
    check lives here: a write whose ``source`` is NOT a governed verb
    (``workflow_apply`` / ``group_resolve``) AND is ``not pending`` is refused when
    the relationship type resolves to ``proposal_only``. A ``pending=True`` write
    is PERMITTED even under ``proposal_only`` — it stages for review, it is not
    live. The typed lifecycle write carries the same ``source`` and so is covered
    by this one predicate (no extra hook). Resolved INSIDE the chokepoint
    (callers pass ``config``) to keep the decision in one funnel.
    """
    # Deferred import: service/__init__ -> ... -> graph.operations, so a
    # top-level import would be circular. Importing the resolver module here
    # breaks that cycle.
    from cruxible_core.service.direct_write_policy import (
        effective_relationship_write_policy,
        is_governed_source,
    )

    rel = validated.relationship
    if (
        not is_governed_source(source)
        and not pending
        and effective_relationship_write_policy(config, rel.relationship_type) == "proposal_only"
    ):
        raise DirectWriteRefusedError("relationship", rel.relationship_type, source)
    if validated.is_update:
        incoming_evidence = rel.metadata.evidence
        existing_rel = graph.get_relationship(
            rel.from_type,
            rel.from_id,
            rel.to_type,
            rel.to_id,
            rel.relationship_type,
        )
        replace_props = dict(rel.properties)
        if existing_rel:
            metadata = existing_rel.metadata
            # Stamp the modification, backfilling provenance when the existing edge
            # carries none so a touch makes a previously-null edge auditable.
            metadata = metadata.model_copy(
                update={
                    "provenance": backfill_provenance_on_touch(
                        metadata.provenance,
                        source,
                        source_ref,
                        source,
                        actor_context=actor_context,
                    ),
                }
            )
            if incoming_evidence is not None:
                metadata = metadata.model_copy(update={"evidence": incoming_evidence})
            if lifecycle is not None:
                # Set ONLY the lifecycle slice of the existing assertion; the
                # review state and group_override are preserved untouched.
                metadata = metadata.model_copy(
                    update={
                        "assertion": metadata.assertion.model_copy(update={"lifecycle": lifecycle}),
                    }
                )
            rel.metadata = metadata
        graph.replace_relationship_state(
            rel.from_type,
            rel.from_id,
            rel.to_type,
            rel.to_id,
            rel.relationship_type,
            properties=replace_props,
            metadata=rel.metadata,
        )
    else:
        incoming_evidence = rel.metadata.evidence
        assertion = (
            _pending_assertion(actor_context)
            if pending
            else _initial_assertion(source, source_ref, actor_context)
        )
        if lifecycle is not None:
            # Override ONLY the lifecycle slice of the freshly-built assertion; the
            # review state computed above (pending vs initial) is preserved.
            assertion = assertion.model_copy(update={"lifecycle": lifecycle})
        rel.metadata = RelationshipMetadata(
            provenance=make_provenance(
                source,
                source_ref,
                receipt_id=receipt_id,
                resolution_id=resolution_id,
                actor_context=actor_context,
            ),
            assertion=assertion,
            evidence=incoming_evidence,
        )
        graph.add_relationship(rel)
