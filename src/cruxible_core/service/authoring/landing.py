"""Land an authored change when its policy and the caller allow it."""

from cruxible_client import contracts
from cruxible_client.contracts.authoring.models import AuthoringSubmitResult
from cruxible_client.contracts.authoring_landing import (
    DELEGATED_PREFIX,
    AuthoringAccept,
    AuthoringLanded,
    AuthoringLanding,
    AuthoringProposed,
    AuthoringRefused,
)
from cruxible_client.contracts.candidates import CandidateMemberLawEvidence
from cruxible_client.contracts.proposal_models import AuthenticatedActor, ProposalResult
from cruxible_client.contracts.repair_spelling import render_repair
from cruxible_client.contracts.repairs import RepairOperation
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import (
    ActivationReceipt,
    ProposalInspection,
    outstanding_approvals,
    service_activate_playbill_proposal,
)


def service_land_authoring_submit(
    coordinator: AuthoringIntentCoordinator,
    *,
    actor: AuthenticatedActor,
    submitted: AuthoringSubmitResult,
    accept: AuthoringAccept,
    may_activate: bool,
) -> contracts.AuthoringSubmitResultRecord:
    """Finish this submission only; no instance population is scanned here."""

    status = submitted.status
    proposal_id = status.proposal_id
    outcome: AuthoringLanding
    if status.state == "accepted":
        assert status.accepted_generation is not None
        # S2: accepted with no proposal is a payload identical to accepted state.
        outcome = AuthoringLanded(
            proposal_id=proposal_id,
            accepted_coordinate=status.accepted_generation,
            unchanged=proposal_id is None,
        )
    elif status.state == "awaiting_external_approval" or any(
        item.condition == "external-approval" for item in status.path_to_acceptance
    ):
        outcome = _stopped(
            proposal_id,
            reason="independent_approval_required",
            signers=_outstanding_signers(
                coordinator.instance, status.candidate_digest, creator=actor.actor_id
            ),
        )
    elif status.state == "preflight_refused":
        # Preflight already re-checked the intent at the head, so a rerun
        # changes nothing: the diagnostics name payload faults to revise.
        resubmit = RepairOperation(
            operation="cruxible.authoring.submit",
            arguments={"intent_id": submitted.intent.intent_id},
        )
        outcome = AuthoringRefused(
            message=(
                "refused: the payload failed preflight and nothing was proposed; revise it "
                "as the diagnostics say and submit it again to this intent"
            ),
            next=render_repair(resubmit, surface="cli"),
            next_repair=resubmit,
        )
    elif status.state != "ready_to_activate" or proposal_id is None:
        outcome = _proposed(
            proposal_id,
            reason="not_ready_to_activate",
            message=f"not activated: the candidate is {status.state}, not ready_to_activate",
            repair=RepairOperation(
                operation="cruxible.authoring.preflight",
                arguments={"intent_id": submitted.intent.intent_id},
            ),
        )
    elif accept == "never":
        outcome = _stopped(proposal_id, reason="accept_never", may_activate=may_activate)
    elif not may_activate:
        outcome = _stopped(proposal_id, reason="activation_not_permitted")
    else:
        receipt, outcome = land_proposal(
            coordinator.instance, proposal_id=proposal_id, actor_id=actor.actor_id
        )
        if receipt.proposal_id != proposal_id:
            # Recompilation updated this intent's candidate association. Return
            # that association and the replacement member digests, not the
            # candidate originally submitted before the head moved.
            submitted = _recompiled_submission(coordinator, actor=actor, submitted=submitted)
            status = submitted.status
        if isinstance(receipt, contracts.ProposalRecompiled):
            status = status.model_copy(
                update={
                    "state": "awaiting_external_approval",
                    "proposal_id": receipt.proposal_id,
                    "candidate_digest": receipt.candidate_digest,
                    "current_accepted_coordinate": receipt.accepted_coordinate,
                }
            )
        elif isinstance(outcome, AuthoringLanded):
            status = status.model_copy(
                update={
                    "state": "accepted",
                    "proposal_id": receipt.proposal_id,
                    "accepted_generation": receipt.accepted_coordinate,
                    "current_accepted_coordinate": receipt.accepted_coordinate,
                    "path_to_acceptance": (),
                }
            )
    data = submitted.model_dump(mode="json")
    data["outcome"] = outcome.model_dump(mode="json")
    from cruxible_core.service.discovery.kit_owners import kit_retirement_warnings

    warned = kit_retirement_warnings(coordinator.instance, status.candidate_digest)
    if warned:
        data["warnings"] = list(warned)
    data["status"] = status.model_dump(mode="json")
    data["intent"]["candidate_status"] = data["status"]
    return contracts.AuthoringSubmitResultRecord.model_validate(data)


def land_proposal(
    instance: PlaybillInstance, *, proposal_id: str, actor_id: str
) -> tuple[ActivationReceipt | contracts.ProposalRecompiled, AuthoringLanding]:
    """Activate one ready proposal for its caller, and say how that ended.

    The one landing step every authoring door shares: an authoring submit, and a
    direct ``claim-type``, ``files`` or ``document`` propose. Reads and writes
    only this proposal.
    """

    receipt = service_activate_playbill_proposal(
        instance, proposal_id=proposal_id, activated_by=actor_id
    )
    if isinstance(receipt, contracts.ProposalRecompiled):
        return receipt, _stopped(
            receipt.proposal_id,
            reason="independent_approval_required",
            signers=_outstanding_signers(instance, receipt.candidate_digest, creator=actor_id),
        )
    if receipt.status == "accepted" and receipt.accepted_coordinate is not None:
        return receipt, AuthoringLanded(
            proposal_id=receipt.proposal_id, accepted_coordinate=receipt.accepted_coordinate
        )
    return receipt, _proposed(
        receipt.proposal_id,
        reason="head_moved",
        message="not activated: accepted state moved after this proposal was checked",
        repair=RepairOperation(
            operation="cruxible.proposal.readmit", arguments={"proposal_id": receipt.proposal_id}
        ),
    )


def land_direct_proposal(
    instance: PlaybillInstance,
    inspection: ProposalInspection,
    *,
    actor_id: str,
    accept: AuthoringAccept,
    may_activate: bool,
) -> ProposalInspection:
    """Land a directly proposed change when its policy and the caller allow it.

    One rule with the authoring submit: an admitted candidate that needs no
    other approver activates now unless the caller asked ``accept="never"`` or
    cannot activate; ``outcome`` says which. A preview is returned unchanged.
    """

    proposal = inspection.proposal
    if inspection.status != "admitted" or not isinstance(proposal, ProposalResult):
        return inspection
    proposal_id = proposal.admission.proposal_id
    outcome: AuthoringLanding
    accepted = inspection.accepted_coordinate
    if proposal.evaluation.verdict != "candidate" or proposal.candidate is None:
        outcome = _proposed(
            proposal_id,
            reason="not_ready_to_activate",
            message="not activated: the proposal was refused; its diagnostics say why",
            repair=RepairOperation(operation="cruxible.get", arguments={"ref": proposal_id}),
        )
    elif getattr(proposal.candidate, "approval_requirements", ()):
        outcome = _stopped(
            proposal_id,
            reason="independent_approval_required",
            signers=_outstanding_signers(
                instance, proposal.candidate.candidate_digest, creator=actor_id
            ),
        )
    elif accept == "never":
        outcome = _stopped(proposal_id, reason="accept_never", may_activate=may_activate)
    elif not may_activate:
        outcome = _stopped(proposal_id, reason="activation_not_permitted")
    else:
        receipt, outcome = land_proposal(instance, proposal_id=proposal_id, actor_id=actor_id)
        if receipt.accepted_coordinate is not None:
            accepted = receipt.accepted_coordinate
    from cruxible_core.service.discovery.kit_owners import kit_retirement_warnings

    warned = (
        ()
        if proposal.candidate is None
        else kit_retirement_warnings(instance, proposal.candidate.candidate_digest)
    )
    return inspection.model_copy(
        update={"outcome": outcome, "accepted_coordinate": accepted, "warnings": warned}
    )


def _recompiled_submission(
    coordinator: AuthoringIntentCoordinator,
    *,
    actor: AuthenticatedActor,
    submitted: AuthoringSubmitResult,
) -> AuthoringSubmitResult:
    """Only a head race reaches here; work grows with this candidate's members."""

    intent = coordinator.get(submitted.intent.intent_id, actor=actor).intent
    status = intent.candidate_status
    old_digest, new_digest = submitted.status.candidate_digest, status.candidate_digest
    members = submitted.members
    if old_digest is not None and new_digest is not None and old_digest != new_digest:
        evidence = coordinator.instance.proposal_evidence()
        original = evidence.read_candidate(old_digest)
        replacement = evidence.read_candidate(new_digest)
        by_path = {member.path: _member_digest(member) for member in replacement.members}
        digests = {_member_digest(member): by_path.get(member.path) for member in original.members}
        members = tuple(
            member.model_copy(
                update={
                    "artifact_digest": digests.get(member.artifact_digest) or member.artifact_digest
                }
            )
            for member in members
        )
    return submitted.model_copy(update={"intent": intent, "status": status, "members": members})


def _member_digest(member: CandidateMemberLawEvidence) -> str | None:
    return member.candidate_artifact_digest


def _outstanding_signers(
    instance: PlaybillInstance, candidate_digest: str | None, *, creator: str
) -> tuple[str, ...]:
    """Who can give the approval this candidate still needs: required signers first.

    Reads this candidate's record, its approvals and its parent's principal
    registry; nothing grows with the instance.
    """

    if candidate_digest is None:
        return ()
    evidence = instance.proposal_evidence()
    candidate = evidence.read_candidate(candidate_digest)
    outstanding = outstanding_approvals(
        evidence.read_approvals(candidate_digest),
        candidate,
        creator=creator,
        principals=instance.generation_for_semantic_root(
            candidate.candidate.parent_semantic_root
        ).principals,
        principal_lifecycle=all(
            member.artifact_kind == "principal-lifecycle" for member in candidate.members
        ),
    )
    return (*outstanding.required_signers, *outstanding.eligible_signers)


def _proposed(
    proposal_id: str | None,
    *,
    reason: str,
    message: str,
    repair: RepairOperation,
    delegated: bool = False,
) -> AuthoringProposed:
    """A stop outcome whose ``next`` is the CLI spelling of its structured repair.

    The MCP server and the SDK print the same repair in their own spelling
    (``landing_for_surface``).
    """

    line = render_repair(repair, surface="cli")
    return AuthoringProposed(
        proposal_id=proposal_id,
        reason=reason,  # type: ignore[arg-type]
        message=message,
        next=f"{DELEGATED_PREFIX}{line}" if delegated else line,
        next_repair=repair,
    )


def _stopped(
    proposal_id: str | None,
    *,
    reason: str,
    signers: tuple[str, ...] = (),
    may_activate: bool = True,
) -> AuthoringProposed:
    activate = RepairOperation(
        operation="cruxible.proposal.activate", arguments={"proposal_id": proposal_id}
    )
    if reason == "independent_approval_required":
        named = f" from one of: {', '.join(signers)}" if signers else ""
        return _proposed(
            proposal_id,
            reason="independent_approval_required",
            message=(
                f"not activated: the candidate needs an external approval{named}. "
                "That signer's approval activates it."
            ),
            repair=RepairOperation(
                operation="cruxible.proposal.approve",
                arguments={
                    "proposal_id": proposal_id,
                    **({"signer_id": signers[0]} if signers else {}),
                },
            ),
        )
    if reason == "accept_never":
        return _proposed(
            proposal_id,
            reason="accept_never",
            message='not activated: the caller requested accept="never"',
            repair=activate,
            delegated=not may_activate,
        )
    return _proposed(
        proposal_id,
        reason="activation_not_permitted",
        message="not activated: the caller's credential cannot activate; GRAPH_WRITE is required",
        repair=activate,
        delegated=True,
    )
