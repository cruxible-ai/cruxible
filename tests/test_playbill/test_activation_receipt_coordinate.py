"""Acceptance receipts identify their own generation despite later publication."""

from pathlib import Path

from cruxible_client.contracts.workspace_advertisement import (
    NOT_ATTACHED_ADVERTISEMENT,
    PlaybillWorkspaceAdvertisement,
)
from cruxible_core.playbill.service.documents import (
    PlaybillAcceptedCoordinate,
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from tests.test_playbill._candidate_support import submit_query_definition_candidate
from tests.test_playbill._knowledge_loop_support import (
    TIMESTAMP,
    accept_proposal,
    seed_claims,
    work_item_query,
)
from tests.test_playbill._support import client_material
from tests.test_playbill.test_activation import _sign


def test_receipt_keeps_own_generation_when_advertisement_observes_later_acceptance(
    tmp_path: Path,
) -> None:
    instance, owner = seed_claims(tmp_path)
    first = submit_query_definition_candidate(
        instance,
        query=work_item_query("first.query"),
        actor_id="owner",
        proposal_name="first-query",
        timestamp=TIMESTAMP,
    )
    candidate = first.proposal.candidate
    assert candidate is not None
    approval = _sign(
        client_material(instance.root.parent, instance),
        candidate.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=first.proposal.admission.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="reviewer",
    )
    first_coordinate: PlaybillAcceptedCoordinate | None = None

    def publish_later_generation() -> PlaybillWorkspaceAdvertisement:
        nonlocal first_coordinate
        if first_coordinate is not None:
            return NOT_ATTACHED_ADVERTISEMENT
        first_coordinate = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())
        # The activation lock is released before advertisement. Model a second
        # writer publishing in that interval using a real subsequent generation.
        second = submit_query_definition_candidate(
            instance,
            query=work_item_query("second.query"),
            actor_id="owner",
            proposal_name="second-query",
            timestamp=TIMESTAMP,
        )
        accept_proposal(instance, owner, second)
        return NOT_ATTACHED_ADVERTISEMENT

    instance.bind_workspace_advertiser(publish_later_generation)
    receipt = service_activate_playbill_proposal(
        instance,
        proposal_id=first.proposal.admission.proposal_id,
        activated_by="owner",
    )

    assert receipt.status == "accepted"
    assert receipt.accepted_coordinate == first_coordinate
    assert receipt.accepted_coordinate != PlaybillAcceptedCoordinate.from_internal(
        instance.accepted_coordinate()
    )
