"""Frozen insertion wire and durable expectation state laws."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.insertions import (
    InsertionProtocolError,
    mark_abandoned,
    mark_claim_accepted,
    mark_expired,
)
from cruxible_core.playbill.authoring.models import (
    AuthoringClaimStatementV1,
    ClaimAuthoringPayloadV1,
    InsertionAnchorWindowV1,
    InsertionConfirmationObservationV1,
    InsertionTargetV1,
    SelfSourceBodyV1,
    WorkingDigestCoordinateV1,
    build_insertion_expectation,
    build_insertion_patch_envelope,
    build_insertion_terminal_tombstone,
    insertion_expectation_id,
    insertion_result_key,
    insertion_target_digest,
)
from cruxible_core.playbill.claims import LiteralClaimObject
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.semantic import SemanticAddress
from tests.test_playbill._support import initialize_local

TIMESTAMP = "2026-08-21T12:00:00.000000Z"


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _target(*, occurrence_count: int = 1) -> InsertionTargetV1:
    preimage = b"status: "
    body = b"ready"
    postimage = preimage + body
    return InsertionTargetV1(
        source_id="repo.work-items",
        coordinate=WorkingDigestCoordinateV1(
            source_content_digest=_digest(preimage),
            source_byte_length=len(preimage),
        ),
        preimage_digest=_digest(preimage),
        selector=InsertionAnchorWindowV1(
            anchor_content_base64=base64.b64encode(preimage).decode("ascii"),
            anchor_bytes_digest=_digest(preimage),
            start_byte=0,
            end_byte=len(preimage),
            insertion_offset=len(preimage),
            observed_occurrence_count=occurrence_count,
        ),
        operation="insert_after",
        postimage_digest=_digest(postimage),
        postimage_byte_length=len(postimage),
    )


def _payload() -> ClaimAuthoringPayloadV1:
    return ClaimAuthoringPayloadV1(
        statement=AuthoringClaimStatementV1(
            subject=SemanticAddress.whole_artifact("subjects/work_item/wi-42.yaml"),
            predicate="work.status",
            object=LiteralClaimObject(value="ready"),
            role="observation",
        ),
        rationale="Publish the governed status beside the work item.",
        source=SelfSourceBodyV1(content_base64=base64.b64encode(b"ready").decode("ascii")),
        insertion_target=_target(),
    )


def _expectation(*, instance_id: str, intent_id: str):
    target = _target()
    expires_at = datetime(2026, 8, 28, 12, tzinfo=UTC)
    patch = build_insertion_patch_envelope(
        source_id=target.source_id,
        preimage_digest=target.preimage_digest,
        preimage_byte_length=target.coordinate.source_byte_length,
        selector=target.selector,
        operation=target.operation,
        body_digest=_digest(b"ready"),
        body_byte_length=5,
        postimage_digest=target.postimage_digest,
        postimage_byte_length=target.postimage_byte_length,
        target_digest=insertion_target_digest(target),
        expires_at=expires_at,
    )
    return build_insertion_expectation(
        expectation_id=insertion_expectation_id(
            instance_id=instance_id,
            intent_id=intent_id,
            intent_revision=0,
        ),
        state="awaiting_claim_acceptance",
        claim_identity="CLM-" + "2" * 32,
        original_claim_artifact_digest=_digest(b"claim"),
        claim_statement_digest=_digest(b"statement"),
        patch=patch,
    )


def test_target_commits_exact_anchor_but_keeps_whole_source_observation_honest() -> None:
    target = _target()

    assert target.coordinate.kind == "observed_digest"
    assert target.selector.content == b"status: "
    assert insertion_target_digest(target).startswith("sha256:")

    with pytest.raises(ValueError, match="exactly one observed occurrence"):
        _target(occurrence_count=2)


def test_patch_length_correspondence_is_daemon_verified() -> None:
    target = _target()
    with pytest.raises(ValueError, match="byte-length arithmetic"):
        build_insertion_patch_envelope(
            source_id=target.source_id,
            preimage_digest=target.preimage_digest,
            preimage_byte_length=target.coordinate.source_byte_length,
            selector=target.selector,
            operation=target.operation,
            body_digest=_digest(b"ready"),
            body_byte_length=5,
            postimage_digest=target.postimage_digest,
            postimage_byte_length=999,
            target_digest=insertion_target_digest(target),
            expires_at=datetime(2026, 8, 28, 12, tzinfo=UTC),
        )


def test_expectation_and_terminal_tombstone_are_self_digesting() -> None:
    expectation = _expectation(instance_id="inst-test", intent_id="AIT-" + "1" * 32)
    finalized = expectation.patch.expires_at
    citation_id = _digest(b"citation")
    observation = InsertionConfirmationObservationV1(
        expectation_id=expectation.expectation_id,
        source_id="repo.work-items",
        coordinate=WorkingDigestCoordinateV1(
            source_content_digest=_digest(b"status: ready"),
            source_byte_length=len(b"status: ready"),
        ),
        observed_content_digest=_digest(b"status: ready"),
        selected_start_byte=8,
        selected_end_byte=13,
        selected_bytes_digest=_digest(b"ready"),
        observed_occurrence_count=1,
    )
    tombstone = build_insertion_terminal_tombstone(
        result_key=insertion_result_key(
            instance_id="inst-test",
            actor_id="owner",
            intent_id="AIT-" + "1" * 32,
            expectation_id=expectation.expectation_id,
        ),
        intent_id="AIT-" + "1" * 32,
        expectation_id=expectation.expectation_id,
        final_state="bound",
        final_result="bound",
        citation_id=citation_id,
        successor_candidate_ref="refs/proposals/owner/intent-" + "1" * 32 + "-publication",
        finalized_at=finalized,
        retain_until=finalized + timedelta(days=30),
        patch_envelope_digest=expectation.patch.envelope_digest,
    )
    bound = build_insertion_expectation(
        expectation_id=expectation.expectation_id,
        state="bound",
        claim_identity=expectation.claim_identity,
        original_claim_artifact_digest=expectation.original_claim_artifact_digest,
        claim_statement_digest=expectation.claim_statement_digest,
        patch=expectation.patch,
        confirmation_observation=observation,
        citation_id=citation_id,
        terminal_tombstone=tombstone,
    )

    assert bound.terminal_tombstone == tombstone
    assert bound.expectation_digest != expectation.expectation_digest


def test_state_reducer_orders_acceptance_expiry_and_abandonment(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    coordinator = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentCoordinator.for_instance(instance).store,
        claim_id_factory=lambda: "CLM-" + "2" * 32,
    )
    intent = coordinator.create(
        actor=AuthenticatedActor(actor_id="owner"),
        payload=_payload(),
        canonical_timestamp=TIMESTAMP,
    ).intent
    waiting = _expectation(
        instance_id=instance.descriptor.instance_id,
        intent_id=intent.intent_id,
    )
    pending = mark_claim_accepted(waiting)

    with pytest.raises(InsertionProtocolError, match="not reached"):
        mark_expired(
            intent, pending, evaluation_time=pending.patch.expires_at - timedelta(seconds=1)
        )

    abandoned = mark_abandoned(intent, pending, finalized_at=pending.patch.expires_at)
    assert abandoned.state == "abandoned"
    assert abandoned.terminal_tombstone is not None

    assert mark_abandoned(intent, abandoned, finalized_at=pending.patch.expires_at) == abandoned


def test_expectation_round_trips_in_the_append_only_intent_log(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    coordinator = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentCoordinator.for_instance(instance).store,
        claim_id_factory=lambda: "CLM-" + "2" * 32,
    )
    actor = AuthenticatedActor(actor_id="owner")
    intent = coordinator.create(
        actor=actor,
        payload=_payload(),
        canonical_timestamp=TIMESTAMP,
    ).intent
    expectation = _expectation(
        instance_id=instance.descriptor.instance_id,
        intent_id=intent.intent_id,
    )

    coordinator.store.transition(
        intent.intent_id,
        actor_id="owner",
        operation_key=_digest(b"mint-expectation"),
        transform=lambda current: current.model_copy(update={"insertion_expectation": expectation}),
    )

    resumed = coordinator.resume(intent.intent_id, actor=actor).intent
    assert resumed.insertion_expectation == expectation
