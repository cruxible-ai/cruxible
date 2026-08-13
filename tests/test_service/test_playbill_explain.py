"""PB-E coordinate-bound Playbill explanation service tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.errors import PlaybillFormatError
from cruxible_core.playbill.semantic import SemanticAddress, SemanticSelector
from cruxible_core.service.playbill_documents import (
    PlaybillAcceptedCoordinate,
    service_activate_playbill_proposal,
    service_propose_playbill_document,
    service_store_playbill_body,
    service_submit_playbill_approval,
)
from cruxible_core.service.playbill_explain import (
    PlaybillExplainResult,
    PlaybillExplainUnsupportedDetail,
    service_explain_playbill_subject,
)
from tests.test_playbill.test_activation import _sign
from tests.test_service.test_playbill_documents import TIMESTAMP, _instance, _shell


def _accepted(tmp_path: Path):
    instance, owner, reviewer = _instance(tmp_path)
    body = service_store_playbill_body(
        instance,
        content=b"# Playbill design\n\nSecret reviewable prose.\n",
    )
    proposal = service_propose_playbill_document(
        instance,
        shell=_shell(body.digest),
        actor_id="owner",
        proposal_name="explain-design",
        timestamp=TIMESTAMP,
        source_compilation_digest="sha256:" + "77" * 32,
    ).proposal
    assert proposal.candidate is not None
    for material in (owner, reviewer):
        signed = _sign(
            material,
            proposal.candidate.candidate_digest,
            proposal.candidate.candidate.parent_semantic_root,
        )
        service_submit_playbill_approval(
            instance,
            proposal_id=proposal.admission.proposal_id,
            attestation=signed.attestation,
            authenticated_submitter="relay",
        )
    service_activate_playbill_proposal(
        instance,
        proposal_id=proposal.admission.proposal_id,
    )
    return instance, proposal


def test_summary_and_evidence_preserve_coverage_without_body_leakage(tmp_path: Path) -> None:
    instance, proposal = _accepted(tmp_path)
    coordinate = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())
    subject = SemanticAddress.whole_artifact("documents/design.yaml")

    summary = service_explain_playbill_subject(
        instance,
        subject=subject,
        at=coordinate,
        detail="summary",
        access=BodyAccessContext(principal_id="auditor"),
    )
    assert isinstance(summary, PlaybillExplainResult)
    assert summary.coordinate == coordinate
    assert summary.source_mapping is None
    assert summary.redactions == ("body", "source_mapping")
    assert summary.attestation_coverage["coverage_binding"]["coverage"] == (  # type: ignore[index]
        "containing_change_set"
    )
    assert "exact_subject" not in str(summary.model_dump(mode="json"))
    assert {item["signer_id"] for item in summary.attestation_coverage["attestations"]} == {  # type: ignore[union-attr]
        "owner",
        "reviewer",
    }
    assert len(summary.proof_references) == 1
    assert summary.proof_references[0]["candidate_digest"]["$digest"] == (  # type: ignore[index]
        proposal.candidate.candidate_digest  # type: ignore[union-attr]
    )

    evidence = service_explain_playbill_subject(
        instance,
        subject=subject,
        at=coordinate,
        detail="evidence",
        access=BodyAccessContext(principal_id="owner", can_read_body=True),
    )
    assert isinstance(evidence, PlaybillExplainResult)
    assert evidence.redactions == ()
    assert evidence.source_mapping is not None
    assert {item["kind"] for item in evidence.attestation_coverage["basis"]} == {  # type: ignore[union-attr]
        "authority_ruled",
        "cryptographically_committed",
        "replay_verified",
    }
    serialized = str(evidence.model_dump(mode="json"))
    assert "Secret reviewable prose" not in serialized
    assert "deterministic_actions" not in serialized
    assert "private_key" not in serialized


def test_proof_is_typed_deferred_and_coordinate_mixing_refuses(tmp_path: Path) -> None:
    instance, _proposal = _accepted(tmp_path)
    coordinate = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())
    subject = SemanticAddress.whole_artifact("documents/design.yaml")

    proof = service_explain_playbill_subject(
        instance,
        subject=subject,
        at=coordinate,
        detail="proof",
        access=BodyAccessContext(principal_id="auditor"),
    )
    assert isinstance(proof, PlaybillExplainUnsupportedDetail)
    assert proof.supported_details == ("summary", "evidence")

    mixed = coordinate.model_copy(update={"generation_root": "sha256:" + "88" * 32})
    with pytest.raises(PlaybillFormatError, match="mixed"):
        service_explain_playbill_subject(
            instance,
            subject=subject,
            at=mixed,
            detail="summary",
            access=BodyAccessContext(principal_id="auditor"),
        )
    wrong_compiler = coordinate.model_copy(update={"compiler_digest": "sha256:" + "99" * 32})
    with pytest.raises(PlaybillFormatError, match="compiler"):
        service_explain_playbill_subject(
            instance,
            subject=subject,
            at=wrong_compiler,
            detail="summary",
            access=BodyAccessContext(principal_id="auditor"),
        )
    with pytest.raises(ValueError, match="unknown semantic selector"):
        SemanticAddress(
            artifact_path="documents/design.yaml",
            selector=SemanticSelector(scheme="json-pointer-v1", value="/title"),
        )
