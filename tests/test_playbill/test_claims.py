"""PC-B Claim identity, Capture backing, and atomic proposal tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from cruxible_core.playbill.artifacts import (
    ArtifactAuthority,
    ArtifactIdentity,
    ArtifactPin,
)
from cruxible_core.playbill.candidates import CandidateRecordV2
from cruxible_core.playbill.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    build_direct_claim_capture,
    capture_contract_digest,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_core.playbill.claim_types import ClaimType, claim_type_digest, render_claim_type
from cruxible_core.playbill.claims import (
    ClaimArtifact,
    ClaimBacking,
    ClaimLawEvidenceV1,
    ClaimReferentContext,
    ClaimStatement,
    LiteralClaimObject,
    claim_artifact_digest,
    claim_path,
    claim_statement_address,
    claim_statement_digest,
    new_claim_id,
    parse_claim,
    render_claim,
)
from cruxible_core.playbill.policies import (
    ClaimAdmissionPolicyV1,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionRuleV1,
    ClaimResolutionPolicyV1,
)
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.semantic import ContentSpan, SemanticAddress, SourceMapping
from cruxible_core.playbill.subjects import (
    SubjectShell,
    render_subject,
    subject_digest,
    subject_path,
)
from tests.test_playbill._support import initialize_local

TIMESTAMP = "2026-08-16T18:30:00.000000Z"
OBSERVED_AT = datetime(2026, 8, 16, 18, 30, tzinfo=timezone.utc)
AUTHORITY = ArtifactAuthority(propose_roles=("owner",), approve_roles=("owner",))


def _subject() -> SubjectShell:
    return SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name="project.work_item/wi-42"),
        subject_kind="project.work_item",
        subject_id="wi-42",
        authority=AUTHORITY,
    )


def _claim_type() -> ClaimType:
    contract_digest = capture_contract_digest(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT).tagged
    return ClaimType(
        identity=ArtifactIdentity(kind="ClaimType", name="project.work_item.status"),
        predicate="project.work_item.status",
        allowed_subject_kinds=("project.work_item",),
        object_kind="literal",
        literal_schema={"enum": ["blocked", "done", "ready"], "type": "string"},
        cardinality="one",
        permitted_roles=("normative", "observation"),
        evidence_admission_policy=ClaimEvidenceAdmissionPolicyV1(
            rules=(
                ClaimEvidenceAdmissionRuleV1(
                    rule_id="direct-self-asserted",
                    claim_roles=("normative", "observation"),
                    capture_contract_digests=(contract_digest,),
                    evidence_kinds=("self_asserted",),
                    admission="direct",
                    subject_binding="exact_claim_subject",
                ),
            )
        ),
        admission_policy=ClaimAdmissionPolicyV1(),
        resolution_policy=ClaimResolutionPolicyV1(
            cardinality="one",
            eligible_verdicts=("supported",),
            selector="only_contender",
        ),
        authority=AUTHORITY,
    )


def _claim(
    *,
    claim_id: str,
    capture_digest: str,
    source_digest: str,
    source_length: int,
) -> ClaimArtifact:
    shell = _subject()
    claim_type = _claim_type()
    path = claim_path(claim_id)
    return ClaimArtifact(
        identity=ArtifactIdentity(kind="Claim", name=claim_id),
        statement=ClaimStatement(
            subject=SemanticAddress.whole_artifact(
                subject_path(shell.subject_kind, shell.subject_id)
            ),
            claim_type=claim_type.identity,
            claim_type_digest=claim_type_digest(claim_type).tagged,
            predicate=claim_type.predicate,
            object=LiteralClaimObject(value="ready"),
            role="observation",
        ),
        backing=ClaimBacking(
            referent_context=ClaimReferentContext(
                subject_content_digest=subject_digest(shell).tagged,
                observed_at=OBSERVED_AT,
            ),
            capture_digests=(capture_digest,),
            source_mappings=(
                SourceMapping(
                    subject=claim_statement_address(path),
                    spans=(
                        ContentSpan(
                            content_digest=source_digest,
                            start_byte=0,
                            end_byte=source_length,
                        ),
                    ),
                ),
            ),
        ),
        authority=AUTHORITY,
        pins=tuple(
            sorted(
                (
                    ArtifactPin(
                        role="capture-contract",
                        target=DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity,
                        artifact_digest=capture_contract_digest(
                            DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT
                        ).tagged,
                    ),
                    ArtifactPin(
                        role="claim-type",
                        target=claim_type.identity,
                        artifact_digest=claim_type_digest(claim_type).tagged,
                    ),
                    ArtifactPin(
                        role="subject",
                        target=shell.identity,
                        artifact_digest=subject_digest(shell).tagged,
                    ),
                ),
                key=lambda item: (item.role.encode(), item.target.qualified.encode()),
            )
        ),
    )


def test_claim_identity_sharding_and_three_digest_layers(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    claim_id = new_claim_id()
    assert claim_id.startswith("CLM-") and len(claim_id) == 36
    assert claim_path(claim_id) == f"claims/{claim_id[4:6]}/{claim_id}.yaml"
    capture = build_direct_claim_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=claim_id,
        value="ready",
        rationale="The work item is ready for review.",
        observed_at=OBSERVED_AT,
        accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    )
    assert capture.envelope.commitment.byte_length is not None
    claim = _claim(
        claim_id=claim_id,
        capture_digest=capture.capture_digest,
        source_digest=capture.source_body_digest,
        source_length=capture.envelope.commitment.byte_length,
    )
    parsed = parse_claim(render_claim(claim), path=claim_path(claim_id))
    assert parsed == claim
    statement_digest = claim_statement_digest(claim.statement)
    stronger_backing = claim.model_copy(
        update={
            "backing": claim.backing.model_copy(
                update={
                    "input_claim_digests": ("sha256:" + "ab" * 32,),
                    "reducer_digest": "sha256:" + "cd" * 32,
                }
            )
        }
    )
    assert claim_statement_digest(stronger_backing.statement) == statement_digest
    assert claim_artifact_digest(stronger_backing) != claim_artifact_digest(claim)


def test_subject_claim_type_capture_contract_and_claim_form_one_atomic_candidate(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    base = instance.accepted_coordinate()
    claim_id = new_claim_id()
    capture = build_direct_claim_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=claim_id,
        value="ready",
        rationale="The accepted review inputs are complete.",
        observed_at=OBSERVED_AT,
        accepted_coordinate=AcceptedCoordinate.from_internal(base),
    )
    assert capture.envelope.commitment.byte_length is not None
    claim = _claim(
        claim_id=claim_id,
        capture_digest=capture.capture_digest,
        source_digest=capture.source_body_digest,
        source_length=capture.envelope.commitment.byte_length,
    )
    shell = _subject()
    claim_type = _claim_type()
    tree = {
        **instance.tree_at(base.git_oid),
        subject_path(shell.subject_kind, shell.subject_id): render_subject(shell),
        "claim-types/project.work_item/status.yaml": render_claim_type(claim_type),
        capture_contract_path(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity.name): (
            render_capture_contract(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT)
        ),
        claim_path(claim_id): render_claim(claim),
    }
    evaluated = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/first-claim",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp=TIMESTAMP,
    )
    assert not evaluated.evaluation.diagnostics
    assert isinstance(evaluated.candidate, CandidateRecordV2)
    assert tuple(item.artifact_kind for item in evaluated.candidate.members) == (
        "capture-contract",
        "claim-type",
        "claim",
        "subject",
    )
    claim_evidence = ClaimLawEvidenceV1.model_validate(
        next(
            item.result["claim_evidence"]
            for item in evaluated.candidate.law_evidence
            if item.path == claim_path(claim_id)
        )
    )
    assert claim_evidence.initial_verdict == "supported"
    assert claim_evidence.evidence_basis == ("direct",)
