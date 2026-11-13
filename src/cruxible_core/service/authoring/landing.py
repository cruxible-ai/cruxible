"""Land an authored change when its policy and the caller allow it."""

from cruxible_client import contracts
from cruxible_client.contracts.authoring.models import AuthoringSubmitResult
from cruxible_client.contracts.authoring_landing import (
    AuthoringAccept,
    AuthoringLanded,
    AuthoringLanding,
    AuthoringProposed,
)
from cruxible_client.contracts.candidates import CandidateMemberLawEvidence
from cruxible_client.contracts.proposal_models import AuthenticatedActor, ProposalResult
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import (
    ActivationReceipt,
    ProposalInspection,
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
        outcome = AuthoringLanded(
            proposal_id=proposal_id, accepted_coordinate=status.accepted_generation
        )
    elif status.state == "awaiting_external_approval" or any(
        item.condition == "external-approval" for item in status.path_to_acceptance
    ):
        outcome = _stopped(proposal_id, reason="independent_approval_required")
    elif status.state != "ready_to_activate" or proposal_id is None:
        outcome = AuthoringProposed(
            proposal_id=proposal_id,
            reason="preflight_refused"
            if status.state == "preflight_refused"
            else "not_ready_to_activate",
            message=f"not activated: the candidate is {status.state}, not ready_to_activate",
            next=f"cruxible authoring preflight {submitted.intent.intent_id}",
        )
    elif accept == "never":
        outcome = _stopped(proposal_id, reason="accept_never")
        if not may_activate:
            outcome = outcome.model_copy(update={"next": f"someone who can runs {outcome.next}"})
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
        return receipt, _stopped(receipt.proposal_id, reason="independent_approval_required")
    if receipt.status == "accepted" and receipt.accepted_coordinate is not None:
        return receipt, AuthoringLanded(
            proposal_id=receipt.proposal_id, accepted_coordinate=receipt.accepted_coordinate
        )
    return receipt, AuthoringProposed(
        proposal_id=receipt.proposal_id,
        reason="head_moved",
        message="not activated: accepted state moved after this proposal was checked",
        next=f"cruxible proposal readmit {receipt.proposal_id}",
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
        outcome = AuthoringProposed(
            proposal_id=proposal_id,
            reason="not_ready_to_activate",
            message="not activated: the proposal was refused; its diagnostics say why",
            next=f"cruxible get {proposal_id}",
        )
    elif getattr(proposal.candidate, "approval_requirements", ()):
        outcome = _stopped(proposal_id, reason="independent_approval_required")
    elif accept == "never":
        outcome = _stopped(proposal_id, reason="accept_never")
        if not may_activate:
            outcome = outcome.model_copy(update={"next": f"someone who can runs {outcome.next}"})
    elif not may_activate:
        outcome = _stopped(proposal_id, reason="activation_not_permitted")
    else:
        receipt, outcome = land_proposal(instance, proposal_id=proposal_id, actor_id=actor_id)
        if receipt.accepted_coordinate is not None:
            accepted = receipt.accepted_coordinate
    return inspection.model_copy(update={"outcome": outcome, "accepted_coordinate": accepted})


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


def _stopped(
    proposal_id: str | None,
    *,
    reason: str,
) -> AuthoringProposed:
    if reason == "independent_approval_required":
        return AuthoringProposed(
            proposal_id=proposal_id,
            reason="independent_approval_required",
            message=(
                "not activated: the candidate needs an external approval. "
                "The eligible signer's `cruxible proposal approve` activates it."
            ),
            next=f"cruxible proposal approve {proposal_id}",
        )
    if reason == "accept_never":
        return AuthoringProposed(
            proposal_id=proposal_id,
            reason="accept_never",
            message='not activated: the caller requested accept="never"',
            next=f"cruxible proposal activate {proposal_id}",
        )
    return AuthoringProposed(
        proposal_id=proposal_id,
        reason="activation_not_permitted",
        message="not activated: the caller's credential cannot activate; GRAPH_WRITE is required",
        next=f"someone who can runs cruxible proposal activate {proposal_id}",
    )
