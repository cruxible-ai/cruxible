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


def test_contract_proposal_lowers_against_exact_base(tmp_path):
    from cruxible_core.service.evidence.capture_contracts import service_propose_capture_contract
    from tests.core_support._support import initialize_local

    instance, _owner = initialize_local(tmp_path)
    result = service_propose_capture_contract(
        instance,
        input=CaptureContractInputRecord(name="test.http", shape="http_response"),
        actor_id="owner",
        proposal_name="new-http-contract",
        timestamp="2026-10-24T12:00:00.000000Z",
    )
    assert result.proposal.candidate is not None, result.proposal.evaluation.diagnostics


def test_source_overlap_is_per_contract_and_empty_means_all():
    from cruxible_core.service.claims.evidence_rule_upgrade import _overlaps

    def rule(source):
        return ClaimEvidenceAdmissionRule(
            **_rule(capture_contracts=[{"contract": "test.capture", "logical_sources": source}])
        )

    assert not _overlaps(rule(["osv.record"]), rule(["kev.record"]))
    assert _overlaps(rule([]), rule(["osv.record"]))
    assert _overlaps(rule(["osv.record"]), rule(["osv.record"]))


def test_source_admission_lookup_work_is_independent_of_rule_contract_count():
    """One immutable compilation, then one keyed lookup per Capture."""
    from cruxible_client.contracts.content_memo import seal
    from cruxible_client.contracts.policies import _rule_source_sets

    for size in (10, 100):
        rule = ClaimEvidenceAdmissionRule(
            **_rule(
                capture_contracts=[
                    {"contract": f"test.c{i:03d}", "logical_sources": ["osv.record"]}
                    for i in range(size)
                ]
            )
        )
        assert seal(rule)
        compiled = _rule_source_sets(rule)
        assert rule.admits_source(identity="CaptureContract:test.c000", logical_source="osv.record")
        assert _rule_source_sets(rule) is compiled


def test_builtin_migration_is_one_successor_and_old_capture_bytes_stay_pinned():
    from cruxible_client.contracts.captures import (
        AcceptedCaptureContract,
        builtin_contract_for_write,
        capture_contract_digest,
        evaluate_capture_contract_law,
    )

    old = COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT_V1
    successor = builtin_contract_for_write(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT, old)
    assert successor.lifecycle.predecessor_digest == capture_contract_digest(old).tagged
    assert (
        builtin_contract_for_write(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT, successor) is successor
    )
    accepted = AcceptedCaptureContract(
        path=capture_contract_path(old.identity.name),
        contract=old,
        artifact_digest=capture_contract_digest(old).tagged,
    )
    assert (
        evaluate_capture_contract_law(
            successor, path=accepted.path, predecessor=accepted, compatible_succession=True
        ).verdict
        == "accepted"
    )
    assert render_capture_contract(old) == render_capture_contract(
        COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT_V1
    )


def test_capture_input_forbids_unknown_shape_and_derived_http():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CaptureContractInputRecord(name="test.bad", shape="anything")
    with pytest.raises(ValidationError, match="derived grade"):
        lower_capture_contract_input(
            CaptureContractInputRecord(name="test.bad", shape="http_response", grade="derived")
        )


def test_sdk_payload_preserves_omitted_patch_fields_on_round_trip():
    from cruxible_client.contracts.authoring.models import CaptureContractAuthoringPayload

    record = CaptureContractInputRecord(name="test.http", shape="http_response")
    payload = CaptureContractAuthoringPayload(capture_contract=record)
    encoded = payload.model_dump(mode="json")
    assert encoded["capture_contract"] == {"name": "test.http", "shape": "http_response"}
    parsed = CaptureContractAuthoringPayload.model_validate(encoded)
    assert isinstance(parsed.capture_contract, CaptureContractInputRecord)
    assert parsed.capture_contract.model_fields_set == {"name", "shape"}
    assert lower_capture_contract_input(parsed.capture_contract).replay == "attested"


@pytest.mark.parametrize("ambiguous", [False, True])
def test_v3_rules_keep_each_v1_contract_kind_verdict_and_basis(tmp_path, ambiguous):
    """Disjoint legacy kinds do not become ambiguous when new formats drop kinds."""
    from cruxible_client.contracts.artifacts import ArtifactRef
    from cruxible_client.contracts.captures import (
        AcceptedCaptureContract,
        build_cas_capture,
        capture_contract_digest,
    )
    from cruxible_client.contracts.claims import evaluate_capture_evidence_admissions
    from tests.core_support._pc_c_support import (
        NOW,
        body_store,
        capture_contract,
        digest,
        provider,
        provider_run,
    )
    from tests.test_claims.test_claims import _claim, _claim_type

    contract = capture_contract().model_copy(
        update={"evidence_kinds": ("database_record", "other")}
    )
    actor = provider(contract)
    captured = build_cas_capture(
        store=body_store(tmp_path),
        contract=contract,
        source_body=b"status: ready",
        run_coordinate=provider_run(actor),
        run_receipt_digest=digest("receipt", "legacy-kinds"),
        producer=actor.identity,
        producer_binding_digest=digest("binding", "legacy-kinds"),
        observed_at=NOW,
    )
    claim = _claim(
        claim_id="CLM-" + "a7" * 16,
        capture_digest=captured.capture_digest,
        source_digest=captured.commitment_digest,
        source_length=captured.envelope.commitment.byte_length,
    )

    def rule(name, kind, admission):
        return ClaimEvidenceAdmissionRuleV3(
            rule_id=name,
            claim_roles=("observation",),
            evidence_kinds=(kind,),
            capture_contracts=(ArtifactRef(role="capture-contract", target=contract.identity),),
            admission=admission,
            subject_binding="exact_claim_subject",
        )

    rules = [rule("database", "database_record", "direct"), rule("other", "other", "origin_only")]
    if ambiguous:
        rules.append(rule("second-database", "database_record", "direct"))
    policy = ClaimEvidenceAdmissionPolicyV3(rules=tuple(rules))
    claim_type = _claim_type().model_copy(update={"evidence_admission_policy": policy})
    accepted = AcceptedCaptureContract(
        path=capture_contract_path(contract.identity.name),
        contract=contract,
        artifact_digest=capture_contract_digest(contract).tagged,
    )
    decisions = evaluate_capture_evidence_admissions(
        claim,
        claim_type=claim_type,
        capture_digest=captured.capture_digest,
        capture_contract=accepted,
        envelope=captured.envelope,
        verified_attestations=(),
    )
    assert [
        (
            item.evidence_kind,
            item.trace.result.verdict,
            item.trace.result.admission,
            item.trace.result.refusal_code,
        )
        for item in decisions
    ] == [
        ("database_record", "refused", None, "cruxible.evidence.admission_ambiguous")
        if ambiguous
        else ("database_record", "eligible", "direct", None),
        ("other", "eligible", "origin_only", None),
    ]


@pytest.mark.parametrize("revision", [33, 34, 35, 36])
@pytest.mark.parametrize("policy_version", [3, 4])
def test_new_identity_policy_format_follows_base_compiler(tmp_path, revision, policy_version):
    from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
    from cruxible_client.contracts.errors import FormatError
    from cruxible_core.compiler.compiler import (
        CLAIM_REBIND_COMPILER,
        LIGHT_ARTIFACTS_COMPILER,
        PROVIDER_BUILDS_COMPILER,
        SUBJECT_IDENTITY_COMPILER,
    )
    from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
    from tests.core_support.legacy_capture_builders import initialize_legacy_local
    from tests.test_claims.test_claims import TIMESTAMP, _claim_type

    compiler = {
        33: SUBJECT_IDENTITY_COMPILER,
        34: PROVIDER_BUILDS_COMPILER,
        35: CLAIM_REBIND_COMPILER,
        36: LIGHT_ARTIFACTS_COMPILER,
    }[revision]
    instance, _owner = initialize_legacy_local(tmp_path, compiler=compiler)
    if policy_version == 4:
        policy = ClaimEvidenceAdmissionPolicy(
            rules=(
                ClaimEvidenceAdmissionRule(
                    **_rule(
                        capture_contracts=[
                            {"contract": "test.capture", "logical_sources": ["osv.record"]}
                        ]
                    )
                ),
            )
        )
    else:
        policy = ClaimEvidenceAdmissionPolicyV3(
            rules=(
                ClaimEvidenceAdmissionRuleV3(
                    **_rule(
                        capture_contracts=[
                            {
                                "role": "capture-contract",
                                "target": {"kind": "CaptureContract", "name": "test.capture"},
                            }
                        ],
                        evidence_kinds=("observed",),
                    )
                ),
            )
        )
    claim_type = _claim_type().model_copy(
        update={
            "artifact_format": "playbill-claim-type-v7",
            "evidence_requirement": "captured",
            "revision_evidence": "replace",
            "evidence_admission_policy": policy,
        }
    )
    base = instance.accepted_coordinate()
    tree = {
        **instance.tree_at(base.git_oid),
        claim_type_path(claim_type.predicate): render_claim_type(claim_type),
    }

    def submit():
        return instance.proposal_service().submit(
            actor=AuthenticatedActor(actor_id="owner"),
            request=ProposalAdmissionRequest(
                target_ref="refs/proposals/owner/policy-format", proposed_base_oid=base.git_oid
            ),
            candidate_tree=tree,
            timestamp=TIMESTAMP,
        )

    if (revision == 36) != (policy_version == 4):
        with pytest.raises(FormatError, match="upgrade the compiler"):
            submit()
    else:
        result = submit()
        assert result.candidate is not None, result.evaluation.diagnostics
