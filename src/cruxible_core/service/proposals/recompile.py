"""Lazy recompilation from retained inputs, never from a stale candidate tree.

Only explicit proposal operations call this module. Work is bounded by the
origin chain and this proposal's input/members; it adds no population scan.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from cruxible_client.contracts.authoring.models import ChangeSetAuthoringPayload
from cruxible_client.contracts.errors import ProposalStale
from cruxible_client.contracts.proposal_models import (
    ProposalReadmissionLink,
    ProposalSettleSubmission,
)
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.authoring.preflight import (
    authoring_intent_id_for_proposal_ref,
    compute_preflight,
)
from cruxible_core.authoring.store import AuthoringIntentStoreError
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.indexes.subject_identity import RecordedCreation
from cruxible_core.proposals.proposals import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
    ProposalHeadMovedError,
    ProposalPreview,
    ProposalResult,
    ProposalWithdrawalRecord,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.change_preview import preview_guards


@dataclass(frozen=True)
class RecompileInput:
    tree: Mapping[str, bytes]
    timestamp: str
    rationale: str | None
    operation: str
    creations: tuple[RecordedCreation, ...] = ()
    settle_submission: ProposalSettleSubmission | None = None
    authorize: (
        Callable[[AcceptedProjectionCoordinate, Mapping[str, bytes]], Mapping[str, bytes] | None]
        | None
    ) = None


@dataclass(frozen=True)
class Recompilation:
    source_proposal_id: str
    proposal: ProposalResult | ProposalPreview
    members_identical: bool
    coordinate: AcceptedCoordinate


def lowering_origin(instance: PlaybillInstance, source: ProposalResult) -> ProposalResult | None:
    """Follow only this proposal's immutable chain; raw/exact-byte origins return None."""
    from cruxible_core.service.proposals.proposals import _proposal_result

    seen: set[str] = set()
    while source.admission.readmits is not None:
        if source.admission.proposal_id in seen:
            raise ProposalStale(source.admission.proposal_id, reason="origin_cycle")
        seen.add(source.admission.proposal_id)
        source = _proposal_result(instance, source.admission.readmits.source_proposal_id)
    admission = source.admission
    if (
        admission.procedure_operation is not None
        or authoring_intent_id_for_proposal_ref(admission.target_ref, actor_id=admission.actor_id)
        is not None
    ):
        return source
    return None


def _authoring_input(
    instance: PlaybillInstance, origin: ProposalResult, head: AcceptedCoordinate
) -> RecompileInput:
    admission = origin.admission
    intent_id = authoring_intent_id_for_proposal_ref(
        admission.target_ref, actor_id=admission.actor_id
    )
    assert intent_id is not None
    try:
        intent = AuthoringIntentCoordinator.for_instance(instance).store.get(
            intent_id, actor_id=admission.actor_id
        )
    except AuthoringIntentStoreError as exc:
        raise ProposalStale(
            admission.proposal_id, reason="input_unavailable", detail=str(exc)
        ) from exc
    # Keep every resolved name, minted id, timestamp and expectation. Pins have
    # their own anchor coordinates: changing the base cannot neutralize them.
    intent = intent.model_copy(update={"base_coordinate": head})
    computed = compute_preflight(
        instance, intent=intent, actor=AuthenticatedActor(actor_id=admission.actor_id)
    )
    if computed.result.verdict != "passed" or computed.lowered is None:
        diagnostics = computed.result.frontier.diagnostics
        raise ProposalStale(
            admission.proposal_id,
            reason=diagnostics[0].code if diagnostics else "lowering_refused",
            detail="; ".join(item.message for item in diagnostics),
        )
    lowered = computed.lowered
    return RecompileInput(
        tree=lowered.proposed_tree,
        timestamp=intent.canonical_timestamp,
        rationale=intent.payload.rationale
        if isinstance(intent.payload, ChangeSetAuthoringPayload)
        else None,
        operation=intent.intent_id,
        creations=tuple(item.record for item in lowered.subject_creations),
    )


def recompile_proposal(
    instance: PlaybillInstance,
    source: ProposalResult,
    *,
    preview: bool = False,
    confirm_head: Callable[[str], None] | None = None,
) -> Recompilation | None:
    """Prepare completely before publication; refusal leaves the old proposal alone.

    None means this origin does not lower and keeps ordinary byte readmission.
    The source actor owns the new proposal, even when a reviewer triggers it.
    """
    from cruxible_core.service.proposals.proposals import _proposal_result, _readmission_target_ref

    origin = lowering_origin(instance, source)
    if origin is None:
        return None
    source_id = source.admission.proposal_id
    evidence = instance.proposal_evidence()
    evidence.refuse_closed(source_id)
    head = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    link = ProposalReadmissionLink(source_proposal_id=source_id, coordinate=head)
    target = _readmission_target_ref(source.admission.actor_id, link.operation_digest)
    existing = tuple(
        item for item in evidence.admissions_at_target(target) if item.readmits == link
    )
    if existing:
        result: ProposalResult | ProposalPreview = _proposal_result(
            instance, existing[0].proposal_id
        )
    else:
        with preview_guards(True):
            if origin.admission.procedure_operation is not None:
                from cruxible_core.service.proposals.procedure_recompile import procedure_input

                inputs = procedure_input(instance, origin, head)
            else:
                inputs = _authoring_input(instance, origin, head)
        request = ProposalAdmissionRequest(
            target_ref=target,
            proposed_base_oid=head.git_oid,
            source_compilation_digest=link.operation_digest,
            rationale=inputs.rationale,
        )

        def authorize(
            current: AcceptedProjectionCoordinate, tree: Mapping[str, bytes]
        ) -> Mapping[str, bytes] | None:
            if current.git_oid != head.git_oid:
                raise ProposalStale(source_id, reason="head_moved")
            return None if inputs.authorize is None else inputs.authorize(current, tree)

        service = instance.proposal_service()
        authority = service.subject_assignment_authority
        own = frozenset(inputs.creations)
        service.subject_assignment_authority = lambda operation, creation: (
            (operation == inputs.operation and creation in own)
            or (authority is not None and authority(operation, creation))
        )
        # Run the ordinary admission evaluation before any record is written.
        # Its candidate is pinned through publication, which refuses a head race.
        with preview_guards(True):
            checked = service.preview(
                actor=AuthenticatedActor(actor_id=source.admission.actor_id),
                request=request,
                candidate_tree=inputs.tree,
                timestamp=inputs.timestamp,
                readmits=link,
                authorize=authorize,
                settle_submission=inputs.settle_submission,
            )
        if checked.candidate is None:
            raise ProposalStale(
                source_id,
                reason="evaluation_refused",
                detail="; ".join(item.code for item in checked.evaluation.diagnostics),
            )
        if preview:
            result = checked
        else:
            if inputs.creations:
                instance.subject_assignment_authority().record_creations(
                    inputs.operation, inputs.creations
                )
            try:
                result = service.submit(
                    actor=AuthenticatedActor(actor_id=source.admission.actor_id),
                    request=request,
                    candidate_tree=inputs.tree,
                    timestamp=inputs.timestamp,
                    readmits=link,
                    authorize=authorize,
                    confirm_head=confirm_head,
                    settle_submission=inputs.settle_submission,
                    expected_candidate=(head.git_oid, checked.candidate.candidate_digest),
                )
            except ProposalHeadMovedError as exc:
                raise ProposalStale(source_id, reason="head_moved") from exc
    if result.candidate is None:
        raise ProposalStale(
            source_id,
            reason="evaluation_refused",
            detail="; ".join(item.code for item in result.evaluation.diagnostics),
        )
    # Member evidence pins exact own bytes (including deletions); daemon cards
    # and change-set records are not part of this informational comparison.
    identical = source.candidate is not None and {
        member.path: getattr(
            member, "candidate_artifact_digest", getattr(member, "artifact_digest", None)
        )
        for member in source.candidate.members
    } == {
        member.path: getattr(
            member, "candidate_artifact_digest", getattr(member, "artifact_digest", None)
        )
        for member in result.candidate.members
    }
    if not preview:
        assert isinstance(result, ProposalResult)
        from cruxible_core.service.change_preview import ChangeMode
        from cruxible_core.service.proposals.proposals import _close_once

        if origin.admission.procedure_operation is None:
            intent_id = authoring_intent_id_for_proposal_ref(
                origin.admission.target_ref, actor_id=origin.admission.actor_id
            )
            assert intent_id is not None
            coordinator = AuthoringIntentCoordinator.for_instance(instance)
            status = coordinator._candidate_status(
                proposal_id=result.admission.proposal_id,
                candidate_digest=result.candidate.candidate_digest,
            )
            coordinator.store.transition(
                intent_id,
                actor_id=origin.admission.actor_id,
                operation_key=link.operation_digest,
                transform=lambda intent: intent.model_copy(
                    update={
                        "candidate_status": status,
                    }
                ),
            )
        with instance.refusal_index().closing(source_id):
            _close_once(
                instance,
                ChangeMode(instance, previewing=False),
                ProposalWithdrawalRecord(
                    proposal_id=source_id,
                    actor_id=source.admission.actor_id,
                    reason=f"Recompiled at the head as {result.admission.proposal_id}.",
                    withdrawn_at=source.admission.admitted_at,
                    superseded_by=result.admission.proposal_id,
                ),
            )
    return Recompilation(source_id, result, identical, head)
