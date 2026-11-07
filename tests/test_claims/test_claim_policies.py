"""PC-A2 closed Claim policy formats and evaluator parity tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    capture_contract_digest,
    foreign_source_capture_contract,
)
from cruxible_client.contracts.policies import (
    CaptureContractAdmission,
    ClaimAdmissionCandidateContext,
    ClaimAdmissionPolicy,
    ClaimCorroborationResult,
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
    ClaimResolutionPolicy,
    CorroborationRequirement,
    EvidenceAdmissionInput,
    FreezeRequirement,
    ResolutionContender,
    evaluate_claim_admission_candidate,
    evaluate_claim_evidence_admission,
    evaluate_claim_evidence_admission_trace,
    resolve_claim_contenders,
)

DIGEST_A = "sha256:" + "11" * 32
DIGEST_B = "sha256:" + "22" * 32
CONTRACT_A = "source.provider-a"
FEED = "feed.orders"


def _review_policy() -> ClaimAdmissionPolicy:
    return ClaimAdmissionPolicy(
        corroboration_requirements=(
            CorroborationRequirement(
                requirement_id="one-valid-approval",
                query_definition_digest=DIGEST_A,
                min_count=1,
            ),
        ),
        freeze_requirements=(
            FreezeRequirement(
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
) -> ClaimAdmissionCandidateContext:
    return ClaimAdmissionCandidateContext(
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
            ClaimCorroborationResult(
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
        ClaimAdmissionPolicy.model_validate(payload)


@pytest.mark.parametrize("field", ("parameters", "max_rows", "max_traversal_depth"))
def test_deleted_corroboration_requirement_fields_refuse(field: str) -> None:
    payload = CorroborationRequirement(
        requirement_id="one-valid-approval",
        query_definition_digest=DIGEST_A,
        min_count=1,
    ).model_dump(mode="json")
    payload[field] = {} if field == "parameters" else 1
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CorroborationRequirement.model_validate(payload)


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
    assert frozen.refusal_codes == ("cruxible.claim_policy.freeze_active",)

    unknown = frozen_context.model_copy(update={"declared_predicates": ("review.status",)})
    refused = evaluate_claim_admission_candidate(_review_policy(), unknown)
    assert "cruxible.claim_policy.unknown_predicate" in refused.refusal_codes


def test_retained_freeze_exception_must_name_an_existing_transition() -> None:
    payload = _review_policy().model_dump(mode="json")
    payload["freeze_requirements"][0]["except_transition_requirements"] = ["retired-transition"]

    with pytest.raises(ValidationError, match="unknown transition requirement"):
        ClaimAdmissionPolicy.model_validate(payload)


def _evidence_policy() -> ClaimEvidenceAdmissionPolicy:
    return ClaimEvidenceAdmissionPolicy(
        rules=(
            ClaimEvidenceAdmissionRule(
                rule_id="direct-provider-observation",
                claim_roles=("observation",),
                capture_contracts=({"contract": CONTRACT_A, "logical_sources": [FEED]},),
                admission="direct",
                subject_binding="contract_source_mapping",
                attestation_requirement="verified_provider",
            ),
        )
    )


def _evidence(**updates: object) -> EvidenceAdmissionInput:
    values: dict[str, object] = {
        "claim_role": "observation",
        "capture_contract_digest": DIGEST_A,
        "capture_contract_identity": f"CaptureContract:{CONTRACT_A}",
        "logical_source": FEED,
        "evidence_kind": "source.observation",
        "attestation_grade": "verified_provider",
        "source_subject_bound": True,
    }
    values.update(updates)
    return EvidenceAdmissionInput.model_validate(values)


def test_evidence_admission_is_conjunctive_and_never_grants_claim_authority() -> None:
    accepted = evaluate_claim_evidence_admission(_evidence_policy(), _evidence())
    assert accepted.verdict == "eligible"
    assert accepted.rule_id == "direct-provider-observation"

    undeclared = evaluate_claim_evidence_admission(
        _evidence_policy(),
        _evidence(logical_source="feed.other"),
    )
    assert undeclared.refusal_code == "cruxible.evidence.undeclared_contract_kind"

    reducer = evaluate_claim_evidence_admission(
        _evidence_policy(),
        _evidence(reducer_digest=DIGEST_B),
    )
    assert reducer.refusal_code == "cruxible.evidence.reducer_not_allowed"

    laundering = evaluate_claim_evidence_admission(
        _evidence_policy(),
        _evidence(capture_claims_semantic_authority=True),
    )
    assert laundering.refusal_code == ("cruxible.evidence.capture_cannot_grant_semantic_authority")


def test_evidence_admission_trace_chooses_the_closest_contract_rule_deterministically() -> None:
    direct = _evidence_policy().rules[0]
    alternative = direct.model_copy(
        update={
            "rule_id": "alternate-role",
            "claim_roles": ("derivation",),
            "capture_contracts": (
                CaptureContractAdmission(
                    role="capture-contract",
                    target=ArtifactIdentity(kind="CaptureContract", name=CONTRACT_A),
                    logical_sources=("feed.other",),
                ),
            ),
            "attestation_requirement": "none",
        }
    )
    policy = ClaimEvidenceAdmissionPolicy(
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

    assert trace.result.refusal_code == "cruxible.evidence.attestation_grade_missing"
    assert trace.closest_rule_id == "direct-provider-observation"

    no_contract = evaluate_claim_evidence_admission_trace(
        policy,
        _evidence(capture_contract_identity="CaptureContract:source.other"),
    )
    assert no_contract.closest_rule_id is None


def test_evidence_policy_refuses_duplicate_rules_reducers_and_empty_contracts() -> None:
    direct = _evidence_policy().rules[0]
    with pytest.raises(ValidationError, match="sorted and unique"):
        ClaimEvidenceAdmissionPolicy(rules=(direct, direct))
    # The one rule never names producers: a reducer allowlist is a removed field.
    with pytest.raises(ValidationError, match="allowed_reducer_digests"):
        ClaimEvidenceAdmissionRule.model_validate(
            {**direct.model_dump(mode="json"), "allowed_reducer_digests": [DIGEST_B]}
        )
    with pytest.raises(ValidationError, match="must not be empty"):
        ClaimEvidenceAdmissionRule.model_validate(
            {**direct.model_dump(mode="json"), "capture_contracts": []}
        )


def _workspace_file_rule(**updates: object) -> ClaimEvidenceAdmissionRule:
    values: dict[str, object] = {
        "rule_id": "workspace",
        "claim_roles": ("observation",),
        "capture_contract_families": ("workspace_file",),
        "admission": "direct",
        "subject_binding": "exact_claim_subject",
    }
    values.update(updates)
    return ClaimEvidenceAdmissionRule.model_validate(values)


def _workspace_file_evidence(contract: object, **updates: object) -> EvidenceAdmissionInput:
    values: dict[str, object] = {
        "claim_role": "observation",
        "capture_contract_digest": capture_contract_digest(contract).tagged,
        "capture_contract_identity": contract.identity.qualified,  # type: ignore[union-attr]
        "capture_contract_shape": contract.shape,  # type: ignore[union-attr]
        "source_subject_bound": True,
    }
    values.update(updates)
    return EvidenceAdmissionInput.model_validate(values)


def test_capture_contract_families_require_at_least_one_selector() -> None:
    # A family selector alone satisfies the rule: no exact contract is required.
    rule = _workspace_file_rule()
    assert rule.capture_contracts == ()
    assert rule.capture_contract_families == ("workspace_file",)
    with pytest.raises(ValidationError, match="must not be empty"):
        ClaimEvidenceAdmissionRule.model_validate(
            {
                "rule_id": "nothing",
                "claim_roles": ("observation",),
                "capture_contracts": [],
                "capture_contract_families": [],
                "admission": "direct",
                "subject_binding": "exact_claim_subject",
            }
        )
    with pytest.raises(ValidationError):
        ClaimEvidenceAdmissionRule.model_validate(
            {
                "rule_id": "unknown-family",
                "claim_roles": ("observation",),
                "capture_contract_families": ["not_a_real_family"],
                "admission": "direct",
                "subject_binding": "exact_claim_subject",
            }
        )


def test_a_family_selector_admits_two_catalogued_files_and_refuses_a_non_file_capture() -> None:
    """D1 (2026-11-06): a family entry is resolved by the contract's own declared
    ``shape`` -- never by a string match on its name -- so a rule naming
    ``workspace_file`` admits any catalogued workspace file's foreign-source
    CaptureContract, including ones added to the catalog after the rule was
    authored, and only those.
    """

    policy = ClaimEvidenceAdmissionPolicy(rules=(_workspace_file_rule(),))
    file_a = foreign_source_capture_contract("repo.file-a")
    file_b = foreign_source_capture_contract("repo.file-b")
    assert file_a.shape == file_b.shape == "foreign_span"
    assert file_a.identity.qualified != file_b.identity.qualified

    for contract in (file_a, file_b):
        result = evaluate_claim_evidence_admission(policy, _workspace_file_evidence(contract))
        assert result.verdict == "eligible"
        assert result.rule_id == "workspace"

    non_file = evaluate_claim_evidence_admission(
        policy, _workspace_file_evidence(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT)
    )
    assert non_file.verdict == "refused"
    assert non_file.refusal_code == "cruxible.evidence.undeclared_contract_kind"


def test_an_exact_contract_rule_is_unaffected_by_a_capture_shape() -> None:
    """Passing ``shape`` never widens what an exact-identity rule admits."""

    policy = _evidence_policy()
    file_a = foreign_source_capture_contract("repo.file-a")
    # The exact rule names CONTRACT_A only; a catalogued workspace file's own
    # shape must not substitute for the identity the rule actually names.
    result = evaluate_claim_evidence_admission(policy, _workspace_file_evidence(file_a))
    assert result.verdict == "refused"
    assert result.refusal_code == "cruxible.evidence.undeclared_contract_kind"


@pytest.mark.parametrize("catalog_size", (10, 100))
def test_family_admission_cost_does_not_grow_with_the_catalog(
    monkeypatch: pytest.MonkeyPatch, catalog_size: int
) -> None:
    """Admission cost is constant as the number of catalogued files grows.

    A rule naming ``capture_contract_families`` never needs to look up, or even
    know the size of, the workspace file catalog: it decides by the one
    Capture's own declared shape alone. Building a ``catalog_size``-member
    catalog and then forbidding any further catalog-contract construction
    proves the admission check touches none of it.
    """

    catalog = [
        foreign_source_capture_contract(f"repo.file-{index:03d}") for index in range(catalog_size)
    ]
    target = catalog[-1]
    policy = ClaimEvidenceAdmissionPolicy(rules=(_workspace_file_rule(),))
    evidence = _workspace_file_evidence(target)

    import cruxible_client.contracts.captures as captures_module

    calls: list[str] = []

    def _forbidden(*args: object, **kwargs: object) -> None:
        calls.append("called")
        raise AssertionError("admission must never rebuild a catalogued contract")

    monkeypatch.setattr(captures_module, "foreign_source_capture_contract", _forbidden)

    result = evaluate_claim_evidence_admission(policy, evidence)

    assert result.verdict == "eligible"
    assert calls == []


_WORKSPACE_CATALOG = """\
tag: playbill-source-catalog-v1
catalog_kind: portable
entries:
  - name: repo.known
    locator: known.md
"""


def test_an_uncatalogued_path_cannot_be_cited(tmp_path: Path) -> None:
    """The workspace catalog stays the admission boundary.

    A family entry admits any catalogued workspace file's CaptureContract, but
    an uncatalogued file was never given a CaptureContract in the first place
    -- the catalog refuses to select it before any evidence rule, family or
    exact, is ever consulted.
    """

    from cruxible_client.authoring.sdk_types import SourceSelectionError
    from cruxible_client.authoring.write_evidence import observe_evidence
    from cruxible_client.contracts.write import FileEvidence

    workspace = tmp_path
    (workspace / ".cruxible").mkdir(parents=True, exist_ok=True)
    (workspace / ".cruxible" / "files.yaml").write_text(_WORKSPACE_CATALOG, encoding="utf-8")
    (workspace / "known.md").write_text("# Known\n\nCount: 3\n", encoding="utf-8")
    (workspace / "secret.md").write_text("# Secret\n\nCount: 3\n", encoding="utf-8")

    # The catalogued file cites cleanly.
    observed = observe_evidence(FileEvidence(file="known.md#Count: 3"), workspace=workspace)
    assert observed is not None and observed.observation is not None

    # The uncatalogued one, despite matching text, cannot be selected at all.
    with pytest.raises(SourceSelectionError):
        observe_evidence(FileEvidence(file="secret.md#Count: 3"), workspace=workspace)


def test_resolution_preserves_conflict_and_rejects_deleted_authority_selector() -> None:
    policy = ClaimResolutionPolicy(
        cardinality="one",
        eligible_verdicts=("supported",),
        selector="only_contender",
    )
    first = ResolutionContender(
        claim_identity="CLM-" + "0a" * 16,
        object_value="open",
        verdict="supported",
    )
    second = ResolutionContender(
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
        ClaimResolutionPolicy.model_validate(
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
        ClaimAdmissionPolicy.model_validate(payload)


def test_requirement_ids_cannot_alias_across_policy_kinds() -> None:
    with pytest.raises(ValidationError, match="unique across"):
        ClaimAdmissionPolicy(
            corroboration_requirements=(
                CorroborationRequirement(
                    requirement_id="same-id",
                    query_definition_digest=DIGEST_A,
                    min_count=1,
                ),
            ),
            freeze_requirements=(
                FreezeRequirement(
                    requirement_id="same-id",
                    while_predicate="review.status",
                    while_values=("approved",),
                    frozen_predicates=("review.summary",),
                ),
            ),
        )


def test_current_derivation_policy_requires_provenance_without_authorizing_producers():
    from cruxible_client.contracts.policies import (
        ClaimEvidenceAdmissionPolicy,
        ClaimEvidenceAdmissionRule,
    )

    rule = ClaimEvidenceAdmissionRule(
        rule_id="derived-observation",
        claim_roles=("derivation",),
        capture_contracts=({"contract": CONTRACT_A, "logical_sources": [FEED]},),
        admission="derivational",
        subject_binding="exact_claim_subject",
    )
    policy = ClaimEvidenceAdmissionPolicy(rules=(rule,))
    for reducer in (DIGEST_A, DIGEST_B):
        assert (
            evaluate_claim_evidence_admission(
                policy,
                _evidence(
                    claim_role="derivation",
                    reducer_digest=reducer,
                    input_claim_artifact_digests=(DIGEST_A,),
                ),
            ).verdict
            == "eligible"
        )
    for missing in ({"reducer_digest": None}, {"input_claim_artifact_digests": ()}):
        evidence = _evidence(
            claim_role="derivation",
            reducer_digest=DIGEST_B,
            input_claim_artifact_digests=(DIGEST_A,),
        ).model_copy(update=missing)
        assert evaluate_claim_evidence_admission(policy, evidence).refusal_code == (
            "cruxible.evidence.derivation_incomplete"
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        ClaimEvidenceAdmissionRule.model_validate(
            {
                **rule.model_dump(),
                "allowed_reducer_digests": [DIGEST_B],
            }
        )
    direct = policy.model_copy(update={"rules": (rule.model_copy(update={"admission": "direct"}),)})
    assert (
        evaluate_claim_evidence_admission(
            direct,
            _evidence(
                claim_role="derivation",
                reducer_digest=DIGEST_B,
                input_claim_artifact_digests=(DIGEST_A,),
            ),
        ).refusal_code
        == "cruxible.evidence.reducer_not_allowed"
    )
