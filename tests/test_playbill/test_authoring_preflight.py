"""Binding and whole-truth laws for the AuthoringIntent preflight."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.inputs import (
    ClaimInput,
    SelfSourceInput,
    SubjectObjectInput,
)
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
from cruxible_client.contracts.claim_types import ClaimType, claim_type_path, render_claim_type
from cruxible_client.contracts.claims import LiteralClaimObject, SubjectClaimObject
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionRuleV1,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell, render_subject, subject_path
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.proposals import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
)
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.playbill.settlement import ChangeActorBinding
from cruxible_core.service.playbill_floor import service_export_playbill_floor
from tests.test_playbill._support import client_material, initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_claims import _claim_type, _subject

TIMESTAMP = "2026-08-21T12:00:00.000000Z"


def _seed_claim_surface(
    instance: PlaybillInstance,
    _owner: object,
    *,
    contract: CaptureContractV1 = COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    claim_type_override: ClaimType | None = None,
    additional_subjects: tuple[SubjectShell, ...] = (),
) -> None:
    shell = _subject()
    claim_type = (claim_type_override or _claim_type()).model_copy(
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
    for additional in additional_subjects:
        tree[subject_path(additional.subject_kind, additional.subject_id)] = render_subject(
            additional
        )
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


def test_subject_claim_object_requires_matching_type_and_existing_allowed_subject(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    relationship_type = _claim_type().model_copy(
        update={
            "object_kind": "subject",
            "literal_schema": None,
            "allowed_object_subject_kinds": ("project.work_item",),
        }
    )
    _seed_claim_surface(instance, owner, claim_type_override=relationship_type)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    subject_object = SubjectClaimObject(
        address=SemanticAddress.whole_artifact(
            subject_path(_subject().subject_kind, _subject().subject_id)
        )
    )

    accepted = coordinator.create(
        actor=actor,
        payload=_self_source_payload().model_copy(
            update={
                "statement": _self_source_payload().statement.model_copy(
                    update={"object": subject_object}
                )
            }
        ),
        canonical_timestamp=TIMESTAMP,
    ).intent
    assert coordinator.preflight(accepted.intent_id, actor=actor).verdict == "passed"

    missing = coordinator.create(
        actor=actor,
        payload=_self_source_payload().model_copy(
            update={
                "statement": _self_source_payload().statement.model_copy(
                    update={
                        "object": SubjectClaimObject(
                            address=SemanticAddress.whole_artifact(
                                subject_path("project.work_item", "missing")
                            )
                        )
                    }
                )
            }
        ),
        canonical_timestamp="2026-08-21T12:00:01.000000Z",
    ).intent
    missing_result = coordinator.preflight(missing.intent_id, actor=actor)
    missing_diagnostic = next(
        item
        for item in missing_result.frontier.diagnostics
        if item.code == "playbill.authoring.object_subject_not_found"
    )
    assert missing_diagnostic.offending_element == "statement.object.address"
    assert missing_diagnostic.repairs[0].kind == "propose_subject"
    assert "project.work_item/missing" in missing_diagnostic.message

    literal = coordinator.create(
        actor=actor,
        payload=_self_source_payload(),
        canonical_timestamp="2026-08-21T12:00:02.000000Z",
    ).intent
    literal_result = coordinator.preflight(literal.intent_id, actor=actor)
    literal_diagnostic = next(
        item
        for item in literal_result.frontier.diagnostics
        if item.code == "playbill.claim.object_kind_mismatch"
    )
    assert literal_diagnostic.repairs[0].replacement == {"required_object_kind": "subject"}


def test_claim_object_kind_mismatch_is_a_typed_preflight_refusal(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    payload = _self_source_payload().model_copy(
        update={
            "statement": _self_source_payload().statement.model_copy(
                update={
                    "object": SubjectClaimObject(
                        address=SemanticAddress.whole_artifact(
                            subject_path(_subject().subject_kind, _subject().subject_id)
                        )
                    )
                }
            )
        }
    )
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent

    result = coordinator.preflight(intent.intent_id, actor=actor)

    diagnostic = next(
        item
        for item in result.frontier.diagnostics
        if item.code == "playbill.claim.object_kind_mismatch"
    )
    assert diagnostic.offending_element == "statement.object"
    assert diagnostic.repairs[0].kind == "replace_object"
    assert diagnostic.repairs[0].replacement == {"required_object_kind": "literal"}


def test_subject_input_accepts_cve_package_relation_and_populates_floor_profiles(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    vulnerability = _subject().model_copy(
        update={
            "identity": ArtifactIdentity(kind="Subject", name="sec.vulnerability/cve-2026-0001"),
            "subject_kind": "sec.vulnerability",
            "subject_id": "cve-2026-0001",
        }
    )
    package = _subject().model_copy(
        update={
            "identity": ArtifactIdentity(kind="Subject", name="sec.package/demo"),
            "subject_kind": "sec.package",
            "subject_id": "demo",
        }
    )
    predicate = "sec.vuln.affects_package"
    relationship_type = _claim_type().model_copy(
        update={
            "identity": ArtifactIdentity(kind="ClaimType", name=predicate),
            "predicate": predicate,
            "allowed_subject_kinds": ("sec.vulnerability",),
            "object_kind": "subject",
            "literal_schema": None,
            "allowed_object_subject_kinds": ("sec.package",),
            "cardinality": "many",
            "resolution_policy": _claim_type().resolution_policy.model_copy(
                update={"cardinality": "many", "selector": "all"}
            ),
        }
    )
    _seed_claim_surface(
        instance,
        owner,
        claim_type_override=relationship_type,
        additional_subjects=(vulnerability, package),
    )
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    view = coordinator.create_input(
        actor=actor,
        input=ClaimInput(
            kind="claim",
            subject=vulnerability.identity.name,
            predicate=predicate,
            object=SubjectObjectInput(kind="subject", subject=package.identity.name),
            role="observation",
            rationale="The accepted advisory identifies this affected package.",
            source=SelfSourceInput(kind="self_source", body="affected package: demo\n"),
        ),
        canonical_timestamp="2026-08-21T12:00:03.000000Z",
    )
    submitted = coordinator.submit(view.intent.intent_id, actor=actor)
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
        authenticated_submitter="owner",
    )
    assert (
        service_activate_playbill_proposal(
            instance,
            proposal_id=submitted.status.proposal_id,
            activated_by="owner",
        ).status
        == "accepted"
    )

    floor = service_export_playbill_floor(instance)
    outbound = json.loads(floor["subjects/sec.vulnerability/cve-2026-0001.profile.json"])[
        "relations"
    ]
    inbound = json.loads(floor["subjects/sec.package/demo.profile.json"])["relations"]
    assert outbound == [
        {
            "inbound": False,
            "predicate": predicate,
            "tag": "playbill-interface-relation-v1",
            "target": {
                "tag": "playbill-semantic-address-v1",
                "artifact_path": "subjects/sec.package/demo.json",
                "selector": {"scheme": "artifact-v1", "value": ""},
            },
        }
    ]
    assert inbound[0]["inbound"] is True
    assert inbound[0]["predicate"] == predicate


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
