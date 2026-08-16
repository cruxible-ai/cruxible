"""PC-B exact-content and selected-source validation."""

from __future__ import annotations

from pathlib import Path

from cruxible_core.playbill.artifacts import ArtifactIdentity
from cruxible_core.playbill.captures import DirectByteSpanSelectionV1
from cruxible_core.playbill.claim_types import ClaimType, claim_type_digest
from cruxible_core.playbill.claims import ClaimStatement, ExactContentClaimObject
from cruxible_core.playbill.semantic import ContentSpan, SemanticAddress
from cruxible_core.service.playbill_claims import (
    DirectClaimAuthoringV1,
    service_propose_playbill_claim,
)
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_claims import _claim_type, _subject
from tests.test_playbill.test_direct_claim_authoring import TIMESTAMP


def _exact_content_type() -> ClaimType:
    original = _claim_type()
    rule = original.evidence_admission_policy.rules[0].model_copy(
        update={"claim_roles": ("environment_binding",)}
    )
    return original.model_copy(
        update={
            "identity": ArtifactIdentity(
                kind="ClaimType",
                name="project.work_item.definition_bytes",
            ),
            "predicate": "project.work_item.definition_bytes",
            "object_kind": "exact_content",
            "literal_schema": None,
            "permitted_roles": ("environment_binding",),
            "evidence_admission_policy": original.evidence_admission_policy.model_copy(
                update={"rules": (rule,)}
            ),
        }
    )


def test_exact_content_span_is_bound_without_a_document_artifact(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    stored = instance.body_store().store(b"python=3.13.5\n")
    span = ContentSpan(content_digest=stored.digest, start_byte=7, end_byte=13)
    claim_type = _exact_content_type()
    shell = _subject()
    proposed = service_propose_playbill_claim(
        instance,
        authoring=DirectClaimAuthoringV1(
            statement=ClaimStatement(
                subject=SemanticAddress.whole_artifact(
                    f"subjects/{shell.subject_kind}/{shell.subject_id}.yaml"
                ),
                claim_type=claim_type.identity,
                claim_type_digest=claim_type_digest(claim_type).tagged,
                predicate=claim_type.predicate,
                object=ExactContentClaimObject(content_digest=stored.digest, span=span),
                role="environment_binding",
            ),
            rationale="Bind the exact runtime definition bytes.",
            subject_shell=shell,
            claim_type_artifact=claim_type,
            source_selection=DirectByteSpanSelectionV1(
                span=span,
                media_type="text/plain",
            ),
        ),
        actor_id="owner",
        proposal_name="exact-runtime-bytes",
        timestamp=TIMESTAMP,
    )
    assert proposed.proposal.proposal.candidate is not None
    evaluated_oid = proposed.proposal.proposal.evaluation.evaluated_tree_oid
    assert evaluated_oid is not None
    assert not any(path.startswith("documents/") for path in instance.proposal_tree(evaluated_oid))


def test_exact_content_without_matching_capture_refuses(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    stored = instance.body_store().store(b"python=3.13.5\n")
    span = ContentSpan(content_digest=stored.digest, start_byte=7, end_byte=13)
    claim_type = _exact_content_type()
    shell = _subject()
    proposed = service_propose_playbill_claim(
        instance,
        authoring=DirectClaimAuthoringV1(
            statement=ClaimStatement(
                subject=SemanticAddress.whole_artifact(
                    f"subjects/{shell.subject_kind}/{shell.subject_id}.yaml"
                ),
                claim_type=claim_type.identity,
                claim_type_digest=claim_type_digest(claim_type).tagged,
                predicate=claim_type.predicate,
                object=ExactContentClaimObject(content_digest=stored.digest, span=span),
                role="environment_binding",
            ),
            rationale="Claim exact bytes without selecting their Capture.",
            subject_shell=shell,
            claim_type_artifact=claim_type,
        ),
        actor_id="owner",
        proposal_name="unbound-runtime-bytes",
        timestamp=TIMESTAMP,
    )
    assert proposed.proposal.proposal.candidate is None
    assert {item.code for item in proposed.proposal.proposal.evaluation.diagnostics} == {
        "playbill.claim.exact_content_unverified"
    }
