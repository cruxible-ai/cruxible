"""Binding and whole-truth laws for the AuthoringIntent preflight."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from cruxible_client.contracts.authoring.models import (
    AuthoringClaimStatementV1,
    ClaimAuthoringPayloadV1,
    InsertionAnchorWindowV1,
    InsertionTargetV2,
    SelfSourceBodyV1,
    WorkingAnchorWindowV1,
    WorkingDigestCoordinateV1,
    WorkingSelectionObservationV1,
)
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    CaptureContractV1,
    capture_contract_digest,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_client.contracts.claims import LiteralClaimObject
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionRuleV1,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import render_subject, subject_path
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.proposals import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
)
from cruxible_core.playbill.settlement import ChangeActorBinding
from tests.test_playbill._support import client_material, initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_claims import _claim_type, _subject

TIMESTAMP = "2026-08-21T12:00:00.000000Z"


def _seed_claim_surface(
    instance: PlaybillInstance,
    _owner: object,
    *,
    contract: CaptureContractV1 = COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
) -> None:
    shell = _subject()
    claim_type = _claim_type().model_copy(
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicyV1(
                rules=(
                    ClaimEvidenceAdmissionRuleV1(
                        rule_id="coordinator-source",
                        claim_roles=("normative", "observation"),
                        capture_contract_digests=(capture_contract_digest(contract).tagged,),
                        evidence_kinds=("self_asserted",),
                        admission="direct",
                        subject_binding="exact_claim_subject",
                    ),
                )
            )
        }
    )
    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    tree[subject_path(shell.subject_kind, shell.subject_id)] = render_subject(shell)
    tree[claim_type_path(claim_type.predicate)] = render_claim_type(claim_type)
    tree[capture_contract_path(contract.identity.name)] = render_capture_contract(contract)
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/authoring-seed",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp="2026-08-21T11:59:00.000000Z",
    )
    assert proposed.candidate is not None
    assert proposed.evaluation.evaluated_tree_oid is not None
    bundle = instance.prepare_generation(
        base=base,
        candidate_tree=instance.proposal_tree(proposed.evaluation.evaluated_tree_oid),
        candidate=proposed.candidate,
        approvals=(
            _sign(
                client_material(instance.root.parent, instance),
                proposed.candidate.candidate_digest,
                base.semantic_root,
            ),
        ),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        proposal_actor_id="owner",
        sequence=1,
    )
    publisher = instance.activation_publisher()
    projection = publisher.prebuild(bundle, base=base)
    assert publisher.activate(bundle, projection, base=base).status == "accepted"
    instance.refresh()


def _coordinator(instance: PlaybillInstance) -> AuthoringIntentCoordinator:
    exhaust = instance.root / instance.descriptor.storage.exhaust
    return AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(exhaust, token_factory=lambda: "1" * 32),
        claim_id_factory=lambda: "CLM-" + "2" * 32,
    )


def _self_source_payload(*, insertion_target: object | None = None) -> ClaimAuthoringPayloadV1:
    return ClaimAuthoringPayloadV1(
        statement=AuthoringClaimStatementV1(
            subject=SemanticAddress.whole_artifact(
                subject_path(_subject().subject_kind, _subject().subject_id)
            ),
            predicate=_claim_type().predicate,
            object=LiteralClaimObject(value="ready"),
            role="observation",
        ),
        rationale="The writer observed the current work status.",
        source=SelfSourceBodyV1(
            content_base64=base64.b64encode(b"status: ready\n").decode("ascii")
        ),
        insertion_target=insertion_target,
    )


def _working_payload(
    *,
    occurrence_count: int,
    selected_occurrence: int | None = None,
) -> ClaimAuthoringPayloadV1:
    selected = b"status: ready"
    digest = "sha256:" + hashlib.sha256(selected).hexdigest()
    return ClaimAuthoringPayloadV1(
        statement=_self_source_payload().statement,
        rationale="The repository snapshot says the work is ready.",
        source=WorkingSelectionObservationV1(
            source_id="repo.work-items",
            coordinate=WorkingDigestCoordinateV1(
                source_content_digest=digest,
                source_byte_length=len(selected),
            ),
            selected_content_base64=base64.b64encode(selected).decode("ascii"),
            selected_bytes_digest=digest,
            selector=WorkingAnchorWindowV1(
                anchor="status: ready",
                start_byte=0,
                end_byte=len(selected),
                observed_occurrence_count=occurrence_count,
                selected_occurrence=selected_occurrence,
            ),
        ),
        citation_role="evidence",
    )


def test_preflight_is_deterministic_and_binds_the_current_coordinate(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    intent = coordinator.create(
        actor=actor,
        payload=_self_source_payload(),
        canonical_timestamp=TIMESTAMP,
    ).intent

    first = coordinator.preflight(intent.intent_id, actor=actor)
    second = coordinator.preflight(intent.intent_id, actor=actor)

    assert second == first
    assert first.certificate.accepted_coordinate.git_oid == instance.accepted_coordinate().git_oid
    assert first.certificate.payload_digest == intent.payload_digest
    assert first.certificate.certificate_digest.startswith("sha256:")


def test_preflight_returns_independent_refusals_in_one_frontier(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    preimage = b"Status: "
    payload = _working_payload(occurrence_count=2).model_copy(
        update={
            "insertion_target": InsertionTargetV2(
                source_id="repo.work-items",
                coordinate=WorkingDigestCoordinateV1(
                    source_content_digest="sha256:" + hashlib.sha256(preimage).hexdigest(),
                    source_byte_length=len(preimage),
                ),
                initial_preimage_digest="sha256:" + hashlib.sha256(preimage).hexdigest(),
                initial_preimage_byte_length=len(preimage),
                selector=InsertionAnchorWindowV1(
                    anchor_content_base64=base64.b64encode(preimage).decode("ascii"),
                    anchor_bytes_digest="sha256:" + hashlib.sha256(preimage).hexdigest(),
                    start_byte=0,
                    end_byte=len(preimage),
                    insertion_offset=len(preimage),
                    observed_occurrence_count=1,
                ),
                operation="insert_after",
            )
        }
    )
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent

    result = coordinator.preflight(intent.intent_id, actor=actor)

    codes = {item.code for item in result.frontier.diagnostics}
    assert "playbill.authoring.insertion_target_requires_self_source" in codes
    assert "playbill.authoring.working_selection_ambiguous" in codes
    assert result.verdict == "refused"
    assert result.frontier.frontier_complete is True
    assert all(
        item.repairs or item.disposition in {"wait", "terminal"}
        for item in result.frontier.diagnostics
    )


def test_preflight_accepts_a_client_selected_occurrence_from_multiple_matches(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    intent = coordinator.create(
        actor=actor,
        payload=_working_payload(occurrence_count=2, selected_occurrence=2),
        canonical_timestamp=TIMESTAMP,
    ).intent

    result = coordinator.preflight(intent.intent_id, actor=actor)

    assert result.verdict == "passed"
    assert result.frontier.diagnostics == ()


def test_preflight_refuses_an_actor_absent_from_the_principal_registry(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="unregistered-writer")
    intent = coordinator.create(
        actor=actor,
        payload=_self_source_payload(),
        canonical_timestamp=TIMESTAMP,
    ).intent

    result = coordinator.preflight(intent.intent_id, actor=actor)

    diagnostic = next(
        item
        for item in result.frontier.diagnostics
        if item.code == "playbill.proposal.creator_principal_invalid"
    )
    assert "active Principal" in diagnostic.message
