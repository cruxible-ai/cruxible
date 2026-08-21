"""Durability and machine-owned identity laws for AuthoringIntent."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.models import (
    AuthoringClaimStatementV1,
    ClaimAuthoringPayloadV1,
    SelfSourceBodyV1,
)
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.claims import LiteralClaimObject
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.semantic import SemanticAddress
from tests.test_playbill._support import initialize_local

TIMESTAMP = "2026-08-21T12:00:00.000000Z"


def _payload(*, value: str = "ready") -> ClaimAuthoringPayloadV1:
    return ClaimAuthoringPayloadV1(
        statement=AuthoringClaimStatementV1(
            subject=SemanticAddress.whole_artifact("subjects/work_item/wi-42.yaml"),
            predicate="work.status",
            object=LiteralClaimObject(value=value),
            role="observation",
        ),
        rationale="The writer observed the current work status.",
        source=SelfSourceBodyV1(
            content_base64=base64.b64encode(f"status: {value}".encode()).decode("ascii")
        ),
    )


def _coordinator(tmp_path: Path) -> tuple[AuthoringIntentCoordinator, AuthenticatedActor]:
    instance, _owner = initialize_local(tmp_path)
    exhaust = instance.root / instance.descriptor.storage.exhaust
    store = AuthoringIntentStore(
        exhaust,
        token_factory=lambda: "1" * 32,
    )
    coordinator = AuthoringIntentCoordinator(
        instance=instance,
        store=store,
        claim_id_factory=lambda: "CLM-" + "2" * 32,
    )
    return coordinator, AuthenticatedActor(actor_id="owner")


def test_identical_create_retry_returns_one_intent_and_one_claim_identity(tmp_path: Path) -> None:
    coordinator, actor = _coordinator(tmp_path)

    first = coordinator.create(actor=actor, payload=_payload(), canonical_timestamp=TIMESTAMP)
    second = coordinator.create(actor=actor, payload=_payload(), canonical_timestamp=TIMESTAMP)

    assert second == first
    assert first.intent.intent_id == "AIT-" + "1" * 32
    assert first.intent.semantic_identity == "CLM-" + "2" * 32
    assert coordinator.list_pending(actor=actor).intents == (first.intent,)


def test_create_response_loss_recovers_from_published_event(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    exhaust = instance.root / instance.descriptor.storage.exhaust

    class ResponseLost(RuntimeError):
        pass

    raised = False

    def crash(boundary: str) -> None:
        nonlocal raised
        if boundary == "after_create_publish" and not raised:
            raised = True
            raise ResponseLost

    actor = AuthenticatedActor(actor_id="owner")
    failing = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(
            exhaust,
            crash_hook=crash,
            token_factory=lambda: "3" * 32,
        ),
        claim_id_factory=lambda: "CLM-" + "4" * 32,
    )
    with pytest.raises(ResponseLost):
        failing.create(actor=actor, payload=_payload(), canonical_timestamp=TIMESTAMP)

    resumed = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(exhaust, token_factory=lambda: "5" * 32),
        claim_id_factory=lambda: "CLM-" + "6" * 32,
    ).create(actor=actor, payload=_payload(), canonical_timestamp=TIMESTAMP)
    assert resumed.intent.intent_id == "AIT-" + "3" * 32
    assert resumed.intent.semantic_identity == "CLM-" + "4" * 32


def test_create_crash_before_publish_recovers_once_minted_identity(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    exhaust = instance.root / instance.descriptor.storage.exhaust

    class CrashBeforePublish(RuntimeError):
        pass

    raised = False

    def crash(boundary: str) -> None:
        nonlocal raised
        if boundary == "after_create_event_sync" and not raised:
            raised = True
            raise CrashBeforePublish

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
    with pytest.raises(CrashBeforePublish):
        failing.create(actor=actor, payload=_payload(), canonical_timestamp=TIMESTAMP)

    resumed = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(exhaust, token_factory=lambda: "9" * 32),
        claim_id_factory=lambda: "CLM-" + "a" * 32,
    ).create(actor=actor, payload=_payload(), canonical_timestamp=TIMESTAMP)
    assert resumed.intent.intent_id == "AIT-" + "7" * 32
    assert resumed.intent.semantic_identity == "CLM-" + "8" * 32


def test_resume_is_actor_scoped_and_payload_update_keeps_machine_identity(tmp_path: Path) -> None:
    coordinator, actor = _coordinator(tmp_path)
    created = coordinator.create(actor=actor, payload=_payload(), canonical_timestamp=TIMESTAMP)

    with pytest.raises(Exception, match="another actor"):
        coordinator.resume(
            created.intent.intent_id,
            actor=AuthenticatedActor(actor_id="reviewer"),
        )

    updated = coordinator.replace_payload(
        created.intent.intent_id,
        actor=actor,
        payload=_payload(value="done"),
    )
    assert updated.intent.intent_revision == 1
    assert updated.intent.semantic_identity == created.intent.semantic_identity
    assert updated.intent.candidate_status.state == "draft"
    assert coordinator.resume(created.intent.intent_id, actor=actor) == updated
