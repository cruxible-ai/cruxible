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
    AuthoringExistingClaimDispositionV1,
    InsertionAnchorWindowV1,
    InsertionTargetV2,
    PublicationSourceObservationV2,
    SelfSourceBodyV1,
    WorkingDigestCoordinateV1,
    build_insertion_expectation_v2,
    insertion_prepare_terminal_operation_v2_key,
    insertion_target_v2_digest,
    publication_block_id,
    publication_source_observation_v2_digest,
)
from cruxible_client.contracts.claims import (
    ClaimArtifactV2,
    LiteralClaimObject,
    claim_path,
    parse_claim,
)
from cruxible_client.contracts.declared_blocks import (
    ProjectionBootstrapUnstampedError,
    frame_projection_block,
    parse_projection_blocks,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.insertions import (
    PublicationAnchorAmbiguous,
    PublicationAnchorStale,
    PublicationBodyNotMarkerCompatible,
    PublicationClaimNotAccepted,
    PublicationPreparationStale,
    PublicationRevisionLimitExceeded,
    PublicationSourceHasUnrepinnedBlock,
    PublicationTerminalStateRefused,
    build_publication_preparation,
    mark_publication_prepared,
    publication_confirmation_from_source,
    publication_confirmation_matches,
)
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.service.playbill_next import (
    PlaybillNextRequestV1,
    PlaybillNextSourceObservationV3,
    PlaybillNextWorkspaceObservationV1,
    service_playbill_next,
)
from tests.test_playbill._support import client_material, initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_authoring_preflight import (
    TIMESTAMP,
    _seed_claim_surface,
    _self_source_payload,
    _working_payload,
)

COORDINATE = AcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _activate(
    instance,  # type: ignore[no-untyped-def]
    _owner: object,
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
    activated = service_activate_playbill_proposal(
        instance,
        proposal_id=proposal_id,
        activated_by="owner",
    )
    assert activated.status == "accepted"


def _successor_payload(claim_id: str, *, value: str):  # type: ignore[no-untyped-def]
    payload = _self_source_payload()
    return payload.model_copy(
        update={
            "claim_ref": claim_id,
            "insertion_target": None,
            "rationale": f"Publish the {value} successor.",
            "statement": payload.statement.model_copy(
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


def test_terminal_prepare_operation_key_is_a_public_deterministic_helper() -> None:
    observation = _observation(b"status: ready\n")

    first = insertion_prepare_terminal_operation_v2_key("sha256:" + "1" * 64, observation)
    second = insertion_prepare_terminal_operation_v2_key("sha256:" + "1" * 64, observation)

    assert first == second


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


def test_prepare_confirmation_binds_the_block_frame_not_unrelated_file_bytes() -> None:
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
    moved = b"unrelated prefix\n" + final + b"unrelated suffix\n"
    confirmation = publication_confirmation_from_source(
        intent_id="AIT-" + "b" * 32,
        expectation=prepared,
        observation=_observation(moved),
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
    with pytest.raises(ProjectionBootstrapUnstampedError):
        parse_projection_blocks(bootstrap, source_id=expectation.target.source_id)
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
    with pytest.raises(PublicationPreparationStale):
        mark_publication_prepared(
            prepared,
            preparation=revised.model_copy(update={"revision": 3}),
        )


def test_reprepare_has_a_fixed_revision_cap_with_exact_retry_still_allowed() -> None:
    expectation = _expectation()
    last_content = b""
    for revision in range(1, 17):
        last_content = b"prefix " * revision + b"status: \n"
        preparation = build_publication_preparation(
            expectation,
            observation=_observation(last_content),
            body=b"ready\n",
            accepted_coordinate=COORDINATE,
            accepted_generation=7,
        )
        assert preparation.revision == revision
        expectation = mark_publication_prepared(expectation, preparation=preparation)

    assert (
        build_publication_preparation(
            expectation,
            observation=_observation(last_content),
            body=b"ready\n",
            accepted_coordinate=COORDINATE,
            accepted_generation=7,
        )
        == expectation.preparation
    )
    with pytest.raises(
        PublicationRevisionLimitExceeded,
        match="16-revision limit; confirm the revision-16 postimage.*7-day expiry",
    ):
        build_publication_preparation(
            expectation,
            observation=_observation(b"one more prefix\nstatus: \n"),
            body=b"ready\n",
            accepted_coordinate=COORDINATE,
            accepted_generation=7,
        )


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
    with pytest.raises(PlaybillInsertionApplyError, match="anchor is stale"):
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


def test_prepare_warns_when_body_duplicates_a_live_citation_on_the_target_source(
    tmp_path: Path,
) -> None:
    instance, owner, coordinator, actor, intent_id, preimage, _clock = _submitted_publication(
        tmp_path
    )
    revision_coordinator = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(
            instance.root / instance.descriptor.storage.exhaust,
            token_factory=lambda: "3" * 32,
        ),
        claim_id_factory=lambda: "CLM-" + "4" * 32,
        clock=lambda: datetime(2026, 8, 22, 12, tzinfo=UTC),
    )
    revision_payload = _working_payload(occurrence_count=1).model_copy(
        update={"claim_ref": "CLM-" + "2" * 32}
    )
    revision_intent = revision_coordinator.create(
        actor=actor,
        payload=revision_payload,
        canonical_timestamp="2026-08-22T12:00:01.000000Z",
    ).intent
    revised = revision_coordinator.submit(revision_intent.intent_id, actor=actor)
    assert revised.status.proposal_id is not None
    assert revised.status.candidate_digest is not None
    _activate(
        instance,
        owner,
        proposal_id=revised.status.proposal_id,
        candidate_digest=revised.status.candidate_digest,
    )

    prepared = coordinator.prepare_publication(
        intent_id,
        actor=actor,
        observation=_observation(preimage),
    )
    replayed = coordinator.prepare_publication(
        intent_id,
        actor=actor,
        observation=_observation(preimage),
    )

    (warning,) = prepared.warnings
    assert warning.code == "playbill.authoring.publication_citation_anchor_collision"
    assert warning.source_id == "repo.work-items"
    assert warning.citation_ids == tuple(sorted(set(warning.citation_ids)))
    assert replayed.warnings == prepared.warnings


def test_stale_prepare_replay_cannot_return_a_superseded_preparation(
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
    response_loss_retry = coordinator.prepare_publication(
        intent_id,
        actor=actor,
        observation=_observation(preimage),
    )
    assert response_loss_retry.expectation == first.expectation
    assert response_loss_retry.preparation == first.preparation

    concurrent = b"concurrent heading\n" + preimage
    second = coordinator.prepare_publication(
        intent_id,
        actor=actor,
        observation=_observation(concurrent),
    )
    assert second.preparation is not None and second.preparation.revision == 2

    reverted = coordinator.prepare_publication(
        intent_id,
        actor=actor,
        observation=_observation(preimage),
    )
    assert reverted.preparation is not None and reverted.preparation.revision == 3
    durable = coordinator.store.get(intent_id, actor_id=actor.actor_id)
    assert durable.insertion_expectation == reverted.expectation

    applied = apply_playbill_publication(
        preimage,
        intent_id=intent_id,
        expectation=reverted.expectation.model_dump(mode="json"),
        retained_body=b"status: ready\n",
    )
    confirmation = publication_confirmation_from_source(
        intent_id=intent_id,
        expectation=reverted.expectation,
        observation=_observation(applied.content),
    )
    assert confirmation is not None
    confirmed = coordinator.confirm_insertion(
        intent_id,
        actor=actor,
        observation=confirmation,
    )
    assert confirmed.outcome == "bound"


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
    bound = coordinator.confirm_insertion(intent_id, actor=actor, observation=exact)

    assert bound.outcome == "bound"
    assert bound.expectation.state == "bound"
    assert instance.accepted_coordinate().git_oid == preparation.accepted_coordinate.git_oid
    accepted_tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = claim_path(bound.intent.semantic_identity)
    accepted_claim = parse_claim(accepted_tree[path], path=path)
    assert isinstance(accepted_claim, ClaimArtifactV2)
    assert all(citation.origin != "self_published" for citation in accepted_claim.backing.citations)


def test_prepared_publication_can_be_abandoned_without_observing_the_source(
    tmp_path: Path,
) -> None:
    _instance, _owner, coordinator, actor, intent_id, preimage, _clock = _submitted_publication(
        tmp_path
    )
    prepared = coordinator.prepare_publication(
        intent_id,
        actor=actor,
        observation=_observation(preimage),
    )
    assert prepared.expectation.state == "prepared"
    assert prepared.preparation is not None

    landed = apply_playbill_publication(
        preimage,
        intent_id=intent_id,
        expectation=prepared.expectation.model_dump(mode="json"),
        retained_body=b"status: ready\n",
    )
    markers = tuple(
        block.summary()
        for block in parse_projection_blocks(
            landed.content,
            source_id=prepared.preparation.source_id,
        )
    )
    coordinate = AcceptedCoordinate.from_internal(_instance.accepted_coordinate())
    request = PlaybillNextRequestV1(
        at=coordinate,
        evaluation_time=datetime(2026, 8, 23, 12, tzinfo=UTC),
        access_profile=CoverageAccessProfileV1(
            profile_id="publication-orphan-test",
            permitted_access_classes=("instance", "public"),
        ),
        workspace_observation=PlaybillNextWorkspaceObservationV1(
            source_observations=(
                PlaybillNextSourceObservationV3(
                    tag="playbill-next-source-observation-v3",
                    source_id="repo.work-items",
                    observed_source_digest=_digest(landed.content),
                    byte_length=len(landed.content),
                    marker_summaries=markers,
                    occurrences=(),
                    scanned_commitment_digests=(),
                    scan_complete=True,
                    scan_notes=(),
                    marker_notes=(),
                ),
            )
        ),
    )
    assert not [
        item
        for item in service_playbill_next(_instance, request=request).items
        if item.reason == "unregistered_projection_block"
    ]

    abandoned = coordinator.abandon_insertion(intent_id, actor=actor)
    assert abandoned.expectation.state == "abandoned"
    assert abandoned.expectation.terminal_tombstone is not None
    assert abandoned.expectation.terminal_tombstone.preparation_digest is None

    orphaned = tuple(
        item
        for item in service_playbill_next(_instance, request=request).items
        if item.reason == "unregistered_projection_block"
    )
    assert len(orphaned) == 1
    assert orphaned[0].repair.required_change == "remove_or_register_projection_block"


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


def test_pending_abandon_is_idempotent_and_retains_one_terminal_tombstone(
    tmp_path: Path,
) -> None:
    _instance, _owner, coordinator, actor, intent_id, preimage, _clock = _submitted_publication(
        tmp_path
    )

    abandoned = coordinator.abandon_insertion(intent_id, actor=actor)
    retry = coordinator.abandon_insertion(intent_id, actor=actor)

    assert abandoned.expectation.state == "abandoned"
    assert abandoned.expectation.terminal_tombstone is not None
    assert retry.model_dump_json() == abandoned.model_dump_json()
    resumed = coordinator.resume(intent_id, actor=actor).intent
    assert resumed.insertion_expectation == abandoned.expectation
    with pytest.raises(PublicationTerminalStateRefused):
        coordinator.prepare_publication(
            intent_id,
            actor=actor,
            observation=_observation(preimage),
        )


def test_prepared_to_expired_prepare_response_loss_replays_terminal_result(
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
    assert prepared.outcome == "prepared"
    clock[0] = datetime(2026, 8, 29, 12, tzinfo=UTC)

    terminal = coordinator.prepare_publication(
        intent_id,
        actor=actor,
        observation=_observation(preimage),
    )
    retry = coordinator.prepare_publication(
        intent_id,
        actor=actor,
        observation=_observation(preimage),
    )

    assert terminal.outcome == retry.outcome == "expired"
    assert retry.model_dump_json() == terminal.model_dump_json()


def test_prepared_to_currency_changed_prepare_response_loss_replays_terminal_result(
    tmp_path: Path,
) -> None:
    instance, owner, coordinator, actor, intent_id, preimage, _clock = _submitted_publication(
        tmp_path
    )
    prepared = coordinator.prepare_publication(
        intent_id,
        actor=actor,
        observation=_observation(preimage),
    )
    assert prepared.outcome == "prepared"
    original = coordinator.store.get(intent_id, actor_id=actor.actor_id)
    successor_coordinator = AuthoringIntentCoordinator.for_instance(instance)
    successor = successor_coordinator.create(
        actor=actor,
        payload=_successor_payload(original.semantic_identity, value="done"),
        canonical_timestamp="2026-08-21T12:00:02.000000Z",
    ).intent
    submitted = successor_coordinator.submit(successor.intent_id, actor=actor)
    assert submitted.status.proposal_id is not None
    assert submitted.status.candidate_digest is not None
    _activate(
        instance,
        owner,
        proposal_id=submitted.status.proposal_id,
        candidate_digest=submitted.status.candidate_digest,
    )

    terminal = coordinator.prepare_publication(
        intent_id,
        actor=actor,
        observation=_observation(preimage),
    )
    retry = coordinator.prepare_publication(
        intent_id,
        actor=actor,
        observation=_observation(preimage),
    )

    assert terminal.outcome == retry.outcome == "claim_currency_changed"
    assert retry.model_dump_json() == terminal.model_dump_json()


def test_prepared_to_expired_confirm_response_loss_replays_terminal_result(
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
    exact = publication_confirmation_from_source(
        intent_id=intent_id,
        expectation=prepared.expectation,
        observation=_observation(final),
    )
    assert exact is not None
    nonmatching = exact.model_copy(update={"observed_occurrence_count": 2})
    clock[0] = datetime(2026, 8, 29, 12, tzinfo=UTC)

    terminal = coordinator.confirm_insertion(
        intent_id,
        actor=actor,
        observation=nonmatching,
    )
    retry = coordinator.confirm_insertion(
        intent_id,
        actor=actor,
        observation=nonmatching,
    )

    assert terminal.outcome == retry.outcome == "expired"
    assert retry.model_dump_json() == terminal.model_dump_json()


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


def test_confirmation_rebases_over_a_concurrent_backing_only_successor(tmp_path: Path) -> None:
    instance, owner, coordinator, actor, intent_id, preimage, _clock = _submitted_publication(
        tmp_path
    )
    prepared = coordinator.prepare_publication(
        intent_id,
        actor=actor,
        observation=_observation(preimage),
    )
    final = _final_source(intent_id, prepared, preimage)
    original_intent = coordinator.store.get(intent_id, actor_id=actor.actor_id)

    backing_coordinator = AuthoringIntentCoordinator.for_instance(instance)
    backing_intent = backing_coordinator.create(
        actor=actor,
        payload=_working_payload(occurrence_count=1).model_copy(
            update={"claim_ref": original_intent.semantic_identity}
        ),
        canonical_timestamp="2026-08-21T12:00:02.000000Z",
    ).intent
    submitted = backing_coordinator.submit(backing_intent.intent_id, actor=actor)
    assert submitted.status.proposal_id is not None
    assert submitted.status.candidate_digest is not None
    _activate(
        instance,
        owner,
        proposal_id=submitted.status.proposal_id,
        candidate_digest=submitted.status.candidate_digest,
    )
    path = claim_path(original_intent.semantic_identity)
    before = parse_claim(instance.tree_at(instance.accepted_coordinate().git_oid)[path], path=path)
    assert isinstance(before, ClaimArtifactV2)

    confirmation = publication_confirmation_from_source(
        intent_id=intent_id,
        expectation=prepared.expectation,
        observation=_observation(final),
    )
    assert confirmation is not None
    bound = coordinator.confirm_insertion(intent_id, actor=actor, observation=confirmation)
    after = parse_claim(instance.tree_at(instance.accepted_coordinate().git_oid)[path], path=path)

    assert bound.outcome == "bound"
    assert isinstance(after, ClaimArtifactV2)
    assert after.statement == before.statement
    assert {item.citation_id for item in before.backing.citations}.issubset(
        item.citation_id for item in after.backing.citations
    )


# Kept here so the frozen test module owns one absolute instant used by the
# reducer cases added below without allowing wall-clock construction.
EVALUATION_TIME = datetime(2026, 8, 26, 12, tzinfo=UTC)
