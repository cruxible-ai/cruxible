"""PB-E structured review and client-held signing tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cruxible_client.contracts.declared_blocks import (
    PlaybillPresentationPolicyV2,
    PlaybillProjectionAdvisoryPolicyV1,
    PlaybillProjectionCoverageBindingV1,
    PlaybillProjectionCoverageObservationV1,
    PlaybillReviewWorkspaceObservationV1,
)
from cruxible_client.contracts.errors import PlaybillKeyError
from cruxible_client.contracts.procedures.artifacts import render_procedure
from cruxible_client.contracts.projection import AcceptedCoordinate as ClientAcceptedCoordinate
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.service.documents import (
    service_propose_playbill_document,
    service_propose_playbill_principal_change,
    service_store_playbill_body,
    service_submit_playbill_approval,
)
from cruxible_core.playbill.service.review import (
    PlaybillProjectionAdvisory,
    PlaybillReviewedMember,
    _projection_advisory,
    render_playbill_proposal_review,
    service_prepare_playbill_approval,
    service_review_playbill_proposal,
)
from cruxible_core.playbill.signing import LocalEd25519ApprovalSigner
from tests.test_playbill._claim_authoring_support import service_propose_playbill_claim
from tests.test_playbill._knowledge_loop_support import activate, authoring
from tests.test_playbill._support import generate_client
from tests.test_playbill.test_authoring_preflight import _seed_claim_surface
from tests.test_playbill.test_graph_v4_provider_closure import _accepted_procedure
from tests.test_service.test_playbill_documents import TIMESTAMP, _instance, _shell

CLAIM_PROJECTION_POLICY = PlaybillPresentationPolicyV2(
    projection_advisories=PlaybillProjectionAdvisoryPolicyV1(claim=True, procedure=True)
)


def _claim_proposal(tmp_path: Path, *, work_item: str = "wi-99"):  # type: ignore[no-untyped-def]
    instance, owner, _reviewer = _instance(tmp_path)
    _seed_claim_surface(instance, owner)
    proposed = service_propose_playbill_claim(
        instance,
        authoring=authoring(work_item, "ready", with_claim_type=False),
        actor_id="owner",
        proposal_name=f"projection-{work_item}",
        timestamp=TIMESTAMP,
    )
    return instance, owner, proposed


def _review_observation(instance, *, coordinate=None):  # type: ignore[no-untyped-def]
    selected = coordinate or instance.accepted_coordinate()
    public = ClientAcceptedCoordinate.model_validate(
        AcceptedCoordinate.from_internal(selected).model_dump(mode="json")
    )
    return PlaybillReviewWorkspaceObservationV1(
        presentation_policy=CLAIM_PROJECTION_POLICY,
        projection_coverage=PlaybillProjectionCoverageObservationV1(
            coordinate=public,
            complete_kinds=("Claim", "Procedure"),
            bindings=(),
        ),
    )


def test_review_and_signing_keep_private_key_outside_wire_contract(tmp_path: Path) -> None:
    instance, _owner, reviewer = _instance(tmp_path)
    body = service_store_playbill_body(instance, content=b"# Playbill\n\nGoverned prose.\n")
    proposed = service_propose_playbill_document(
        instance,
        shell=_shell(body.digest),
        actor_id="owner",
        proposal_name="review",
        timestamp=TIMESTAMP,
    )
    proposal_id = proposed.proposal.admission.proposal_id

    review = service_review_playbill_proposal(
        instance,
        proposal_id=proposal_id,
        access=BodyAccessContext(principal_id="owner", can_read_body=True),
    )
    assert review.coordinate_kind == "provisional"
    assert review.base_oid == review.settlement_base.git_oid
    assert review.parent_semantic_root == review.settlement_base.semantic_root
    assert review.candidate_digest == review.candidate.candidate_digest
    assert review.complete_members == review.candidate.members
    assert review.documents[0].candidate_source_mapping is not None
    assert "+# Playbill" in (review.documents[0].readable_diff or "")
    assert review.attestation_coverage["coverage"] == "containing_change_set"
    rendered = render_playbill_proposal_review(review)
    assert f"Candidate: {review.candidate_digest}" in rendered
    assert f"Settlement base OID: {review.base_oid}" in rendered
    assert f"Proposal admission tier: {review.candidate.required_tier}" in rendered
    assert "Approve requires: graph_write" in rendered
    assert "Activate requires: graph_write" in rendered

    advisory_rendered = render_playbill_proposal_review(
        review.model_copy(
            update={
                "projection_advisory": PlaybillProjectionAdvisory(
                    unprojected_count=1,
                    artifact_identities=("Procedure:release-guard",),
                    message=(
                        "1 changed artifact has no projection coverage; "
                        "reviewers will see raw JSON only"
                    ),
                )
            }
        )
    )
    assert "Projection advisory: 1 changed artifact" in advisory_rendered

    redacted = service_review_playbill_proposal(
        instance,
        proposal_id=proposal_id,
        access=BodyAccessContext(principal_id="reader"),
    )
    assert redacted.documents[0].readable_diff is None
    assert redacted.documents[0].candidate_source_mapping is None
    assert "body" in redacted.redactions
    assert body.digest in redacted.model_dump_json()
    assert "Governed prose" not in redacted.model_dump_json()

    challenge = service_prepare_playbill_approval(
        instance,
        proposal_id=proposal_id,
        signer_id="reviewer",
        access=BodyAccessContext(principal_id="reviewer", can_read_body=True),
    )
    serialized = challenge.model_dump_json()
    assert str(reviewer.private_key_path) not in serialized
    assert challenge.statement.payload_digest == review.candidate_digest
    assert challenge.statement.signing_semantic_root == review.parent_semantic_root

    signer = LocalEd25519ApprovalSigner.open(
        signer_id="reviewer",
        private_key_path=reviewer.private_key_path,
        expected_public_key=challenge.signer_principal.public_key,
        forbidden_roots=(instance.root, tmp_path / "workspace"),
    )
    attestation = signer.sign(challenge.statement)
    receipt = service_submit_playbill_approval(
        instance,
        proposal_id=proposal_id,
        attestation=attestation,
        authenticated_submitter="bearer-owner",
    )
    assert receipt.signer_id == "reviewer"
    assert receipt.submitted_by == "bearer-owner"


def test_local_signer_refuses_exposed_or_wrong_key(tmp_path: Path) -> None:
    instance, owner, reviewer = _instance(tmp_path)
    with pytest.raises(PlaybillKeyError, match="does not match"):
        LocalEd25519ApprovalSigner.open(
            signer_id="owner",
            private_key_path=owner.private_key_path,
            expected_public_key=reviewer.principal.public_key,
            forbidden_roots=(instance.root, tmp_path / "workspace"),
        )

    os.chmod(owner.private_key_path, 0o644)
    with pytest.raises(PlaybillKeyError, match="permissions"):
        LocalEd25519ApprovalSigner.open(
            signer_id="owner",
            private_key_path=owner.private_key_path,
            expected_public_key=owner.principal.public_key,
            forbidden_roots=(instance.root, tmp_path / "workspace"),
        )


def test_lifecycle_review_names_the_proposing_actor(tmp_path: Path) -> None:
    instance, owner, _reviewer = _instance(tmp_path)
    keys = tmp_path / "keys-alice"
    keys.mkdir()
    record = generate_client(
        tmp_path, managed_root=tmp_path / "managed-alice", principal_id="alice", roles=("reviewer",)
    )
    proposal = service_propose_playbill_principal_change(
        instance,
        principal=record.principal,
        proposal_name="alice",
        actor_id=owner.principal.principal_id,
        timestamp=TIMESTAMP,
    )
    review = service_review_playbill_proposal(
        instance,
        proposal_id=proposal.proposal.admission.proposal_id,
        access=BodyAccessContext(principal_id="owner", can_read_body=True),
    )
    rendered = render_playbill_proposal_review(review)
    assert f"{owner.principal.principal_id}'s own signature" in rendered
    assert "Required approvals: none" not in rendered


def test_candidate_projection_advisory_counts_generated_successors_and_excludes_invalidations(
    tmp_path: Path,
) -> None:
    instance, _owner, _reviewer = _instance(tmp_path)
    procedure = _accepted_procedure()
    settlement = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    public = ClientAcceptedCoordinate.model_validate(settlement.model_dump(mode="json"))
    coverage = PlaybillProjectionCoverageObservationV1(
        coordinate=public,
        complete_kinds=("Procedure",),
        bindings=(),
    )

    def member(role: str) -> PlaybillReviewedMember:
        return PlaybillReviewedMember(
            path=procedure.path,
            artifact_kind="procedure",
            disposition="added",
            closure_role=role,  # type: ignore[arg-type]
            predecessor_artifact_digest=None,
            candidate_artifact_digest=procedure.artifact_digest,
            base_semantic_artifact=None,
            candidate_semantic_artifact={},
            semantic_delta=(),
            law_identifier="procedure-law",
            law_digest="sha256:" + "1" * 64,
            law_evidence={},
            dependency_proof_refs=(),
        )

    observation = PlaybillReviewWorkspaceObservationV1(
        presentation_policy=PlaybillPresentationPolicyV2(),
        projection_coverage=coverage,
    )
    advisory = _projection_advisory(
        members=(member("generated_successor"),),
        candidate_tree={procedure.path: render_procedure(procedure.procedure)},
        settlement_base=settlement,
        workspace_observation=observation.model_dump(mode="json"),
    )

    assert advisory is not None
    assert advisory.unprojected_count == 1
    assert advisory.artifact_identities == (procedure.procedure.identity.qualified,)
    assert "reviewers will see raw JSON only" in advisory.message
    assert (
        _projection_advisory(
            members=(member("invalidation"),),
            candidate_tree={procedure.path: render_procedure(procedure.procedure)},
            settlement_base=settlement,
            workspace_observation=observation,
        )
        is None
    )

    projected = observation.model_copy(
        update={
            "projection_coverage": coverage.model_copy(
                update={
                    "bindings": (
                        PlaybillProjectionCoverageBindingV1(
                            artifact=procedure.procedure.identity,
                            workspace_path="runbooks/release.md",
                            evidence_kind="procedure_catalog",
                        ),
                    )
                }
            )
        }
    )
    assert (
        _projection_advisory(
            members=(member("authored"),),
            candidate_tree={procedure.path: render_procedure(procedure.procedure)},
            settlement_base=settlement,
            workspace_observation=projected,
        )
        is None
    )


def test_review_uses_projection_evidence_from_a_newer_accepted_head(tmp_path: Path) -> None:
    instance, owner, proposed = _claim_proposal(tmp_path)
    proposal_id = proposed.proposal.proposal.admission.proposal_id
    access = BodyAccessContext(principal_id="owner", can_read_body=True)
    base = instance.accepted_coordinate()

    at_base = service_review_playbill_proposal(
        instance,
        proposal_id=proposal_id,
        access=access,
        workspace_observation=_review_observation(instance, coordinate=base),
    )
    assert at_base.projection_advisory is not None
    assert at_base.projection_evidence is not None
    assert at_base.projection_evidence.status == "used"

    activate(instance, owner, proposed)
    head = instance.accepted_coordinate()
    assert head != base
    at_head = service_review_playbill_proposal(
        instance,
        proposal_id=proposal_id,
        access=access,
        workspace_observation=_review_observation(instance, coordinate=head),
    )

    assert at_head.projection_advisory is not None
    assert at_head.projection_evidence is not None
    assert at_head.projection_evidence.status == "used"
    assert at_head.projection_evidence.coordinate is not None
    assert at_head.projection_evidence.coordinate.git_oid == head.git_oid
    assert f"Projection evidence: used@{head.git_oid}." in render_playbill_proposal_review(at_head)
    assert instance.accepted_coordinate() == head


def test_review_names_projection_evidence_older_than_the_settlement_base(
    tmp_path: Path,
) -> None:
    instance, owner, first = _claim_proposal(tmp_path)
    stale = instance.accepted_coordinate()
    activate(instance, owner, first)
    second = service_propose_playbill_claim(
        instance,
        authoring=authoring("wi-100", "ready", with_claim_type=False),
        actor_id="owner",
        proposal_name="projection-wi-100",
        timestamp="2026-08-24T17:00:04.000000Z",
    )

    review = service_review_playbill_proposal(
        instance,
        proposal_id=second.proposal.proposal.admission.proposal_id,
        access=BodyAccessContext(principal_id="owner", can_read_body=True),
        workspace_observation=_review_observation(instance, coordinate=stale),
    )

    assert review.projection_advisory is None
    assert review.projection_evidence is not None
    assert review.projection_evidence.status == "rejected"
    assert review.projection_evidence.reason == "coordinate_before_settlement_base"
    assert (
        "Projection evidence: rejected:coordinate_before_settlement_base."
        in render_playbill_proposal_review(review)
    )


@pytest.mark.parametrize(
    ("observation", "reason"),
    (
        ({"tag": "not-a-review-observation"}, "observation_invalid"),
        (
            {"tag": "playbill-review-workspace-observation-v1"},
            "coverage_missing",
        ),
    ),
)
def test_review_names_rejected_projection_evidence(
    tmp_path: Path,
    observation: dict[str, object],
    reason: str,
) -> None:
    instance, _owner, proposed = _claim_proposal(tmp_path)
    review = service_review_playbill_proposal(
        instance,
        proposal_id=proposed.proposal.proposal.admission.proposal_id,
        access=BodyAccessContext(principal_id="owner", can_read_body=True),
        workspace_observation=observation,
    )

    assert review.projection_advisory is None
    assert review.projection_evidence is not None
    assert review.projection_evidence.status == "rejected"
    assert review.projection_evidence.reason == reason
