"""Coordinate assertions carried by SDK typed references."""

from __future__ import annotations

from pathlib import Path

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.authoring.models import (
    AuthoringIntentV2,
    AuthoringReferenceExpectationV1,
    authoring_create_fingerprint,
    authoring_payload_digest,
    reference_expectations_digest,
)
from cruxible_client.contracts.subjects import render_subject, subject_digest, subject_path
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.store import AuthoringIntentEventV2, AuthoringIntentStore
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.keys import GeneratedKeyMaterial
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.settlement import ChangeActorBinding
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_authoring_preflight import (
    TIMESTAMP,
    _seed_claim_surface,
    _self_source_payload,
)
from tests.test_playbill.test_claims import _subject


def _accept_subject_successor(
    instance: PlaybillInstance,
    owner: GeneratedKeyMaterial,
) -> None:
    old = _subject()
    successor = old.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="live",
                predecessor_digest=subject_digest(old).tagged,
            )
        }
    )
    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    tree[subject_path(old.subject_kind, old.subject_id)] = render_subject(successor)
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/reference-successor",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp="2026-08-21T12:00:01.000000Z",
    )
    assert proposed.candidate is not None
    assert proposed.evaluation.evaluated_tree_oid is not None
    bundle = instance.prepare_generation(
        base=base,
        candidate_tree=instance.proposal_tree(proposed.evaluation.evaluated_tree_oid),
        candidate=proposed.candidate,
        approvals=(_sign(owner, proposed.candidate.candidate_digest, base.semantic_root),),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        sequence=2,
    )
    projection = instance.activation_publisher().prebuild(bundle, base=base)
    assert (
        instance.activation_publisher().activate(bundle, projection, base=base).status
        == "accepted"
    )
    instance.refresh()


def _expectation(coordinate: AcceptedCoordinate) -> tuple[AuthoringReferenceExpectationV1, ...]:
    return (
        AuthoringReferenceExpectationV1(
            payload_path="statement.subject",
            artifact_kind="Subject",
            address="project.work_item/wi-42",
            minted_coordinate=coordinate,
        ),
    )


def test_stale_ref_names_the_successor_coordinate_and_retry_converges(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    minted = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    _accept_subject_successor(instance, owner)
    current = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    coordinator = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(
            instance.root / instance.descriptor.storage.exhaust,
            token_factory=lambda: "1" * 32,
        ),
        claim_id_factory=lambda: "CLM-" + "2" * 32,
    )
    actor = AuthenticatedActor(actor_id="owner")
    payload = _self_source_payload()

    first = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
        reference_expectations=_expectation(minted),
    ).intent
    refusal = coordinator.preflight(first.intent_id, actor=actor)

    diagnostic = next(
        item
        for item in refusal.frontier.diagnostics
        if item.code == "playbill.authoring.reference_stale"
    )
    replacement = diagnostic.repairs[0].replacement
    assert isinstance(replacement, dict)
    assert replacement["address"] == "project.work_item/wi-42"
    assert replacement["coordinate"] == current.model_dump(mode="json")

    retried = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
        reference_expectations=_expectation(current),
    ).intent
    assert isinstance(retried, AuthoringIntentV2)
    assert retried.intent_id == first.intent_id
    assert retried.semantic_identity == first.semantic_identity
    assert retried.payload_digest == first.payload_digest
    assert retried.create_fingerprint == first.create_fingerprint
    assert retried.intent_revision == first.intent_revision + 1
    assert coordinator.preflight(retried.intent_id, actor=actor).verdict == "passed"


def test_reference_assertions_are_event_committed_but_identity_excluded(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    payload = _self_source_payload()
    expected = _expectation(coordinate)

    view = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
        reference_expectations=expected,
    )
    stored = coordinator.store._load_events(  # noqa: SLF001 - protocol persistence proof
        coordinator.store.root / view.intent.intent_id
    )

    assert len(stored) == 1
    assert isinstance(stored[0], AuthoringIntentEventV2)
    assert stored[0].event_digest.startswith("sha256:")
    assert reference_expectations_digest(expected).startswith("sha256:")
    assert view.intent.payload_digest == authoring_payload_digest(payload)
    assert view.intent.create_fingerprint == authoring_create_fingerprint(
        instance_id=instance.descriptor.instance_id,
        actor_id="owner",
        payload=payload,
    )
