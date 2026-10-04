"""Closed Claim admission, evidence-eligibility, and resolution policy law."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactRef
from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
)
from cruxible_client.contracts.claim_type_structure import ClaimCardinality, ClaimRole
from cruxible_client.contracts.governance import governance_identifier

ClaimVerdict = Literal[
    "supported",
    "contradicted",
    "unresolved",
    "uncovered",
    "stale",
]
AttestationRequirement = Literal[
    "none",
    "verified_provider",
    "verified_principal",
    "any_verified",
]
VerifiedAttestationGrade = Literal[
    "none",
    "verified_provider",
    "verified_principal",
]

_PREDICATE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})+$")
_EVIDENCE_KIND_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_CLAIM_ID_RE = re.compile(r"^CLM-[0-9a-f]{32}$")


class _StrictPolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _sorted_unique(
    values: tuple[str, ...],
    *,
    label: str,
    nonempty: bool = False,
) -> tuple[str, ...]:
    if nonempty and not values:
        raise ValueError(f"{label} must not be empty")
    if values != tuple(sorted(set(values), key=lambda item: item.encode("utf-8"))):
        raise ValueError(f"{label} must be sorted and unique")
    return values


def _predicate(value: str) -> str:
    if not _PREDICATE_RE.fullmatch(value):
        raise ValueError("policy predicate must be a canonical qualified identifier")
    return value


def _canonical_tuple(values: tuple[object, ...], *, label: str) -> tuple[object, ...]:
    normalized = tuple(normalize_canonical(value) for value in values)
    encoded = tuple(canonical_bytes(value) for value in normalized)
    if encoded != tuple(sorted(set(encoded))):
        raise ValueError(f"{label} must be canonically sorted and unique")
    return normalized


class CorroborationRequirement(_StrictPolicyModel):
    tag: Literal["playbill-corroboration-requirement-v1"] = "playbill-corroboration-requirement-v1"
    requirement_id: str
    query_definition_digest: str
    min_count: int = Field(ge=1)

    @field_validator("requirement_id")
    @classmethod
    def _requirement_id(cls, value: str) -> str:
        return governance_identifier(value, label="corroboration requirement_id")

    @field_validator("query_definition_digest")
    @classmethod
    def _query_digest(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value


class FreezeRequirement(_StrictPolicyModel):
    tag: Literal["playbill-freeze-requirement-v1"] = "playbill-freeze-requirement-v1"
    requirement_id: str
    while_predicate: str
    while_values: tuple[object, ...]
    frozen_predicates: tuple[str, ...]
    except_transition_requirements: tuple[str, ...] = ()

    @field_validator("requirement_id")
    @classmethod
    def _requirement_id(cls, value: str) -> str:
        return governance_identifier(value, label="freeze requirement_id")

    @field_validator("while_predicate")
    @classmethod
    def _while_predicate(cls, value: str) -> str:
        return _predicate(value)

    @field_validator("while_values")
    @classmethod
    def _while_values(cls, value: tuple[object, ...]) -> tuple[object, ...]:
        return _canonical_tuple(value, label="freeze while_values")

    @field_validator("frozen_predicates")
    @classmethod
    def _frozen_predicates(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _predicate(item)
        return _sorted_unique(value, label="frozen predicates", nonempty=True)

    @field_validator("except_transition_requirements")
    @classmethod
    def _exceptions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            governance_identifier(item, label="freeze transition exception")
        return _sorted_unique(value, label="freeze transition exceptions")


class ClaimAdmissionPolicy(_StrictPolicyModel):
    tag: Literal["playbill-claim-admission-policy-v1"] = "playbill-claim-admission-policy-v1"
    corroboration_requirements: tuple[CorroborationRequirement, ...] = ()
    freeze_requirements: tuple[FreezeRequirement, ...] = ()

    @model_validator(mode="after")
    def _closed_requirement_graph(self) -> "ClaimAdmissionPolicy":
        groups = tuple(
            tuple(item.requirement_id for item in group)
            for group in (
                self.corroboration_requirements,
                self.freeze_requirements,
            )
        )
        for ids in groups:
            if ids != tuple(sorted(set(ids), key=lambda item: item.encode("utf-8"))):
                raise ValueError("policy requirements must be sorted and unique by requirement_id")
        all_ids = tuple(item for group in groups for item in group)
        if len(all_ids) != len(set(all_ids)):
            raise ValueError("policy requirement IDs must be unique across requirement kinds")
        if any(item.except_transition_requirements for item in self.freeze_requirements):
            raise ValueError("freeze exception refers to an unknown transition requirement")
        return self


class ClaimResolutionPolicy(_StrictPolicyModel):
    tag: Literal["playbill-claim-resolution-policy-v1"] = "playbill-claim-resolution-policy-v1"
    cardinality: ClaimCardinality
    eligible_verdicts: tuple[ClaimVerdict, ...]
    required_basis_kinds: tuple[str, ...] = ()
    require_current: bool = True
    selector: Literal["all", "only_contender"]
    conflict_result: Literal["unresolved", "refuse"] = "unresolved"

    @field_validator("eligible_verdicts")
    @classmethod
    def _eligible_verdicts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="resolution policy set", nonempty=True)

    @field_validator("required_basis_kinds")
    @classmethod
    def _required_basis_kinds(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="resolution required basis kinds")

    @model_validator(mode="after")
    def _selector_shape(self) -> "ClaimResolutionPolicy":
        if self.cardinality == "many" and self.selector != "all":
            raise ValueError("many-cardinality resolution requires selector='all'")
        if self.cardinality == "one" and self.selector == "all":
            raise ValueError("one-cardinality resolution cannot select all contenders")
        return self


class _EvidenceRuleBase(_StrictPolicyModel):
    rule_id: str
    claim_roles: tuple[ClaimRole, ...]
    evidence_kinds: tuple[str, ...]
    admission: Literal["origin_only", "direct", "derivational"]
    subject_binding: Literal["exact_claim_subject", "contract_source_mapping"]
    attestation_requirement: AttestationRequirement = "none"

    @field_validator("rule_id")
    @classmethod
    def _rule_id(cls, value: str) -> str:
        return governance_identifier(value, label="evidence-admission rule_id")

    @field_validator("claim_roles", "evidence_kinds")
    @classmethod
    def _rule_sets(cls, value: tuple[str, ...], info: object) -> tuple[str, ...]:
        field_name = str(getattr(info, "field_name", "evidence-admission field"))
        _sorted_unique(value, label=field_name, nonempty=True)
        if field_name == "evidence_kinds":
            if any(not _EVIDENCE_KIND_RE.fullmatch(item) for item in value):
                raise ValueError("evidence kinds must be canonical identifiers")
        return value


class _EvidenceRule(_EvidenceRuleBase):
    capture_contract_digests: tuple[str, ...]

    @field_validator("capture_contract_digests")
    @classmethod
    def _contract_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _sorted_unique(value, label="capture_contract_digests", nonempty=True)
        for item in value:
            ArtifactDigest.from_tagged(item)
        return value

    def names_capture_contract(self, *, digest: str, identity: str | None) -> bool:
        """Exact-digest rules name one accepted version each, never a lineage."""

        return digest in self.capture_contract_digests


class ClaimEvidenceAdmissionRuleV1(_EvidenceRule):
    """Frozen producer allowlist for historical ClaimTypes only."""

    tag: Literal["playbill-claim-evidence-admission-rule-v1"] = (
        "playbill-claim-evidence-admission-rule-v1"
    )
    allowed_reducer_digests: tuple[str, ...] = ()

    @field_validator("allowed_reducer_digests")
    @classmethod
    def _reducers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _sorted_unique(value, label="allowed_reducer_digests", nonempty=False)
        for digest in value:
            ArtifactDigest.from_tagged(digest)
        return value

    @model_validator(mode="after")
    def _reducer_shape(self) -> "ClaimEvidenceAdmissionRuleV1":
        if self.admission == "derivational" and not self.allowed_reducer_digests:
            raise ValueError("derivational evidence requires at least one allowed reducer")
        if self.admission != "derivational" and self.allowed_reducer_digests:
            raise ValueError("only derivational evidence may name reducers")
        return self


class ClaimEvidenceAdmissionRuleV2(_EvidenceRule):
    """Evidence requirements; producer authorization belongs to governed mandates."""

    tag: Literal["playbill-claim-evidence-admission-rule-v2"] = (
        "playbill-claim-evidence-admission-rule-v2"
    )


CAPTURE_CONTRACT_REF_ROLE = "capture-contract"


class ClaimEvidenceAdmissionRule(_EvidenceRuleBase):
    """Evidence requirements naming CaptureContracts by identity.

    The rule admits evidence captured under any accepted version of a named
    contract. That is safe because a contract successor must be compatible with
    its predecessor; a change that alters what the evidence means is a new
    contract identity, which the rule does not name until someone edits it.
    """

    # One schema for requests and responses: served ClaimTypes carry this rule.
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")

    tag: Literal["playbill-claim-evidence-admission-rule-v3"] = (
        "playbill-claim-evidence-admission-rule-v3"
    )
    capture_contracts: tuple[ArtifactRef, ...]

    @field_validator("capture_contracts")
    @classmethod
    def _contracts(cls, value: tuple[ArtifactRef, ...]) -> tuple[ArtifactRef, ...]:
        if not value:
            raise ValueError("capture_contracts must not be empty")
        names = tuple(item.target.qualified for item in value)
        if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
            raise ValueError("capture_contracts must be sorted and unique by identity")
        for item in value:
            if item.role != CAPTURE_CONTRACT_REF_ROLE or item.target.kind != "CaptureContract":
                raise ValueError(
                    "capture_contracts must name CaptureContracts with role 'capture-contract'"
                )
        return value

    def names_capture_contract(self, *, digest: str, identity: str | None) -> bool:
        """Identity rules name every accepted version of the named contracts."""

        return identity is not None and any(
            item.target.qualified == identity for item in self.capture_contracts
        )


ClaimEvidenceAdmissionRuleAny = (
    ClaimEvidenceAdmissionRuleV1 | ClaimEvidenceAdmissionRuleV2 | ClaimEvidenceAdmissionRule
)


class ClaimEvidenceAdmissionPolicyV1(_StrictPolicyModel):
    tag: Literal["playbill-claim-evidence-admission-policy-v1"] = (
        "playbill-claim-evidence-admission-policy-v1"
    )
    rules: tuple[ClaimEvidenceAdmissionRuleV1, ...] = ()

    @field_validator("rules")
    @classmethod
    def _rules(
        cls, value: tuple[ClaimEvidenceAdmissionRuleV1, ...]
    ) -> tuple[ClaimEvidenceAdmissionRuleV1, ...]:
        ids = tuple(item.rule_id for item in value)
        if ids != tuple(sorted(set(ids), key=lambda item: item.encode("utf-8"))):
            raise ValueError("evidence-admission rules must be sorted and unique by rule_id")
        return value


class ClaimEvidenceAdmissionPolicyV2(_StrictPolicyModel):
    tag: Literal["playbill-claim-evidence-admission-policy-v2"] = (
        "playbill-claim-evidence-admission-policy-v2"
    )
    rules: tuple[ClaimEvidenceAdmissionRuleV2, ...] = ()

    @field_validator("rules")
    @classmethod
    def _rules(
        cls, value: tuple[ClaimEvidenceAdmissionRuleV2, ...]
    ) -> tuple[ClaimEvidenceAdmissionRuleV2, ...]:
        ids = tuple(item.rule_id for item in value)
        if ids != tuple(sorted(set(ids), key=lambda item: item.encode("utf-8"))):
            raise ValueError("evidence-admission rules must be sorted and unique by rule_id")
        return value


class ClaimEvidenceAdmissionPolicy(_StrictPolicyModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")

    tag: Literal["playbill-claim-evidence-admission-policy-v3"] = (
        "playbill-claim-evidence-admission-policy-v3"
    )
    rules: tuple[ClaimEvidenceAdmissionRule, ...] = ()

    @field_validator("rules")
    @classmethod
    def _rules(
        cls, value: tuple[ClaimEvidenceAdmissionRule, ...]
    ) -> tuple[ClaimEvidenceAdmissionRule, ...]:
        ids = tuple(item.rule_id for item in value)
        if ids != tuple(sorted(set(ids), key=lambda item: item.encode("utf-8"))):
            raise ValueError("evidence-admission rules must be sorted and unique by rule_id")
        return value

    @property
    def capture_contract_identities(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {item.target.qualified for rule in self.rules for item in rule.capture_contracts},
                key=lambda item: item.encode("utf-8"),
            )
        )


ClaimEvidenceAdmissionPolicyAny = (
    ClaimEvidenceAdmissionPolicyV1 | ClaimEvidenceAdmissionPolicyV2 | ClaimEvidenceAdmissionPolicy
)


class ClaimCorroborationResult(_StrictPolicyModel):
    tag: Literal["playbill-claim-corroboration-result-v1"] = (
        "playbill-claim-corroboration-result-v1"
    )
    requirement_id: str
    query_definition_digest: str
    parameter_digest: str
    result_digest: str
    query_verdict: Literal["completed", "refused"]
    query_refusal_code: str | None = None
    observed_count: int = Field(ge=0)
    truncated: bool
    satisfied: bool

    @field_validator("requirement_id")
    @classmethod
    def _requirement_id(cls, value: str) -> str:
        return governance_identifier(value, label="claim corroboration requirement_id")

    @field_validator("query_definition_digest", "parameter_digest", "result_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _verdict_shape(self) -> "ClaimCorroborationResult":
        if (self.query_verdict == "refused") != (self.query_refusal_code is not None):
            raise ValueError("a refused corroboration query names exactly one refusal code")
        if self.query_verdict == "refused" and (self.observed_count != 0 or self.satisfied):
            raise ValueError("a refused corroboration query observes no rows and is unsatisfied")
        return self


class ClaimAdmissionEvaluationAccount(_StrictPolicyModel):
    tag: Literal["playbill-claim-admission-evaluation-account-v1"] = (
        "playbill-claim-admission-evaluation-account-v1"
    )
    claim_path: str
    claim_type_identity: str
    claim_type_digest: str
    policy_digest: str
    corroboration_results: tuple[ClaimCorroborationResult, ...] = ()
    satisfied: bool

    @field_validator("claim_type_digest", "policy_digest")
    @classmethod
    def _digests(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("corroboration_results")
    @classmethod
    def _results(
        cls, value: tuple[ClaimCorroborationResult, ...]
    ) -> tuple[ClaimCorroborationResult, ...]:
        ids = tuple(item.requirement_id for item in value)
        if ids != tuple(sorted(set(ids), key=lambda item: item.encode("utf-8"))):
            raise ValueError("corroboration results must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _satisfaction(self) -> "ClaimAdmissionEvaluationAccount":
        if self.satisfied and not all(item.satisfied for item in self.corroboration_results):
            raise ValueError("a satisfied admission account cannot contain an unsatisfied result")
        return self


class ClaimAdmissionCandidateContext(_StrictPolicyModel):
    evaluation_time: str
    declared_predicates: tuple[str, ...]
    parent_values: dict[str, tuple[object, ...]]
    candidate_values: dict[str, tuple[object, ...]]
    corroboration_results: tuple[ClaimCorroborationResult, ...] = ()

    @field_validator("declared_predicates")
    @classmethod
    def _predicates(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _predicate(item)
        return _sorted_unique(value, label="declared predicates")

    @field_validator("parent_values", "candidate_values")
    @classmethod
    def _values(cls, value: dict[str, tuple[object, ...]]) -> dict[str, tuple[object, ...]]:
        result: dict[str, tuple[object, ...]] = {}
        for predicate in sorted(value, key=lambda item: item.encode("utf-8")):
            _predicate(predicate)
            result[predicate] = _canonical_tuple(value[predicate], label="projected values")
        return result

    @field_validator("corroboration_results")
    @classmethod
    def _corroboration_results(
        cls, value: tuple[ClaimCorroborationResult, ...]
    ) -> tuple[ClaimCorroborationResult, ...]:
        ids = tuple(item.requirement_id for item in value)
        if ids != tuple(sorted(set(ids), key=lambda item: item.encode("utf-8"))):
            raise ValueError("claim corroboration results must be sorted and unique")
        return value


class ClaimAdmissionCandidateResult(_StrictPolicyModel):
    tag: Literal["playbill-claim-admission-candidate-result-v1"] = (
        "playbill-claim-admission-candidate-result-v1"
    )
    verdict: Literal["eligible", "refused"]
    corroboration_results: tuple[ClaimCorroborationResult, ...] = ()
    refusal_codes: tuple[str, ...] = ()


def evaluate_claim_admission_candidate(
    policy: ClaimAdmissionPolicy,
    context: ClaimAdmissionCandidateContext,
) -> ClaimAdmissionCandidateResult:
    """Evaluate deterministic corroboration and freeze requirements together."""

    declared = set(context.declared_predicates)
    policy_predicates = {item.while_predicate for item in policy.freeze_requirements} | {
        predicate for item in policy.freeze_requirements for predicate in item.frozen_predicates
    }
    refusal_codes: set[str] = set()
    results_by_id = {item.requirement_id: item for item in context.corroboration_results}
    for requirement in policy.corroboration_requirements:
        result = results_by_id.get(requirement.requirement_id)
        if result is None or result.query_definition_digest != requirement.query_definition_digest:
            refusal_codes.add("cruxible.claim.corroboration_query_unresolved")
            continue
        expected_satisfied = (
            result.query_verdict == "completed" and result.observed_count >= requirement.min_count
        )
        if result.satisfied != expected_satisfied:
            raise ValueError("corroboration result satisfaction does not reproduce")
        elif result.query_verdict == "refused":
            refusal_codes.add("cruxible.claim.corroboration_query_refused")
        elif not result.satisfied:
            refusal_codes.add("cruxible.claim.corroboration_insufficient")
    if policy_predicates - declared:
        refusal_codes.add("cruxible.claim_policy.unknown_predicate")

    for freeze in policy.freeze_requirements:
        parent = context.parent_values.get(freeze.while_predicate, ())
        if len(parent) > 1:
            refusal_codes.add("cruxible.claim_policy.ambiguous_single_value")
            continue
        active = bool(parent) and parent[0] in freeze.while_values
        changed = any(
            context.parent_values.get(predicate, ())
            != context.candidate_values.get(predicate, context.parent_values.get(predicate, ()))
            for predicate in freeze.frozen_predicates
        )
        if active and changed:
            refusal_codes.add("cruxible.claim_policy.freeze_active")

    codes = tuple(sorted(refusal_codes, key=lambda item: item.encode("utf-8")))
    return ClaimAdmissionCandidateResult(
        verdict="refused" if codes else "eligible",
        corroboration_results=context.corroboration_results,
        refusal_codes=codes,
    )


class EvidenceAdmissionInput(_StrictPolicyModel):
    claim_role: ClaimRole
    capture_contract_digest: str
    #: The accepted identity of the exact contract version the Capture names.
    #: Identity rules match it; exact-digest rules ignore it.
    capture_contract_identity: str | None = None
    evidence_kind: str
    reducer_digest: str | None = None
    input_claim_artifact_digests: tuple[str, ...] = ()
    attestation_grade: VerifiedAttestationGrade = "none"
    source_subject_bound: bool
    capture_claims_semantic_authority: bool = False

    @field_validator("capture_contract_digest", "reducer_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            ArtifactDigest.from_tagged(value)
        return value

    @field_validator("input_claim_artifact_digests")
    @classmethod
    def _input_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _sorted_unique(value, label="input Claim artifact digests")
        for item in value:
            ArtifactDigest.from_tagged(item)
        return value

    @field_validator("evidence_kind")
    @classmethod
    def _evidence_kind(cls, value: str) -> str:
        if not _EVIDENCE_KIND_RE.fullmatch(value):
            raise ValueError("evidence kind must be a canonical identifier")
        return value


class ClaimEvidenceAdmissionResult(_StrictPolicyModel):
    tag: Literal["playbill-claim-evidence-admission-result-v1"] = (
        "playbill-claim-evidence-admission-result-v1"
    )
    verdict: Literal["eligible", "refused"]
    rule_id: str | None = None
    admission: Literal["origin_only", "direct", "derivational"] | None = None
    refusal_code: str | None = None


class ClaimEvidenceAdmissionTrace(_StrictPolicyModel):
    """Internal trace from the authoritative evidence-admission evaluator."""

    result: ClaimEvidenceAdmissionResult
    closest_rule_id: str | None = None


def _attestation_satisfied(
    requirement: AttestationRequirement,
    grade: VerifiedAttestationGrade,
) -> bool:
    if requirement == "none":
        return True
    if requirement == "verified_provider":
        return grade == "verified_provider"
    if requirement == "verified_principal":
        return grade == "verified_principal"
    return grade != "none"


def _derivation_satisfied(
    rule: ClaimEvidenceAdmissionRuleAny,
    evidence: EvidenceAdmissionInput,
) -> bool:
    if rule.admission == "derivational":
        if not isinstance(rule, ClaimEvidenceAdmissionRuleV1):
            return evidence.reducer_digest is not None and bool(
                evidence.input_claim_artifact_digests
            )
        return evidence.reducer_digest in rule.allowed_reducer_digests and bool(
            evidence.input_claim_artifact_digests
        )
    return evidence.reducer_digest is None and not evidence.input_claim_artifact_digests


def evaluate_claim_evidence_admission_trace(
    policy: ClaimEvidenceAdmissionPolicyAny,
    evidence: EvidenceAdmissionInput,
    *,
    subject_binding_by_rule: Mapping[str, bool] | None = None,
) -> ClaimEvidenceAdmissionTrace:
    """Evaluate evidence and retain the deterministic nearest repair rule."""

    contract_rules = tuple(
        rule
        for rule in policy.rules
        if rule.names_capture_contract(
            digest=evidence.capture_contract_digest,
            identity=evidence.capture_contract_identity,
        )
    )
    closest_rule_id: str | None = None
    if contract_rules:
        binding = subject_binding_by_rule or {}

        def mismatch_count(rule: ClaimEvidenceAdmissionRuleAny) -> tuple[int, bytes]:
            mismatches = sum(
                (
                    evidence.claim_role not in rule.claim_roles,
                    evidence.evidence_kind not in rule.evidence_kinds,
                    not binding.get(rule.rule_id, evidence.source_subject_bound),
                    not _attestation_satisfied(
                        rule.attestation_requirement, evidence.attestation_grade
                    ),
                    not _derivation_satisfied(rule, evidence),
                )
            )
            return mismatches, rule.rule_id.encode("utf-8")

        closest_rule_id = min(contract_rules, key=mismatch_count).rule_id

    if evidence.capture_claims_semantic_authority:
        result = ClaimEvidenceAdmissionResult(
            verdict="refused",
            refusal_code="cruxible.evidence.capture_cannot_grant_semantic_authority",
        )
        return ClaimEvidenceAdmissionTrace(result=result, closest_rule_id=closest_rule_id)
    matches = [
        rule
        for rule in policy.rules
        if evidence.claim_role in rule.claim_roles
        and rule.names_capture_contract(
            digest=evidence.capture_contract_digest,
            identity=evidence.capture_contract_identity,
        )
        and evidence.evidence_kind in rule.evidence_kinds
    ]
    if len(matches) != 1:
        result = ClaimEvidenceAdmissionResult(
            verdict="refused",
            refusal_code=(
                "cruxible.evidence.admission_ambiguous"
                if matches
                else "cruxible.evidence.undeclared_contract_kind"
            ),
        )
        return ClaimEvidenceAdmissionTrace(result=result, closest_rule_id=closest_rule_id)
    rule = matches[0]
    if not evidence.source_subject_bound:
        result = ClaimEvidenceAdmissionResult(
            verdict="refused",
            refusal_code="cruxible.evidence.subject_binding_failed",
        )
        return ClaimEvidenceAdmissionTrace(result=result, closest_rule_id=closest_rule_id)
    if not _attestation_satisfied(rule.attestation_requirement, evidence.attestation_grade):
        result = ClaimEvidenceAdmissionResult(
            verdict="refused",
            refusal_code="cruxible.evidence.attestation_grade_missing",
        )
        return ClaimEvidenceAdmissionTrace(result=result, closest_rule_id=closest_rule_id)
    if not _derivation_satisfied(rule, evidence):
        result = ClaimEvidenceAdmissionResult(
            verdict="refused",
            refusal_code=(
                "cruxible.evidence.derivation_incomplete"
                if rule.admission == "derivational"
                else "cruxible.evidence.reducer_not_allowed"
            ),
        )
        return ClaimEvidenceAdmissionTrace(result=result, closest_rule_id=closest_rule_id)
    result = ClaimEvidenceAdmissionResult(
        verdict="eligible",
        rule_id=rule.rule_id,
        admission=rule.admission,
    )
    return ClaimEvidenceAdmissionTrace(result=result, closest_rule_id=closest_rule_id)


def evaluate_claim_evidence_admission(
    policy: ClaimEvidenceAdmissionPolicyAny,
    evidence: EvidenceAdmissionInput,
) -> ClaimEvidenceAdmissionResult:
    """Evaluate evidence shape without granting Claim activation authority."""

    return evaluate_claim_evidence_admission_trace(policy, evidence).result


class ResolutionContender(_StrictPolicyModel):
    claim_identity: str
    object_value: object
    verdict: ClaimVerdict
    basis_kinds: tuple[str, ...] = ()
    current: bool = True

    @field_validator("claim_identity")
    @classmethod
    def _claim_identity(cls, value: str) -> str:
        if not _CLAIM_ID_RE.fullmatch(value):
            raise ValueError("Claim identity must be CLM- plus 128-bit lowercase hex")
        return value

    @field_validator("object_value")
    @classmethod
    def _object_value(cls, value: object) -> object:
        return normalize_canonical(value)

    @field_validator("basis_kinds")
    @classmethod
    def _basis_kinds(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="basis kinds")


class ClaimResolutionResult(_StrictPolicyModel):
    tag: Literal["playbill-claim-resolution-result-v1"] = "playbill-claim-resolution-result-v1"
    status: Literal["resolved", "unresolved", "refused"]
    selected_claim_identities: tuple[str, ...] = ()
    contender_claim_identities: tuple[str, ...] = ()


def resolve_claim_contenders(
    policy: ClaimResolutionPolicy,
    contenders: tuple[ResolutionContender, ...],
) -> ClaimResolutionResult:
    """Project accepted contenders without deleting them or inventing confidence."""

    ordered = tuple(
        sorted(
            contenders,
            key=lambda item: (
                canonical_bytes(item.object_value),
                item.claim_identity.encode("utf-8"),
            ),
        )
    )
    eligible = tuple(
        item
        for item in ordered
        if item.verdict in policy.eligible_verdicts
        and (not policy.require_current or item.current)
        and set(policy.required_basis_kinds).issubset(item.basis_kinds)
    )
    identities = tuple(item.claim_identity for item in eligible)
    if policy.selector == "all":
        return ClaimResolutionResult(
            status="resolved",
            selected_claim_identities=identities,
            contender_claim_identities=identities,
        )
    if len(eligible) == 1:
        return ClaimResolutionResult(
            status="resolved",
            selected_claim_identities=identities,
            contender_claim_identities=identities,
        )
    return ClaimResolutionResult(
        status="refused" if policy.conflict_result == "refuse" else "unresolved",
        contender_claim_identities=identities,
    )


__all__ = [
    "AttestationRequirement",
    "ClaimAdmissionCandidateContext",
    "ClaimAdmissionCandidateResult",
    "ClaimAdmissionPolicy",
    "ClaimEvidenceAdmissionPolicyV1",
    "ClaimEvidenceAdmissionPolicyV2",
    "ClaimEvidenceAdmissionPolicyAny",
    "ClaimEvidenceAdmissionResult",
    "ClaimEvidenceAdmissionRuleV1",
    "ClaimEvidenceAdmissionRuleV2",
    "ClaimEvidenceAdmissionRuleAny",
    "ClaimResolutionPolicy",
    "ClaimResolutionResult",
    "ClaimVerdict",
    "EvidenceAdmissionInput",
    "ClaimAdmissionEvaluationAccount",
    "ClaimCorroborationResult",
    "CorroborationRequirement",
    "FreezeRequirement",
    "ResolutionContender",
    "evaluate_claim_admission_candidate",
    "evaluate_claim_evidence_admission",
    "resolve_claim_contenders",
]
