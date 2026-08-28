"""PC-B Claim identity, Capture backing, and atomic proposal tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.candidates import CandidateRecordV3
from cruxible_client.contracts.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    build_direct_claim_capture,
    capture_contract_digest,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_client.contracts.claim_types import ClaimType, claim_type_digest, render_claim_type
from cruxible_client.contracts.claims import (
    ClaimArtifactV2,
    ClaimArtifactV3,
    ClaimBacking,
    ClaimBackingV2,
    ClaimLawEvidenceV1,
    ClaimReferentContext,
    ClaimRetirementAttributionV1,
    ClaimStatement,
    ClaimUnsupportedFormatError,
    LegacyCitationReferenceV1,
    LiteralClaimObject,
    _capture_is_explicitly_eligible,
    _citation_origin_refusal,
    build_claim_citation,
    claim_artifact_digest,
    claim_citation_references,
    claim_path,
    claim_statement_address,
    claim_statement_digest,
    new_claim_id,
    parse_claim,
    render_claim,
)
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicyV1,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionRuleV1,
    ClaimResolutionPolicyV1,
)
from cruxible_client.contracts.semantic import ContentSpan, SemanticAddress, SourceMapping
from cruxible_client.contracts.subjects import (
    SubjectShell,
    render_subject,
    subject_digest,
    subject_path,
)
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.service.playbill_claims import service_playbill_claim_history
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_resolution_contracts import _accept_tree

TIMESTAMP = "2026-08-16T18:30:00.000000Z"
OBSERVED_AT = datetime(2026, 8, 16, 18, 30, tzinfo=timezone.utc)


def _subject() -> SubjectShell:
    return SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name="project.work_item/wi-42"),
        subject_kind="project.work_item",
        subject_id="wi-42",
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
    )


def _claim(
    *,
    claim_id: str,
    capture_digest: str,
    source_digest: str,
    source_length: int,
) -> ClaimArtifactV2:
    shell = _subject()
    claim_type = _claim_type()
    path = claim_path(claim_id)
    return ClaimArtifactV2(
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
        backing=ClaimBackingV2(
            referent_context=ClaimReferentContext(
                subject_content_digest=subject_digest(shell).tagged,
                observed_at=OBSERVED_AT,
            ),
            capture_digests=(capture_digest,),
            citations=(
                build_claim_citation(
                    ArtifactIdentity(kind="Claim", name=claim_id),
                    capture_digest=capture_digest,
                    role="evidence",
                    origin="self_source",
                ),
            ),
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


def test_claim_v3_digest_commits_retirement_attribution_without_moving_v2() -> None:
    predecessor = _claim(
        claim_id="CLM-0123456789abcdef0123456789abcdef",
        capture_digest="sha256:" + "11" * 32,
        source_digest="sha256:" + "22" * 32,
        source_length=1,
    )
    predecessor_digest = claim_artifact_digest(predecessor)
    rescinded = ClaimArtifactV3(
        identity=predecessor.identity,
        statement=predecessor.statement,
        backing=predecessor.backing,
        pins=predecessor.pins,
        lifecycle=ArtifactLifecycle(
            state="retired",
            predecessor_digest=predecessor_digest.tagged,
        ),
        retirement=ClaimRetirementAttributionV1(reason="was-rescinded"),
    )
    wrong = rescinded.model_copy(
        update={"retirement": ClaimRetirementAttributionV1(reason="was-wrong")}
    )

    assert parse_claim(render_claim(rescinded), path=claim_path(rescinded.identity.name)) == (
        rescinded
    )
    assert claim_artifact_digest(rescinded) != claim_artifact_digest(wrong)
    assert claim_artifact_digest(predecessor) == predecessor_digest


def test_claim_v3_preserves_all_legacy_v1_backing_read_and_evidence_laws(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    claim_id = "CLM-fedcbafedcbafedcbafedcbafedcbafe"
    capture = build_direct_claim_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=claim_id,
        value="ready",
        rationale="Exercise the retained v1 backing laws.",
        observed_at=OBSERVED_AT,
        accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    )
    assert capture.envelope.commitment.byte_length is not None
    predecessor = _claim(
        claim_id=claim_id,
        capture_digest=capture.capture_digest,
        source_digest=capture.source_body_digest,
        source_length=capture.envelope.commitment.byte_length,
    )
    legacy_backing = ClaimBacking(
        referent_context=predecessor.backing.referent_context,
        capture_digests=predecessor.backing.capture_digests,
        attestation_digests=predecessor.backing.attestation_digests,
        input_claim_digests=predecessor.backing.input_claim_digests,
        reducer_digest=predecessor.backing.reducer_digest,
        source_mappings=predecessor.backing.source_mappings,
    )
    retired = ClaimArtifactV3(
        identity=predecessor.identity,
        statement=predecessor.statement,
        backing=legacy_backing,
        pins=predecessor.pins,
        lifecycle=ArtifactLifecycle(
            state="retired",
            predecessor_digest=claim_artifact_digest(predecessor).tagged,
        ),
        retirement=ClaimRetirementAttributionV1(reason="was-rescinded"),
    )

    assert parse_claim(render_claim(retired), path=claim_path(claim_id)) == retired
    references = claim_citation_references(retired)
    assert len(references) == 1
    assert isinstance(references[0], LegacyCitationReferenceV1)
    assert references[0].capture_digest == capture.capture_digest
    assert _capture_is_explicitly_eligible(
        retired,
        capture_digest=capture.capture_digest,
    )
    assert (
        _citation_origin_refusal(
            retired,
            capture_digest=capture.capture_digest,
            envelope=capture.envelope,
            contract=capture.contract,
            store=instance.body_store(),
        )
        is None
    )


def test_unknown_claim_wire_has_a_typed_format_refusal() -> None:
    claim_id = "CLM-0123456789abcdef0123456789abcdef"
    with pytest.raises(ClaimUnsupportedFormatError, match="playbill.claim.format_unsupported"):
        parse_claim(
            b'{"artifact_format":"playbill-claim-v999"}\n',
            path=claim_path(claim_id),
        )


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
    assert isinstance(evaluated.candidate, CandidateRecordV3)
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
    assert claim_evidence.initial_verdict == "uncovered"
    assert claim_evidence.evidence_basis == ("origin_only",)


def test_v2_claim_successor_preserves_the_base_accepted_authority_change_shape(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    base = instance.accepted_coordinate()
    claim_id = "CLM-abcdefabcdefabcdefabcdefabcdefab"
    capture = build_direct_claim_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=claim_id,
        value="ready",
        rationale="Seed the v2 succession law.",
        observed_at=OBSERVED_AT,
        accepted_coordinate=AcceptedCoordinate.from_internal(base),
    )
    assert capture.envelope.commitment.byte_length is not None
    predecessor = _claim(
        claim_id=claim_id,
        capture_digest=capture.capture_digest,
        source_digest=capture.source_body_digest,
        source_length=capture.envelope.commitment.byte_length,
    )
    shell = _subject()
    tree = {
        **instance.tree_at(base.git_oid),
        subject_path(shell.subject_kind, shell.subject_id): render_subject(shell),
        "claim-types/project.work_item/status.yaml": render_claim_type(_claim_type()),
        capture_contract_path(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity.name): (
            render_capture_contract(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT)
        ),
        claim_path(claim_id): render_claim(predecessor),
    }
    _accept_tree(
        instance,
        owner,
        tree,
        timestamp=TIMESTAMP,
        proposal_name="v2-authority-seed",
    )

    accepted = instance.accepted_coordinate()
    successor = predecessor.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=claim_artifact_digest(predecessor).tagged
            ),
        }
    )
    successor_tree = instance.tree_at(accepted.git_oid)
    successor_tree[claim_path(claim_id)] = render_claim(successor)
    evaluated = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/v2-authority-successor",
            proposed_base_oid=accepted.git_oid,
        ),
        candidate_tree=successor_tree,
        timestamp=TIMESTAMP,
    )
    assert evaluated.candidate is not None
    assert "playbill.claim.authority_change_unsupported" not in {
        item.code for item in evaluated.evaluation.diagnostics
    }


def test_service_claim_history_returns_each_accepted_lineage_entry(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    base = instance.accepted_coordinate()
    claim_id = "CLM-aabbccddaabbccddaabbccddaabbccdd"
    capture = build_direct_claim_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=claim_id,
        value="ready",
        rationale="Seed the history service law.",
        observed_at=OBSERVED_AT,
        accepted_coordinate=AcceptedCoordinate.from_internal(base),
    )
    assert capture.envelope.commitment.byte_length is not None
    predecessor = _claim(
        claim_id=claim_id,
        capture_digest=capture.capture_digest,
        source_digest=capture.source_body_digest,
        source_length=capture.envelope.commitment.byte_length,
    )
    shell = _subject()
    tree = {
        **instance.tree_at(base.git_oid),
        subject_path(shell.subject_kind, shell.subject_id): render_subject(shell),
        "claim-types/project.work_item/status.yaml": render_claim_type(_claim_type()),
        capture_contract_path(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity.name): (
            render_capture_contract(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT)
        ),
        claim_path(claim_id): render_claim(predecessor),
    }
    _accept_tree(
        instance,
        owner,
        tree,
        timestamp=TIMESTAMP,
        proposal_name="history-seed",
    )

    accepted = instance.accepted_coordinate()
    successor = predecessor.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=claim_artifact_digest(predecessor).tagged
            ),
        }
    )
    successor_tree = instance.tree_at(accepted.git_oid)
    successor_tree[claim_path(claim_id)] = render_claim(successor)
    _accept_tree(
        instance,
        owner,
        successor_tree,
        timestamp=TIMESTAMP,
        proposal_name="history-successor",
    )

    history = service_playbill_claim_history(instance, identity=f"Claim:{claim_id}")

    assert history.identity == f"Claim:{claim_id}"
    assert tuple(entry.sequence for entry in history.entries) == (1, 2)
    assert history.entries[0].artifact_digest == claim_artifact_digest(predecessor).tagged
    assert history.entries[0].predecessor_digest is None
    assert history.entries[1].artifact_digest == claim_artifact_digest(successor).tagged
    assert history.entries[1].predecessor_digest == claim_artifact_digest(predecessor).tagged
    assert tuple(entry.lifecycle_state for entry in history.entries) == ("live", "live")
    assert tuple(entry.change_set_path for entry in history.entries) == (
        "changesets/cs-00000000000000000001.json",
        "changesets/cs-00000000000000000002.json",
    )
    assert all(entry.changeset_digest for entry in history.entries)
    assert all(entry.candidate_digest for entry in history.entries)
