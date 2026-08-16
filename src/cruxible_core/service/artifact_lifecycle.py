"""Receipted lifecycle adjudication for claims and entities."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NoReturn

from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import ConfigError, CoreError, EntityNotFoundError
from cruxible_core.graph.assertion_state import (
    EntityLifecycleState,
    RelationshipLifecycleState,
    SupersessionPointer,
    relationship_assertion_from_metadata,
    relationship_is_live,
)
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.evidence import EvidenceRef, normalize_evidence_ref
from cruxible_core.graph.operations import (
    ValidatedRelationship,
    apply_entity,
    apply_relationship,
    validate_entity,
)
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.runtime.permissions import check_permission
from cruxible_core.service.mutation_receipts import mutation_receipt
from cruxible_core.service.types import ClaimLifecycleResult, EntityLifecycleResult
from cruxible_core.temporal import utc_now

_CLAIM_SUPERSEDE_SOURCE = "lifecycle_supersede"
_CLAIM_RETRACT_SOURCE = "lifecycle_retract"


def service_supersede_claim(
    instance: InstanceProtocol,
    claim_id: str,
    successor_claim_id: str,
    *,
    reason: str,
    actor_context: GovernedActorContext | None,
    evidence_ref: EvidenceRef | Mapping[str, Any] | None = None,
) -> ClaimLifecycleResult:
    """Settle one eligible claim as superseded by an existing live same-type claim."""
    parameters = _parameters(
        subject={"kind": "claim", "claim_id": claim_id},
        transition={"from": "active", "to": "superseded"},
        reason=reason,
        successor_ref={"claim_id": successor_claim_id},
        evidence_ref=evidence_ref,
    )
    with mutation_receipt(
        instance,
        "lifecycle_supersede",
        parameters,
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        check_permission("cruxible_supersede_claim")
        actor = _require_actor(actor_context, builder=ctx.builder)
        normalized_reason = _require_reason(reason, action="supersede", builder=ctx.builder)
        _normalize_optional_evidence(evidence_ref, builder=ctx.builder)
        config = instance.load_config()
        graph = ctx.uow.graph.load_graph()

        if claim_id.strip() == successor_claim_id.strip():
            _refuse(ctx.builder, f"claim '{claim_id}' cannot supersede itself")
        predecessor = _get_claim(graph, claim_id, role="predecessor", builder=ctx.builder)
        successor = _get_claim(graph, successor_claim_id, role="successor", builder=ctx.builder)
        _require_claim_predecessor_eligible(predecessor, builder=ctx.builder)
        if predecessor.relationship_type != successor.relationship_type:
            _refuse(
                ctx.builder,
                "claim supersession requires the same relationship type; "
                f"predecessor is '{predecessor.relationship_type}', successor is "
                f"'{successor.relationship_type}'",
            )
        if not relationship_is_live(successor.metadata):
            successor_status = relationship_assertion_from_metadata(
                successor.metadata
            ).lifecycle.status
            _refuse(
                ctx.builder,
                f"successor claim '{successor_claim_id}' must be live; found lifecycle "
                f"status '{successor_status}'",
            )

        successor_assertion = relationship_assertion_from_metadata(successor.metadata)
        _refuse_reused_successor(
            successor_assertion.lifecycle.supersedes,
            builder=ctx.builder,
            successor_label=f"claim '{successor_claim_id}'",
            predecessor_label=f"claim '{claim_id}'",
        )

        predecessor_pointer = SupersessionPointer(claim_id=claim_id)
        successor_pointer = SupersessionPointer(claim_id=successor_claim_id)
        updated_successor_lifecycle = successor_assertion.lifecycle.model_copy(
            update={"supersedes": predecessor_pointer}
        )
        updated_successor = _apply_claim_lifecycle(
            graph,
            config=config,
            relationship=successor,
            lifecycle=updated_successor_lifecycle,
            source=_CLAIM_SUPERSEDE_SOURCE,
            actor_context=actor,
            receipt_id=ctx.builder.receipt_id,
        )

        predecessor_assertion = relationship_assertion_from_metadata(predecessor.metadata)
        updated_predecessor_lifecycle = predecessor_assertion.lifecycle.model_copy(
            update={
                "status": "superseded",
                "reason": normalized_reason,
                "closed_at": utc_now(),
                "closed_by": actor.actor_id,
                "superseded_by": successor_pointer,
            }
        )
        updated_predecessor = _apply_claim_lifecycle(
            graph,
            config=config,
            relationship=predecessor,
            lifecycle=updated_predecessor_lifecycle,
            source=_CLAIM_SUPERSEDE_SOURCE,
            actor_context=actor,
            receipt_id=ctx.builder.receipt_id,
        )
        ctx.uow.graph.upsert_relationships([updated_successor, updated_predecessor])
        _record_claim_write(
            ctx.builder,
            updated_successor,
            action="supersede",
            role="successor",
        )
        _record_claim_write(
            ctx.builder,
            updated_predecessor,
            action="supersede",
            role="predecessor",
        )
        _record_adjudication(ctx.builder, parameters)
        result = ClaimLifecycleResult(
            action="supersede",
            claim=updated_predecessor,
            successor=updated_successor,
            reason=normalized_reason,
        )
        ctx.set_result(result)
    return result


def service_retract_claim(
    instance: InstanceProtocol,
    claim_id: str,
    *,
    reason: str,
    actor_context: GovernedActorContext | None,
    evidence_ref: EvidenceRef | Mapping[str, Any] | None = None,
) -> ClaimLifecycleResult:
    """Settle one eligible relationship claim as retracted without a successor."""
    parameters = _parameters(
        subject={"kind": "claim", "claim_id": claim_id},
        transition={"from": "active", "to": "retracted"},
        reason=reason,
        successor_ref=None,
        evidence_ref=evidence_ref,
    )
    with mutation_receipt(
        instance,
        "lifecycle_retract",
        parameters,
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        check_permission("cruxible_retract_claim")
        actor = _require_actor(actor_context, builder=ctx.builder)
        normalized_reason = _require_reason(reason, action="retract", builder=ctx.builder)
        _normalize_optional_evidence(evidence_ref, builder=ctx.builder)
        config = instance.load_config()
        graph = ctx.uow.graph.load_graph()
        predecessor = _get_claim(graph, claim_id, role="subject", builder=ctx.builder)
        _require_claim_predecessor_eligible(predecessor, builder=ctx.builder)
        assertion = relationship_assertion_from_metadata(predecessor.metadata)
        lifecycle = assertion.lifecycle.model_copy(
            update={
                "status": "retracted",
                "reason": normalized_reason,
                "closed_at": utc_now(),
                "closed_by": actor.actor_id,
            }
        )
        updated = _apply_claim_lifecycle(
            graph,
            config=config,
            relationship=predecessor,
            lifecycle=lifecycle,
            source=_CLAIM_RETRACT_SOURCE,
            actor_context=actor,
            receipt_id=ctx.builder.receipt_id,
        )
        ctx.uow.graph.upsert_relationships([updated])
        _record_claim_write(ctx.builder, updated, action="retract", role="subject")
        _record_adjudication(ctx.builder, parameters)
        result = ClaimLifecycleResult(
            action="retract",
            claim=updated,
            reason=normalized_reason,
        )
        ctx.set_result(result)
    return result


def service_supersede_entity(
    instance: InstanceProtocol,
    entity_type: str,
    entity_id: str,
    successor_entity_type: str,
    successor_entity_id: str,
    *,
    reason: str,
    actor_context: GovernedActorContext | None,
    evidence_ref: EvidenceRef | Mapping[str, Any] | None = None,
) -> EntityLifecycleResult:
    """Settle one live entity as superseded; attached edges do not migrate."""
    subject_ref = {
        "kind": "entity",
        "entity_type": entity_type,
        "entity_id": entity_id,
    }
    successor_ref = {
        "entity_type": successor_entity_type,
        "entity_id": successor_entity_id,
    }
    parameters = _parameters(
        subject=subject_ref,
        transition={"from": "live", "to": "superseded"},
        reason=reason,
        successor_ref=successor_ref,
        evidence_ref=evidence_ref,
    )
    with mutation_receipt(
        instance,
        "lifecycle_supersede",
        parameters,
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        check_permission("cruxible_supersede_entity")
        actor = _require_actor(actor_context, builder=ctx.builder)
        normalized_reason = _require_reason(reason, action="supersede", builder=ctx.builder)
        _normalize_optional_evidence(evidence_ref, builder=ctx.builder)
        config = instance.load_config()
        graph = ctx.uow.graph.load_graph()

        if (entity_type, entity_id) == (successor_entity_type, successor_entity_id):
            _refuse(
                ctx.builder,
                f"entity {entity_type}:{entity_id} cannot supersede itself",
                entity_type=entity_type,
                entity_id=entity_id,
            )
        predecessor = _get_entity(
            graph,
            entity_type,
            entity_id,
            role="predecessor",
            builder=ctx.builder,
        )
        successor = _get_entity(
            graph,
            successor_entity_type,
            successor_entity_id,
            role="successor",
            builder=ctx.builder,
        )
        _require_entity_predecessor_eligible(predecessor, builder=ctx.builder)
        if entity_type != successor_entity_type:
            _refuse(
                ctx.builder,
                "entity supersession requires the same entity type; "
                f"predecessor is '{entity_type}', successor is '{successor_entity_type}'",
                entity_type=entity_type,
                entity_id=entity_id,
            )
        if not successor.metadata.is_live():
            _refuse(
                ctx.builder,
                f"successor entity {successor_entity_type}:{successor_entity_id} must be "
                f"live; found '{successor.metadata.lifecycle_status()}'",
                entity_type=successor_entity_type,
                entity_id=successor_entity_id,
            )

        successor_lifecycle = successor.metadata.lifecycle or EntityLifecycleState()
        _refuse_reused_successor(
            successor_lifecycle.supersedes,
            builder=ctx.builder,
            successor_label=f"entity {successor_entity_type}:{successor_entity_id}",
            predecessor_label=f"entity {entity_type}:{entity_id}",
            entity_type=entity_type,
            entity_id=entity_id,
        )

        predecessor_pointer = SupersessionPointer(
            entity_type=entity_type,
            entity_id=entity_id,
        )
        successor_pointer = SupersessionPointer(
            entity_type=successor_entity_type,
            entity_id=successor_entity_id,
        )
        updated_successor = _apply_entity_lifecycle(
            graph,
            config=config,
            entity=successor,
            lifecycle=successor_lifecycle.model_copy(update={"supersedes": predecessor_pointer}),
            source=_CLAIM_SUPERSEDE_SOURCE,
            actor_context=actor,
        )
        predecessor_lifecycle = predecessor.metadata.lifecycle or EntityLifecycleState()
        updated_predecessor = _apply_entity_lifecycle(
            graph,
            config=config,
            entity=predecessor,
            lifecycle=predecessor_lifecycle.model_copy(
                update={
                    "status": "superseded",
                    "reason": normalized_reason,
                    "closed_at": utc_now(),
                    "closed_by": actor.actor_id,
                    "superseded_by": successor_pointer,
                }
            ),
            source=_CLAIM_SUPERSEDE_SOURCE,
            actor_context=actor,
        )
        ctx.uow.graph.upsert_entities([updated_successor, updated_predecessor])
        _record_entity_write(ctx.builder, updated_successor, action="supersede", role="successor")
        _record_entity_write(
            ctx.builder,
            updated_predecessor,
            action="supersede",
            role="predecessor",
        )
        _record_adjudication(
            ctx.builder,
            parameters,
            entity_type=entity_type,
            entity_id=entity_id,
        )
        result = EntityLifecycleResult(
            action="supersede",
            entity=updated_predecessor,
            successor=updated_successor,
            reason=normalized_reason,
        )
        ctx.set_result(result)
    return result


def service_retire_entity(
    instance: InstanceProtocol,
    entity_type: str,
    entity_id: str,
    *,
    reason: str,
    actor_context: GovernedActorContext | None,
    evidence_ref: EvidenceRef | Mapping[str, Any] | None = None,
) -> EntityLifecycleResult:
    """Settle one live entity as retired without cascading attached claims."""
    parameters = _parameters(
        subject={"kind": "entity", "entity_type": entity_type, "entity_id": entity_id},
        transition={"from": "live", "to": "retired"},
        reason=reason,
        successor_ref=None,
        evidence_ref=evidence_ref,
    )
    with mutation_receipt(
        instance,
        "lifecycle_retract",
        parameters,
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        check_permission("cruxible_retire_entity")
        actor = _require_actor(actor_context, builder=ctx.builder)
        normalized_reason = _require_reason(reason, action="retire", builder=ctx.builder)
        _normalize_optional_evidence(evidence_ref, builder=ctx.builder)
        config = instance.load_config()
        graph = ctx.uow.graph.load_graph()
        predecessor = _get_entity(
            graph,
            entity_type,
            entity_id,
            role="subject",
            builder=ctx.builder,
        )
        _require_entity_predecessor_eligible(predecessor, builder=ctx.builder)
        stranded_live_edge_count = _live_attached_edge_count(graph, predecessor)
        parameters["stranded_live_edge_count"] = stranded_live_edge_count
        ctx.builder.update_node_detail(
            ctx.builder.root_id,
            {"parameters": dict(parameters)},
        )
        lifecycle = predecessor.metadata.lifecycle or EntityLifecycleState()
        updated = _apply_entity_lifecycle(
            graph,
            config=config,
            entity=predecessor,
            lifecycle=lifecycle.model_copy(
                update={
                    "status": "retired",
                    "reason": normalized_reason,
                    "closed_at": utc_now(),
                    "closed_by": actor.actor_id,
                }
            ),
            source=_CLAIM_RETRACT_SOURCE,
            actor_context=actor,
        )
        ctx.uow.graph.upsert_entities([updated])
        _record_adjudication(
            ctx.builder,
            parameters,
            entity_type=entity_type,
            entity_id=entity_id,
        )
        _record_entity_write(ctx.builder, updated, action="retire", role="subject")
        result = EntityLifecycleResult(
            action="retire",
            entity=updated,
            reason=normalized_reason,
            stranded_live_edge_count=stranded_live_edge_count,
        )
        ctx.set_result(result)
    return result


def _parameters(
    *,
    subject: dict[str, str],
    transition: dict[str, str],
    reason: str,
    successor_ref: dict[str, str] | None,
    evidence_ref: EvidenceRef | Mapping[str, Any] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "subject": subject,
        "transition": transition,
        "reason": reason,
        "successor_ref": successor_ref,
    }
    if evidence_ref is not None:
        if isinstance(evidence_ref, EvidenceRef):
            payload["evidence_ref"] = evidence_ref.to_payload()
        else:
            payload["evidence_ref"] = dict(evidence_ref)
    return payload


def _require_actor(
    actor_context: GovernedActorContext | None,
    *,
    builder: ReceiptBuilder,
) -> GovernedActorContext:
    if actor_context is None:
        _refuse(builder, "lifecycle adjudication actor context is required")
    return actor_context


def _require_reason(reason: str | None, *, action: str, builder: ReceiptBuilder) -> str:
    normalized = "" if reason is None else reason.strip()
    if not normalized:
        _refuse(builder, f"lifecycle {action} requires a non-empty reason")
    return normalized


def _normalize_optional_evidence(
    evidence_ref: EvidenceRef | Mapping[str, Any] | None,
    *,
    builder: ReceiptBuilder,
) -> EvidenceRef | None:
    if evidence_ref is None:
        return None
    try:
        return normalize_evidence_ref(evidence_ref)
    except ValueError as exc:
        _refuse(builder, f"invalid lifecycle evidence_ref: {exc}")


def _get_claim(
    graph: EntityGraph,
    claim_id: str,
    *,
    role: str,
    builder: ReceiptBuilder,
) -> RelationshipInstance:
    normalized = claim_id.strip()
    if not normalized:
        _refuse(builder, f"{role} claim_id must not be empty")
    relationship = graph.find_relationship_by_claim_id(normalized)
    if relationship is None:
        _refuse(builder, f"{role} claim '{normalized}' not found")
    return relationship


def _get_entity(
    graph: EntityGraph,
    entity_type: str,
    entity_id: str,
    *,
    role: str,
    builder: ReceiptBuilder,
) -> EntityInstance:
    entity = graph.get_entity(entity_type, entity_id)
    if entity is None:
        # The standard not-found class, not a bare ConfigError: "you named
        # something that does not exist" is a 404, not a 400, and the HTTP
        # mapper already routes EntityNotFoundError there. The role-aware
        # phrasing stays in the refusal RECEIPT, which is where the audit value
        # of "predecessor vs successor" actually lives.
        _refuse(
            builder,
            f"{role} entity {entity_type}:{entity_id} not found",
            entity_type=entity_type,
            entity_id=entity_id,
            error=EntityNotFoundError(entity_type, entity_id),
        )
    return entity


def _require_claim_predecessor_eligible(
    relationship: RelationshipInstance,
    *,
    builder: ReceiptBuilder,
) -> None:
    assertion = relationship_assertion_from_metadata(relationship.metadata)
    if assertion.lifecycle.status != "active" or assertion.review.status in {
        "pending",
        "rejected",
    }:
        _refuse(
            builder,
            f"predecessor claim '{relationship.claim_id}' is not eligible for lifecycle "
            "adjudication; requires lifecycle 'active' and review not pending/rejected, "
            f"found lifecycle '{assertion.lifecycle.status}' and review "
            f"'{assertion.review.status}'",
        )


def _require_entity_predecessor_eligible(
    entity: EntityInstance,
    *,
    builder: ReceiptBuilder,
) -> None:
    if entity.metadata.lifecycle_status() != "live":
        _refuse(
            builder,
            f"predecessor entity {entity.entity_type}:{entity.entity_id} is not eligible for "
            f"lifecycle adjudication; requires status 'live', found "
            f"'{entity.metadata.lifecycle_status()}'",
            entity_type=entity.entity_type,
            entity_id=entity.entity_id,
        )


def _refuse_reused_successor(
    existing: SupersessionPointer | None,
    *,
    builder: ReceiptBuilder,
    successor_label: str,
    predecessor_label: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> None:
    """Refuse a successor that is already the successor of something else.

    ``supersedes`` is a SINGLE pointer, so a second supersession onto the same
    successor would overwrite the first one's back-reference and leave the
    original predecessor holding a ``superseded_by`` that nothing points back at
    — exactly the unpaired corpus D3's both-direction pointers exist to prevent.
    The honest answer is to refuse and name the supersession already recorded,
    so the adjudicator can decide whether THAT one was wrong rather than
    silently displacing it.
    """
    if existing is None:
        return
    _refuse(
        builder,
        f"{successor_label} already supersedes {_pointer_label(existing)}; a successor "
        "can record only one predecessor. Adjudicate the existing supersession "
        f"before superseding {predecessor_label} onto it.",
        entity_type=entity_type,
        entity_id=entity_id,
    )


def _pointer_label(pointer: SupersessionPointer) -> str:
    if pointer.claim_id is not None:
        return f"claim '{pointer.claim_id}'"
    if pointer.entity_type is not None:
        return f"entity {pointer.entity_type}:{pointer.entity_id}"
    # The pointer model is deliberately OPEN, so a pointer kind this build does
    # not know about must still be nameable in the refusal.
    return str(pointer.model_dump(mode="json", exclude_none=True))


def _apply_claim_lifecycle(
    graph: EntityGraph,
    *,
    config: CoreConfig,
    relationship: RelationshipInstance,
    lifecycle: RelationshipLifecycleState,
    source: str,
    actor_context: GovernedActorContext,
    receipt_id: str,
) -> RelationshipInstance:
    """Move ONE edge's lifecycle axis, carrying its content verbatim.

    Deliberately does NOT re-run :func:`validate_relationship`. Two reasons, both
    load-bearing:

    * **Exact-edge fidelity.** ``validate_relationship`` seeds its property
      payload from ``graph.get_relationship``'s FIRST tuple match. On a tuple
      carrying parallel edges that is a SIBLING of the edge being adjudicated,
      so retracting one edge would have rewritten its properties from the
      other's. The adjudication addresses its subject by ``edge_key`` /
      ``claim_id``; its content must come from that same subject.
    * **Schema drift must not block settlement.** A required property added to
      the type's schema after the edge was written would make that edge
      permanently un-adjudicable — the corpus would be unable to retract
      exactly the stale claims that motivated the schema change.

    A lifecycle transition introduces no new content, so there is nothing to
    validate: ``is_update=True`` by construction (the edge is loaded from the
    graph inside the receipt boundary), and the properties are copied through
    unchanged.
    """
    candidate = RelationshipInstance(
        relationship_type=relationship.relationship_type,
        from_type=relationship.from_type,
        from_id=relationship.from_id,
        to_type=relationship.to_type,
        to_id=relationship.to_id,
        edge_key=relationship.edge_key,
        claim_id=relationship.claim_id,
        properties=dict(relationship.properties),
    )
    validated = ValidatedRelationship(relationship=candidate, is_update=True)
    return apply_relationship(
        graph,
        validated,
        source,
        source_ref=source,
        config=config,
        receipt_id=receipt_id,
        actor_context=actor_context,
        lifecycle=lifecycle,
        trusted_lifecycle_transition=True,
    )


def _apply_entity_lifecycle(
    graph: EntityGraph,
    *,
    config: CoreConfig,
    entity: EntityInstance,
    lifecycle: EntityLifecycleState,
    source: str,
    actor_context: GovernedActorContext,
) -> EntityInstance:
    metadata = entity.metadata.model_copy(
        update={"lifecycle": lifecycle, "actor_context": actor_context}
    )
    validated = validate_entity(
        config,
        graph,
        entity.entity_type,
        entity.entity_id,
        dict(entity.properties),
        metadata=metadata,
    )
    apply_entity(
        graph,
        validated,
        config=config,
        source=source,
        trusted_lifecycle_transition=True,
    )
    updated = graph.get_entity(entity.entity_type, entity.entity_id)
    if updated is None:  # pragma: no cover - the update above just wrote it
        raise AssertionError("updated lifecycle entity disappeared")
    return updated


def _live_attached_edge_count(graph: EntityGraph, entity: EntityInstance) -> int:
    return sum(
        1
        for relationship in graph.iter_relationships()
        if relationship_is_live(relationship.metadata)
        and (
            (relationship.from_type, relationship.from_id) == (entity.entity_type, entity.entity_id)
            or (relationship.to_type, relationship.to_id) == (entity.entity_type, entity.entity_id)
        )
    )


def _record_claim_write(
    builder: ReceiptBuilder,
    relationship: RelationshipInstance,
    *,
    action: str,
    role: str,
) -> None:
    builder.record_relationship_write(
        relationship.from_type,
        relationship.from_id,
        relationship.to_type,
        relationship.to_id,
        relationship.relationship_type,
        True,
        claim_id=relationship.claim_id,
        detail={"action": action, "role": role},
    )


def _record_entity_write(
    builder: ReceiptBuilder,
    entity: EntityInstance,
    *,
    action: str,
    role: str,
) -> None:
    builder.record_entity_write(
        entity.entity_type,
        entity.entity_id,
        True,
        detail={"action": action, "role": role},
    )


def _record_adjudication(
    builder: ReceiptBuilder,
    parameters: Mapping[str, Any],
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> None:
    builder.record_validation(
        passed=True,
        detail={"lifecycle_adjudication": dict(parameters)},
        entity_type=entity_type,
        entity_id=entity_id,
    )


def _refuse(
    builder: ReceiptBuilder,
    reason: str,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    error: CoreError | None = None,
) -> NoReturn:
    """Record the refusal on the open receipt, then raise.

    Always inside the ``mutation_receipt`` boundary, so the raise both persists a
    refusal receipt and rolls the open write transaction back. ``error`` selects
    a more precise exception class than the ``ConfigError`` default — used for
    not-found, which is a 404 rather than a 400.
    """
    builder.record_validation(
        passed=False,
        detail={"reason": reason},
        entity_type=entity_type,
        entity_id=entity_id,
    )
    raise error if error is not None else ConfigError(reason)


__all__ = [
    "service_retract_claim",
    "service_retire_entity",
    "service_supersede_claim",
    "service_supersede_entity",
]
