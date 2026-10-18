"""Land a submitted authoring intent when its policy and the caller allow it."""

from cruxible_client import contracts
from cruxible_client.contracts.authoring.models import AuthoringSubmitResult
from cruxible_client.contracts.authoring_landing import (
    AuthoringAccept,
    AuthoringLanded,
    AuthoringLanding,
    AuthoringProposed,
)
from cruxible_client.contracts.proposal_models import AuthenticatedActor
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal


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
    elif not may_activate:
        outcome = _stopped(proposal_id, reason="activation_not_permitted")
    else:
        receipt = service_activate_playbill_proposal(
            coordinator.instance, proposal_id=proposal_id, activated_by=actor.actor_id
        )
        if isinstance(receipt, contracts.ProposalRecompiled):
            outcome = _stopped(receipt.proposal_id, reason="independent_approval_required")
            status = status.model_copy(
                update={
                    "state": "awaiting_external_approval",
                    "proposal_id": receipt.proposal_id,
                    "candidate_digest": receipt.candidate_digest,
                    "current_accepted_coordinate": receipt.accepted_coordinate,
                }
            )
        elif receipt.status == "accepted" and receipt.accepted_coordinate is not None:
            outcome = AuthoringLanded(
                proposal_id=receipt.proposal_id, accepted_coordinate=receipt.accepted_coordinate
            )
            status = status.model_copy(
                update={
                    "state": "accepted",
                    "proposal_id": receipt.proposal_id,
                    "accepted_generation": receipt.accepted_coordinate,
                    "current_accepted_coordinate": receipt.accepted_coordinate,
                    "path_to_acceptance": (),
                }
            )
        else:
            outcome = AuthoringProposed(
                proposal_id=receipt.proposal_id,
                reason="head_moved",
                message="not activated: accepted state moved after this proposal was checked",
                next=f"cruxible proposal readmit {receipt.proposal_id}",
            )
    data = submitted.model_dump(mode="json")
    data["outcome"] = outcome.model_dump(mode="json")
    data["status"] = status.model_dump(mode="json")
    data["intent"]["candidate_status"] = data["status"]
    return contracts.AuthoringSubmitResultRecord.model_validate(data)


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
