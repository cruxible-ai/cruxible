"""PC-A2 closed Claim policy formats and evaluator parity tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.policies import (
    ClaimAdmissionCandidateContextV1,
    ClaimAdmissionPolicyV1,
    ClaimCorroborationResultV1,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionRuleV1,
    ClaimResolutionPolicyV1,
    CorroborationRequirementV1,
    EvidenceAdmissionInputV1,
    FreezeRequirementV1,
    ResolutionContenderV1,
    evaluate_claim_admission_candidate,
    evaluate_claim_evidence_admission,
    evaluate_claim_evidence_admission_trace,
    resolve_claim_contenders,
)

DIGEST_A = "sha256:" + "11" * 32
DIGEST_B = "sha256:" + "22" * 32


def _review_policy() -> ClaimAdmissionPolicyV1:
    return ClaimAdmissionPolicyV1(
        corroboration_requirements=(
            CorroborationRequirementV1(
                requirement_id="one-valid-approval",
                query_definition_digest=DIGEST_A,
                min_count=1,
            ),
        ),
        freeze_requirements=(
            FreezeRequirementV1(
                requirement_id="approved-review-freeze",
                while_predicate="review.status",
                while_values=("approved",),
                frozen_predicates=("review.change_head", "review.summary"),
            ),
        ),
    )


def _context(
    *,
    parent_status: str = "open",
    candidate_status: str = "approved",
    truncated: bool = False,
) -> ClaimAdmissionCandidateContextV1:
    return ClaimAdmissionCandidateContextV1(
        evaluation_time="2026-08-16T12:00:00.000000Z",
        declared_predicates=("review.change_head", "review.status", "review.summary"),
        parent_values={
            "review.change_head": ("abc",),
            "review.status": (parent_status,),
            "review.summary": ("summary",),
        },
        candidate_values={
            "review.change_head": ("abc",),
            "review.status": (candidate_status,),
            "review.summary": ("summary",),
        },
        corroboration_results=(
            ClaimCorroborationResultV1(
                requirement_id="one-valid-approval",
                query_definition_digest=DIGEST_A,
                parameter_digest=DIGEST_A,
                result_digest=DIGEST_B,
                query_verdict="completed",
                observed_count=1,
                truncated=truncated,
                satisfied=True,
            ),
        ),
    )


def test_corroboration_requirement_evaluates_committed_query_result() -> None:
    candidate = evaluate_claim_admission_candidate(_review_policy(), _context())
    truncated = evaluate_claim_admission_candidate(
        _review_policy(),
        _context(truncated=True),
    )

    assert candidate.verdict == "eligible"
    assert candidate.corroboration_results == _context().corroboration_results
    assert truncated.verdict == "eligible"
    assert truncated.refusal_codes == ()
    assert set(candidate.model_fields_set) == {
        "verdict",
        "corroboration_results",
        "refusal_codes",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("transition_requirements", []),
        ("actor_requirements", []),
    ),
)
def test_deleted_admission_policy_fields_refuse(field: str, value: object) -> None:
    payload = _review_policy().model_dump(mode="json")
    payload[field] = value
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ClaimAdmissionPolicyV1.model_validate(payload)


@pytest.mark.parametrize("field", ("parameters", "max_rows", "max_traversal_depth"))
def test_deleted_corroboration_requirement_fields_refuse(field: str) -> None:
    payload = CorroborationRequirementV1(
        requirement_id="one-valid-approval",
        query_definition_digest=DIGEST_A,
        min_count=1,
    ).model_dump(mode="json")
    payload[field] = {} if field == "parameters" else 1
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CorroborationRequirementV1.model_validate(payload)


def test_admission_refuses_freeze_bypass_and_unknown_predicate() -> None:
    frozen_context = _context(parent_status="approved", candidate_status="approved")
    frozen_context = frozen_context.model_copy(
        update={
            "candidate_values": {
                **frozen_context.candidate_values,
                "review.change_head": ("different",),
            }
        }
    )
    frozen = evaluate_claim_admission_candidate(_review_policy(), frozen_context)
    assert frozen.refusal_codes == ("playbill.claim_policy.freeze_active",)

    unknown = frozen_context.model_copy(update={"declared_predicates": ("review.status",)})
    refused = evaluate_claim_admission_candidate(_review_policy(), unknown)
    assert "playbill.claim_policy.unknown_predicate" in refused.refusal_codes


def test_retained_freeze_exception_must_name_an_existing_transition() -> None:
    payload = _review_policy().model_dump(mode="json")
    payload["freeze_requirements"][0]["except_transition_requirements"] = ["retired-transition"]

    with pytest.raises(ValidationError, match="unknown transition requirement"):
        ClaimAdmissionPolicyV1.model_validate(payload)


def _evidence_policy() -> ClaimEvidenceAdmissionPolicyV1:
    return ClaimEvidenceAdmissionPolicyV1(
        rules=(
            ClaimEvidenceAdmissionRuleV1(
                rule_id="direct-provider-observation",
                claim_roles=("observation",),
                capture_contract_digests=(DIGEST_A,),
                evidence_kinds=("source.observation",),
                admission="direct",
                subject_binding="contract_source_mapping",
                attestation_requirement="verified_provider",
            ),
        )
    )


def _evidence(**updates: object) -> EvidenceAdmissionInputV1:
    values: dict[str, object] = {
        "claim_role": "observation",
        "capture_contract_digest": DIGEST_A,
        "evidence_kind": "source.observation",
        "attestation_grade": "verified_provider",
        "source_subject_bound": True,
    }
    values.update(updates)
    return EvidenceAdmissionInputV1.model_validate(values)


def test_evidence_admission_is_conjunctive_and_never_grants_claim_authority() -> None:
    accepted = evaluate_claim_evidence_admission(_evidence_policy(), _evidence())
    assert accepted.verdict == "eligible"
    assert accepted.rule_id == "direct-provider-observation"

    undeclared = evaluate_claim_evidence_admission(
        _evidence_policy(),
        _evidence(evidence_kind="source.other"),
    )
    assert undeclared.refusal_code == "playbill.evidence.undeclared_contract_kind"

    reducer = evaluate_claim_evidence_admission(
        _evidence_policy(),
        _evidence(reducer_digest=DIGEST_B),
    )
    assert reducer.refusal_code == "playbill.evidence.reducer_not_allowed"

    laundering = evaluate_claim_evidence_admission(
        _evidence_policy(),
        _evidence(capture_claims_semantic_authority=True),
    )
    assert laundering.refusal_code == ("playbill.evidence.capture_cannot_grant_semantic_authority")


def test_evidence_admission_trace_chooses_the_closest_contract_rule_deterministically() -> None:
    direct = _evidence_policy().rules[0]
    alternative = direct.model_copy(
        update={
            "rule_id": "alternate-role",
            "claim_roles": ("derivation",),
            "evidence_kinds": ("source.other",),
            "attestation_requirement": "none",
        }
    )
    policy = ClaimEvidenceAdmissionPolicyV1(
        rules=tuple(sorted((alternative, direct), key=lambda item: item.rule_id.encode("utf-8")))
    )

    trace = evaluate_claim_evidence_admission_trace(
        policy,
        _evidence(attestation_grade="none"),
        subject_binding_by_rule={
            "alternate-role": True,
            "direct-provider-observation": True,
        },
    )

    assert trace.result.refusal_code == "playbill.evidence.attestation_grade_missing"
    assert trace.closest_rule_id == "direct-provider-observation"

    no_contract = evaluate_claim_evidence_admission_trace(
        policy,
        _evidence(capture_contract_digest=DIGEST_B),
    )
    assert no_contract.closest_rule_id is None


def test_evidence_policy_refuses_duplicate_rules_and_illegal_reducer_shapes() -> None:
    direct = _evidence_policy().rules[0]
    with pytest.raises(ValidationError, match="sorted and unique"):
        ClaimEvidenceAdmissionPolicyV1(rules=(direct, direct))
    with pytest.raises(ValidationError, match="only derivational"):
        ClaimEvidenceAdmissionRuleV1(
            **direct.model_dump(exclude={"allowed_reducer_digests"}),
            allowed_reducer_digests=(DIGEST_B,),
        )
    with pytest.raises(ValidationError, match="requires at least one"):
        ClaimEvidenceAdmissionRuleV1(
            **direct.model_dump(exclude={"admission"}),
            admission="derivational",
        )


def test_resolution_preserves_conflict_and_rejects_deleted_authority_selector() -> None:
    policy = ClaimResolutionPolicyV1(
        cardinality="one",
        eligible_verdicts=("supported",),
        selector="only_contender",
    )
    first = ResolutionContenderV1(
        claim_identity="CLM-" + "0a" * 16,
        object_value="open",
        verdict="supported",
    )
    second = ResolutionContenderV1(
        claim_identity="CLM-" + "0b" * 16,
        object_value="closed",
        verdict="supported",
    )
    assert resolve_claim_contenders(policy, (first,)).status == "resolved"
    conflict = resolve_claim_contenders(policy, (first, second))
    assert conflict.status == "unresolved"
    assert set(conflict.contender_claim_identities) == {
        "CLM-" + "0a" * 16,
        "CLM-" + "0b" * 16,
    }
    with pytest.raises(ValidationError):
        ClaimResolutionPolicyV1.model_validate(
            {
                **policy.model_dump(mode="json"),
                "selector": "authority_rule",
                "authority_rule_digest": DIGEST_A,
            }
        )


def test_unknown_policy_requirement_field_refuses_fail_closed() -> None:
    payload = _review_policy().model_dump(mode="json")
    payload["transition_requirements"] = [{"tag": "playbill-transition-requirement-v2"}]
    with pytest.raises(ValidationError):
        ClaimAdmissionPolicyV1.model_validate(payload)


def test_requirement_ids_cannot_alias_across_policy_kinds() -> None:
    with pytest.raises(ValidationError, match="unique across"):
        ClaimAdmissionPolicyV1(
            corroboration_requirements=(
                CorroborationRequirementV1(
                    requirement_id="same-id",
                    query_definition_digest=DIGEST_A,
                    min_count=1,
                ),
            ),
            freeze_requirements=(
                FreezeRequirementV1(
                    requirement_id="same-id",
                    while_predicate="review.status",
                    while_values=("approved",),
                    frozen_predicates=("review.summary",),
                ),
            ),
        )
