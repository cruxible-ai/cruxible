"""Admission reuse follows the complete derived question, never the Claim identity."""

from copy import deepcopy
from datetime import datetime, timezone

from cruxible_client.contracts import claims
from cruxible_client.contracts.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    AcceptedCaptureContract,
    DirectForeignSourceSelection,
    build_foreign_source_capture,
    capture_contract_digest,
    capture_contract_path,
    foreign_source_capture_contract,
)
from cruxible_client.contracts.content_memo import artifact_request
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
)
from cruxible_client.contracts.semantic import ContentSpan
from cruxible_core.indexes.projection import AcceptedCoordinate
from tests.core_support._support import initialize_local
from tests.core_support.legacy_capture_builders import build_direct_claim_capture
from tests.test_claims.test_claims import OBSERVED_AT, _claim, _claim_type


def test_admission_carrier_matches_cold_and_releases_at_request_exit(tmp_path, monkeypatch):
    instance, _ = initialize_local(tmp_path)
    claim_id = "CLM-" + "a" * 32
    capture = build_direct_claim_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=claim_id,
        value="ready",
        rationale="ready",
        observed_at=OBSERVED_AT,
        accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    )
    claim = _claim(
        claim_id=claim_id,
        capture_digest=capture.capture_digest,
        source_digest=capture.source_body_digest,
        source_length=1,
    )
    contract = DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT
    accepted = AcceptedCaptureContract(
        path=capture_contract_path(contract.identity.name),
        contract=contract,
        artifact_digest=capture_contract_digest(contract).tagged,
    )
    claim_type = _claim_type()
    kwargs = dict(
        claim_type=claim_type,
        capture_digest=capture.capture_digest,
        capture_contract=accepted,
        envelope=capture.envelope,
        verified_attestations=(),
    )
    cold = claims.evaluate_capture_evidence_admissions(claim, **kwargs)
    assert cold[0].trace.result.verdict == "eligible"
    original = claims.evaluate_claim_evidence_admission_trace
    calls = []

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(claims, "evaluate_claim_evidence_admission_trace", counted)
    with artifact_request():
        first = claims.evaluate_capture_evidence_admissions(claim, **kwargs)
        assert first == cold
        again = claims.evaluate_capture_evidence_admissions(deepcopy(claim), **kwargs)
        assert again[0] is first[0]
        assert len(calls) == 1
        other_role = claim.model_copy(
            update={"statement": claim.statement.model_copy(update={"role": "normative"})}
        )
        changed = claims.evaluate_capture_evidence_admissions(other_role, **kwargs)
        assert changed == cold
        assert changed[0] is not first[0]
        assert len(calls) == 2
        # Unrelated ClaimType metadata does not change the policy question.
        kwargs["claim_type"] = claim_type.model_copy(update={"description": "changed"})
        assert claims.evaluate_capture_evidence_admissions(claim, **kwargs) == cold
        assert len(calls) == 2
        # The entire policy, including the selected rule's identity, is keyed.
        changed_type = claim_type.model_copy(
            update={
                "evidence_admission_policy": claim_type.evidence_admission_policy.model_copy(
                    update={
                        "rules": (
                            claim_type.evidence_admission_policy.rules[0].model_copy(
                                update={"rule_id": "renamed"}
                            ),
                        )
                    }
                )
            }
        )
        kwargs["claim_type"] = changed_type
        changed_policy = claims.evaluate_capture_evidence_admissions(claim, **kwargs)
        assert changed_policy[0].trace.result.rule_id == "renamed"
        assert len(calls) == 3
        silent = claim_type.model_copy(
            update={
                "evidence_admission_policy": claim_type.evidence_admission_policy.model_copy(
                    update={"rules": ()}
                )
            }
        )
        kwargs["claim_type"] = silent
        refused = claims.evaluate_capture_evidence_admissions(claim, **kwargs)
        assert refused[0].trace.result.verdict == "refused"
        assert claims.evaluate_capture_evidence_admissions(claim, **kwargs) == refused
        assert len(calls) == 5
    kwargs["claim_type"] = claim_type
    assert claims.evaluate_capture_evidence_admissions(claim, **kwargs) == cold
    assert len(calls) == 6


def _family_claim_type() -> object:
    """A ClaimType whose one evidence rule names only the workspace_file family."""

    return _claim_type().model_copy(
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(
                rules=(
                    ClaimEvidenceAdmissionRule(
                        rule_id="workspace",
                        claim_roles=("normative", "observation"),
                        capture_contract_families=("workspace_file",),
                        admission="direct",
                        subject_binding="exact_claim_subject",
                    ),
                )
            )
        }
    )


def _foreign_capture(instance, *, source_id: str, text: bytes):
    store = instance.body_store()
    stored = store.store(text)
    return build_foreign_source_capture(
        store=store,
        actor_id="owner",
        claim_id="CLM-" + "a" * 32,
        rationale=f"cited from catalogued file {source_id}",
        observed_at=datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
        accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        selection=DirectForeignSourceSelection(
            logical_source_identity=source_id,
            span=ContentSpan(content_digest=stored.digest, start_byte=0, end_byte=len(text)),
        ),
    )


def test_a_family_rule_admits_two_catalogued_files_and_refuses_a_non_file_capture(
    tmp_path,
) -> None:
    """D1 (2026-11-06), exercised through the real admission entry point.

    ``claims.evaluate_capture_evidence_admissions`` is what a Claim's actual
    write and verdict paths call; this proves the family selector works there,
    not only in the pure policy evaluator.
    """

    instance, _ = initialize_local(tmp_path)
    claim_id = "CLM-" + "a" * 32
    claim_type = _family_claim_type()

    for source_id, text in (("repo.file-a", b"status: ready"), ("repo.file-b", b"status: done")):
        built = _foreign_capture(instance, source_id=source_id, text=text)
        contract = foreign_source_capture_contract(source_id)
        accepted = AcceptedCaptureContract(
            path=capture_contract_path(contract.identity.name),
            contract=contract,
            artifact_digest=capture_contract_digest(contract).tagged,
        )
        claim = _claim(
            claim_id=claim_id,
            capture_digest=built.capture_digest,
            source_digest=built.source_body_digest,
            source_length=len(text),
            contract=contract,
        )
        decisions = claims.evaluate_capture_evidence_admissions(
            claim,
            claim_type=claim_type,
            capture_digest=built.capture_digest,
            capture_contract=accepted,
            envelope=built.envelope,
            verified_attestations=(),
        )
        assert decisions[0].trace.result.verdict == "eligible"
        assert decisions[0].trace.result.rule_id == "workspace"

    # A Capture under the coordinator self-source contract (not a catalogued
    # workspace file -- a different declared shape) is refused.
    direct = build_direct_claim_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=claim_id,
        value="ready",
        rationale="ready",
        observed_at=OBSERVED_AT,
        accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    )
    direct_contract = DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT
    direct_accepted = AcceptedCaptureContract(
        path=capture_contract_path(direct_contract.identity.name),
        contract=direct_contract,
        artifact_digest=capture_contract_digest(direct_contract).tagged,
    )
    direct_claim = _claim(
        claim_id=claim_id,
        capture_digest=direct.capture_digest,
        source_digest=direct.source_body_digest,
        source_length=1,
        contract=direct_contract,
    )
    refused = claims.evaluate_capture_evidence_admissions(
        direct_claim,
        claim_type=claim_type,
        capture_digest=direct.capture_digest,
        capture_contract=direct_accepted,
        envelope=direct.envelope,
        verified_attestations=(),
    )
    assert refused[0].trace.result.verdict == "refused"
