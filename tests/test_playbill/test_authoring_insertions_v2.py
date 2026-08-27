"""Flow-B v2 publication wire and reducer laws."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.authoring.insertions import (
    PlaybillInsertionApplyError,
    apply_playbill_publication,
)
from cruxible_client.contracts.authoring.models import (
    InsertionAnchorWindowV1,
    InsertionTargetV2,
    PublicationSourceObservationV2,
    SelfSourceBodyV1,
    WorkingDigestCoordinateV1,
    build_insertion_expectation_v2,
    insertion_target_v2_digest,
    publication_block_id,
    publication_source_observation_v2_digest,
)
from cruxible_client.contracts.claims import ClaimArtifactV2, claim_path, parse_claim
from cruxible_client.contracts.declared_blocks import frame_projection_block
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.insertions import (
    PublicationAnchorAmbiguous,
    PublicationAnchorStale,
    PublicationBodyNotMarkerCompatible,
    PublicationClaimNotAccepted,
    PublicationPrepareOrConfirmRequired,
    PublicationSourceHasUnrepinnedBlock,
    PublicationTerminalStateRefused,
    build_publication_preparation,
    mark_publication_prepared,
    publication_confirmation_from_source,
    publication_confirmation_matches,
)
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.proposals import AuthenticatedActor
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_authoring_insertions import _activate, _successor_payload
from tests.test_playbill.test_authoring_preflight import (
    TIMESTAMP,
    _seed_claim_surface,
    _self_source_payload,
)

COORDINATE = AcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _target(content: bytes = b"status: \n") -> InsertionTargetV2:
    return InsertionTargetV2(
        source_id="repo.work-items",
        coordinate=WorkingDigestCoordinateV1(
            source_content_digest=_digest(content),
            source_byte_length=len(content),
        ),
        initial_preimage_digest=_digest(content),
        initial_preimage_byte_length=len(content),
        selector=InsertionAnchorWindowV1(
            anchor_content_base64=base64.b64encode(content).decode("ascii"),
            anchor_bytes_digest=_digest(content),
            start_byte=0,
            end_byte=len(content),
            insertion_offset=len(content),
            observed_occurrence_count=1,
        ),
        operation="insert_after",
    )


def _observation(content: bytes) -> PublicationSourceObservationV2:
    return PublicationSourceObservationV2(
        source_id="repo.work-items",
        content_base64=base64.b64encode(content).decode("ascii"),
        content_digest=_digest(content),
        byte_length=len(content),
    )


def _expectation():
    return build_insertion_expectation_v2(
        expectation_id=_digest(b"expectation"),
        state="pending",
        claim_identity="CLM-" + "a" * 32,
        original_claim_artifact_digest=_digest(b"claim"),
        claim_statement_digest=_digest(b"statement"),
        accepted_claim_coordinate=COORDINATE,
        target=_target(),
        expires_at=datetime(2026, 8, 28, 12, tzinfo=UTC),
    )


def _submitted_publication(tmp_path: Path, *, activate_claim: bool = True):
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    clock = [datetime(2026, 8, 22, 12, tzinfo=UTC)]
    coordinator = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(
            instance.root / instance.descriptor.storage.exhaust,
            token_factory=lambda: "1" * 32,
        ),
        claim_id_factory=lambda: "CLM-" + "2" * 32,
        clock=lambda: clock[0],
    )
    actor = AuthenticatedActor(actor_id="owner")
    preimage = b"# work item\n"
    payload = _self_source_payload(insertion_target=_target(preimage)).model_copy(
        update={
            "source": SelfSourceBodyV1(
                content_base64=base64.b64encode(b"status: ready\n").decode("ascii")
            )
        }
    )
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent
    submitted = coordinator.submit(intent.intent_id, actor=actor)
    assert submitted.status.proposal_id is not None
    assert submitted.status.candidate_digest is not None
    if activate_claim:
        _activate(
            instance,
            owner,
            proposal_id=submitted.status.proposal_id,
            candidate_digest=submitted.status.candidate_digest,
        )
    resumed = coordinator.resume(intent.intent_id, actor=actor).intent
    assert resumed.insertion_expectation is not None
    assert resumed.insertion_expectation.state == (
        "pending" if activate_claim else "awaiting_claim_acceptance"
    )
    return instance, owner, coordinator, actor, intent.intent_id, preimage, clock


def _final_source(intent_id: str, prepared, preimage: bytes) -> bytes:  # type: ignore[no-untyped-def]
    assert prepared.preparation is not None
    preparation = prepared.preparation
    framed = frame_projection_block(stamp=preparation.stamp, body=b"status: ready\n")
    offset = preparation.rebased_selector.insertion_offset
    final = preimage[:offset] + framed + preimage[offset:]
    assert (
        publication_confirmation_from_source(
            intent_id=intent_id,
            expectation=prepared.expectation,
            observation=_observation(final),
        )
        is not None
    )
    return final


def test_v2_target_and_source_observation_digest_exact_bytes() -> None:
    target = _target()
    source = PublicationSourceObservationV2(
        source_id=target.source_id,
        content_base64=base64.b64encode(b"status: ").decode("ascii"),
        content_digest=_digest(b"status: "),
        byte_length=8,
    )

    assert insertion_target_v2_digest(target).startswith("sha256:")
    assert publication_source_observation_v2_digest(source).startswith("sha256:")
    assert source.content == b"status: "


def test_v2_source_observation_refuses_noncanonical_base64_and_wrong_digest() -> None:
    with pytest.raises(ValidationError, match="canonical base64"):
        PublicationSourceObservationV2(
            source_id="repo.work-items",
            content_base64="c3RhdHVzOiA",
            content_digest=_digest(b"status: "),
            byte_length=8,
        )
    with pytest.raises(ValidationError, match="digest does not reproduce"):
        PublicationSourceObservationV2(
            source_id="repo.work-items",
            content_base64=base64.b64encode(b"status: ").decode("ascii"),
            content_digest=_digest(b"different"),
            byte_length=8,
        )


def test_publication_block_id_is_deterministic_and_parser_safe() -> None:
    expectation_id = _digest(b"expectation")
    first = publication_block_id(expectation_id)

    assert first == publication_block_id(expectation_id)
    assert first.startswith("pub-")
    assert len(first) == 36


def test_prepare_frames_the_accepted_body_and_exact_source_reproduces_confirmation() -> None:
    expectation = _expectation()
    preparation = build_publication_preparation(
        expectation,
        observation=_observation(b"status: \n"),
        body=b"ready\n",
        accepted_coordinate=COORDINATE,
        accepted_generation=7,
    )
    prepared = mark_publication_prepared(expectation, preparation=preparation)
    opening = preparation.block_start_byte
    final = (
        b"status: \n"[: preparation.rebased_selector.insertion_offset]
        + frame_projection_block(stamp=preparation.stamp, body=b"ready\n")
        + b"status: \n"[preparation.rebased_selector.insertion_offset :]
    )
    confirmation = publication_confirmation_from_source(
        intent_id="AIT-" + "b" * 32,
        expectation=prepared,
        observation=_observation(final),
    )

    assert preparation.revision == 1
    assert preparation.block_start_byte == opening == len(b"status: \n")
    assert confirmation is not None
    assert publication_confirmation_matches(
        prepared,
        confirmation,
        intent_id="AIT-" + "b" * 32,
    )
    assert not publication_confirmation_matches(
        prepared,
        confirmation,
        intent_id="AIT-" + "c" * 32,
    )


def test_prepare_refuses_stale_ambiguous_and_marker_incompatible_bodies() -> None:
    expectation = _expectation()
    with pytest.raises(PublicationAnchorStale):
        build_publication_preparation(
            expectation,
            observation=_observation(b"state: "),
            body=b"ready\n",
            accepted_coordinate=COORDINATE,
            accepted_generation=7,
        )
    with pytest.raises(PublicationAnchorAmbiguous):
        build_publication_preparation(
            expectation,
            observation=_observation(b"status: \nstatus: \n"),
            body=b"ready\n",
            accepted_coordinate=COORDINATE,
            accepted_generation=7,
        )
    with pytest.raises(PublicationBodyNotMarkerCompatible):
        build_publication_preparation(
            expectation,
            observation=_observation(b"status: \n"),
            body=b"ready without terminal LF",
            accepted_coordinate=COORDINATE,
            accepted_generation=7,
        )
    bootstrap = (
        b"<!-- playbill:block:draft -->\nunstamped\n<!-- /playbill:block:draft -->\nstatus: \n"
    )
    with pytest.raises(PublicationSourceHasUnrepinnedBlock, match="block repin"):
        build_publication_preparation(
            expectation,
            observation=_observation(bootstrap),
            body=b"ready\n",
            accepted_coordinate=COORDINATE,
            accepted_generation=7,
        )


def test_reprepare_is_deterministic_and_increments_only_for_a_new_clean_preimage() -> None:
    expectation = _expectation()
    first = build_publication_preparation(
        expectation,
        observation=_observation(b"status: \n"),
        body=b"ready\n",
        accepted_coordinate=COORDINATE,
        accepted_generation=7,
    )
    prepared = mark_publication_prepared(expectation, preparation=first)
    retry = build_publication_preparation(
        prepared,
        observation=_observation(b"status: \n"),
        body=b"ready\n",
        accepted_coordinate=COORDINATE,
        accepted_generation=7,
    )
    revised = build_publication_preparation(
        prepared,
        observation=_observation(b"prefix\nstatus: \n"),
        body=b"ready\n",
        accepted_coordinate=COORDINATE,
        accepted_generation=7,
    )

    assert retry == first
    assert revised.revision == 2
    assert revised.preparation_digest != first.preparation_digest
    assert mark_publication_prepared(prepared, preparation=revised).preparation == revised


def test_coordinator_reprepares_after_client_cas_refuses_a_concurrent_edit(
    tmp_path: Path,
) -> None:
    _instance, _owner, coordinator, actor, intent_id, preimage, _clock = _submitted_publication(
        tmp_path
    )
    first = coordinator.prepare_publication(
        intent_id,
        actor=actor,
        observation=_observation(preimage),
    )
    concurrent = b"concurrent heading\n" + preimage
    with pytest.raises(PlaybillInsertionApplyError, match="preimage"):
        apply_playbill_publication(
            concurrent,
            intent_id=intent_id,
            expectation=first.expectation.model_dump(mode="json"),
            retained_body=b"status: ready\n",
        )

    revised = coordinator.prepare_publication(
        intent_id,
        actor=actor,
        observation=_observation(concurrent),
    )
    assert revised.outcome == "prepared"
    assert revised.preparation is not None
    assert revised.preparation.revision == 2
    applied = apply_playbill_publication(
        concurrent,
        intent_id=intent_id,
        expectation=revised.expectation.model_dump(mode="json"),
        retained_body=b"status: ready\n",
    )
    assert applied.outcome == "applied"


def test_prepare_before_claim_acceptance_refuses_without_terminalizing_then_recovers(
    tmp_path: Path,
) -> None:
    instance, owner, coordinator, actor, intent_id, preimage, _clock = _submitted_publication(
        tmp_path, activate_claim=False
    )
    with pytest.raises(PublicationClaimNotAccepted):
        coordinator.prepare_publication(
            intent_id,
            actor=actor,
            observation=_observation(preimage),
        )
    unchanged = coordinator.store.get(intent_id, actor_id=actor.actor_id)
    assert unchanged.insertion_expectation is not None
    assert unchanged.insertion_expectation.state == "awaiting_claim_acceptance"
    assert unchanged.candidate_status.proposal_id is not None
    assert unchanged.candidate_status.candidate_digest is not None

    _activate(
        instance,
        owner,
        proposal_id=unchanged.candidate_status.proposal_id,
        candidate_digest=unchanged.candidate_status.candidate_digest,
    )
    prepared = coordinator.prepare_publication(
        intent_id,
        actor=actor,
        observation=_observation(preimage),
    )
    assert prepared.outcome == "prepared"
    assert prepared.expectation.state == "prepared"


def test_pin_15_prepared_status_never_passively_terminalizes_and_exact_confirm_rescues(
    tmp_path: Path,
) -> None:
    instance, _owner, coordinator, actor, intent_id, preimage, clock = _submitted_publication(
        tmp_path
    )
    prepared = coordinator.prepare_publication(
        intent_id,
        actor=actor,
        observation=_observation(preimage),
    )
    assert prepared.preparation is not None
    preparation = prepared.preparation
    framed = frame_projection_block(stamp=preparation.stamp, body=b"status: ready\n")
    offset = preparation.rebased_selector.insertion_offset
    final = preimage[:offset] + framed + preimage[offset:]
    exact = publication_confirmation_from_source(
        intent_id=intent_id,
        expectation=prepared.expectation,
        observation=_observation(final),
    )
    assert exact is not None

    clock[0] = datetime(2026, 8, 29, 12, tzinfo=UTC)
    resumed = coordinator.resume(intent_id, actor=actor).intent
    assert resumed.insertion_expectation is not None
    assert resumed.insertion_expectation.state == "prepared"
    with pytest.raises(PublicationPrepareOrConfirmRequired):
        coordinator.abandon_insertion(intent_id, actor=actor)
    bound = coordinator.confirm_insertion(intent_id, actor=actor, observation=exact)

    assert bound.outcome == "bound"
    assert bound.expectation.state == "bound"
    assert instance.accepted_coordinate().git_oid == preparation.accepted_coordinate.git_oid
    accepted_tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = claim_path(bound.intent.semantic_identity)
    accepted_claim = parse_claim(accepted_tree[path], path=path)
    assert isinstance(accepted_claim, ClaimArtifactV2)
    assert all(citation.origin != "self_published" for citation in accepted_claim.backing.citations)


def test_prepare_response_loss_and_terminal_conflicts_are_deterministic(tmp_path: Path) -> None:
    _instance, _owner, coordinator, actor, intent_id, preimage, clock = _submitted_publication(
        tmp_path
    )
    clock[0] = datetime(2026, 8, 29, 12, tzinfo=UTC)

    first = coordinator.prepare_publication(
        intent_id,
        actor=actor,
        observation=_observation(preimage),
    )
    retry = coordinator.prepare_publication(
        intent_id,
        actor=actor,
        observation=_observation(preimage),
    )

    assert first.outcome == retry.outcome == "expired"
    assert first.expectation == retry.expectation
    with pytest.raises(PublicationTerminalStateRefused):
        coordinator.prepare_publication(
            intent_id,
            actor=actor,
            observation=_observation(b"changed source\n"),
        )


def test_exact_postimage_prepare_rescues_after_expiry_and_confirm_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    _instance, _owner, coordinator, actor, intent_id, preimage, clock = _submitted_publication(
        tmp_path
    )
    prepared = coordinator.prepare_publication(
        intent_id,
        actor=actor,
        observation=_observation(preimage),
    )
    final = _final_source(intent_id, prepared, preimage)
    clock[0] = datetime(2026, 8, 29, 12, tzinfo=UTC)

    rescued = coordinator.prepare_publication(
        intent_id,
        actor=actor,
        observation=_observation(final),
    )
    assert rescued.outcome == "bound"
    confirmation = publication_confirmation_from_source(
        intent_id=intent_id,
        expectation=rescued.expectation,
        observation=_observation(final),
    )
    assert confirmation is not None
    retry = coordinator.confirm_insertion(intent_id, actor=actor, observation=confirmation)
    assert retry.outcome == "already_bound"

    wrong = confirmation.model_copy(update={"observed_occurrence_count": 2})
    with pytest.raises(PublicationTerminalStateRefused):
        coordinator.confirm_insertion(intent_id, actor=actor, observation=wrong)


def test_prepared_currency_change_is_passive_until_exact_confirmation(tmp_path: Path) -> None:
    instance, owner, coordinator, actor, intent_id, preimage, _clock = _submitted_publication(
        tmp_path
    )
    prepared = coordinator.prepare_publication(
        intent_id,
        actor=actor,
        observation=_observation(preimage),
    )
    final = _final_source(intent_id, prepared, preimage)
    original = coordinator.store.get(intent_id, actor_id=actor.actor_id)
    other = AuthoringIntentCoordinator.for_instance(instance)
    successor = other.create(
        actor=actor,
        payload=_successor_payload(original.semantic_identity, value="done"),
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

    passive = coordinator.resume(intent_id, actor=actor).intent
    assert passive.insertion_expectation is not None
    assert passive.insertion_expectation.state == "prepared"
    confirmation = publication_confirmation_from_source(
        intent_id=intent_id,
        expectation=passive.insertion_expectation,
        observation=_observation(final),
    )
    assert confirmation is not None
    bound = coordinator.confirm_insertion(intent_id, actor=actor, observation=confirmation)
    assert bound.outcome == "bound"


# Kept here so the frozen test module owns one absolute instant used by the
# reducer cases added below without allowing wall-clock construction.
EVALUATION_TIME = datetime(2026, 8, 26, 12, tzinfo=UTC)
