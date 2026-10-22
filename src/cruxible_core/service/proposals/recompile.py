"""Lazy recompilation from retained inputs, never from a stale candidate tree.

Only explicit proposal operations call this module. Work is bounded by the
origin chain and this proposal's input/members; it adds no population scan.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from heapq import nsmallest
from itertools import chain

from pydantic import ValidationError

from cruxible_client.contracts.authoring.models import ChangeSetAuthoringPayload
from cruxible_client.contracts.errors import CruxibleError, ProposalNotFoundError, ProposalStale
from cruxible_client.contracts.proposal_models import (
    ProposalReadmissionLink,
    ProposalSettleSubmission,
)
from cruxible_client.contracts.recompilation import (
    RECOMPILED_MEMBER_PATH_LIMIT,
    RecompiledMemberChange,
)
from cruxible_client.contracts.temporal import format_datetime
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
    ProposalService,
    ProposalWithdrawalRecord,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.change_preview import preview_guards
from cruxible_core.storage.cas import dry_run_held_bodies


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
    #: Every change the source carried is already live at the head: nothing is
    #: left to propose, and the source closes as answered.
    already_live: bool = False
    #: Called once the recompilation committed, with the successor's proposal ID
    #: (None when the source closed as already live): the origin's own follow-up
    #: records, such as a Line's open-proposal record and re-observations.
    published: Callable[[str | None], None] | None = None


@dataclass(frozen=True)
class Recompilation:
    source_proposal_id: str
    #: None exactly when ``already_live``: nothing was proposed.
    proposal: ProposalResult | ProposalPreview | None
    members_identical: bool
    coordinate: AcceptedCoordinate
    changed_members: tuple[RecompiledMemberChange, ...]
    changed_members_truncated: bool
    already_live: bool = False


ALREADY_LIVE_REASON = (
    "Already live at the head: every change this proposal carried is accepted, "
    "so nothing is left to propose."
)


def _changed_members(
    before: Mapping[str, str | None], after: Mapping[str, str | None]
) -> tuple[tuple[RecompiledMemberChange, ...], bool]:
    """On explicit recompilation, compare only these candidates' own members.

    Two keyed lists plus a fixed-size selection keep work O(members), including
    the sorted bounded preview. No accepted-instance population is visited.
    """
    changes = chain(
        (
            RecompiledMemberChange(path=path, change="added" if path not in before else "replaced")
            for path, digest in after.items()
            if path not in before or before[path] != digest
        ),
        (
            RecompiledMemberChange(path=path, change="removed")
            for path in before
            if path not in after
        ),
    )
    selected = nsmallest(RECOMPILED_MEMBER_PATH_LIMIT + 1, changes, key=lambda item: item.path)
    truncated = len(selected) > RECOMPILED_MEMBER_PATH_LIMIT
    return tuple(selected[:RECOMPILED_MEMBER_PATH_LIMIT]), truncated


def recompiled_successor(instance: PlaybillInstance, source: ProposalResult) -> ProposalResult:
    """The last replacement of this proposal, from keyed immutable links only."""
    from cruxible_core.service.proposals.proposals import _proposal_result

    admission = source.admission
    seen: set[str] = set()
    while True:
        pid = admission.proposal_id
        if pid in seen:
            raise ProposalStale(pid, reason="origin_cycle")
        seen.add(pid)
        withdrawal = instance.proposal_evidence().read_withdrawal(pid)
        if withdrawal is None or withdrawal.superseded_by is None:
            break
        successor = instance.proposal_evidence().read_admission(withdrawal.superseded_by)
        if successor.readmits is None or successor.readmits.source_proposal_id != pid:
            break
        admission = successor
    return (
        source
        if admission.proposal_id == source.admission.proposal_id
        else _proposal_result(instance, admission.proposal_id)
    )


def lowering_origin(instance: PlaybillInstance, source: ProposalResult) -> ProposalResult | None:
    """Follow only this proposal's immutable chain; raw/exact-byte origins return None."""
    from cruxible_core.service.proposals.proposals import _proposal_result

    admission = source.admission
    seen: set[str] = set()
    while admission.readmits is not None:
        if admission.proposal_id in seen:
            raise ProposalStale(source.admission.proposal_id, reason="origin_cycle")
        seen.add(admission.proposal_id)
        try:
            admission = instance.proposal_evidence().read_admission(
                admission.readmits.source_proposal_id
            )
        except ProposalNotFoundError as exc:
            raise ProposalStale(
                source.admission.proposal_id,
                reason="input_unavailable",
                detail="Readmission origin is unavailable",
            ) from exc
    if (
        admission.procedure_operation is not None
        or authoring_intent_id_for_proposal_ref(admission.target_ref, actor_id=admission.actor_id)
        is not None
    ):
        return (
            source
            if admission.proposal_id == source.admission.proposal_id
            else _proposal_result(instance, admission.proposal_id)
        )
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
    """Serialize recompilation with withdrawal/rejection of this same source."""
    with instance.refusal_index().closing(source.admission.proposal_id):
        return _recompile_proposal(instance, source, preview=preview, confirm_head=confirm_head)


def _recompile_proposal(
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
    inputs: RecompileInput | None = None
    if existing:
        result: ProposalResult | ProposalPreview = _proposal_result(
            instance, existing[0].proposal_id
        )
    else:
        try:
            # One body overlay lasts through lowering AND ordinary evaluation.
            # In particular self-source Capture envelopes bind the new head.
            with preview_guards(True):
                if origin.admission.procedure_operation is not None:
                    from cruxible_core.service.proposals.procedure_recompile import procedure_input

                    inputs = procedure_input(instance, origin, head)
                else:
                    inputs = _authoring_input(instance, origin, head)
                if not inputs.already_live:
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

                    def service_with_creations() -> ProposalService:
                        service = instance.proposal_service()
                        authority = service.subject_assignment_authority
                        own = frozenset(inputs.creations)
                        service.subject_assignment_authority = lambda operation, creation: (
                            (operation == inputs.operation and creation in own)
                            or (authority is not None and authority(operation, creation))
                        )
                        return service

                    checked = service_with_creations().preview(
                        actor=AuthenticatedActor(actor_id=source.admission.actor_id),
                        request=request,
                        candidate_tree=inputs.tree,
                        timestamp=inputs.timestamp,
                        readmits=link,
                        authorize=authorize,
                        settle_submission=inputs.settle_submission,
                    )
                    generated_bodies = dict(dry_run_held_bodies() or {})
        except ProposalStale as exc:
            if exc.proposal_id == source_id:
                raise
            raise ProposalStale(source_id, reason=exc.reason, detail=exc.detail) from exc
        except (CruxibleError, ValidationError) as exc:
            raise ProposalStale(
                source_id, reason=str(getattr(exc, "code", "input_unavailable")), detail=str(exc)
            ) from exc
        if inputs.already_live:
            # Outside the preview overlay: closing the answered source is a write.
            return _close_already_live(instance, source, inputs, head, preview=preview)
        if checked.candidate is None:
            raise ProposalStale(
                source_id,
                reason="evaluation_refused",
                detail="; ".join(item.code for item in checked.evaluation.diagnostics),
            )
        if preview:
            result = checked
        else:
            # Only clean preparations publish bodies, assignments or proposals.
            # This loop is exactly this input's newly lowered material.
            for content in generated_bodies.values():
                instance.body_store().store(content)
            if inputs.creations:
                instance.subject_assignment_authority().record_creations(
                    inputs.operation, inputs.creations
                )
            try:
                result = service_with_creations().submit(
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
    before = (
        {}
        if source.candidate is None
        else {
            member.path: getattr(
                member, "candidate_artifact_digest", getattr(member, "artifact_digest", None)
            )
            for member in source.candidate.members
        }
    )
    after = {
        member.path: getattr(
            member, "candidate_artifact_digest", getattr(member, "artifact_digest", None)
        )
        for member in result.candidate.members
    }
    identical = source.candidate is not None and before == after
    changed_members, truncated = _changed_members(before, after)
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
                recover_creates=False,
                transform=lambda intent: intent.model_copy(
                    update={
                        "candidate_status": status,
                    }
                ),
            )
        _close_once(
            instance,
            ChangeMode(instance, previewing=False),
            ProposalWithdrawalRecord(
                proposal_id=source_id,
                actor_id=source.admission.actor_id,
                reason=f"Recompiled at the head as {result.admission.proposal_id}.",
                withdrawn_at=str(format_datetime(datetime.now(timezone.utc))),
                superseded_by=result.admission.proposal_id,
            ),
        )
        if inputs is not None and inputs.published is not None:
            inputs.published(result.admission.proposal_id)
    return Recompilation(source_id, result, identical, head, changed_members, truncated)


def _close_already_live(
    instance: PlaybillInstance,
    source: ProposalResult,
    inputs: RecompileInput,
    head: AcceptedCoordinate,
    *,
    preview: bool,
) -> Recompilation:
    """Nothing is left to propose at the head: the stale source is answered.

    The source closes under its own author, with no successor; a preview
    reports the outcome and records nothing. Work is this source's own members.
    """
    from cruxible_core.service.change_preview import ChangeMode
    from cruxible_core.service.proposals.proposals import _close_once

    source_id = source.admission.proposal_id
    before = (
        {}
        if source.candidate is None
        else {
            member.path: getattr(
                member, "candidate_artifact_digest", getattr(member, "artifact_digest", None)
            )
            for member in source.candidate.members
        }
    )
    changed_members, truncated = _changed_members(before, {})
    if not preview:
        _close_once(
            instance,
            ChangeMode(instance, previewing=False),
            ProposalWithdrawalRecord(
                proposal_id=source_id,
                actor_id=source.admission.actor_id,
                reason=ALREADY_LIVE_REASON,
                withdrawn_at=str(format_datetime(datetime.now(timezone.utc))),
            ),
        )
        if inputs.published is not None:
            inputs.published(None)
    return Recompilation(
        source_id,
        None,
        members_identical=False,
        coordinate=head,
        changed_members=changed_members,
        changed_members_truncated=truncated,
        already_live=True,
    )
