"""Cite-existing Capture authoring and shared scope law."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import (
    AuthoringIntentV2,
    ClaimAuthoringPayloadV3,
    ClaimDependencyDraftsV1,
    ExistingCaptureCitationSourceV1,
)
from cruxible_client.contracts.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    CaptureRunCoordinateV1,
    DirectByteSpanSelectionV1,
    InputReceiptSetManifestV1,
    build_cas_capture,
    build_derived_cas_capture,
    build_direct_claim_selection_capture,
    build_working_selection_capture,
    capture_contract_digest,
    capture_contract_path,
    foreign_source_capture_contract,
)
from cruxible_client.contracts.claims import (
    build_claim_citation,
    claim_artifact_digest,
    claim_path,
    claim_statement_address,
    parse_claim,
    render_claim,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.semantic import ContentSpan
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.lowering import lower_authoring
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.proposals import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
)
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from tests.test_playbill._pc_c_support import artifact_digest, capture_contract, digest
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
    assert retry_lowered.resolved_authoring["outcome"] == (
        "playbill.authoring.existing_capture_already_associated"
    )
    assert retry.last_preflight is None
    assert coordinator.submit(retry.intent_id, actor=actor) == repeated


def _direct_selection_intent(
    root: Path,
    *,
    claim_id: str = "CLM-" + "a" * 32,
):  # type: ignore[no-untyped-def]
    instance, owner = initialize_local(root)
    _seed_claim_surface(instance, owner, contract=DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT)
    selected = instance.body_store().store(b"status: ready")
    capture = build_direct_claim_selection_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=claim_id,
        rationale="Bind an already selected exact source span.",
        observed_at=datetime(2026, 8, 21, 12, 1, tzinfo=UTC),
        accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        selection=DirectByteSpanSelectionV1(
            span=ContentSpan(
                content_digest=selected.digest,
                start_byte=0,
                end_byte=len(b"status: ready"),
            )
        ),
    )
    coordinator = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(instance.root / instance.descriptor.storage.exhaust),
        claim_id_factory=lambda: claim_id,
    )
    payload = ClaimAuthoringPayloadV3(
        statement=_working_payload(occurrence_count=1).statement,
        rationale="Reuse the exact direct selection on its bound Claim.",
        source=ExistingCaptureCitationSourceV1(capture_digest=capture.capture_digest),
        citation_role="copy",
        dependency_drafts=ClaimDependencyDraftsV1(),
    )
    intent = coordinator.create(
        actor=AuthenticatedActor(actor_id="owner"),
        payload=payload,
        canonical_timestamp="2026-08-21T12:02:00.000000Z",
    ).intent
    return instance, owner, capture, coordinator, intent, payload


def test_direct_selection_cite_existing_submits_on_its_claim_and_refuses_cross_claim(
    tmp_path: Path,
) -> None:
    instance, _owner, capture, coordinator, intent, payload = _direct_selection_intent(tmp_path)
    actor = AuthenticatedActor(actor_id="owner")

    evidence_intent = coordinator.create(
        actor=actor,
        payload=payload.model_copy(update={"citation_role": "evidence"}),
        canonical_timestamp="2026-08-21T12:02:01.000000Z",
    ).intent
    evidence_refusal = coordinator.preflight(evidence_intent.intent_id, actor=actor)
    assert evidence_refusal.frontier.diagnostics[0].code == (
        "playbill.authoring.existing_capture_not_admitted"
    )
    assert evidence_refusal.frontier.diagnostics[0].offending_element == "citation_role"

    submitted = coordinator.submit(intent.intent_id, actor=actor)
    assert submitted.status.proposal_id is not None
    assert submitted.status.state == "ready_to_activate"

    cross_coordinator = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(instance.root / instance.descriptor.storage.exhaust),
        claim_id_factory=lambda: "CLM-" + "b" * 32,
    )
    cross = cross_coordinator.create(
        actor=actor,
        payload=payload.model_copy(
            update={"statement": payload.statement.model_copy(update={"qualifier": "cross-claim"})}
        ),
        canonical_timestamp="2026-08-21T12:02:02.000000Z",
    ).intent
    cross_refusal = cross_coordinator.preflight(cross.intent_id, actor=actor)
    assert cross_refusal.frontier.diagnostics[0].code == (
        "playbill.claim.self_source_capture_unbound"
    )
    assert cross_refusal.frontier.diagnostics[0].offending_element == "source.capture_digest"


def test_raw_proposal_rechecks_claim_bound_capture_scope_after_lowering_is_bypassed(
    tmp_path: Path,
) -> None:
    instance, _owner, capture, _coordinator, intent, _payload = _direct_selection_intent(tmp_path)
    own = lower_authoring(instance, intent=intent, actor_id="owner")
    own_path = claim_path(intent.semantic_identity)
    own_claim = parse_claim(own.proposed_tree[own_path], path=own_path)
    cross_id = "CLM-" + "b" * 32
    cross_path = claim_path(cross_id)
    cross_identity = ArtifactIdentity(kind="Claim", name=cross_id)
    cross_citation = build_claim_citation(
        cross_identity,
        capture_digest=capture.capture_digest,
        role="copy",
        origin="independent",
    )
    cross_claim = own_claim.model_copy(
        update={
            "identity": cross_identity,
            "backing": own_claim.backing.model_copy(
                update={
                    "citations": (cross_citation,),
                    "source_mappings": tuple(
                        mapping.model_copy(update={"subject": claim_statement_address(cross_path)})
                        for mapping in own_claim.backing.source_mappings
                    ),
                }
            ),
        }
    )
    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    tree[cross_path] = render_claim(cross_claim)

    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/raw-cross-claim-capture",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp="2026-08-21T12:03:00.000000Z",
    )

    assert {item.code for item in result.evaluation.diagnostics} == {
        "playbill.claim.self_source_capture_unbound"
    }


@pytest.mark.parametrize(
    ("grade", "expected_code"),
    [
        ("derived", "playbill.authoring.capture_not_shareable"),
        ("observed", "playbill.authoring.existing_capture_not_admitted"),
    ],
)
def test_existing_capture_shareability_and_admission_refusals_are_reachable(
    tmp_path: Path,
    grade: str,
    expected_code: str,
) -> None:
    instance, owner = initialize_local(tmp_path)
    contract = capture_contract(name=f"test.{grade}-capture-v1", epistemic_grade=grade)
    _seed_claim_surface(instance, owner, contract=contract)
    run = CaptureRunCoordinateV1(
        run_kind="watcher",
        run_id=f"{grade}-capture",
        bound_generation=instance.accepted_coordinate().generation_root,
        executable_identity=contract.identity,
        executable_digest=capture_contract_digest(contract).tagged,
    )
    common = {
        "store": instance.body_store(),
        "contract": contract,
        "run_coordinate": run,
        "run_receipt_digest": digest("receipt", grade),
        "producer": ArtifactIdentity(kind="Principal", name="owner"),
        "producer_binding_digest": digest("binding", grade),
        "observed_at": datetime(2026, 8, 21, 12, 4, tzinfo=UTC),
    }
    if grade == "derived":
        capture = build_derived_cas_capture(
            **common,
            output_body=b'{"status":"ready"}',
            manifest=InputReceiptSetManifestV1(
                input_receipt_digests=(digest("input-receipt", grade),)
            ),
            reducer_digest=artifact_digest("reducer", grade),
        )
    else:
        capture = build_cas_capture(**common, source_body=b'{"status":"ready"}')
    coordinator = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(instance.root / instance.descriptor.storage.exhaust),
        claim_id_factory=lambda: "CLM-" + "c" * 32,
    )
    intent = coordinator.create(
        actor=AuthenticatedActor(actor_id="owner"),
        payload=ClaimAuthoringPayloadV3(
            statement=_working_payload(occurrence_count=1).statement,
            rationale="Probe the exact shareability and admission refusal.",
            source=ExistingCaptureCitationSourceV1(capture_digest=capture.capture_digest),
            citation_role="evidence",
            dependency_drafts=ClaimDependencyDraftsV1(),
        ),
        canonical_timestamp="2026-08-21T12:05:00.000000Z",
    ).intent

    refusal = coordinator.preflight(intent.intent_id, actor=AuthenticatedActor(actor_id="owner"))

    assert refusal.frontier.diagnostics[0].code == expected_code
    assert refusal.frontier.diagnostics[0].offending_element == "source.capture_digest"


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
