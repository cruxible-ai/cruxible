"""Light contract wire bytes, source admission, and frozen legacy meanings."""

from __future__ import annotations

import pytest

from cruxible_client.contracts.capture_contract_inputs import (
    CaptureContractInputRecord,
    lower_capture_contract_input,
)
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT_V1,
    CaptureContractV1,
    capture_contract_path,
    capture_contract_successor_break,
    contract_source_kinds,
    parse_capture_contract,
    render_capture_contract,
)
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionPolicyV3,
    ClaimEvidenceAdmissionRule,
    ClaimEvidenceAdmissionRuleV3,
    EvidenceAdmissionInput,
    evaluate_claim_evidence_admission,
)


def test_v1_bytes_are_read_without_reinterpretation():
    contract = COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT_V1
    content = render_capture_contract(contract)
    parsed = parse_capture_contract(content, path=capture_contract_path(contract.identity.name))
    assert isinstance(parsed, CaptureContractV1)
    assert render_capture_contract(parsed) == content
    assert (
        capture_contract_successor_break(contract, COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT) is None
    )


@pytest.mark.parametrize(
    "shape,kinds",
    [
        ("http_response", ("external",)),
        ("procedure_output", ("cas",)),
        ("foreign_span", ("external",)),
        ("database_snapshot", ("external", "ledger")),
    ],
)
def test_authored_shape_freezes_components_without_redundant_sets(shape, kinds):
    contract = lower_capture_contract_input(
        CaptureContractInputRecord(name="test.capture", shape=shape)
    )
    assert contract_source_kinds(contract) == kinds
    wire = contract.model_dump(mode="json")
    assert not {"allowed_source_kinds", "evidence_kinds", "logical_source_identities"} & wire.keys()
    assert wire["components"]["commitment_canonicalizer"]
    assert (
        parse_capture_contract(
            render_capture_contract(contract), path=capture_contract_path(contract.identity.name)
        )
        == contract
    )


def test_edit_is_a_merge_patch_and_budget_can_only_rise():
    contract = lower_capture_contract_input(
        CaptureContractInputRecord(name="test.capture", shape="http_response")
    )
    raised = lower_capture_contract_input(
        CaptureContractInputRecord(name="test.capture", max_bytes=32 * 1024 * 1024),
        predecessor=contract,
    )
    assert raised.components == contract.components
    assert raised.retention_erasure_policy == contract.retention_erasure_policy
    assert raised.lifecycle.predecessor_digest is not None
    with pytest.raises(Exception, match="incompatible_successor"):
        lower_capture_contract_input(
            CaptureContractInputRecord(name="test.capture", max_bytes=1), predecessor=contract
        )


def _rule(**kwargs):
    return dict(
        rule_id="observed",
        claim_roles=("observation",),
        admission="direct",
        subject_binding="contract_source_mapping",
        **kwargs,
    )


def _evidence(**kwargs):
    return EvidenceAdmissionInput(
        claim_role="observation",
        capture_contract_digest="sha256:" + "1" * 64,
        capture_contract_identity="CaptureContract:test.capture",
        source_subject_bound=True,
        **kwargs,
    )


@pytest.mark.parametrize(
    "source,verdict", [("osv.record", "eligible"), ("kev.record", "refused"), (None, "refused")]
)
def test_v4_source_restriction_matches_recorded_external_source_only(source, verdict):
    rule = ClaimEvidenceAdmissionRule(
        **_rule(capture_contracts=[{"contract": "test.capture", "logical_sources": ["osv.record"]}])
    )
    result = evaluate_claim_evidence_admission(
        ClaimEvidenceAdmissionPolicy(rules=(rule,)), _evidence(logical_source=source)
    )
    assert result.verdict == verdict


def test_v4_unrestricted_contract_admits_cas_and_v3_keeps_kind_gate_only_for_v1():
    current = ClaimEvidenceAdmissionRule(**_rule(capture_contracts=["test.capture"]))
    assert (
        evaluate_claim_evidence_admission(
            ClaimEvidenceAdmissionPolicy(rules=(current,)), _evidence()
        ).verdict
        == "eligible"
    )
    legacy = ClaimEvidenceAdmissionRuleV3(
        **_rule(capture_contracts=current.capture_contracts, evidence_kinds=("observed",))
    )
    policy = ClaimEvidenceAdmissionPolicyV3(rules=(legacy,))
    assert (
        evaluate_claim_evidence_admission(policy, _evidence(evidence_kind="observed")).verdict
        == "eligible"
    )
    assert (
        evaluate_claim_evidence_admission(policy, _evidence(evidence_kind="other")).verdict
        == "refused"
    )
    assert evaluate_claim_evidence_admission(policy, _evidence()).verdict == "eligible"
