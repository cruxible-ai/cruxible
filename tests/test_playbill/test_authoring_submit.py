"""Idempotent submit and causal CandidateStatus reduction."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.captures import foreign_source_capture_contract
from cruxible_client.contracts.errors import ApprovalIntegrityError
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from tests.test_playbill._support import client_material, initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_authoring_preflight import (
    TIMESTAMP,
    _coordinator,
    _seed_claim_surface,
    _working_payload,
)


def test_submit_retry_reuses_candidate_and_status_tracks_acceptance(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(
        instance,
        owner,
        contract=foreign_source_capture_contract("repo.work-items"),
    )
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    intent = coordinator.create(
        actor=actor,
        payload=_working_payload(occurrence_count=1),
        canonical_timestamp=TIMESTAMP,
    ).intent

    first = coordinator.submit(intent.intent_id, actor=actor)
    retry = coordinator.submit(intent.intent_id, actor=actor)

    assert retry.status == first.status
    assert first.status.state == "awaiting_external_approval"
    assert first.status.proposal_id is not None
    assert first.status.candidate_digest is not None
    assert first.status.path_to_acceptance[-1].condition == "activation"
    assert first.status.path_to_acceptance[-1].satisfied is False
    assert first.status.path_to_acceptance[0].model_dump() == {
        "condition": "approval:independent-principal:1",
        "owner": "approver",
        "action": "Wait for 1 distinct non-creator Principal approval.",
        "satisfied": False,
    }

    creator_approval = _sign(
        owner,
        first.status.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    with pytest.raises(ApprovalIntegrityError, match="creator_forbidden"):
        service_submit_playbill_approval(
            instance,
            proposal_id=first.status.proposal_id,
            attestation=creator_approval.attestation,
            authenticated_submitter="owner",
        )
    assert instance.proposal_evidence().read_approvals(first.status.candidate_digest) == ()

    approval = _sign(
        client_material(tmp_path, instance),
        first.status.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    receipt = service_submit_playbill_approval(
        instance,
        proposal_id=first.status.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="reviewer",
    )
    retry_receipt = service_submit_playbill_approval(
        instance,
        proposal_id=first.status.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="reviewer",
    )
    assert retry_receipt == receipt
    assert coordinator.status(intent.intent_id, actor=actor).state == "ready_to_activate"

    activated = service_activate_playbill_proposal(
        instance,
        proposal_id=first.status.proposal_id,
    )
    assert activated.status == "accepted"
    accepted = coordinator.status(intent.intent_id, actor=actor)
    assert accepted.state == "accepted"
    assert accepted.accepted_generation == activated.accepted_coordinate
    assert coordinator.resume(intent.intent_id, actor=actor).intent.candidate_status == accepted
    assert coordinator.list_pending(actor=actor).intents == ()


def test_submit_response_loss_recovers_the_same_proposal(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(
        instance,
        owner,
        contract=foreign_source_capture_contract("repo.work-items"),
    )
    exhaust = instance.root / instance.descriptor.storage.exhaust
    transition_count = 0

    class ResponseLost(RuntimeError):
        pass

    def crash(boundary: str) -> None:
        nonlocal transition_count
        if boundary != "after_transition_event_sync":
            return
        transition_count += 1
        if transition_count == 2:
            raise ResponseLost

    actor = AuthenticatedActor(actor_id="owner")
    failing = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(
            exhaust,
            crash_hook=crash,
            token_factory=lambda: "7" * 32,
        ),
        claim_id_factory=lambda: "CLM-" + "8" * 32,
    )
    intent = failing.create(
        actor=actor,
        payload=_working_payload(occurrence_count=1),
        canonical_timestamp=TIMESTAMP,
    ).intent

    try:
        failing.submit(intent.intent_id, actor=actor)
    except ResponseLost:
        pass
    else:  # pragma: no cover - crash-hook contract
        raise AssertionError("submit response-loss hook did not fire")

    resumed = AuthoringIntentCoordinator.for_instance(instance).submit(
        intent.intent_id,
        actor=actor,
    )
    assert resumed.status.state == "awaiting_external_approval"
    assert resumed.status.proposal_id is not None
    event_paths = tuple(
        (exhaust / "authoring-intents" / intent.intent_id / "events").glob("*.json")
    )
    assert len(event_paths) == 3
