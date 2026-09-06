"""Durability and machine-owned identity laws for AuthoringIntent."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from cruxible_client.contracts.authoring.models import (
    AuthoringClaimStatementV1,
    ChangeSetAuthoringPayloadV1,
    ClaimAuthoringPayloadV1,
    SelfSourceBodyV1,
    SubjectAuthoringPayloadV1,
    authoring_payload_digest,
)
from cruxible_client.contracts.claims import LiteralClaimObject
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.proposals import AuthenticatedActor
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_authoring_change_set_intents import _shell

TIMESTAMP = "2026-08-21T12:00:00.000000Z"


def _payload(*, value: str = "ready") -> ClaimAuthoringPayloadV1:
    return ClaimAuthoringPayloadV1(
        statement=AuthoringClaimStatementV1(
            subject=SemanticAddress.whole_artifact("subjects/work_item/wi-42.json"),
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


def _prose_set(rationale: str) -> ChangeSetAuthoringPayloadV1:
    """One change set whose members never move and whose prose does."""

    return ChangeSetAuthoringPayloadV1(
        members=(SubjectAuthoringPayloadV1(subject=_shell("wi-42")),),
        rationale=rationale,
    )


def test_two_rationale_only_replacements_are_two_revisions_and_not_one_replay(
    tmp_path: Path,
) -> None:
    """Rewriting only the prose must not read as a retry of the rewrite before it.

    `replace_payload`'s idempotency key names the create fingerprint rather than
    the payload digest, because a change set's payload digest drops its
    rationale and nothing in that preimage is revision-scoped: one key for two
    different requests would swallow the second for the life of the intent and
    hand its author a success view carrying the prose it had just replaced.
    """

    coordinator, actor = _coordinator(tmp_path)
    first = _prose_set("The first prose.")
    second = _prose_set("Second prose, deliberately different.")
    third = _prose_set("Third prose, corrected again.")
    assert (
        authoring_payload_digest(first)
        == authoring_payload_digest(second)
        == authoring_payload_digest(third)
    )

    created = coordinator.create(actor=actor, payload=first, canonical_timestamp=TIMESTAMP)
    intent_id = created.intent.intent_id
    after_second = coordinator.replace_payload(intent_id, actor=actor, payload=second)
    after_third = coordinator.replace_payload(intent_id, actor=actor, payload=third)

    assert after_second.intent.intent_revision == 1
    assert after_second.intent.payload.rationale == "Second prose, deliberately different."
    assert after_third.intent.intent_revision == 2
    assert after_third.intent.payload.rationale == "Third prose, corrected again."

    # The read path, not only the view the caller was handed: this is where a
    # payload shared under a colliding digest used to return the older prose.
    resumed = coordinator.resume(intent_id, actor=actor)
    assert resumed.intent.payload.rationale == "Third prose, corrected again."
    assert [event.intent.payload.rationale for event in coordinator.store.events()] == [
        "The first prose.",
        "Second prose, deliberately different.",
        "Third prose, corrected again.",
    ]

    # And on disk, independently of any decode-time sharing.
    events = sorted((coordinator.store.root / intent_id / "events").glob("*.json"))
    assert len(events) == 3
    assert b"Third prose, corrected again." in events[-1].read_bytes()

    # A genuine retry of one request is still one operation.
    retry = coordinator.replace_payload(intent_id, actor=actor, payload=third)
    assert retry == after_third
    assert len(sorted((coordinator.store.root / intent_id / "events").glob("*.json"))) == 3
