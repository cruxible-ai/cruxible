"""Admission reuse follows the complete derived question, never the Claim identity."""

from copy import deepcopy

from cruxible_client.contracts import claims
from cruxible_client.contracts.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    AcceptedCaptureContract,
    build_direct_claim_capture,
    capture_contract_digest,
    capture_contract_path,
)
from cruxible_client.contracts.content_memo import artifact_request
from cruxible_core.indexes.projection import AcceptedCoordinate
from tests.core_support._support import initialize_local
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
        claim_type.literal_schema["description"] = "changed"
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
