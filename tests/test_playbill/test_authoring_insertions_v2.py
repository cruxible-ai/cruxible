"""Publication-v2 wire shapes, and the reducer laws over records already written.

Nothing in the product mints a publication any more: a Claim projected as its own
page text was the overlap the two-block-kinds law refuses, so `insertion_target`
refuses at the authoring door and the prepare/confirm road is gone. What an
instance that published before that ruling still holds is here -- its bound
registration still folds, `next` still reports the marker it owns, coverage still
resolves the card, and `block depublish` still releases it. Those records are
written by `tests.support.legacy_publications`, the only thing left that can
write one, and that is the point: the laws below have to keep holding for records
nothing can create again.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.authoring.workspace import _projection_marker_observation
from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.authoring.models import (
    AuthoringExistingClaimDispositionV1,
    InsertionAnchorWindowV1,
    InsertionExpectationV2,
    InsertionTargetV2,
    PublicationSourceObservationV2,
    SelfSourceBodyV1,
    WorkingDigestCoordinateV1,
    insertion_prepare_terminal_operation_v2_key,
    insertion_target_v2_digest,
    publication_block_id,
    publication_source_observation_v2_digest,
)
from cruxible_client.contracts.claims import (
    ClaimArtifactV2,
    LiteralClaimObject,
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
    parse_claim,
    render_claim,
)
from cruxible_client.contracts.declared_blocks import parse_projection_blocks
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.coverage.adapter import observe_working_source
from cruxible_core.playbill.coverage.contracts import (
    CoverageAccessProfileV1,
    LogicalSourceIdentityV1,
)
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.service.playbill_coverage import service_resolve_playbill_coverage
from cruxible_core.service.playbill_next import (
    PlaybillNextRequestV1,
    PlaybillNextSourceObservationV3,
    PlaybillNextSourceObservationV4,
    PlaybillNextWorkspaceObservationV1,
    _registered_publication_blocks,
    _registrations_released_by_retirement,
    service_playbill_next,
)
from cruxible_core.service.playbill_publications import service_depublish_playbill_block
from tests.support.legacy_publications import register_legacy_publication
from tests.test_playbill._support import client_material, initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_authoring_preflight import (
    TIMESTAMP,
    _seed_claim_surface,
    _self_source_payload,
)

# The body every fixture below publishes: the authored Claim's own self-source
# body, which is what the removed road framed into the page.
PUBLISHED_BODY = b"status: ready\n"


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


def _retire(instance, owner, claim_id: str) -> None:  # type: ignore[no-untyped-def]
    """Retire one accepted Claim through an ordinary proposal.

    It used to live beside the reverse-drift tests. Those tested a `next` row
    that could only fire on a citation origin nothing ever wrote, so they went
    with the origin; this is the part of them that was about retirement.
    """

    base = instance.accepted_coordinate()
    path = claim_path(claim_id)
    tree = instance.tree_at(base.git_oid)
    claim = parse_claim(tree[path], path=path)
    assert isinstance(claim, ClaimArtifactV2)
    retired = claim.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired",
                predecessor_digest=claim_artifact_digest(claim).tagged,
            )
        }
    )
    tree[path] = render_claim(retired)
    submitted = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/retire-published-copy",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp="2026-08-21T12:01:00.000000Z",
    )
    assert submitted.evaluation.candidate_digest is not None
    _activate(
        instance,
        owner,
        proposal_id=submitted.admission.proposal_id,
        candidate_digest=submitted.evaluation.candidate_digest,
    )


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


def _submitted_publication(
    tmp_path: Path,
    *,
    activate_claim: bool = True,
):
    """Submit -- and by default accept -- the Claim a legacy publication backs.

    The payload carries no `insertion_target`, because the authoring door
    refuses one now: this intent reaches acceptance exactly as any other Flow-B
    Claim does. The target arrives later, from `_registered_publication`, which
    writes the record the removed road used to write.
    """

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
    intent = coordinator.create(
        actor=actor,
        payload=_self_source_payload(),
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
    assert resumed.insertion_expectations == ()
    return instance, owner, coordinator, actor, intent.intent_id, preimage, clock


def _registered_publication(
    instance: PlaybillInstance,
    coordinator: AuthoringIntentCoordinator,
    actor: AuthenticatedActor,
    intent_id: str,
    preimage: bytes,
    *,
    observed_at: datetime = datetime(2026, 8, 22, 12, tzinfo=UTC),
) -> tuple[InsertionExpectationV2, bytes]:
    """Land the bound registration, and the page bytes, an old instance holds.

    The Claim is accepted, so its artifact and statement digests are read back
    from the accepted tree exactly as the vanished mint read them from the
    lowered one. The coverage join compares the registration's statement digest
    against the live Claim, so a fixture that invented one would resolve nothing.
    """

    stored = coordinator.store.get(intent_id, actor_id=actor.actor_id)
    path = claim_path(stored.semantic_identity)
    coordinate = instance.accepted_coordinate()
    accepted = parse_claim(instance.tree_at(coordinate.git_oid)[path], path=path)
    return register_legacy_publication(
        coordinator.store,
        intent_id,
        actor_id=actor.actor_id,
        target=_target(preimage),
        preimage=preimage,
        body=PUBLISHED_BODY,
        accepted_coordinate=AcceptedCoordinate.from_internal(coordinate),
        accepted_generation=next(
            generation.sequence
            for generation in instance.accepted_history()
            if generation.oid == coordinate.git_oid
        ),
        claim_artifact_digest=claim_artifact_digest(accepted).tagged,
        claim_statement_digest=claim_statement_digest(accepted.statement).tagged,
        observed_at=observed_at,
    )


def _published_publication_next_request(tmp_path: Path):  # type: ignore[no-untyped-def]
    """One registered publication, its page bytes, and a `next` request over them."""

    instance, _owner, coordinator, actor, intent_id, preimage, _clock = _submitted_publication(
        tmp_path
    )
    bound, landed = _registered_publication(instance, coordinator, actor, intent_id, preimage)
    assert bound.preparation is not None
    markers = tuple(
        block.summary()
        for block in parse_projection_blocks(
            landed,
            source_id=bound.preparation.source_id,
        )
    )
    request = PlaybillNextRequestV1(
        at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
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
                    observed_source_digest=_digest(landed),
                    byte_length=len(landed),
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
    return instance, coordinator, actor, intent_id, bound, landed, request


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


def test_only_a_registered_publication_block_suppresses_next_orphan_repair(
    tmp_path: Path,
) -> None:
    """A `pub-` marker the instance does not register is a hole, and says so.

    The registration is the whole of what tells `next` that a publication block
    on the page is accounted for. An identically-shaped marker for a block no
    expectation registers is the orphan case, and its repair names both ways out
    -- remove the block, or register it -- rather than assuming which one the
    author meant.
    """

    instance, _coordinator, _actor, _intent_id, bound, _landed, request = (
        _published_publication_next_request(tmp_path)
    )
    assert bound.preparation is not None
    registered = (bound.preparation.source_id, bound.preparation.block_id)
    assert registered in (_registered_publication_blocks(instance) or frozenset())
    assert not [
        item
        for item in service_playbill_next(instance, request=request).items
        if item.reason == "unregistered_projection_block"
    ]

    # A second block of exactly the same shape, for an expectation that was
    # never minted. The registered marker stays on the page beside it, so the
    # only difference between the two rows is the registration itself.
    unregistered_block_id = publication_block_id(_digest(b"an expectation nothing minted"))
    source_observation = request.workspace_observation.source_observations[0]
    (marker,) = source_observation.marker_summaries
    span = marker.end_byte - marker.start_byte
    orphan_marker = marker.model_copy(
        update={
            "stamp": marker.stamp.model_copy(update={"block_id": unregistered_block_id}),
            "start_byte": marker.end_byte,
            "end_byte": marker.end_byte + span,
        }
    )
    orphan_request = request.model_copy(
        update={
            "workspace_observation": request.workspace_observation.model_copy(
                update={
                    "source_observations": (
                        source_observation.model_copy(
                            update={"marker_summaries": (marker, orphan_marker)}
                        ),
                    )
                }
            )
        }
    )

    orphaned = tuple(
        item
        for item in service_playbill_next(instance, request=orphan_request).items
        if item.reason == "unregistered_projection_block"
    )
    assert len(orphaned) == 1
    assert orphaned[0].severity == "warning"
    assert orphaned[0].repair.required_change == "remove_or_register_projection_block"
    assert orphaned[0].repair.arguments == {
        "source_id": "repo.work-items",
        "block_id": unregistered_block_id,
    }
    assert (
        "repo.work-items",
        unregistered_block_id,
    ) not in (_registered_publication_blocks(instance) or frozenset())


def test_registered_publication_resolves_exact_then_drifted_coverage(tmp_path: Path) -> None:
    instance, _owner, coordinator, actor, intent_id, preimage, _clock = _submitted_publication(
        tmp_path
    )
    bound, landed = _registered_publication(instance, coordinator, actor, intent_id, preimage)
    assert bound.preparation is not None
    source = LogicalSourceIdentityV1(plane="external", identity="repo.work-items")

    exact = service_resolve_playbill_coverage(
        instance,
        instance_id=instance.descriptor.instance_id,
        observations=(observe_working_source(source, landed),),
    )
    assert exact.summary.exact == 1
    assert exact.summary.candidate == 0
    (exact_card,) = exact.spans[0].cards
    assert exact_card.accepted_source == source
    assert exact_card.line_overlay is not None
    assert exact_card.line_overlay.start_byte == bound.preparation.body_start_byte
    assert exact_card.line_overlay.end_byte == bound.preparation.body_end_byte

    changed = landed.replace(PUBLISHED_BODY, b"status: stale\n", 1)
    drifted = service_resolve_playbill_coverage(
        instance,
        instance_id=instance.descriptor.instance_id,
        observations=(observe_working_source(source, changed),),
    )
    assert drifted.summary.drifted == 1
    assert drifted.summary.candidate == 0
    (drifted_card,) = drifted.spans[0].cards
    assert drifted_card.expected_commitment_digest == _digest(PUBLISHED_BODY)
    assert drifted_card.observed_commitment_digest == _digest(b"status: stale\n")

    # Releasing the registration demotes the very same bytes back to a
    # candidate: the exact card was the registration's, never the block's.
    service_depublish_playbill_block(
        instance,
        coordinator=coordinator,
        actor=actor,
        source_id=bound.preparation.source_id,
        block_id=bound.preparation.block_id,
    )
    released = service_resolve_playbill_coverage(
        instance,
        instance_id=instance.descriptor.instance_id,
        observations=(observe_working_source(source, landed),),
    )
    assert released.summary.exact == 0
    assert released.summary.candidate == 1


def test_registered_publication_coverage_fold_is_lock_free_and_nonrecovering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _owner, coordinator, actor, intent_id, preimage, _clock = _submitted_publication(
        tmp_path
    )
    _bound, landed = _registered_publication(instance, coordinator, actor, intent_id, preimage)

    lock_path = coordinator.store.root / ".lock"
    lock_path.unlink(missing_ok=True)

    def fail_recovery(_store: AuthoringIntentStore) -> None:
        raise AssertionError("coverage reads must not recover the authoring store")

    monkeypatch.setattr(AuthoringIntentStore, "_recover_creating_directories", fail_recovery)
    source = LogicalSourceIdentityV1(plane="external", identity="repo.work-items")
    result = service_resolve_playbill_coverage(
        instance,
        instance_id=instance.descriptor.instance_id,
        observations=(observe_working_source(source, landed),),
    )

    assert result.summary.exact == 1
    assert not lock_path.exists()


def test_registered_publication_only_promotes_its_exact_block_occurrence(
    tmp_path: Path,
) -> None:
    instance, _owner, coordinator, actor, intent_id, preimage, _clock = _submitted_publication(
        tmp_path
    )
    _bound, landed = _registered_publication(instance, coordinator, actor, intent_id, preimage)

    duplicate = PUBLISHED_BODY + landed
    source = LogicalSourceIdentityV1(plane="external", identity="repo.work-items")
    result = service_resolve_playbill_coverage(
        instance,
        instance_id=instance.descriptor.instance_id,
        observations=(observe_working_source(source, duplicate),),
    )

    cards = result.spans[0].cards
    assert sorted(card.match_state for card in cards) == ["candidate", "exact"]
    exact_card = next(card for card in cards if card.match_state == "exact")
    candidate_card = next(card for card in cards if card.match_state == "candidate")
    assert exact_card.line_overlay != candidate_card.line_overlay


@pytest.mark.parametrize("claim_mutation", ["retired", "restated"])
def test_registered_publication_never_promotes_a_nonmatching_live_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    claim_mutation: str,
) -> None:
    instance, _owner, coordinator, actor, intent_id, preimage, _clock = _submitted_publication(
        tmp_path
    )
    bound, landed = _registered_publication(instance, coordinator, actor, intent_id, preimage)

    claim_path_value = claim_path(bound.claim_identity)
    coordinate = instance.accepted_coordinate()
    accepted_tree = instance.tree_at(coordinate.git_oid)
    accepted = parse_claim(accepted_tree[claim_path_value], path=claim_path_value)
    if claim_mutation == "retired":
        changed = accepted.model_copy(
            update={"lifecycle": accepted.lifecycle.model_copy(update={"state": "retired"})}
        )
    else:
        changed = accepted.model_copy(
            update={
                "statement": accepted.statement.model_copy(
                    update={
                        "object": accepted.statement.object.model_copy(
                            update={"value": "a different accepted statement"}
                        )
                    }
                )
            }
        )
    changed_tree = {**accepted_tree, claim_path_value: render_claim(changed)}
    original_tree_at = instance.tree_at
    monkeypatch.setattr(
        instance,
        "tree_at",
        lambda oid: changed_tree if oid == coordinate.git_oid else original_tree_at(oid),
    )

    source = LogicalSourceIdentityV1(plane="external", identity="repo.work-items")
    result = service_resolve_playbill_coverage(
        instance,
        instance_id=instance.descriptor.instance_id,
        observations=(observe_working_source(source, landed),),
    )
    assert result.summary.exact == 0


def test_an_abandoned_publication_retains_its_terminal_tombstone_shape(
    tmp_path: Path,
) -> None:
    """The tombstone commits page bytes only while the block is claimed to be there.

    A depublication is not a publication that never happened: the expectation
    keeps its own preparation, so the record still names the source and the
    block it took down. What the tombstone drops is the preparation DIGEST,
    because a tombstone that still committed the postimage would be asserting a
    page state the instance no longer stands behind.
    """

    instance, coordinator, actor, intent_id, bound, _landed, _request = (
        _published_publication_next_request(tmp_path)
    )

    abandoned = coordinator.abandon_insertion(intent_id, actor=actor)

    assert bound.state == "bound"
    assert abandoned.expectation.state == "abandoned"
    assert abandoned.expectation.terminal_tombstone is not None
    assert abandoned.expectation.terminal_tombstone.preparation_digest is None
    assert abandoned.expectation.preparation == bound.preparation
    assert not (_registered_publication_blocks(instance) or frozenset())


def test_block_repin_cannot_manufacture_a_false_publication_orphan(tmp_path: Path) -> None:
    instance, _coordinator, _actor, _intent_id, bound, _landed, request = (
        _published_publication_next_request(tmp_path)
    )
    assert bound.preparation is not None

    source_observation = request.workspace_observation.source_observations[0]
    repinned_markers = tuple(
        marker.model_copy(
            update={
                "stamp": marker.stamp.model_copy(
                    update={"declared_generation": marker.stamp.declared_generation + 1}
                )
            }
        )
        for marker in source_observation.marker_summaries
    )
    repinned_request = request.model_copy(
        update={
            "workspace_observation": request.workspace_observation.model_copy(
                update={
                    "source_observations": (
                        source_observation.model_copy(
                            update={"marker_summaries": repinned_markers}
                        ),
                    )
                }
            )
        }
    )

    assert (
        bound.preparation.source_id,
        bound.preparation.block_id,
    ) in (_registered_publication_blocks(instance) or frozenset())
    assert not [
        item
        for item in service_playbill_next(instance, request=repinned_request).items
        if item.reason == "unregistered_projection_block"
    ]


@pytest.mark.parametrize("corruption", ("closing_id_changed", "opening_deleted"))
@pytest.mark.parametrize("observation_version", (3, 4))
def test_bound_publication_marker_corruption_surfaces_exact_blocking_repair(
    tmp_path: Path,
    corruption: str,
    observation_version: int,
) -> None:
    instance, _coordinator, _actor, _intent_id, bound, landed, request = (
        _published_publication_next_request(tmp_path)
    )
    assert bound.preparation is not None
    block_id = bound.preparation.block_id
    source_id = bound.preparation.source_id
    (parsed,) = parse_projection_blocks(landed, source_id=source_id)
    if corruption == "closing_id_changed":
        closer = f"<!-- /playbill:block:{block_id} -->\n".encode()
        corrupted = landed.replace(
            closer,
            b"<!-- /playbill:block:pub-CORRUPTED -->\n",
        )
    else:
        corrupted = landed[: parsed.opening_start] + landed[parsed.opening_end :]

    marker_summaries, marker_notes = _projection_marker_observation(source_id, corrupted)
    assert marker_summaries == []
    assert marker_notes == ("projection_marker_invalid",)
    assert request.workspace_observation is not None
    if observation_version == 3:
        source_observation = PlaybillNextSourceObservationV3(
            tag="playbill-next-source-observation-v3",
            source_id=source_id,
            observed_source_digest=_digest(corrupted),
            byte_length=len(corrupted),
            marker_summaries=(),
            occurrences=(),
            scanned_commitment_digests=(),
            scan_complete=False,
            scan_notes=(),
            marker_notes=marker_notes,
        )
    else:
        source_observation = PlaybillNextSourceObservationV4(
            source_id=source_id,
            observed_source_digest=_digest(corrupted),
            byte_length=len(corrupted),
            marker_summaries=(),
            occurrences=(),
            commitment_scan_proofs=(),
            citation_window_observations=(),
            scan_notes=("coverage_occurrence_unverified",),
            marker_notes=marker_notes,
        )
    corrupted_request = request.model_copy(
        update={
            "workspace_observation": PlaybillNextWorkspaceObservationV1(
                source_observations=(source_observation,)
            )
        }
    )

    rows = tuple(
        item
        for item in service_playbill_next(instance, request=corrupted_request).items
        if item.reason == "projection_marker_invalid"
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.severity == "blocking"
    assert row.subject_identity == f"{source_id}#{block_id}"
    assert row.detail == {
        "source_id": source_id,
        "block_id": block_id,
        "error_code": "playbill.projection.marker_invalid",
        "marker_status": "invalid",
    }
    assert row.repair.operation == "playbill.block.repin"
    assert row.repair.required_change == "restore_projection_frame_then_repin"
    assert row.repair.command == f"cruxible playbill block repin {source_id} {block_id}"


def test_prepared_publication_can_be_abandoned_without_observing_the_source(
    tmp_path: Path,
) -> None:
    """Abandoning takes no observation, and the released marker becomes the orphan.

    The function keeps its name because another `next` scenario module drives
    this exact case by it; the record it abandons is a registered publication,
    which is the only kind an instance can hold now.
    """

    instance, coordinator, actor, intent_id, bound, _landed, request = (
        _published_publication_next_request(tmp_path)
    )
    assert bound.preparation is not None

    abandoned = coordinator.abandon_insertion(intent_id, actor=actor)

    assert abandoned.expectation.state == "abandoned"
    orphaned = tuple(
        item
        for item in service_playbill_next(instance, request=request).items
        if item.reason == "unregistered_projection_block"
    )
    assert len(orphaned) == 1
    assert orphaned[0].severity == "warning"
    assert orphaned[0].repair.required_change == "remove_or_register_projection_block"
    assert orphaned[0].repair.arguments == {
        "source_id": "repo.work-items",
        "block_id": bound.preparation.block_id,
    }


def test_a_marker_no_registration_knows_is_an_orphan_whatever_its_id(tmp_path: Path) -> None:
    """A block id is not a sanction, and it never was a good proxy for one.

    This test used to hold the opposite: a marker whose block id did not begin
    `pub-` was left alone, because that prefix was the only structurally
    recognizable identity class and the question "does this instance stand
    behind this marker?" was answered by spelling. Every block an agent declared
    with `block repin` was therefore checked against nothing, and `workspace
    detach` could not refuse on one. Both roads register now, under the identity
    the fold owns -- the source and block the page itself names -- so a marker
    no registration knows is an orphan whichever road wrote it.
    """

    instance, _coordinator, _actor, _intent_id, _bound, _landed, request = (
        _published_publication_next_request(tmp_path)
    )
    source_observation = request.workspace_observation.source_observations[0]
    (publication_marker,) = source_observation.marker_summaries
    span = publication_marker.end_byte - publication_marker.start_byte
    voluntary_marker = publication_marker.model_copy(
        update={
            "stamp": publication_marker.stamp.model_copy(update={"block_id": "notes"}),
            "start_byte": publication_marker.end_byte,
            "end_byte": publication_marker.end_byte + span,
        }
    )
    voluntary_request = request.model_copy(
        update={
            "workspace_observation": request.workspace_observation.model_copy(
                update={
                    "source_observations": (
                        source_observation.model_copy(
                            update={"marker_summaries": (publication_marker, voluntary_marker)}
                        ),
                    )
                }
            )
        }
    )

    orphans = [
        item
        for item in service_playbill_next(instance, request=voluntary_request).items
        if item.reason == "unregistered_projection_block"
    ]
    assert [item.subject_identity for item in orphans] == ["repo.work-items#notes"]
    (orphan,) = orphans
    assert orphan.severity == "warning"
    assert orphan.repair.required_change == "remove_or_register_projection_block"


def test_abandon_is_idempotent_and_retains_one_terminal_tombstone(
    tmp_path: Path,
) -> None:
    instance, _owner, coordinator, actor, intent_id, preimage, _clock = _submitted_publication(
        tmp_path
    )
    _bound, _landed = _registered_publication(instance, coordinator, actor, intent_id, preimage)

    abandoned = coordinator.abandon_insertion(intent_id, actor=actor)
    retry = coordinator.abandon_insertion(intent_id, actor=actor)

    assert abandoned.expectation.state == "abandoned"
    assert abandoned.expectation.terminal_tombstone is not None
    # The candidate status is derived and recomputed on every read, so the
    # replay is over the durable record: the same one expectation, with the same
    # one tombstone, and no second abandonment beside it.
    excluded = {"intent": {"candidate_status"}}
    assert retry.model_dump(exclude=excluded) == abandoned.model_dump(exclude=excluded)
    resumed = coordinator.resume(intent_id, actor=actor).intent
    assert resumed.insertion_expectation == abandoned.expectation


def test_a_bound_publication_can_be_depublished_and_the_registration_released(
    tmp_path: Path,
) -> None:
    """Card 113: `bound` was terminal, so a published block could never be unpublished.

    The consequence was a lifecycle with no exit: publish once and that page
    carries that block, with that id, forever. Removing the marker made `next`
    emit a blocking row whose named repair was to restore the block a later
    ruling had told the author to delete.
    """

    instance, _owner, coordinator, actor, intent_id, preimage, _clock = _submitted_publication(
        tmp_path
    )
    bound, _landed = _registered_publication(instance, coordinator, actor, intent_id, preimage)
    assert bound.preparation is not None
    preparation = bound.preparation
    registered = (preparation.source_id, preparation.block_id)
    assert registered in (_registered_publication_blocks(instance) or frozenset())

    released = service_depublish_playbill_block(
        instance,
        coordinator=coordinator,
        actor=actor,
        source_id=preparation.source_id,
        block_id=preparation.block_id,
    )

    assert released.outcome == "depublished"
    assert released.intent_id == intent_id
    assert released.claim_identity == bound.claim_identity
    assert registered not in (_registered_publication_blocks(instance) or frozenset())

    # Idempotent, and it recycles no identity: the second call finds the
    # released expectation and says so rather than minting a new one.
    repeated = service_depublish_playbill_block(
        instance,
        coordinator=coordinator,
        actor=actor,
        source_id=preparation.source_id,
        block_id=preparation.block_id,
    )
    assert repeated.outcome == "already_depublished"
    assert repeated.expectation_id == released.expectation_id
    assert repeated.intent_id == released.intent_id


def test_depublishing_a_block_no_registration_names_refuses_by_name(
    tmp_path: Path,
) -> None:
    """Releasing a block nothing registers refuses without inventing a road.

    The refusal used to say "no bound publication registers this", which is a
    true sentence about one of the two roads and a misleading one about the
    other: a block declared with `block repin` is registered by a declaration
    and never by a publication, so a caller who released one and asked again
    was told their block had never been a publication -- which it had not, and
    which was not the question.
    """

    instance, _owner, coordinator, actor, _intent_id, _preimage, _clock = _submitted_publication(
        tmp_path
    )

    with pytest.raises(PlaybillFormatError, match="playbill.block.not_registered"):
        service_depublish_playbill_block(
            instance,
            coordinator=coordinator,
            actor=actor,
            source_id="repo.work-items",
            block_id="pub-nothing-registers-this",
        )


def test_a_retired_backing_releases_the_marker_it_backed(tmp_path: Path) -> None:
    """The registration never read the Claim's lifecycle, so a retirement freed nothing.

    A ruling that retires a block's backing Claim is a ruling that the block
    goes. `next` went on reporting the removed marker as blocking, with the
    repair "restore the projection frame then repin" -- put back the block the
    ruling told you to delete.
    """

    instance, owner, coordinator, actor, intent_id, preimage, _clock = _submitted_publication(
        tmp_path
    )
    bound, _landed = _registered_publication(instance, coordinator, actor, intent_id, preimage)
    assert bound.preparation is not None
    preparation = bound.preparation

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    registered = (preparation.source_id, preparation.block_id)
    folded = _registered_publication_blocks(instance)
    assert folded is not None
    assert registered in folded
    assert registered not in _registrations_released_by_retirement(folded, tree=tree)

    _retire(instance, owner, bound.claim_identity)

    after_tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    after_fold = _registered_publication_blocks(instance)
    assert after_fold is not None
    # The registration itself stands -- a retirement is not a depublication --
    # and it is the retirement fold that stops it demanding the frame.
    assert registered in after_fold
    assert registered in _registrations_released_by_retirement(after_fold, tree=after_tree)

    # And the block's absence from the page is no longer a blocking row.
    request = PlaybillNextRequestV1(
        at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        evaluation_time=datetime(2026, 8, 23, 12, tzinfo=UTC),
        access_profile=CoverageAccessProfileV1(
            profile_id="publication-retirement-test",
            permitted_access_classes=("instance", "public"),
        ),
        workspace_observation=PlaybillNextWorkspaceObservationV1(
            source_observations=(
                PlaybillNextSourceObservationV3(
                    tag="playbill-next-source-observation-v3",
                    source_id=preparation.source_id,
                    observed_source_digest=_digest(preimage),
                    byte_length=len(preimage),
                    marker_summaries=(),
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
        for item in service_playbill_next(instance, request=request).items
        if item.reason == "projection_marker_invalid"
    ]


def test_depublishing_releases_the_registration_and_leaves_the_marker_to_remove(
    tmp_path: Path,
) -> None:
    """Card 113's second step, which only a later `next` revealed.

    `block depublish` touches the REGISTRATION and nothing in the workspace, so
    immediately afterwards the page still carries the marker the registration
    used to own. That is not an error and not a hole -- it is the operator's
    separate edit -- but nothing said so, and nothing pinned what `next` answers
    in between. It answers `unregistered_projection_block`, repair
    `remove_or_register_projection_block`, which is the right instruction and
    the opposite of the row it replaces: the old one asked for the block back.
    """

    instance, _owner, coordinator, actor, intent_id, preimage, _clock = _submitted_publication(
        tmp_path
    )
    bound, landed = _registered_publication(instance, coordinator, actor, intent_id, preimage)
    assert bound.preparation is not None
    preparation = bound.preparation

    marker_summaries, marker_notes = _projection_marker_observation(preparation.source_id, landed)
    assert [summary["stamp"]["block_id"] for summary in marker_summaries] == [preparation.block_id]

    def _next_items():  # type: ignore[no-untyped-def]
        request = PlaybillNextRequestV1(
            at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
            evaluation_time=datetime(2026, 8, 23, 12, tzinfo=UTC),
            access_profile=CoverageAccessProfileV1(
                profile_id="depublication-sequence-test",
                permitted_access_classes=("instance", "public"),
            ),
            workspace_observation=PlaybillNextWorkspaceObservationV1(
                source_observations=(
                    PlaybillNextSourceObservationV3(
                        tag="playbill-next-source-observation-v3",
                        source_id=preparation.source_id,
                        observed_source_digest=_digest(landed),
                        byte_length=len(landed),
                        marker_summaries=tuple(marker_summaries),
                        occurrences=(),
                        scanned_commitment_digests=(),
                        scan_complete=True,
                        scan_notes=(),
                        marker_notes=marker_notes,
                    ),
                )
            ),
        )
        return service_playbill_next(instance, request=request).items

    # While the registration stands, the marker on the page is exactly what the
    # host expects to find and no row names it.
    assert not [item for item in _next_items() if item.reason == "unregistered_projection_block"]

    service_depublish_playbill_block(
        instance,
        coordinator=coordinator,
        actor=actor,
        source_id=preparation.source_id,
        block_id=preparation.block_id,
    )

    after = _next_items()
    unregistered = [item for item in after if item.reason == "unregistered_projection_block"]
    assert len(unregistered) == 1
    row = unregistered[0]
    assert row.severity == "warning"
    assert row.subject_identity == f"{preparation.source_id}#{preparation.block_id}"
    assert row.repair is not None
    assert row.repair.required_change == "remove_or_register_projection_block"
    assert row.repair.arguments == {
        "source_id": preparation.source_id,
        "block_id": preparation.block_id,
    }
    # And the released registration demands nothing back: the block that left
    # the ledger does not become a missing marker.
    assert not [item for item in after if item.reason == "registered_marker_missing"]
