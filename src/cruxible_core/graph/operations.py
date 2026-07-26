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
from cruxible_core.errors import (
    DataValidationError,
    DirectWriteRefusedError,
    PendingEdgeWriteRefusedError,
    TerminalLifecycleWriteRefusedError,
)
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.assertion_state import (
    TERMINAL_ENTITY_LIFECYCLE_STATUSES,
    TERMINAL_RELATIONSHIP_LIFECYCLE_STATUSES,
    WRITABLE_ENTITY_LIFECYCLE_STATUSES,
    WRITABLE_RELATIONSHIP_LIFECYCLE_STATUSES,
    RelationshipAssertion,
    RelationshipLifecycleState,
    RelationshipReviewState,
    relationship_assertion_from_metadata,
)
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.provenance import (
    backfill_provenance_on_touch,
    make_provenance,
)
from cruxible_core.graph.types import (
    EntityInstance,
    EntityMetadata,
    RelationshipInstance,
    RelationshipMetadata,
    mint_claim_id,
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
    metadata: EntityMetadata | dict[str, Any] | None = None,
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
        metadata=EntityMetadata.from_metadata(metadata or {}),
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


def _refuse_terminal_lifecycle_write(
    status: str | None,
    *,
    kind: str,
    terminal: frozenset[str],
    writable: str,
    trusted_lifecycle_transition: bool,
) -> None:
    """Refuse a terminal lifecycle status arriving through a free graph write.

    Retracting, superseding, or retiring is a governed judgement about a claim's
    standing, not a property edit. Reachable from a plain add/update it is a
    one-call, unreceipted way to make live state vanish from every live-gated
    read with no reviewer, no required reason, and nothing recording who decided
    it. Refuse and teach callers to use ``relationship supersede|retract`` or
    ``entity supersede|retire`` instead. ``writable`` statuses stay writable —
    those are reversible participation flips, not settled adjudications.

    WHY HERE, and not only at the contract mappers: the contract mappers
    (``service/lifecycle_inputs.py``) only cover payloads that arrive as
    ``EntityInput.lifecycle`` / ``RelationshipInput.lifecycle`` over HTTP or MCP.
    The exported service functions (``service_batch_direct_write``,
    ``service_add_entity_inputs``, ``service_add_relationship_inputs``) take typed
    core models directly, and the LOCAL CLI calls the service layer directly — so
    mapper-only enforcement left the free-write channel open on the default
    surface. ``apply_entity`` / ``apply_relationship`` are the single seam EVERY
    free write shares, so the refusal belongs here. The mapper refusals stay as
    earlier, friendlier errors.

    ``trusted_lifecycle_transition`` is the escape hatch for machinery that has
    EARNED the transition: it is an internal keyword argument, deliberately not
    reachable from any contract payload (no ``EntityInput`` /
    ``RelationshipInput`` / batch-input field maps to it), so no caller-supplied
    JSON can set it. Only the dedicated receipted lifecycle verbs pass it.
    """
    if trusted_lifecycle_transition or status is None:
        return
    if status in terminal:
        raise TerminalLifecycleWriteRefusedError(kind, status, writable)


def apply_entity(
    graph: EntityGraph,
    validated: ValidatedEntity,
    *,
    config: CoreConfig,
    source: str,
    trusted_lifecycle_transition: bool = False,
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

    The same chokepoint refuses a TERMINAL entity lifecycle status
    (``retired`` / ``superseded``) carried on the typed
    ``EntityMetadata.lifecycle`` envelope, unless the caller passes
    ``trusted_lifecycle_transition=True``. Entity lifecycle can only arrive
    through that typed field (a hand-authored ``metadata['lifecycle']`` lands
    inert in ``extra``), so checking it here covers every write path.
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
        raise DirectWriteRefusedError("entity", entity_type, source, policy=policy)
    if not is_governed_source(source) and policy == "proposal_only":
        raise DirectWriteRefusedError("entity", entity_type, source, policy=policy)

    existing_entity = graph.get_entity(
        validated.entity.entity_type,
        validated.entity.entity_id,
    )
    if (
        validated.is_update
        and existing_entity is not None
        and existing_entity.metadata.lifecycle_status() == "retired"
        and not trusted_lifecycle_transition
    ):
        raise DataValidationError(
            f"Entity {entity_type}:{validated.entity.entity_id} is retired and cannot be "
            "re-added or updated. Preserve this identity and use the future "
            "'cruxible entity reinstate' adjudication path when it becomes available."
        )

    # Ordered AFTER the write-policy refusals on purpose: "you may not direct-write
    # this type at all" is the coarser, harder answer, and reporting it first keeps
    # a proposal_only type's refusal reason stable no matter what the payload asked
    # for. The lifecycle refusal is what a caller hits once the type IS writable.
    entity_lifecycle = validated.entity.metadata.lifecycle
    _refuse_terminal_lifecycle_write(
        entity_lifecycle.status if entity_lifecycle is not None else None,
        kind="entity",
        terminal=TERMINAL_ENTITY_LIFECYCLE_STATUSES,
        writable=WRITABLE_ENTITY_LIFECYCLE_STATUSES,
        trusted_lifecycle_transition=trusted_lifecycle_transition,
    )

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


def _refuse_write_onto_pending_edge(
    existing_rel: RelationshipInstance | None,
    *,
    pending: bool,
) -> None:
    """Refuse a non-pending write landing on an unresolved PENDING edge.

    ``graph.get_relationship`` is state-blind: it returns a pending proposal
    exactly like a live edge, so ``validate_relationship`` reports
    ``is_update=True`` and the update branch below would replace the proposal's
    properties in place while a reviewer is still adjudicating it
    (wi-pending-edge-clobber). Robert's ruling is REFUSE, not silently-clobber.

    Scope of the refusal, and why:

    * ``pending=True`` writes are NOT refused here -- pending-onto-pending stays
      governed by the existing create-only rule in ``service/mutations.py``
      ("pending relationship writes can only create new edges"), which is the
      layer that owns it.
    * Governed verbs are NOT exempt. ``workflow_apply`` reaches the update
      branch with ordinary upsert semantics, so an unattended canonical workflow
      would otherwise overwrite a human's staged proposal; that is exactly the
      clobber being closed. ``group_resolve`` never reaches this branch --
      group approval skips every tuple that already carries an edge
      (``relationship_count_between > 0``) and blesses those through
      ``_stamp_existing_edges``, the reviewer-side resolution machinery, which
      does not funnel through this chokepoint.
    * The typed ``lifecycle`` write is covered too: it also replaces properties
      via ``replace_relationship_state``, so it clobbers a proposal just as a
      plain property write does.
    """
    # Multi-edge invariant: ``existing_rel`` is the FIRST match for the tuple,
    # and the update branch below writes through the SAME first-match
    # resolution — so the edge checked here is always the edge written. A
    # sibling edge on the same tuple can never be silently clobbered in its
    # place.
    if existing_rel is None or pending:
        return
    if relationship_assertion_from_metadata(existing_rel.metadata).review.status != "pending":
        return
    raise PendingEdgeWriteRefusedError(
        existing_rel.relationship_type,
        existing_rel.from_type,
        existing_rel.from_id,
        existing_rel.to_type,
        existing_rel.to_id,
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
    trusted_lifecycle_transition: bool = False,
) -> RelationshipInstance:
    """Apply a validated relationship to the graph (add or update).

    RETURNS THE DURABLE RELATIONSHIP -- the edge as it now stands in the graph,
    carrying its ``claim_id``. Callers that record write nodes, stamp receipts,
    or persist deltas must take their values from this return, AFTER the apply,
    never from the pre-validation input: an input never carries the id of an
    edge that did not exist yet, so a create could never reliably stamp it.

    New edges get metadata provenance stamped via make_provenance(source, source_ref)
    and a default assertion, including the creating receipt_id / resolution_id /
    actor_context when supplied. Updated edges preserve existing metadata while
    stamping provenance modification fields when provenance exists; creation-time
    correlation fields are never rewritten.

    This is the SINGLE mint site for ``claim_id`` outside the named
    legacy-image backfill: the create branch mints one and hands it to
    ``graph.add_relationship``, which preserves it. The update branch never
    re-mints -- the id is immutable for the life of the claim.

    ``lifecycle`` is the typed, review-SAFE lifecycle write channel. When supplied,
    it sets ONLY ``assertion.lifecycle`` -- the review axis (``assertion.review``)
    and ``group_override`` are left exactly as computed for the add path or as
    found on the existing edge for the update path. Because ``lifecycle`` is typed
    as :class:`RelationshipLifecycleState` (which has no ``review`` /
    ``group_override`` fields), a lifecycle write is structurally incapable of
    self-approving/rejecting an edge or flipping the group override.

    The single relationship chokepoint, so the ``refuse_direct_writes`` governance
    check lives here: a write whose ``source`` is NOT a governed verb
    (``workflow_apply`` / ``group_resolve`` / the receipted lifecycle verbs) AND
    is ``not pending`` is refused when the relationship type resolves to
    ``proposal_only``. A ``pending=True`` write is PERMITTED even under
    ``proposal_only`` — it stages for review, it is not live. The typed lifecycle
    write carries the same ``source`` and so is covered by this one predicate (no
    extra hook). Resolved INSIDE the chokepoint (callers pass ``config``) to keep
    the decision in one funnel.

    The same chokepoint refuses a non-pending write whose target edge is an
    unresolved PENDING proposal (:func:`_refuse_write_onto_pending_edge`), so no
    write path can replace a proposal's content while it awaits review.

    It also refuses a TERMINAL lifecycle status (``retracted`` / ``superseded``)
    unless the caller passes ``trusted_lifecycle_transition=True``
    (see :func:`_refuse_terminal_lifecycle_write`). ``lifecycle`` is the ONLY
    channel that can set a relationship's lifecycle through this function: the
    add branch discards the incoming metadata's assertion entirely (it rebuilds
    ``RelationshipMetadata`` from provenance + the freshly computed assertion),
    and the update branch carries the EXISTING edge's assertion forward. So the
    single ``lifecycle`` check below covers every terminal write, and the
    governed constructors that never pass ``lifecycle`` — ``workflow_apply``
    (``workflow/apply.py``), ``group_resolve`` (``service/group_transitions.py``),
    ``attestation`` (``service/attestations.py``), and ``token_mint``
    (``server/auth_managed_entities.py``) — are unaffected and need no trusted
    capability today.
    """
    # Deferred import: service/__init__ -> ... -> graph.operations, so a
    # top-level import would be circular. Importing the resolver module here
    # breaks that cycle.
    from cruxible_core.service.direct_write_policy import (
        effective_relationship_write_policy,
        is_governed_source,
    )

    rel = validated.relationship
    policy = effective_relationship_write_policy(config, rel.relationship_type)
    if not is_governed_source(source) and not pending and policy == "proposal_only":
        raise DirectWriteRefusedError("relationship", rel.relationship_type, source, policy=policy)

    # Ordered AFTER the write-policy refusal on purpose — see apply_entity.
    _refuse_terminal_lifecycle_write(
        lifecycle.status if lifecycle is not None else None,
        kind="relationship",
        terminal=TERMINAL_RELATIONSHIP_LIFECYCLE_STATUSES,
        writable=WRITABLE_RELATIONSHIP_LIFECYCLE_STATUSES,
        trusted_lifecycle_transition=trusted_lifecycle_transition,
    )
    if validated.is_update:
        incoming_evidence = rel.metadata.evidence
        existing_rel = graph.get_relationship(
            rel.from_type,
            rel.from_id,
            rel.to_type,
            rel.to_id,
            rel.relationship_type,
            edge_key=rel.edge_key,
        )
        _refuse_write_onto_pending_edge(existing_rel, pending=pending)
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
            edge_key=rel.edge_key,
        )
        return _durable_relationship(graph, rel)
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
        # THE mint site. Preserve an id the caller already carries (re-applying
        # an already-identified claim); otherwise mint one now, before the edge
        # enters the graph, because add_relationship preserves-or-raises.
        if rel.claim_id is None:
            rel.claim_id = mint_claim_id()
        edge_key = graph.add_relationship(rel)
        return _durable_relationship(graph, rel, edge_key=edge_key)


def _durable_relationship(
    graph: EntityGraph,
    rel: RelationshipInstance,
    *,
    edge_key: int | None = None,
) -> RelationshipInstance:
    """Re-read the just-applied edge so callers stamp from graph truth.

    Re-reading (rather than returning the input) is what makes the return value
    DURABLE: it carries the graph-assigned ``edge_key`` and the metadata the
    graph actually holds, not the caller's pre-apply view of them.
    """
    persisted = graph.get_relationship(
        rel.from_type,
        rel.from_id,
        rel.to_type,
        rel.to_id,
        rel.relationship_type,
        # Creates address the brand-new sibling by its assigned key. Exact-edge
        # updates (including claim lifecycle adjudication) retain the incoming
        # edge key; ordinary tuple upserts keep the historical first-match path.
        edge_key=edge_key if edge_key is not None else rel.edge_key,
    )
    if persisted is None:  # pragma: no cover - the apply above just wrote it
        raise ValueError(
            f"Applied relationship is not resolvable in the graph: {rel.relationship_label()}"
        )
    return persisted
