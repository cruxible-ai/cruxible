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
from cruxible_core.service.mutation_receipts import mutation_receipt, save_graph_for_mutation
from cruxible_core.service.types import ResolveGroupResult, UpdateTrustStatusResult
from cruxible_core.storage.protocols import UnitOfWorkProtocol


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
    applied_tuples: list[dict[str, str]]
    validation_failures: int
    validation_errors: list[str]


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
        raise ConfigError(
            f"Invalid action '{action}'. Use: {', '.join(_VALID_RESOLVE_ACTIONS)}"
        )
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
        },
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
                is_retry=target.is_retry,
                builder=ctx.builder,
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
        ctx.uow.groups.update_resolution_trust_status(resolution_id, trust_status, reason)
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
            builder.record_validation(
                passed=False,
                detail={
                    "member": relationship.endpoint_label(),
                    "reason": "edge_exists",
                },
            )
            edges_skipped += 1
            skipped_existing.append(relationship.identity_payload())
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
            builder.record_validation(
                passed=False,
                detail={
                    "member": relationship.endpoint_label(),
                    "reason": "validation_failed",
                },
            )
            edges_skipped += 1
            validation_failures += 1
            detail = "; ".join(exc.errors) if exc.errors else str(exc)
            validation_errors.append(f"{relationship.relationship_label()}: {detail}")
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
) -> str:
    if is_retry:
        return cast(str, group.resolution_id)

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
    graph: EntityGraph,
    group_id: str,
    relationships: list[ValidatedRelationship],
    uow: UnitOfWorkProtocol,
    receipt_id: str | None = None,
    resolution_id: str | None = None,
) -> int:
    if not relationships:
        return 0

    touched_relationships: list[RelationshipInstance] = []
    for validated in relationships:
        relationship = validated.relationship
        apply_relationship(
            graph,
            validated,
            "group_resolve",
            f"group:{group_id}",
            receipt_id=receipt_id,
            resolution_id=resolution_id,
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
    is_retry: bool,
    builder: ReceiptBuilder,
) -> ResolveGroupResult:
    check_upstream_type_ownership(
        instance.get_upstream_metadata(),
        relationship_types=[group.relationship_type],
    )
    instance.invalidate_graph_cache()
    config = instance.load_config()
    graph = instance.load_graph()

    validation = _validate_approval_members(
        config=config,
        graph=graph,
        group=group,
        members=members,
        builder=builder,
    )
    resolution_id = _start_approval_resolution(
        group_store=uow.groups,
        group=group,
        rationale=rationale,
        resolved_by=resolved_by,
        is_retry=is_retry,
        validation=validation,
    )
    _record_relationship_write_nodes(builder, validation.valid_inputs)
    edges_created = _apply_resolved_relationships(
        instance=instance,
        graph=graph,
        group_id=group.group_id,
        relationships=validation.valid_inputs,
        uow=uow,
        receipt_id=builder.receipt_id,
        resolution_id=resolution_id,
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
        },
    )
    return ResolveGroupResult(
        group_id=group.group_id,
        action="approve",
        edges_created=edges_created,
        edges_skipped=validation.edges_skipped,
        resolution_id=resolution_id,
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
