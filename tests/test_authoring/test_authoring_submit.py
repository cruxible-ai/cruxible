"""Idempotent submit and causal CandidateStatus reduction."""

from __future__ import annotations

from pathlib import Path

from cruxible_client.contracts.captures import foreign_source_capture_contract
from cruxible_client.contracts.workspace_advertisement import WorkspaceAdvertisement
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.authoring.store import AuthoringIntentStore
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.service.authoring.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from tests.core_support._support import initialize_local
from tests.test_authoring.test_authoring_preflight import (
    TIMESTAMP,
    _coordinator,
    _seed_claim_surface,
    _working_payload,
)
from tests.test_ledger.test_activation import _sign


def test_submit_retry_reuses_candidate_and_status_tracks_acceptance(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(
        instance,
        owner,
        contract=foreign_source_capture_contract("repo.work-items"),
    )
    advertisements = 0
    # Named rather than counted: the approval door refreshes the workspace lane
    # too, so an index would silently move this failure onto a different door.
    failing = False

    def advertise() -> WorkspaceAdvertisement:
        nonlocal advertisements
        advertisements += 1
        if failing:
            raise MemoryError("simulated activation advertisement failure")
        return WorkspaceAdvertisement(
            status="failed",
            workspace_path=str(tmp_path),
            failure_code="fetch_failed",
        )

    instance.bind_workspace_advertiser(advertise, workspace_path=tmp_path)
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
    # Advertisement runs after the response; the settled outcome reports it.
    assert first.workspace_advertisement.status == "scheduled"
    assert retry.workspace_advertisement.status == "scheduled"
    assert instance.settled_workspace_advertisement().failure_code == "fetch_failed"
    assert advertisements >= 1
    assert first.status.state == "ready_to_activate"
    assert first.status.proposal_id is not None
    assert first.status.candidate_digest is not None
    assert first.status.path_to_acceptance[-1].condition == "activation"
    assert first.status.path_to_acceptance[-1].satisfied is False
    assert tuple(item.condition for item in first.status.path_to_acceptance) == ("activation",)

    creator_approval = _sign(
        owner,
        first.status.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    receipt = service_submit_playbill_approval(
        instance,
        proposal_id=first.status.proposal_id,
        attestation=creator_approval.attestation,
        authenticated_submitter="owner",
    )
    retry_receipt = service_submit_playbill_approval(
        instance,
        proposal_id=first.status.proposal_id,
        attestation=creator_approval.attestation,
        authenticated_submitter="owner",
    )
    assert retry_receipt == receipt
    assert coordinator.status(intent.intent_id, actor=actor).state == "ready_to_activate"

    instance.settled_workspace_advertisement()
    failing = True
    activated = service_activate_playbill_proposal(
        instance,
        proposal_id=first.status.proposal_id,
        activated_by="owner",
    )
    assert activated.status == "accepted"
    assert activated.workspace_advertisement.status == "scheduled"
    settled = instance.settled_workspace_advertisement()
    assert settled.status == "failed"
    assert settled.failure_code == "unexpected_failure"
    accepted = coordinator.status(intent.intent_id, actor=actor)
    assert accepted.state == "accepted"
    assert accepted.accepted_generation == activated.accepted_coordinate
    assert coordinator.get(intent.intent_id, actor=actor).intent.candidate_status == accepted
    # (The world's fixture intents that created its Subjects stay where they are.)
    assert intent.intent_id not in {
        item.intent_id for item in coordinator.list_pending(actor=actor).intents
    }


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
    assert resumed.status.state == "ready_to_activate"
    assert resumed.status.proposal_id is not None
    event_paths = tuple(
        (exhaust / "authoring-intents" / intent.intent_id / "events").glob("*.json")
    )
    assert len(event_paths) == 3


def test_revising_a_submitted_intent_is_a_typed_refusal_naming_a_new_intent(
    tmp_path: Path,
) -> None:
    """gapc-21: compile --intent-id / reprepare on a submitted intent answered a bare 500."""

    import pytest

    from cruxible_client.contracts.repairs import RepairOperation
    from cruxible_core.authoring.store import AuthoringIntentRefused
    from cruxible_core.server.errors import error_to_response

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
    submitted = coordinator.submit(intent.intent_id, actor=actor)
    assert submitted.status.state == "ready_to_activate"

    with pytest.raises(AuthoringIntentRefused) as refused:
        coordinator.replace_payload(
            intent.intent_id, actor=actor, payload=_working_payload(occurrence_count=1)
        )

    assert refused.value.error_code == "cruxible.authoring.intent_submitted"
    assert str(submitted.status.proposal_id) in str(refused.value)
    status, body = error_to_response(refused.value)
    assert status == 409
    assert body.error_code == "cruxible.authoring.intent_submitted"
    assert body.repair == RepairOperation(operation="cruxible.authoring.compile")
    # Nothing was appended: the intent is still the submitted one.
    assert coordinator.status(intent.intent_id, actor=actor).state == "ready_to_activate"


def test_an_unknown_or_foreign_intent_is_a_typed_refusal(tmp_path: Path) -> None:
    """gapc-22: status/get of an unknown intent id answered a bare 500."""

    import pytest

    from cruxible_client.contracts.repairs import RepairOperation
    from cruxible_core.authoring.store import AuthoringIntentRefused, AuthoringIntentStoreError
    from cruxible_core.server.errors import error_to_response

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(
        instance,
        owner,
        contract=foreign_source_capture_contract("repo.work-items"),
    )
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    for unknown in ("AIT-" + "0" * 32, "nope", ".."):
        for call in (coordinator.status, coordinator.get, coordinator.preflight):
            with pytest.raises(AuthoringIntentRefused) as refused:
                call(unknown, actor=actor)
            assert refused.value.error_code == "cruxible.authoring.intent_not_found"
            status, body = error_to_response(refused.value)
            assert status == 404
            assert body.repair == RepairOperation(operation="cruxible.authoring.list")
    with pytest.raises(AuthoringIntentRefused, match="intent_not_found"):
        coordinator.replace_payload(
            "AIT-" + "0" * 32, actor=actor, payload=_working_payload(occurrence_count=1)
        )

    intent = coordinator.create(
        actor=actor,
        payload=_working_payload(occurrence_count=1),
        canonical_timestamp=TIMESTAMP,
    ).intent
    with pytest.raises(AuthoringIntentRefused) as foreign:
        coordinator.status(intent.intent_id, actor=AuthenticatedActor(actor_id="other"))
    assert foreign.value.error_code == "cruxible.authoring.intent_other_actor"
    assert error_to_response(foreign.value)[0] == 403
    # Callers that read a missing intent as nothing to do still catch it.
    assert issubclass(AuthoringIntentRefused, AuthoringIntentStoreError)


def test_a_refused_one_shot_submit_lands_as_refused_naming_a_revised_payload(
    tmp_path: Path,
) -> None:
    """gapc-19: a refused submit read `proposed` with a preflight rerun as its next step."""

    from cruxible_client import contracts
    from cruxible_core.service.authoring.landing import service_land_authoring_submit

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(
        instance,
        owner,
        contract=foreign_source_capture_contract("repo.work-items"),
    )
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    submitted = coordinator.compile_and_submit(
        actor=actor,
        payload=_working_payload(occurrence_count=2),
        canonical_timestamp=TIMESTAMP,
    )
    assert submitted.status.state == "preflight_refused"

    record = service_land_authoring_submit(
        coordinator, actor=actor, submitted=submitted, accept="if_allowed", may_activate=True
    )

    assert isinstance(record.outcome, contracts.AuthoringRefused)
    assert record.outcome.status == "refused"
    assert record.outcome.next == (
        f"cruxible authoring submit PAYLOAD --intent-id {submitted.intent.intent_id}"
    )
    assert record.status.proposal_id is None
