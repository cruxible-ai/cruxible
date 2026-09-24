"""A local instance keeps submitted work until it finishes, then leaves a receipt."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from cruxible_client.contracts.captures import foreign_source_capture_contract
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.authoring.store import AUTHORING_INTENTS_ENV, AuthoringIntentStore
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.service.authoring import documents
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

ACTOR = AuthenticatedActor(actor_id="owner")


@pytest.fixture(autouse=True)
def _retention_off(monkeypatch):
    monkeypatch.delenv(AUTHORING_INTENTS_ENV, raising=False)


def _submitted(tmp_path: Path):
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(
        instance, owner, contract=foreign_source_capture_contract("repo.work-items")
    )
    coordinator = _coordinator(instance)
    intent = coordinator.create(
        actor=ACTOR,
        payload=_working_payload(occurrence_count=1),
        canonical_timestamp=TIMESTAMP,
    ).intent
    submitted = coordinator.submit(intent.intent_id, actor=ACTOR)
    assert submitted.status.proposal_id is not None
    return instance, owner, coordinator, intent.intent_id, submitted.status


def _activate(instance, owner, status) -> None:
    approval = _sign(owner, status.candidate_digest, instance.accepted_coordinate().semantic_root)
    service_submit_playbill_approval(
        instance,
        proposal_id=status.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="owner",
    )
    activated = service_activate_playbill_proposal(
        instance, proposal_id=status.proposal_id, activated_by="owner"
    )
    assert activated.status == "accepted"


def _exhaust(instance) -> Path:
    return instance.root / instance.descriptor.storage.exhaust / "authoring-intents"


def test_acceptance_through_the_proposal_leaves_only_a_receipt(tmp_path: Path) -> None:
    instance, owner, coordinator, intent_id, status = _submitted(tmp_path)
    _activate(instance, owner, status)
    assert not (_exhaust(instance) / intent_id).exists()
    assert (_exhaust(instance) / ".finished" / f"{intent_id}.json").is_file()
    accepted = coordinator.status(intent_id, actor=ACTOR)
    assert accepted.state == "accepted"
    retry = coordinator.submit(intent_id, actor=ACTOR)
    assert retry.status.state == "accepted"


def test_a_crash_before_compaction_still_leaves_the_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, owner, _coordinator_, intent_id, status = _submitted(tmp_path)
    monkeypatch.setattr(documents, "_finalize_completed_intents", lambda instance: None)
    _activate(instance, owner, status)

    class Crashed(RuntimeError):
        pass

    def crash(boundary: str) -> None:
        if boundary == "after_transition_event_sync":
            raise Crashed

    exhaust = instance.root / instance.descriptor.storage.exhaust
    failing = AuthoringIntentCoordinator(
        instance=instance, store=AuthoringIntentStore(exhaust, crash_hook=crash)
    )
    with pytest.raises(Crashed):
        failing.finalize_completed()
    # The terminal event is durable but the stream was not compacted.
    assert (_exhaust(instance) / intent_id).exists()

    reopened = AuthoringIntentCoordinator.for_instance(instance)
    reopened.create(
        actor=ACTOR, payload=_working_payload(occurrence_count=2), canonical_timestamp=TIMESTAMP
    )
    assert not (_exhaust(instance) / intent_id).exists()
    assert reopened.status(intent_id, actor=ACTOR).state == "accepted"
    assert reopened.submit(intent_id, actor=ACTOR).status.state == "accepted"


def test_a_submitted_intent_outlives_the_draft_expiry(tmp_path: Path) -> None:
    instance, _owner, coordinator, intent_id, _status = _submitted(tmp_path)
    old = time.time() - 2 * 24 * 60 * 60
    for event in (_exhaust(instance) / intent_id / "events").iterdir():
        os.utime(event, (old, old))
    AuthoringIntentCoordinator.for_instance(instance).create(
        actor=ACTOR, payload=_working_payload(occurrence_count=2), canonical_timestamp=TIMESTAMP
    )
    assert (_exhaust(instance) / intent_id).exists()
    assert coordinator.status(intent_id, actor=ACTOR).state == "ready_to_activate"


def test_racing_completions_finish_once_and_a_new_draft_still_creates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, owner, _coordinator_, intent_id, status = _submitted(tmp_path)
    monkeypatch.setattr(documents, "_finalize_completed_intents", lambda instance: None)
    _activate(instance, owner, status)
    first = AuthoringIntentCoordinator.for_instance(instance)
    second = AuthoringIntentCoordinator.for_instance(instance)
    # Both callers select the accepted intent before either compacts it.
    selected = second.store.submitted_pending()
    assert [item.intent_id for item in selected] == [intent_id]
    first.finalize_completed()
    assert not (_exhaust(instance) / intent_id).exists()
    monkeypatch.setattr(second.store, "submitted_pending", lambda: selected)
    second.finalize_completed()  # the receipt is the answer, not an error
    created = second.create(
        actor=ACTOR, payload=_working_payload(occurrence_count=2), canonical_timestamp=TIMESTAMP
    )
    assert created.intent.intent_id != intent_id
    assert second.status(intent_id, actor=ACTOR).state == "accepted"
