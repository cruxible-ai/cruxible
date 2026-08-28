"""Cite-existing Capture authoring and shared scope law."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cruxible_client.contracts.authoring.models import (
    AuthoringIntentV2,
    ClaimAuthoringPayloadV3,
    ClaimDependencyDraftsV1,
    ExistingCaptureCitationSourceV1,
)
from cruxible_client.contracts.captures import (
    build_working_selection_capture,
    capture_contract_path,
    foreign_source_capture_contract,
)
from cruxible_client.contracts.claims import claim_artifact_digest, claim_path, parse_claim
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.lowering import lower_authoring
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from tests.test_playbill._support import client_material, initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_authoring_preflight import (
    TIMESTAMP,
    _coordinator,
    _seed_claim_surface,
    _self_source_payload,
    _working_payload,
)


def _activate(instance, submitted) -> None:  # type: ignore[no-untyped-def]
    assert submitted.status.proposal_id is not None
    assert submitted.status.candidate_digest is not None
    approval = _sign(
        client_material(instance.root.parent, instance),
        submitted.status.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=submitted.status.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="reviewer",
    )
    service_activate_playbill_proposal(
        instance,
        proposal_id=submitted.status.proposal_id,
        activated_by="owner",
    )


def shared_capture_world(root: Path):  # type: ignore[no-untyped-def]
    instance, owner = initialize_local(root)
    source_id = "repo.work-items"
    contract = foreign_source_capture_contract(source_id)
    _seed_claim_surface(instance, owner, contract=contract)
    actor = AuthenticatedActor(actor_id="owner")
    first_coordinator = _coordinator(instance)
    first = first_coordinator.create(
        actor=actor,
        payload=_working_payload(occurrence_count=1),
        canonical_timestamp=TIMESTAMP,
    ).intent
    _activate(instance, first_coordinator.submit(first.intent_id, actor=actor))
    instance.refresh()
    first_path = claim_path(first.semantic_identity)
    first_claim = parse_claim(
        instance.tree_at(instance.accepted_coordinate().git_oid)[first_path],
        path=first_path,
    )
    capture_digest = first_claim.backing.capture_digests[0]

    coordinator = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(instance.root / instance.descriptor.storage.exhaust),
        claim_id_factory=lambda: "CLM-" + "3" * 32,
    )
    payload = ClaimAuthoringPayloadV3(
        statement=_working_payload(occurrence_count=1).statement.model_copy(
            update={"qualifier": "reused"}
        ),
        rationale="The same accepted observation supports another qualified statement.",
        source=ExistingCaptureCitationSourceV1(capture_digest=capture_digest),
        citation_role="evidence",
        dependency_drafts=ClaimDependencyDraftsV1(),
    )
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp="2026-08-21T12:01:00.000000Z",
    ).intent
    assert isinstance(intent, AuthoringIntentV2)
    assert len(intent.reference_expectations) == 1
    assert intent.reference_expectations[0].address == capture_contract_path(contract.identity.name)

    preflight = coordinator.preflight(intent.intent_id, actor=actor)
    assert preflight.verdict == "passed"
    lowered = lower_authoring(instance, intent=intent, actor_id=actor.actor_id)
    assert lowered.resolved_authoring["capture_digest"] == capture_digest
    assert lowered.resolved_authoring["citation_id"]
    assert capture_contract_path(contract.identity.name) not in {
        path for path, _content in lowered.changed_members
    }
    submitted = coordinator.submit(intent.intent_id, actor=actor)
    assert submitted.status.proposal_id is not None
    _activate(instance, submitted)
    instance.refresh()

    return (
        instance,
        owner,
        actor,
        first.semantic_identity,
        intent.semantic_identity,
        coordinator,
        payload,
    )


def test_existing_capture_submits_through_real_proposal_and_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    (
        instance,
        _owner,
        actor,
        _first_claim_id,
        second_claim_id,
        coordinator,
        payload,
    ) = shared_capture_world(tmp_path)

    retry_payload = payload.model_copy(update={"claim_ref": second_claim_id})
    retry = coordinator.create(
        actor=actor,
        payload=retry_payload,
        canonical_timestamp="2026-08-21T12:02:00.000000Z",
    ).intent
    current_claim = parse_claim(
        instance.tree_at(instance.accepted_coordinate().git_oid)[claim_path(second_claim_id)],
        path=claim_path(second_claim_id),
    )
    retry_lowered = lower_authoring(instance, intent=retry, actor_id=actor.actor_id)
    assert (
        retry_lowered.resolved_authoring["artifact_digest"]
        == claim_artifact_digest(current_claim).tagged
    )
    repeated = coordinator.submit(retry.intent_id, actor=actor)
    assert repeated.status.state == "accepted"
    assert repeated.status.proposal_id is None
    assert retry.last_preflight is None
    assert coordinator.submit(retry.intent_id, actor=actor) == repeated


def test_claim_bound_capture_cannot_be_reused_by_another_claim(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    actor = AuthenticatedActor(actor_id="owner")
    first_coordinator = _coordinator(instance)
    self_source = first_coordinator.create(
        actor=actor,
        payload=_self_source_payload(),
        canonical_timestamp=TIMESTAMP,
    ).intent
    lowered = lower_authoring(instance, intent=self_source, actor_id=actor.actor_id)
    capture_digest = lowered.resolved_authoring["capture_digest"]
    assert isinstance(capture_digest, str)
    second = ClaimAuthoringPayloadV3(
        statement=_working_payload(occurrence_count=1).statement.model_copy(
            update={"qualifier": "other"}
        ),
        rationale="Attempt to reuse another Claim's self source.",
        source=ExistingCaptureCitationSourceV1(capture_digest=capture_digest),
        citation_role="copy",
        dependency_drafts=ClaimDependencyDraftsV1(),
    )
    coordinator = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(instance.root / instance.descriptor.storage.exhaust),
        claim_id_factory=lambda: "CLM-" + "4" * 32,
    )
    intent = coordinator.create(
        actor=actor,
        payload=second,
        canonical_timestamp="2026-08-21T12:03:00.000000Z",
    ).intent

    refusal = coordinator.preflight(intent.intent_id, actor=actor)
    assert refusal.frontier.diagnostics[0].code == ("playbill.claim.self_source_capture_unbound")
    assert refusal.frontier.diagnostics[0].offending_element == "source.capture_digest"


def test_missing_invalid_and_unaccepted_contract_captures_refuse_at_the_source_path(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    actor = AuthenticatedActor(actor_id="owner")
    digests = ["sha256:" + "9" * 64, instance.body_store().store(b"not a capture").digest]
    orphan = build_working_selection_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id="CLM-" + "5" * 32,
        rationale="An orphan Capture remains legal CAS content.",
        observed_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
        accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        source_id="repo.orphan",
        coordinate={
            "source_byte_length": 5,
            "source_content_digest": instance.body_store().store(b"ready").digest,
        },
        selector={"anchor": "ready", "start_byte": 0, "end_byte": 5},
        selected_content=b"ready",
    )
    digests.append(orphan.capture_digest)
    expected = [
        "playbill.authoring.existing_capture_not_found",
        "playbill.authoring.existing_capture_invalid",
        "playbill.authoring.capture_contract_unresolved",
    ]

    for index, (capture_digest, code) in enumerate(zip(digests, expected, strict=True)):
        coordinator = AuthoringIntentCoordinator(
            instance=instance,
            store=AuthoringIntentStore(
                instance.root / instance.descriptor.storage.exhaust,
                token_factory=lambda index=index: f"{index + 6:x}" * 32,
            ),
            claim_id_factory=lambda index=index: f"CLM-{index + 6:032x}",
        )
        payload = ClaimAuthoringPayloadV3(
            statement=_working_payload(occurrence_count=1).statement.model_copy(
                update={"qualifier": f"refusal-{index}"}
            ),
            rationale="Probe the typed cite-existing refusal.",
            source=ExistingCaptureCitationSourceV1(capture_digest=capture_digest),
            citation_role="evidence",
            dependency_drafts=ClaimDependencyDraftsV1(),
        )
        intent = coordinator.create(
            actor=actor,
            payload=payload,
            canonical_timestamp=f"2026-08-21T12:0{index + 4}:00.000000Z",
        ).intent

        result = coordinator.preflight(intent.intent_id, actor=actor)

        assert result.frontier.diagnostics[0].code == code
        assert result.frontier.diagnostics[0].offending_element == "source.capture_digest"
