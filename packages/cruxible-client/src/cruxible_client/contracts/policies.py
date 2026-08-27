"""Closed Claim admission, evidence-eligibility, and resolution policy law."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class TransitionRequirementV1(_StrictPolicyModel):
    tag: Literal["playbill-transition-requirement-v1"] = "playbill-transition-requirement-v1"
    requirement_id: str
    when_predicate: str
    from_values: tuple[object, ...]
    to_value: object
    require: tuple[str, ...]

    @field_validator("requirement_id")
    @classmethod
    def _requirement_id(cls, value: str) -> str:
        return governance_identifier(value, label="transition requirement_id")

    @field_validator("when_predicate")
    @classmethod
    def _when_predicate(cls, value: str) -> str:
        return _predicate(value)

    @field_validator("from_values")
    @classmethod
    def _from_values(cls, value: tuple[object, ...]) -> tuple[object, ...]:
        return _canonical_tuple(value, label="transition from_values")

    @field_validator("to_value")
    @classmethod
    def _to_value(cls, value: object) -> object:
        return normalize_canonical(value)

    @field_validator("require")
    @classmethod
    def _require(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            governance_identifier(item, label="transition required policy identifier")
        return _sorted_unique(value, label="transition required policy identifiers", nonempty=True)


class ActorRequirementV1(_StrictPolicyModel):
    tag: Literal["playbill-actor-requirement-v1"] = "playbill-actor-requirement-v1"
    requirement_id: str
    admission_actor_roles: tuple[str, ...] = ()
    admission_actor_subjects: tuple[str, ...] = ()
    signer_roles: tuple[str, ...] = ()
    signer_subjects: tuple[str, ...] = ()
    signer_control_domains: tuple[str, ...] = ()
    minimum_distinct_signers: int = Field(default=1, ge=1)
    signer_distinct_from_lineage_creation_actor: bool = False

    @field_validator("requirement_id")
    @classmethod
    def _requirement_id(cls, value: str) -> str:
        return governance_identifier(value, label="actor requirement_id")

    @field_validator(
        "admission_actor_roles",
        "admission_actor_subjects",
        "signer_roles",
        "signer_subjects",
        "signer_control_domains",
    )
    @classmethod
    def _identifiers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            governance_identifier(item, label="actor requirement value")
        return _sorted_unique(value, label="actor requirement values")

    @model_validator(mode="after")
    def _has_constraint(self) -> "ActorRequirementV1":
        if not any(
            (
                self.admission_actor_roles,
                self.admission_actor_subjects,
                self.signer_roles,
                self.signer_subjects,
                self.signer_control_domains,
                self.signer_distinct_from_lineage_creation_actor,
            )
        ):
            raise ValueError("actor requirement must constrain admission or approval")
        signer_constraint = any(
            (
                self.signer_roles,
                self.signer_subjects,
                self.signer_control_domains,
                self.signer_distinct_from_lineage_creation_actor,
            )
        )
        if not signer_constraint and self.minimum_distinct_signers != 1:
            raise ValueError("minimum_distinct_signers requires a signer constraint")
        return self


class EvidenceRequirementV1(_StrictPolicyModel):
    tag: Literal["playbill-evidence-requirement-v1"] = "playbill-evidence-requirement-v1"
    requirement_id: str
    query_definition_digest: str
    parameters: dict[str, object] = Field(default_factory=dict)
    max_rows: int = Field(default=100, ge=1)
    max_traversal_depth: int = Field(default=4, ge=0)
    min_count: int = Field(ge=1)

    @field_validator("requirement_id")
    @classmethod
    def _requirement_id(cls, value: str) -> str:
        return governance_identifier(value, label="evidence requirement_id")

    @field_validator("query_definition_digest")
    @classmethod
    def _query_digest(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value

    @field_validator("parameters")
    @classmethod
    def _parameters(cls, value: dict[str, object]) -> dict[str, object]:
        normalized = normalize_canonical(value)
        if not isinstance(normalized, dict):  # pragma: no cover - field type proves this
            raise ValueError("query parameters must be a canonical object")
        return {str(key): item for key, item in normalized.items()}


class FreezeRequirementV1(_StrictPolicyModel):
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


PolicyRequirementV1 = Annotated[
    TransitionRequirementV1 | ActorRequirementV1 | EvidenceRequirementV1 | FreezeRequirementV1,
    Field(discriminator="tag"),
]


class ClaimAdmissionPolicyV1(_StrictPolicyModel):
    tag: Literal["playbill-claim-admission-policy-v1"] = "playbill-claim-admission-policy-v1"
    transition_requirements: tuple[TransitionRequirementV1, ...] = ()
    actor_requirements: tuple[ActorRequirementV1, ...] = ()
    evidence_requirements: tuple[EvidenceRequirementV1, ...] = ()
    freeze_requirements: tuple[FreezeRequirementV1, ...] = ()

    @model_validator(mode="after")
    def _closed_requirement_graph(self) -> "ClaimAdmissionPolicyV1":
        groups = tuple(
            tuple(item.requirement_id for item in group)
            for group in (
                self.transition_requirements,
                self.actor_requirements,
                self.evidence_requirements,
                self.freeze_requirements,
            )
        )
        for ids in groups:
            if ids != tuple(sorted(set(ids), key=lambda item: item.encode("utf-8"))):
                raise ValueError("policy requirements must be sorted and unique by requirement_id")
        all_ids = tuple(item for group in groups for item in group)
        if len(all_ids) != len(set(all_ids)):
            raise ValueError("policy requirement IDs must be unique across requirement kinds")
        actionable = {item.requirement_id for item in self.actor_requirements} | {
            item.requirement_id for item in self.evidence_requirements
        }
        for transition in self.transition_requirements:
            unknown = set(transition.require) - actionable
            if unknown:
                raise ValueError("transition refers to an unknown actor/evidence requirement")
        transition_ids = {item.requirement_id for item in self.transition_requirements}
        for freeze in self.freeze_requirements:
            if set(freeze.except_transition_requirements) - transition_ids:
                raise ValueError("freeze exception refers to an unknown transition requirement")
        return self


class ClaimResolutionPolicyV1(_StrictPolicyModel):
    tag: Literal["playbill-claim-resolution-policy-v1"] = "playbill-claim-resolution-policy-v1"
    cardinality: ClaimCardinality
    eligible_verdicts: tuple[ClaimVerdict, ...]
    required_basis_kinds: tuple[str, ...] = ()
    require_current: bool = True
    selector: Literal["all", "only_contender", "authority_rule"]
    authority_rule_digest: str | None = None
    conflict_result: Literal["unresolved", "refuse"] = "unresolved"

    @field_validator("eligible_verdicts")
    @classmethod
    def _eligible_verdicts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="resolution policy set", nonempty=True)

    @field_validator("required_basis_kinds")
    @classmethod
    def _required_basis_kinds(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="resolution required basis kinds")

    @field_validator("authority_rule_digest")
    @classmethod
    def _authority_rule_digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _selector_shape(self) -> "ClaimResolutionPolicyV1":
        if self.cardinality == "many" and self.selector != "all":
            raise ValueError("many-cardinality resolution requires selector='all'")
        if self.cardinality == "one" and self.selector == "all":
            raise ValueError("one-cardinality resolution cannot select all contenders")
        if (self.selector == "authority_rule") != (self.authority_rule_digest is not None):
            raise ValueError("authority_rule selector requires exactly one registered rule digest")
        return self


class ClaimEvidenceAdmissionRuleV1(_StrictPolicyModel):
    tag: Literal["playbill-claim-evidence-admission-rule-v1"] = (
        "playbill-claim-evidence-admission-rule-v1"
    )
    rule_id: str
    claim_roles: tuple[ClaimRole, ...]
    capture_contract_digests: tuple[str, ...]
    evidence_kinds: tuple[str, ...]
    admission: Literal["origin_only", "direct", "derivational"]
    subject_binding: Literal["exact_claim_subject", "contract_source_mapping"]
    allowed_reducer_digests: tuple[str, ...] = ()
    attestation_requirement: AttestationRequirement = "none"

    @field_validator("rule_id")
    @classmethod
    def _rule_id(cls, value: str) -> str:
        return governance_identifier(value, label="evidence-admission rule_id")

    @field_validator(
        "claim_roles",
        "capture_contract_digests",
        "evidence_kinds",
        "allowed_reducer_digests",
    )
    @classmethod
    def _rule_sets(cls, value: tuple[str, ...], info: object) -> tuple[str, ...]:
        field_name = str(getattr(info, "field_name", "evidence-admission field"))
        nonempty = field_name != "allowed_reducer_digests"
        _sorted_unique(value, label=field_name, nonempty=nonempty)
        if field_name in {"capture_contract_digests", "allowed_reducer_digests"}:
            for item in value:
                ArtifactDigest.from_tagged(item)
        elif field_name == "evidence_kinds":
            if any(not _EVIDENCE_KIND_RE.fullmatch(item) for item in value):
                raise ValueError("evidence kinds must be canonical identifiers")
        return value

    @model_validator(mode="after")
    def _reducer_shape(self) -> "ClaimEvidenceAdmissionRuleV1":
        if self.admission == "derivational" and not self.allowed_reducer_digests:
            raise ValueError("derivational evidence requires at least one allowed reducer")
        if self.admission != "derivational" and self.allowed_reducer_digests:
            raise ValueError("only derivational evidence may name reducers")
        return self


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


class AdmissionActorV1(_StrictPolicyModel):
    actor_id: str
    roles: tuple[str, ...]
    subject_identities: tuple[str, ...] = ()
    control_domain: str | None = None

    @field_validator("actor_id", "control_domain")
    @classmethod
    def _identifier(cls, value: str | None) -> str | None:
        if value is not None:
            governance_identifier(value, label="admission actor identifier")
        return value

    @field_validator("roles", "subject_identities")
    @classmethod
    def _sets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="admission actor set")


class QueryEvidenceResultV1(_StrictPolicyModel):
    requirement_id: str
    query_definition_digest: str
    result_digest: str
    matching_count: int = Field(ge=0)
    truncated: bool = False

    @field_validator("requirement_id")
    @classmethod
    def _requirement_id(cls, value: str) -> str:
        return governance_identifier(value, label="query evidence requirement_id")

    @field_validator("query_definition_digest", "result_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class ClaimAdmissionCandidateContextV1(_StrictPolicyModel):
    evaluation_time: str
    declared_predicates: tuple[str, ...]
    parent_values: dict[str, tuple[object, ...]]
    candidate_values: dict[str, tuple[object, ...]]
    admission_actor: AdmissionActorV1
    lineage_creation_actor_id: str | None
    query_results: tuple[QueryEvidenceResultV1, ...] = ()

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

    @field_validator("lineage_creation_actor_id")
    @classmethod
    def _lineage_actor(cls, value: str | None) -> str | None:
        if value is not None:
            governance_identifier(value, label="lineage creation actor")
        return value

    @field_validator("query_results")
    @classmethod
    def _query_results(
        cls, value: tuple[QueryEvidenceResultV1, ...]
    ) -> tuple[QueryEvidenceResultV1, ...]:
        ids = tuple(item.requirement_id for item in value)
        if ids != tuple(sorted(set(ids), key=lambda item: item.encode("utf-8"))):
            raise ValueError("query evidence results must be sorted and unique")
        return value


class RequiredSignerConstraintV1(_StrictPolicyModel):
    requirement_id: str
    roles: tuple[str, ...] = ()
    subject_identities: tuple[str, ...] = ()
    control_domains: tuple[str, ...] = ()
    minimum_distinct_signers: int = Field(ge=1)
    distinct_from_lineage_creation_actor: bool = False


class ClaimAdmissionCandidateResultV1(_StrictPolicyModel):
    tag: Literal["playbill-claim-admission-candidate-result-v1"] = (
        "playbill-claim-admission-candidate-result-v1"
    )
    verdict: Literal["eligible", "refused"]
    triggered_transitions: tuple[str, ...] = ()
    required_signers: tuple[RequiredSignerConstraintV1, ...] = ()
    evidence_results: tuple[QueryEvidenceResultV1, ...] = ()
    lineage_creation_actor_id: str | None
    refusal_codes: tuple[str, ...] = ()

    @field_validator("lineage_creation_actor_id")
    @classmethod
    def _lineage_actor(cls, value: str | None) -> str | None:
        if value is not None:
            governance_identifier(value, label="candidate lineage creation actor")
        return value


class VerifiedPolicySignerV1(_StrictPolicyModel):
    signer_id: str
    roles: tuple[str, ...]
    subject_identities: tuple[str, ...] = ()
    control_domain: str | None = None


class ClaimAdmissionSettlementResultV1(_StrictPolicyModel):
    tag: Literal["playbill-claim-admission-settlement-result-v1"] = (
        "playbill-claim-admission-settlement-result-v1"
    )
    verdict: Literal["satisfied", "refused"]
    satisfied_requirements: tuple[str, ...] = ()
    refusal_codes: tuple[str, ...] = ()


def evaluate_claim_admission_candidate(
    policy: ClaimAdmissionPolicyV1,
    context: ClaimAdmissionCandidateContextV1,
) -> ClaimAdmissionCandidateResultV1:
    """Evaluate parent-bound rules and emit signer constraints for phase two."""

    declared = set(context.declared_predicates)
    policy_predicates = (
        {item.when_predicate for item in policy.transition_requirements}
        | {item.while_predicate for item in policy.freeze_requirements}
        | {predicate for item in policy.freeze_requirements for predicate in item.frozen_predicates}
    )
    refusal_codes: set[str] = set()
    if policy_predicates - declared:
        refusal_codes.add("playbill.claim_policy.unknown_predicate")

    triggered: list[str] = []
    required: set[str] = set()
    for transition in policy.transition_requirements:
        parent = context.parent_values.get(transition.when_predicate, ())
        candidate = context.candidate_values.get(transition.when_predicate, parent)
        if len(parent) > 1 or len(candidate) > 1:
            refusal_codes.add("playbill.claim_policy.ambiguous_single_value")
            continue
        old = parent[0] if parent else None
        new = candidate[0] if candidate else None
        if old in transition.from_values and new == transition.to_value and old != new:
            triggered.append(transition.requirement_id)
            required.update(transition.require)

    for freeze in policy.freeze_requirements:
        parent = context.parent_values.get(freeze.while_predicate, ())
        if len(parent) > 1:
            refusal_codes.add("playbill.claim_policy.ambiguous_single_value")
            continue
        active = bool(parent) and parent[0] in freeze.while_values
        changed = any(
            context.parent_values.get(predicate, ())
            != context.candidate_values.get(predicate, context.parent_values.get(predicate, ()))
            for predicate in freeze.frozen_predicates
        )
        if (
            active
            and changed
            and not set(triggered).intersection(freeze.except_transition_requirements)
        ):
            refusal_codes.add("playbill.claim_policy.freeze_active")

    actor_requirement_ids = {item.requirement_id for item in policy.actor_requirements}
    evidence_by_id = {item.requirement_id: item for item in policy.evidence_requirements}
    query_by_id = {item.requirement_id: item for item in context.query_results}
    used_evidence: list[QueryEvidenceResultV1] = []
    for requirement_id in sorted(required, key=lambda item: item.encode("utf-8")):
        if requirement_id in actor_requirement_ids:
            continue
        evidence = evidence_by_id[requirement_id]
        result = query_by_id.get(requirement_id)
        if result is None or result.query_definition_digest != evidence.query_definition_digest:
            refusal_codes.add("playbill.claim_policy.evidence_query_missing")
        elif result.truncated:
            refusal_codes.add("playbill.claim_policy.evidence_query_truncated")
        elif result.matching_count < evidence.min_count:
            refusal_codes.add("playbill.claim_policy.evidence_min_count")
        else:
            used_evidence.append(result)

    codes = tuple(sorted(refusal_codes, key=lambda item: item.encode("utf-8")))
    return ClaimAdmissionCandidateResultV1(
        verdict="refused" if codes else "eligible",
        triggered_transitions=tuple(sorted(triggered, key=lambda item: item.encode("utf-8"))),
        required_signers=(),
        evidence_results=tuple(
            sorted(used_evidence, key=lambda item: item.requirement_id.encode("utf-8"))
        ),
        lineage_creation_actor_id=None,
        refusal_codes=codes,
    )


def evaluate_claim_admission_settlement(
    candidate_result: ClaimAdmissionCandidateResultV1,
    signers: tuple[VerifiedPolicySignerV1, ...],
    *,
    lineage_creation_actor_id: str | None,
) -> ClaimAdmissionSettlementResultV1:
    """Check candidate-emitted signer constraints after cryptographic verification."""

    if candidate_result.verdict != "eligible":
        return ClaimAdmissionSettlementResultV1(
            verdict="refused",
            refusal_codes=("playbill.claim_policy.candidate_phase_refused",),
        )
    ordered = tuple(sorted(signers, key=lambda item: item.signer_id.encode("utf-8")))
    if signers != ordered or len({item.signer_id for item in signers}) != len(signers):
        return ClaimAdmissionSettlementResultV1(
            verdict="refused",
            refusal_codes=("playbill.claim_policy.signers_not_canonical",),
        )
    return ClaimAdmissionSettlementResultV1(
        verdict="satisfied",
        satisfied_requirements=(),
        refusal_codes=(),
    )


class EvidenceAdmissionInputV1(_StrictPolicyModel):
    claim_role: ClaimRole
    capture_contract_digest: str
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


class ClaimEvidenceAdmissionResultV1(_StrictPolicyModel):
    tag: Literal["playbill-claim-evidence-admission-result-v1"] = (
        "playbill-claim-evidence-admission-result-v1"
    )
    verdict: Literal["eligible", "refused"]
    rule_id: str | None = None
    admission: Literal["origin_only", "direct", "derivational"] | None = None
    refusal_code: str | None = None


class ClaimEvidenceAdmissionTrace(_StrictPolicyModel):
    """Internal trace from the authoritative evidence-admission evaluator."""

    result: ClaimEvidenceAdmissionResultV1
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
    rule: ClaimEvidenceAdmissionRuleV1,
    evidence: EvidenceAdmissionInputV1,
) -> bool:
    if rule.admission == "derivational":
        return evidence.reducer_digest in rule.allowed_reducer_digests and bool(
            evidence.input_claim_artifact_digests
        )
    return evidence.reducer_digest is None and not evidence.input_claim_artifact_digests


def evaluate_claim_evidence_admission_trace(
    policy: ClaimEvidenceAdmissionPolicyV1,
    evidence: EvidenceAdmissionInputV1,
    *,
    subject_binding_by_rule: Mapping[str, bool] | None = None,
) -> ClaimEvidenceAdmissionTrace:
    """Evaluate evidence and retain the deterministic nearest repair rule."""

    contract_rules = tuple(
        rule
        for rule in policy.rules
        if evidence.capture_contract_digest in rule.capture_contract_digests
    )
    closest_rule_id: str | None = None
    if contract_rules:
        binding = subject_binding_by_rule or {}

        def mismatch_count(rule: ClaimEvidenceAdmissionRuleV1) -> tuple[int, bytes]:
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
        result = ClaimEvidenceAdmissionResultV1(
            verdict="refused",
            refusal_code="playbill.evidence.capture_cannot_grant_semantic_authority",
        )
        return ClaimEvidenceAdmissionTrace(result=result, closest_rule_id=closest_rule_id)
    matches = [
        rule
        for rule in policy.rules
        if evidence.claim_role in rule.claim_roles
        and evidence.capture_contract_digest in rule.capture_contract_digests
        and evidence.evidence_kind in rule.evidence_kinds
    ]
    if len(matches) != 1:
        result = ClaimEvidenceAdmissionResultV1(
            verdict="refused",
            refusal_code=(
                "playbill.evidence.admission_ambiguous"
                if matches
                else "playbill.evidence.undeclared_contract_kind"
            ),
        )
        return ClaimEvidenceAdmissionTrace(result=result, closest_rule_id=closest_rule_id)
    rule = matches[0]
    if not evidence.source_subject_bound:
        result = ClaimEvidenceAdmissionResultV1(
            verdict="refused",
            refusal_code="playbill.evidence.subject_binding_failed",
        )
        return ClaimEvidenceAdmissionTrace(result=result, closest_rule_id=closest_rule_id)
    if not _attestation_satisfied(rule.attestation_requirement, evidence.attestation_grade):
        result = ClaimEvidenceAdmissionResultV1(
            verdict="refused",
            refusal_code="playbill.evidence.attestation_grade_missing",
        )
        return ClaimEvidenceAdmissionTrace(result=result, closest_rule_id=closest_rule_id)
    if not _derivation_satisfied(rule, evidence):
        result = ClaimEvidenceAdmissionResultV1(
            verdict="refused",
            refusal_code=(
                "playbill.evidence.derivation_incomplete"
                if rule.admission == "derivational"
                else "playbill.evidence.reducer_not_allowed"
            ),
        )
        return ClaimEvidenceAdmissionTrace(result=result, closest_rule_id=closest_rule_id)
    result = ClaimEvidenceAdmissionResultV1(
        verdict="eligible",
        rule_id=rule.rule_id,
        admission=rule.admission,
    )
    return ClaimEvidenceAdmissionTrace(result=result, closest_rule_id=closest_rule_id)


def evaluate_claim_evidence_admission(
    policy: ClaimEvidenceAdmissionPolicyV1,
    evidence: EvidenceAdmissionInputV1,
) -> ClaimEvidenceAdmissionResultV1:
    """Evaluate evidence shape without granting Claim activation authority."""

    return evaluate_claim_evidence_admission_trace(policy, evidence).result


class ResolutionContenderV1(_StrictPolicyModel):
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


class AuthorityRuleDecisionV1(_StrictPolicyModel):
    rule_digest: str
    selected_claim_identity: str
    proof_digest: str

    @field_validator("rule_digest", "proof_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class ClaimResolutionResultV1(_StrictPolicyModel):
    tag: Literal["playbill-claim-resolution-result-v1"] = "playbill-claim-resolution-result-v1"
    status: Literal["resolved", "unresolved", "refused"]
    selected_claim_identities: tuple[str, ...] = ()
    contender_claim_identities: tuple[str, ...] = ()
    authority_proof_digest: str | None = None


def resolve_claim_contenders(
    policy: ClaimResolutionPolicyV1,
    contenders: tuple[ResolutionContenderV1, ...],
    *,
    authority_decision: AuthorityRuleDecisionV1 | None = None,
) -> ClaimResolutionResultV1:
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
        return ClaimResolutionResultV1(
            status="resolved",
            selected_claim_identities=identities,
            contender_claim_identities=identities,
        )
    if policy.selector == "only_contender":
        if len(eligible) == 1:
            return ClaimResolutionResultV1(
                status="resolved",
                selected_claim_identities=identities,
                contender_claim_identities=identities,
            )
        return ClaimResolutionResultV1(
            status="refused" if policy.conflict_result == "refuse" else "unresolved",
            contender_claim_identities=identities,
        )
    if (
        authority_decision is None
        or authority_decision.rule_digest != policy.authority_rule_digest
        or authority_decision.selected_claim_identity not in identities
    ):
        return ClaimResolutionResultV1(
            status="refused",
            contender_claim_identities=identities,
        )
    return ClaimResolutionResultV1(
        status="resolved",
        selected_claim_identities=(authority_decision.selected_claim_identity,),
        contender_claim_identities=identities,
        authority_proof_digest=authority_decision.proof_digest,
    )


__all__ = [
    "ActorRequirementV1",
    "AdmissionActorV1",
    "AttestationRequirement",
    "AuthorityRuleDecisionV1",
    "ClaimAdmissionCandidateContextV1",
    "ClaimAdmissionCandidateResultV1",
    "ClaimAdmissionPolicyV1",
    "ClaimAdmissionSettlementResultV1",
    "ClaimEvidenceAdmissionPolicyV1",
    "ClaimEvidenceAdmissionResultV1",
    "ClaimEvidenceAdmissionRuleV1",
    "ClaimResolutionPolicyV1",
    "ClaimResolutionResultV1",
    "ClaimVerdict",
    "EvidenceAdmissionInputV1",
    "EvidenceRequirementV1",
    "FreezeRequirementV1",
    "QueryEvidenceResultV1",
    "RequiredSignerConstraintV1",
    "ResolutionContenderV1",
    "TransitionRequirementV1",
    "VerifiedPolicySignerV1",
    "evaluate_claim_admission_candidate",
    "evaluate_claim_admission_settlement",
    "evaluate_claim_evidence_admission",
    "resolve_claim_contenders",
]
