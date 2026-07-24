"""Governance state transitions and mutation boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from cruxible_core.config.ownership import check_upstream_type_ownership
from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import (
    ConfigError,
    DataValidationError,
    GroupNotFoundError,
)
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.assertion_state import RelationshipReviewState
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.evidence import (
    EvidenceRef,
    RelationshipEvidence,
    merge_evidence_refs,
)
from cruxible_core.graph.operations import (
    ValidatedRelationship,
    apply_relationship,
    validate_relationship,
)
from cruxible_core.graph.provenance import make_provenance, stamp_provenance_modified
from cruxible_core.graph.types import RelationshipInstance, RelationshipMetadata
from cruxible_core.group.types import (
    CandidateGroup,
    CandidateMember,
    GroupResolution,
    TrustStatus,
)
from cruxible_core.instance_protocol import GroupStoreProtocol, InstanceProtocol
from cruxible_core.primitives import ordered_unique
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.service.mutation_guards import (
    evaluate_relationship_mutation_guards,
    record_guard_evaluation,
)
from cruxible_core.service.mutation_proposals import (
    build_proposal,
    relationship_instance_member,
)
from cruxible_core.service.mutation_receipts import mutation_receipt, save_graph_for_mutation
from cruxible_core.service.types import ResolveGroupResult, UpdateTrustStatusResult
from cruxible_core.storage.protocols import UnitOfWorkProtocol
from cruxible_core.temporal import utc_now


@dataclass(frozen=True)
class _ResolveTarget:
    group: CandidateGroup
    members: list[CandidateMember]
    is_retry: bool


@dataclass(frozen=True)
class _ApprovalValidation:
    valid_inputs: list[ValidatedRelationship]
    edges_skipped: int
    skipped_existing: list[dict[str, str]]
    skipped_members: list[dict[str, str]]
    applied_tuples: list[dict[str, str]]
    validation_failures: int
    validation_errors: list[str]


def _skip_entry(
    relationship: RelationshipInstance,
    *,
    reason: str,
    skip_kind: str,
) -> dict[str, str]:
    """Build an explained skip record for the resolution result/receipt."""
    return {
        **relationship.identity_payload(),
        "skip_kind": skip_kind,
        "reason": reason,
    }


def _identity_only(payload: dict[str, str]) -> tuple[str, str, str, str, str]:
    return (
        payload["from_type"],
        payload["from_id"],
        payload["to_type"],
        payload["to_id"],
        payload["relationship_type"],
    )


def _annotate_stamped_skips(
    skipped_members: list[dict[str, str]],
    stamped_tuples: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Mark each skip with whether stamp-existing blessed its surviving edge."""
    stamped_keys = {_identity_only(entry) for entry in stamped_tuples}
    annotated: list[dict[str, str]] = []
    for entry in skipped_members:
        blessed = _identity_only(entry) in stamped_keys
        annotated.append({**entry, "stamped": "true" if blessed else "false"})
    return annotated


_VALID_RESOLVE_ACTIONS = ("approve", "reject")
_VALID_RESOLVE_SOURCES = ("human", "agent")
_VALID_TRUST_STATUSES = ("trusted", "watch", "invalidated")


def validate_resolve_request(
    *,
    action: str,
    resolved_by: str,
    expected_pending_version: int | None,
) -> None:
    if action not in _VALID_RESOLVE_ACTIONS:
        raise ConfigError(f"Invalid action '{action}'. Use: {', '.join(_VALID_RESOLVE_ACTIONS)}")
    if resolved_by not in _VALID_RESOLVE_SOURCES:
        raise ConfigError(
            f"Invalid resolved_by '{resolved_by}'. Use: {', '.join(_VALID_RESOLVE_SOURCES)}"
        )
    if expected_pending_version is None:
        raise ConfigError("Resolve requires expected_pending_version")


def resolve_group_transition(
    instance: InstanceProtocol,
    group_id: str,
    action: Literal["approve", "reject"],
    rationale: str = "",
    resolved_by: Literal["human", "agent"] = "human",
    expected_pending_version: int | None = None,
    actor_context: GovernedActorContext | None = None,
    stamp_existing: bool = False,
) -> ResolveGroupResult:
    """Resolve a candidate group through one mutation receipt/UOW boundary."""
    validate_resolve_request(
        action=action,
        resolved_by=resolved_by,
        expected_pending_version=expected_pending_version,
    )
    assert expected_pending_version is not None

    target = _load_group_for_resolve(instance, group_id=group_id, action=action)

    with mutation_receipt(
        instance,
        "group_resolve",
        {
            "group_id": group_id,
            "action": action,
            "expected_pending_version": expected_pending_version,
            "stamp_existing": stamp_existing,
        },
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        _validate_resolve_pending_version(
            group=target.group,
            expected_pending_version=expected_pending_version,
        )
        if action == "reject":
            resolved = _reject_group(
                group_store=ctx.uow.groups,
                group=target.group,
                members=target.members,
                rationale=rationale,
                resolved_by=resolved_by,
                actor_context=actor_context,
                builder=ctx.builder,
            )
        else:
            resolved = _approve_group(
                instance=instance,
                uow=ctx.uow,
                group=target.group,
                members=target.members,
                rationale=rationale,
                resolved_by=resolved_by,
                actor_context=actor_context,
                is_retry=target.is_retry,
                builder=ctx.builder,
                stamp_existing=stamp_existing,
            )
        ctx.set_result(resolved)

    final_result = ctx.result
    assert isinstance(final_result, ResolveGroupResult)
    return final_result


def update_trust_status_transition(
    instance: InstanceProtocol,
    resolution_id: str,
    trust_status: Literal["trusted", "watch", "invalidated"],
    reason: str = "",
    actor_context: GovernedActorContext | None = None,
) -> UpdateTrustStatusResult:
    """Update trust on the latest confirmed approval through one UOW boundary."""
    if trust_status not in _VALID_TRUST_STATUSES:
        raise ConfigError(
            f"Invalid trust_status '{trust_status}'. Use: {', '.join(_VALID_TRUST_STATUSES)}"
        )

    group_store = instance.get_group_store()
    try:
        resolution = group_store.get_resolution(resolution_id)
        if resolution is None:
            raise ConfigError(f"Resolution '{resolution_id}' not found")

        if resolution.action != "approve":
            raise ConfigError("Trust status can only be set on approved resolutions")

        if not resolution.confirmed:
            raise ConfigError(
                "Trust status can only be set on confirmed resolutions (group must be resolved)"
            )

        latest = group_store.find_resolution(
            resolution.relationship_type,
            resolution.group_signature,
            action="approve",
            confirmed=True,
        )
        if latest is None or latest.resolution_id != resolution_id:
            latest_id = latest.resolution_id if latest else "none"
            raise ConfigError(
                "Can only update trust on the latest confirmed approval "
                f"for this signature. Latest: {latest_id}"
            )
    finally:
        group_store.close()

    with mutation_receipt(
        instance,
        "group_trust_update",
        {
            "resolution_id": resolution_id,
            "trust_status": trust_status,
            "reason": reason,
        },
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        ctx.builder.record_validation(
            passed=True,
            detail={
                "resolution_id": resolution_id,
                "relationship_type": resolution.relationship_type,
                "signature": resolution.group_signature,
                "previous_trust_status": resolution.trust_status,
                "new_trust_status": trust_status,
                "reason": reason,
            },
        )
        ctx.uow.groups.update_resolution_trust_status(
            resolution_id,
            trust_status,
            reason,
            trust_actor_context=actor_context,
        )
        ctx.set_result(
            UpdateTrustStatusResult(
                resolution_id=resolution_id,
                trust_status=trust_status,
            )
        )

    final_result = ctx.result
    assert isinstance(final_result, UpdateTrustStatusResult)
    return final_result


def _load_group_for_resolve(
    instance: InstanceProtocol,
    *,
    group_id: str,
    action: Literal["approve", "reject"],
) -> _ResolveTarget:
    group_store = instance.get_group_store()
    try:
        group = group_store.get_group(group_id)
        if group is None:
            raise GroupNotFoundError(group_id)

        if group.status == "resolved":
            raise ConfigError("Group already resolved")
        if group.status == "applying" and action != "approve":
            raise ConfigError("Group is in applying state from a prior approve — cannot reject")

        return _ResolveTarget(
            group=group,
            members=group_store.get_members(group_id),
            is_retry=group.status == "applying",
        )
    finally:
        group_store.close()


def _validate_resolve_pending_version(
    *,
    group: CandidateGroup,
    expected_pending_version: int,
) -> None:
    if group.pending_version != expected_pending_version:
        raise ConfigError(
            "Group changed during review; expected pending_version "
            f"{expected_pending_version}, found {group.pending_version}"
        )


def _reject_group(
    *,
    group_store: GroupStoreProtocol,
    group: CandidateGroup,
    members: list[CandidateMember],
    rationale: str,
    resolved_by: Literal["human", "agent"],
    actor_context: GovernedActorContext | None,
    builder: ReceiptBuilder,
) -> ResolveGroupResult:
    builder.record_validation(
        passed=True,
        detail={
            "action": "reject",
            "members": len(members),
            "pending_version_at_resolve": group.pending_version,
        },
    )
    resolution_id = group_store.save_resolution(
        group.relationship_type,
        group.signature,
        "reject",
        rationale,
        group.thesis_text,
        group.thesis_facts,
        group.analysis_state,
        resolved_by,
        trust_status="watch",
        confirmed=True,
        resolved_actor_context=actor_context,
        receipt_id=builder.receipt_id,
    )
    group_store.update_group_status(
        group.group_id,
        "resolved",
        resolution_id=resolution_id,
    )
    return ResolveGroupResult(
        group_id=group.group_id,
        action="reject",
        edges_created=0,
        edges_skipped=0,
        resolution_id=resolution_id,
    )


def _validate_approval_members(
    *,
    config: CoreConfig,
    graph: EntityGraph,
    group: CandidateGroup,
    members: list[CandidateMember],
    builder: ReceiptBuilder,
) -> _ApprovalValidation:
    valid_inputs: list[ValidatedRelationship] = []
    edges_skipped = 0
    skipped_existing: list[dict[str, str]] = []
    skipped_members: list[dict[str, str]] = []
    applied_tuples: list[dict[str, str]] = []
    validation_failures = 0
    validation_errors: list[str] = []

    for member in members:
        relationship = member.as_relationship()
        count = graph.relationship_count_between(
            from_type=relationship.from_type,
            from_id=relationship.from_id,
            to_type=relationship.to_type,
            to_id=relationship.to_id,
            relationship_type=relationship.relationship_type,
        )
        if count > 0:
            reason = (
                f"member tuple already live (existing edge {relationship.relationship_label()})"
            )
            builder.record_validation(
                passed=False,
                detail={
                    "member": relationship.endpoint_label(),
                    "reason": "edge_exists",
                },
            )
            edges_skipped += 1
            skipped_existing.append(relationship.identity_payload())
            skipped_members.append(
                _skip_entry(relationship, reason=reason, skip_kind="existing_edge")
            )
            continue

        try:
            validated = validate_relationship(
                config,
                graph,
                relationship.from_type,
                relationship.from_id,
                relationship.relationship_type,
                relationship.to_type,
                relationship.to_id,
                relationship.properties,
            )
        except DataValidationError as exc:
            detail = "; ".join(exc.errors) if exc.errors else str(exc)
            builder.record_validation(
                passed=False,
                detail={
                    "member": relationship.endpoint_label(),
                    "reason": "validation_failed",
                },
            )
            edges_skipped += 1
            validation_failures += 1
            validation_errors.append(f"{relationship.relationship_label()}: {detail}")
            skipped_members.append(
                _skip_entry(
                    relationship,
                    reason=f"member failed validation ({detail})",
                    skip_kind="validation_failed",
                )
            )
            continue

        builder.record_validation(
            passed=True,
            detail={"member": relationship.endpoint_label()},
        )
        validated.relationship.metadata = RelationshipMetadata(
            evidence=_relationship_evidence_for_member(group, member)
        )
        valid_inputs.append(validated)
        applied_tuples.append(relationship.identity_payload())

    return _ApprovalValidation(
        valid_inputs=valid_inputs,
        edges_skipped=edges_skipped,
        skipped_existing=skipped_existing,
        skipped_members=skipped_members,
        applied_tuples=applied_tuples,
        validation_failures=validation_failures,
        validation_errors=validation_errors,
    )


def _inherited_trust_status(prior: GroupResolution | None) -> TrustStatus:
    if prior is not None and prior.trust_status in ("trusted", "watch"):
        return prior.trust_status
    return "watch"


def _start_approval_resolution(
    *,
    group_store: GroupStoreProtocol,
    group: CandidateGroup,
    rationale: str,
    resolved_by: Literal["human", "agent"],
    is_retry: bool,
    validation: _ApprovalValidation,
    actor_context: GovernedActorContext | None,
    receipt_id: str,
) -> str:
    """Return the resolution id this approve attempt applies edges under.

    A retry is crash recovery: the group is in ``applying`` because a prior
    attempt already wrote its (unconfirmed) resolution row and stamped that
    attempt's receipt id on it. We deliberately keep the FIRST attempt's
    ``receipt_id`` — the resolution was created by that act, and re-pointing it
    at the recovery receipt would erase the act that produced it. The retry is
    not lost: it has its own ``group_resolve`` receipt, joinable through the
    edge provenance it stamps.

    The one case where a retry finds NO receipt id is a resolution row written
    before the column existed (the additive migration backfills NULL) — that
    predates the invariant, so the recovery attempt fills it rather than leaving
    a permanently unjoinable row. A non-null id is never overwritten.
    """
    if is_retry:
        prior_resolution_id = cast(str, group.resolution_id)
        existing = group_store.get_resolution(prior_resolution_id)
        if existing is not None and existing.receipt_id is None:
            group_store.stamp_resolution_receipt_id(prior_resolution_id, receipt_id)
        return prior_resolution_id

    if not validation.valid_inputs and not validation.skipped_existing:
        if validation.validation_errors:
            raise DataValidationError(
                "Cannot approve group: candidate validation failed",
                errors=validation.validation_errors,
            )
        raise ConfigError("Cannot approve: no creatable edges")

    prior = group_store.find_resolution(
        group.relationship_type,
        group.signature,
        action="approve",
        confirmed=True,
    )
    resolution_id: str = group_store.save_resolution(
        group.relationship_type,
        group.signature,
        "approve",
        rationale,
        group.thesis_text,
        group.thesis_facts,
        group.analysis_state,
        resolved_by,
        trust_status=_inherited_trust_status(prior),
        confirmed=False,
        resolved_actor_context=actor_context,
        receipt_id=receipt_id,
    )
    group_store.update_group_status(
        group.group_id,
        "applying",
        resolution_id=resolution_id,
    )
    return resolution_id


def _record_relationship_write_nodes(
    builder: ReceiptBuilder,
    relationships: list[ValidatedRelationship],
) -> None:
    for validated in relationships:
        relationship = validated.relationship
        builder.record_relationship_write(
            from_type=relationship.from_type,
            from_id=relationship.from_id,
            to_type=relationship.to_type,
            to_id=relationship.to_id,
            relationship=relationship.relationship_type,
            is_update=validated.is_update,
        )


def _apply_resolved_relationships(
    *,
    instance: InstanceProtocol,
    config: CoreConfig,
    graph: EntityGraph,
    group_id: str,
    relationships: list[ValidatedRelationship],
    uow: UnitOfWorkProtocol,
    receipt_id: str | None = None,
    resolution_id: str | None = None,
    actor_context: GovernedActorContext | None = None,
) -> int:
    if not relationships:
        return 0

    touched_relationships: list[RelationshipInstance] = []
    for validated in relationships:
        relationship = validated.relationship
        # source="group_resolve" is a governed verb — always permitted by the
        # chokepoint regardless of write_policy.
        apply_relationship(
            graph,
            validated,
            "group_resolve",
            f"group:{group_id}",
            config=config,
            receipt_id=receipt_id,
            resolution_id=resolution_id,
            actor_context=actor_context,
        )
        persisted = graph.get_relationship(
            relationship.from_type,
            relationship.from_id,
            relationship.to_type,
            relationship.to_id,
            relationship.relationship_type,
        )
        if persisted is not None:
            touched_relationships.append(persisted)

    save_graph_for_mutation(
        instance,
        graph,
        entities=[],
        relationships=touched_relationships,
        uow=uow,
    )
    return sum(1 for validated in relationships if not validated.is_update)


def _blessed_metadata_for_existing(
    existing: RelationshipInstance,
    *,
    group_id: str,
    receipt_id: str | None,
    resolution_id: str | None,
    actor_context: GovernedActorContext | None,
) -> RelationshipMetadata:
    """Build metadata that blesses a pre-existing edge with the group's review.

    Mirrors the creation-time stamp ``apply_relationship`` gives a freshly
    group-resolved edge: the blessed edge becomes indistinguishable in identity
    from a natively group-resolved one (review approved/source=group, provenance
    source ``group_resolve``/``group:<id>`` with the resolution correlation),
    applied as a modification to a surviving direct-added edge.

    Internal-consistency invariant: an edge's provenance must never simultaneously
    claim a non-group origin (``source_ref`` like ``add_relationship``) AND carry a
    group ``resolution_id``/``receipt_id`` -- lineage derives group identity solely
    from ``source_ref.startswith("group:")`` (see ``provenance_group_id``), so a
    direct-write ``source_ref`` paired with a group resolution receipt would report
    as non-group-provenance while carrying a group resolution receipt. When we
    inject the group correlation we therefore also relabel ``source``/``source_ref``
    to the group values so lineage's group-identity verdict and the receipt
    correlation agree. Direct-write creation history is preserved as real history:
    ``created_at``/``created_actor_context`` survive (``stamp_provenance_modified``
    only touches ``last_modified_*``), and the prior direct-write receipt remains in
    the audit chain. A null-provenance direct-add is backfilled with fresh group
    provenance so it becomes auditable.
    """
    metadata = existing.metadata
    now = utc_now()
    source_ref = f"group:{group_id}"
    if metadata.provenance is None:
        provenance = make_provenance(
            "group_resolve",
            source_ref,
            receipt_id=receipt_id,
            resolution_id=resolution_id,
            actor_context=actor_context,
        )
    else:
        provenance = stamp_provenance_modified(
            metadata.provenance,
            "group_resolve",
            actor_context=actor_context,
        ).model_copy(
            update={
                "source": "group_resolve",
                "source_ref": source_ref,
                "resolution_id": resolution_id,
                "receipt_id": receipt_id,
            }
        )
    review = RelationshipReviewState(
        status="approved",
        source="group",
        updated_at=now,
        updated_by=source_ref,
        actor_context=actor_context,
    )
    assertion = metadata.assertion.model_copy(update={"review": review})
    return metadata.model_copy(update={"provenance": provenance, "assertion": assertion})


def _stamp_existing_edges(
    *,
    instance: InstanceProtocol,
    graph: EntityGraph,
    group_id: str,
    skipped_existing: list[dict[str, str]],
    uow: UnitOfWorkProtocol,
    receipt_id: str | None,
    resolution_id: str | None,
    actor_context: GovernedActorContext | None,
) -> list[dict[str, str]]:
    """Bless each surviving direct-added edge with the group's review/provenance.

    Returns the identity payloads of the edges that were actually stamped so the
    caller can mark them in the resolution result.
    """
    stamped: list[dict[str, str]] = []
    touched_relationships: list[RelationshipInstance] = []
    for identity in skipped_existing:
        existing = graph.get_relationship(
            identity["from_type"],
            identity["from_id"],
            identity["to_type"],
            identity["to_id"],
            identity["relationship_type"],
        )
        if existing is None:
            continue
        blessed = _blessed_metadata_for_existing(
            existing,
            group_id=group_id,
            receipt_id=receipt_id,
            resolution_id=resolution_id,
            actor_context=actor_context,
        )
        graph.replace_relationship_state(
            existing.from_type,
            existing.from_id,
            existing.to_type,
            existing.to_id,
            existing.relationship_type,
            properties=dict(existing.properties),
            metadata=blessed,
            edge_key=existing.edge_key,
        )
        refreshed = graph.get_relationship(
            existing.from_type,
            existing.from_id,
            existing.to_type,
            existing.to_id,
            existing.relationship_type,
        )
        if refreshed is not None:
            touched_relationships.append(refreshed)
        stamped.append(dict(identity))

    if touched_relationships:
        save_graph_for_mutation(
            instance,
            graph,
            entities=[],
            relationships=touched_relationships,
            uow=uow,
        )
    return stamped


def _revalidated_trust_status(
    *,
    group_store: GroupStoreProtocol,
    group: CandidateGroup,
) -> TrustStatus | None:
    prior = group_store.find_resolution(
        group.relationship_type,
        group.signature,
        action="approve",
        confirmed=True,
    )
    if prior is not None and prior.trust_status == "invalidated":
        return "watch"
    return None


def _confirm_approval_resolution(
    *,
    group_store: GroupStoreProtocol,
    group: CandidateGroup,
    resolution_id: str,
) -> None:
    revalidated_trust = _revalidated_trust_status(
        group_store=group_store,
        group=group,
    )
    group_store.confirm_resolution(
        resolution_id,
        trust_status=revalidated_trust,
    )
    group_store.update_group_status(group.group_id, "resolved")


def _approve_group(
    *,
    instance: InstanceProtocol,
    uow: UnitOfWorkProtocol,
    group: CandidateGroup,
    members: list[CandidateMember],
    rationale: str,
    resolved_by: Literal["human", "agent"],
    actor_context: GovernedActorContext | None,
    is_retry: bool,
    builder: ReceiptBuilder,
    stamp_existing: bool = False,
) -> ResolveGroupResult:
    check_upstream_type_ownership(
        instance.get_upstream_metadata(),
        relationship_types=[group.relationship_type],
    )
    instance.invalidate_graph_cache()
    config = instance.load_config()
    graph = instance.load_graph()

    # An approval's proposal IS the group's staged edges. Recorded before member
    # validation and before the guards, so a refused approval retains the full
    # staged set — including members that validation or a guard rejected, which
    # nothing else on the receipt would preserve.
    proposal, subjects = build_proposal(
        operation="group_approve",
        relationships=[
            relationship_instance_member(member.as_relationship()) for member in members
        ],
        extra={
            "group_id": group.group_id,
            "relationship_type": group.relationship_type,
            "group_signature": group.signature,
            "pending_version": group.pending_version,
            "rationale": rationale,
            "resolved_by": resolved_by,
            "stamp_existing": stamp_existing,
            "is_retry": is_retry,
        },
    )
    builder.record_proposal(proposal, subjects=subjects)

    validation = _validate_approval_members(
        config=config,
        graph=graph,
        group=group,
        members=members,
        builder=builder,
    )
    guard_evaluation = evaluate_relationship_mutation_guards(
        instance,
        config,
        current_graph=graph,
        relationships=validation.valid_inputs,
    )
    record_guard_evaluation(builder, guard_evaluation)
    guard_errors = guard_evaluation.messages
    if guard_errors:
        raise DataValidationError(
            f"Mutation guard validation failed with {len(guard_errors)} error(s)",
            errors=guard_errors,
        )

    resolution_id = _start_approval_resolution(
        group_store=uow.groups,
        group=group,
        rationale=rationale,
        resolved_by=resolved_by,
        is_retry=is_retry,
        validation=validation,
        actor_context=actor_context,
        receipt_id=builder.receipt_id,
    )
    _record_relationship_write_nodes(builder, validation.valid_inputs)
    edges_created = _apply_resolved_relationships(
        instance=instance,
        config=config,
        graph=graph,
        group_id=group.group_id,
        relationships=validation.valid_inputs,
        uow=uow,
        receipt_id=builder.receipt_id,
        resolution_id=resolution_id,
        actor_context=actor_context,
    )
    stamped_tuples: list[dict[str, str]] = []
    if stamp_existing and validation.skipped_existing:
        stamped_tuples = _stamp_existing_edges(
            instance=instance,
            graph=graph,
            group_id=group.group_id,
            skipped_existing=validation.skipped_existing,
            uow=uow,
            receipt_id=builder.receipt_id,
            resolution_id=resolution_id,
            actor_context=actor_context,
        )
    skipped_members = _annotate_stamped_skips(
        validation.skipped_members,
        stamped_tuples,
    )
    _confirm_approval_resolution(
        group_store=uow.groups,
        group=group,
        resolution_id=resolution_id,
    )
    builder.record_validation(
        passed=validation.validation_failures == 0,
        detail={
            "pending_version_at_resolve": group.pending_version,
            "resolution_id": resolution_id,
            "applied_tuples": validation.applied_tuples,
            "skipped_tuples_existing_edges": validation.skipped_existing,
            "stamped_existing_edges": stamped_tuples,
        },
    )
    return ResolveGroupResult(
        group_id=group.group_id,
        action="approve",
        edges_created=edges_created,
        edges_skipped=validation.edges_skipped,
        resolution_id=resolution_id,
        skipped_members=skipped_members,
        edges_stamped=len(stamped_tuples),
    )


def _relationship_evidence_for_member(
    group: CandidateGroup,
    member: CandidateMember,
) -> RelationshipEvidence:
    evidence_refs = [
        EvidenceRef.model_validate(ref)
        for ref in merge_evidence_refs(
            member.evidence_refs,
            *(signal.evidence_refs for signal in member.signals),
        )
    ]
    source_receipt_ids = ordered_unique(
        [
            *([group.source_workflow_receipt_id] if group.source_workflow_receipt_id else []),
            *group.source_query_receipt_ids,
        ]
    )
    return RelationshipEvidence(
        evidence_refs=evidence_refs,
        rationale=member.evidence_rationale,
        source_group_id=group.group_id,
        source_receipt_ids=source_receipt_ids,
        source_trace_ids=ordered_unique(group.source_trace_ids),
        source_step_ids=ordered_unique(group.source_step_ids),
    )
