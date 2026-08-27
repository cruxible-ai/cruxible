"""Frozen insertion wire and durable expectation state laws."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cruxible_client.authoring.insertions import (
    PlaybillInsertionApplyError,
    apply_playbill_insertion,
)
from cruxible_client.contracts.authoring.models import (
    AuthoringClaimStatementV1,
    AuthoringExistingClaimDispositionV1,
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
from cruxible_client.contracts.claims import (
    ClaimArtifactV2,
    LiteralClaimObject,
    claim_path,
    parse_claim,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.insertions import (
    InsertionProtocolError,
    mark_abandoned,
    mark_claim_accepted,
    mark_expired,
)
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from tests.test_playbill._support import client_material, initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_authoring_preflight import _seed_claim_surface
from tests.test_playbill.test_claims import _claim_type, _subject

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
            subject=SemanticAddress.whole_artifact(
                f"subjects/{_subject().subject_kind}/{_subject().subject_id}.yaml"
            ),
            predicate=_claim_type().predicate,
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


def _observation(expectation_id: str, *, occurrence_count: int = 1):
    postimage = b"status: ready"
    return InsertionConfirmationObservationV1(
        expectation_id=expectation_id,
        source_id="repo.work-items",
        coordinate=WorkingDigestCoordinateV1(
            source_content_digest=_digest(postimage),
            source_byte_length=len(postimage),
        ),
        observed_content_digest=_digest(postimage),
        selected_start_byte=8,
        selected_end_byte=13,
        selected_bytes_digest=_digest(b"ready"),
        observed_occurrence_count=occurrence_count,
    )


def _activate(
    instance: PlaybillInstance,
    owner: object,
    *,
    proposal_id: str,
    candidate_digest: str,
) -> None:
    approver = client_material(instance.root.parent, instance)
    approval = _sign(
        approver,
        candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=proposal_id,
        attestation=approval.attestation,
        authenticated_submitter=approver.principal.principal_id,
    )
    activated = service_activate_playbill_proposal(instance, proposal_id=proposal_id)
    assert activated.status == "accepted"


def _submitted_insertion(
    tmp_path: Path,
) -> tuple[PlaybillInstance, object, AuthoringIntentCoordinator, AuthenticatedActor, str]:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(
            instance.root / instance.descriptor.storage.exhaust,
            token_factory=lambda: "1" * 32,
        ),
        claim_id_factory=lambda: "CLM-" + "2" * 32,
    )
    actor = AuthenticatedActor(actor_id="owner")
    intent = coordinator.create(
        actor=actor,
        payload=_payload(),
        canonical_timestamp=TIMESTAMP,
    ).intent
    submitted = coordinator.submit(intent.intent_id, actor=actor)
    assert submitted.status.proposal_id is not None
    assert submitted.status.candidate_digest is not None
    assert submitted.intent.insertion_expectation is not None
    assert submitted.intent.insertion_expectation.state == "awaiting_claim_acceptance"
    _activate(
        instance,
        owner,
        proposal_id=submitted.status.proposal_id,
        candidate_digest=submitted.status.candidate_digest,
    )
    resumed = coordinator.resume(intent.intent_id, actor=actor).intent
    assert resumed.insertion_expectation is not None
    assert resumed.insertion_expectation.state == "pending"
    return instance, owner, coordinator, actor, intent.intent_id


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


def test_client_patch_adapter_applies_once_and_recovers_postimage_retry() -> None:
    expectation = _expectation(instance_id="inst-test", intent_id="AIT-" + "1" * 32)

    applied = apply_playbill_insertion(
        b"status: ",
        expectation=expectation.model_dump(mode="json"),
        retained_body=b"ready",
    )
    retry = apply_playbill_insertion(
        applied.content,
        expectation=expectation.model_dump(mode="json"),
        retained_body=b"ready",
    )

    assert applied.outcome == "applied"
    assert retry.outcome == "already_applied"
    assert retry.content == b"status: ready"
    assert retry.observation == applied.observation
    with pytest.raises(PlaybillInsertionApplyError, match="neither patch preimage nor postimage"):
        apply_playbill_insertion(
            b"status: done",
            expectation=expectation.model_dump(mode="json"),
            retained_body=b"ready",
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


def test_confirm_retry_resumes_one_successor_then_binds_after_activation(
    tmp_path: Path,
) -> None:
    instance, owner, coordinator, actor, intent_id = _submitted_insertion(tmp_path)
    expectation = coordinator.resume(intent_id, actor=actor).intent.insertion_expectation
    assert expectation is not None
    observation = _observation(expectation.expectation_id)

    first = coordinator.confirm_insertion(intent_id, actor=actor, observation=observation)
    retry = coordinator.confirm_insertion(intent_id, actor=actor, observation=observation)

    assert first.outcome == retry.outcome == "backing_candidate_pending"
    assert first.expectation.successor_candidate_digest is not None
    assert (
        retry.expectation.successor_candidate_digest == first.expectation.successor_candidate_digest
    )
    assert retry.expectation.successor_proposal_id == first.expectation.successor_proposal_id
    assert first.successor_status is not None
    assert first.successor_status.proposal_id is not None
    assert first.successor_status.candidate_digest is not None

    _activate(
        instance,
        owner,
        proposal_id=first.successor_status.proposal_id,
        candidate_digest=first.successor_status.candidate_digest,
    )
    after_loss = coordinator.confirm_insertion(intent_id, actor=actor, observation=observation)
    final_retry = coordinator.confirm_insertion(intent_id, actor=actor, observation=observation)

    assert after_loss.outcome == "bound"
    assert final_retry.outcome == "already_bound"
    assert final_retry.expectation.terminal_tombstone is not None
    current_path = claim_path(final_retry.intent.semantic_identity)
    claim = parse_claim(
        instance.tree_at(instance.accepted_coordinate().git_oid)[current_path],
        path=current_path,
    )
    assert isinstance(claim, ClaimArtifactV2)
    assert ("copy", "self_published") in {
        (item.role, item.origin) for item in claim.backing.citations
    }


def test_confirmation_correspondence_refuses_ambiguity_and_stale_target(
    tmp_path: Path,
) -> None:
    _instance, _owner, coordinator, actor, intent_id = _submitted_insertion(tmp_path)
    expectation = coordinator.resume(intent_id, actor=actor).intent.insertion_expectation
    assert expectation is not None

    ambiguous = coordinator.confirm_insertion(
        intent_id,
        actor=actor,
        observation=_observation(expectation.expectation_id, occurrence_count=2),
    )
    stale = coordinator.confirm_insertion(
        intent_id,
        actor=actor,
        observation=_observation(expectation.expectation_id).model_copy(
            update={"source_id": "repo.other-items"}
        ),
    )

    assert ambiguous.outcome == "ambiguous"
    assert stale.outcome == "stale_target"
    assert coordinator.resume(intent_id, actor=actor).intent.insertion_expectation == expectation


def test_confirmation_response_loss_after_event_publish_resumes_same_candidate(
    tmp_path: Path,
) -> None:
    instance, _owner, coordinator, actor, intent_id = _submitted_insertion(tmp_path)
    expectation = coordinator.resume(intent_id, actor=actor).intent.insertion_expectation
    assert expectation is not None
    transition_count = 0

    class ResponseLost(RuntimeError):
        pass

    def crash(boundary: str) -> None:
        nonlocal transition_count
        if boundary != "after_transition_event_sync":
            return
        transition_count += 1
        if transition_count == 1:
            raise ResponseLost

    failing = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(
            instance.root / instance.descriptor.storage.exhaust,
            crash_hook=crash,
        ),
    )
    observation = _observation(expectation.expectation_id)
    with pytest.raises(ResponseLost):
        failing.confirm_insertion(intent_id, actor=actor, observation=observation)

    resumed = coordinator.confirm_insertion(intent_id, actor=actor, observation=observation)
    assert resumed.outcome == "backing_candidate_pending"
    assert resumed.expectation.successor_candidate_digest is not None
    proposals = tuple(
        (instance.root / instance.descriptor.storage.exhaust / "proposals").glob("*.json")
    )
    assert len(proposals) >= 2


def test_expiry_and_abandonment_are_terminal_and_keep_idempotency_tombstones(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, _owner)
    actor = AuthenticatedActor(actor_id="owner")
    now = datetime(2026, 8, 21, 12, tzinfo=UTC)
    coordinator = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentCoordinator.for_instance(instance).store,
        claim_id_factory=lambda: "CLM-" + "2" * 32,
        clock=lambda: now,
    )
    intent = coordinator.create(
        actor=actor,
        payload=_payload(),
        canonical_timestamp=TIMESTAMP,
    ).intent
    submitted = coordinator.submit(intent.intent_id, actor=actor)
    abandoned = coordinator.abandon_insertion(intent.intent_id, actor=actor)
    retry = coordinator.abandon_insertion(intent.intent_id, actor=actor)
    assert abandoned.expectation.state == retry.expectation.state == "abandoned"
    assert abandoned.expectation.terminal_tombstone == retry.expectation.terminal_tombstone

    other = coordinator.create(
        actor=actor,
        payload=_payload().model_copy(
            update={
                "statement": _payload().statement.model_copy(
                    update={"object": LiteralClaimObject(value="done")}
                )
            }
        ),
        canonical_timestamp="2026-08-21T12:00:01.000000Z",
    ).intent
    coordinator.submit(other.intent_id, actor=actor)
    waiting = coordinator.store.get(other.intent_id, actor_id="owner")
    assert waiting.insertion_expectation is not None
    expired_clock = waiting.insertion_expectation.patch.expires_at + timedelta(seconds=1)
    expired_coordinator = AuthoringIntentCoordinator(
        instance=instance,
        store=coordinator.store,
        clock=lambda: expired_clock,
    )
    expired = expired_coordinator.resume(other.intent_id, actor=actor).intent
    assert expired.insertion_expectation is not None
    assert expired.insertion_expectation.state == "expired"
    assert submitted.intent.insertion_expectation is not None


def _successor_payload(claim_id: str, *, value: str) -> ClaimAuthoringPayloadV1:
    return _payload().model_copy(
        update={
            "claim_ref": claim_id,
            "insertion_target": None,
            "rationale": f"Publish the {value} successor.",
            "statement": _payload().statement.model_copy(
                update={"object": LiteralClaimObject(value=value)}
            ),
            "source": SelfSourceBodyV1(
                content_base64=base64.b64encode(value.encode()).decode("ascii")
            ),
            "existing_claim_dispositions": (
                AuthoringExistingClaimDispositionV1(
                    claim_id=claim_id,
                    disposition="not_tested",
                ),
            ),
        }
    )


def test_confirmation_rebases_over_concurrent_backing_and_unions_both_citations(
    tmp_path: Path,
) -> None:
    instance, owner, coordinator, actor, intent_id = _submitted_insertion(tmp_path)
    intent = coordinator.resume(intent_id, actor=actor).intent
    expectation = intent.insertion_expectation
    assert expectation is not None
    first = coordinator.confirm_insertion(
        intent_id,
        actor=actor,
        observation=_observation(expectation.expectation_id),
    )
    assert first.successor_status is not None
    stale_digest = first.successor_status.candidate_digest

    other = AuthoringIntentCoordinator.for_instance(instance)
    concurrent = other.create(
        actor=actor,
        payload=_successor_payload(intent.semantic_identity, value="ready"),
        canonical_timestamp="2026-08-21T12:00:02.000000Z",
    ).intent
    submitted = other.submit(concurrent.intent_id, actor=actor)
    assert submitted.status.proposal_id is not None
    assert submitted.status.candidate_digest is not None
    _activate(
        instance,
        owner,
        proposal_id=submitted.status.proposal_id,
        candidate_digest=submitted.status.candidate_digest,
    )

    rebased = coordinator.confirm_insertion(
        intent_id,
        actor=actor,
        observation=_observation(expectation.expectation_id),
    )
    assert rebased.outcome == "backing_candidate_pending"
    assert rebased.successor_status is not None
    assert rebased.successor_status.candidate_digest != stale_digest
    evaluation = instance.proposal_evidence().read_evaluation(
        rebased.successor_status.proposal_id or ""
    )
    assert evaluation.evaluated_tree_oid is not None
    path = claim_path(intent.semantic_identity)
    candidate_claim = parse_claim(
        instance.proposal_tree(evaluation.evaluated_tree_oid)[path],
        path=path,
    )
    assert isinstance(candidate_claim, ClaimArtifactV2)
    citation_origins = {(item.role, item.origin) for item in candidate_claim.backing.citations}
    assert ("copy", "self_source") in citation_origins
    assert ("copy", "self_published") in citation_origins


def test_semantic_successor_wins_before_confirmation_with_typed_currency_result(
    tmp_path: Path,
) -> None:
    instance, owner, coordinator, actor, intent_id = _submitted_insertion(tmp_path)
    intent = coordinator.resume(intent_id, actor=actor).intent
    other = AuthoringIntentCoordinator.for_instance(instance)
    successor = other.create(
        actor=actor,
        payload=_successor_payload(intent.semantic_identity, value="done"),
        canonical_timestamp="2026-08-21T12:00:02.000000Z",
    ).intent
    submitted = other.submit(successor.intent_id, actor=actor)
    assert submitted.status.proposal_id is not None
    assert submitted.status.candidate_digest is not None
    _activate(
        instance,
        owner,
        proposal_id=submitted.status.proposal_id,
        candidate_digest=submitted.status.candidate_digest,
    )

    current = coordinator.resume(intent_id, actor=actor).intent
    assert current.insertion_expectation is not None
    assert current.insertion_expectation.state == "claim_currency_changed"
    outcome = coordinator.confirm_insertion(
        intent_id,
        actor=actor,
        observation=_observation(current.insertion_expectation.expectation_id),
    )
    assert outcome.outcome == "claim_currency_changed"
