"""Deterministic evidence-relative Claim verdicts at explicit evaluation times."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_core.playbill.artifacts import ArtifactIdentity
from cruxible_core.playbill.canonical import ArtifactDigest, CasDigest, Sha256Value, typed_digest
from cruxible_core.playbill.captures import CanonicalDurationV1
from cruxible_core.playbill.claim_attestations import VerifiedClaimAttestationV1
from cruxible_core.playbill.claim_types import ClaimType
from cruxible_core.playbill.policies import ClaimEvidenceAdmissionPolicyV1
from cruxible_core.playbill.providers import ProviderV1

EvidenceBasisKind = Literal[
    "origin_only",
    "signature_verified",
    "replay_verified",
    "arithmetic_derived",
    "authority_ruled",
]
EvidenceProvenanceGrade = Literal["self-asserted", "daemon-fetched", "provider-signed", "witnessed"]
EvidenceEpistemicGrade = Literal["observed", "derived", "predicted"]
EvidenceCurrency = Literal["current", "stale", "not_applicable"]
EvidenceRelativeClaimVerdict = Literal[
    "supported", "uncovered", "stale", "contradicted", "unresolved"
]


class _StrictVerdictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _policy_digest(policy: ClaimEvidenceAdmissionPolicyV1) -> str:
    payload = policy.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(
        Sha256Value,
        "playbill-claim-evidence-admission-policy-v1",
        payload,
    ).tagged


class ClaimAdjudicationRuleV1(_StrictVerdictModel):
    tag: Literal["playbill-claim-adjudication-rule-v1"] = "playbill-claim-adjudication-rule-v1"
    claim_type_digest: str
    evidence_policy_digest: str
    shell_sensitive: bool
    max_evidence_age: CanonicalDurationV1 | None = None
    require_current_replay: bool = False
    minimum_supporting_control_domains: int = 1
    minimum_contradicting_control_domains: int = 1
    conflict_behavior: Literal["unresolved", "contradiction_precedence"] = "unresolved"

    @field_validator("claim_type_digest", "evidence_policy_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _thresholds(self) -> "ClaimAdjudicationRuleV1":
        if (
            self.minimum_supporting_control_domains < 1
            or self.minimum_contradicting_control_domains < 1
        ):
            raise ValueError("Claim adjudication independence floors must be positive")
        return self


def claim_adjudication_rule(
    claim_type: ClaimType,
    *,
    claim_type_digest: str,
) -> ClaimAdjudicationRuleV1:
    """Compile the conservative v1 rule from the exact accepted ClaimType."""

    ArtifactDigest.from_tagged(claim_type_digest)
    return ClaimAdjudicationRuleV1(
        claim_type_digest=claim_type_digest,
        evidence_policy_digest=_policy_digest(claim_type.evidence_admission_policy),
        shell_sensitive=claim_type.referent_sensitivity == "shell",
        require_current_replay=claim_type.resolution_policy.require_current,
        conflict_behavior=(
            "contradiction_precedence" if claim_type.cardinality == "one" else "unresolved"
        ),
    )


def claim_adjudication_rule_digest(rule: ClaimAdjudicationRuleV1) -> str:
    payload = rule.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(
        Sha256Value,
        "playbill-claim-adjudication-rule-v1",
        payload,
    ).tagged


class CaptureVerdictEvidenceV1(_StrictVerdictModel):
    tag: Literal["playbill-capture-verdict-evidence-v1"] = "playbill-capture-verdict-evidence-v1"
    capture_digest: str
    admission: Literal["origin_only", "direct", "derivational"]
    basis_kind: EvidenceBasisKind
    producer: ArtifactIdentity
    control_domain: str
    upstream_provenance: tuple[ArtifactIdentity, ...] = ()
    epistemic_grade: EvidenceEpistemicGrade
    provenance_grade: EvidenceProvenanceGrade
    observed_at: datetime
    source_effective_until: datetime | None = None
    current_replay_available: bool

    @field_validator("capture_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        CasDigest.from_tagged(value)
        return value

    @field_validator("observed_at", "source_effective_until")
    @classmethod
    def _time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("verdict evidence times must be timezone-aware")
        return value

    @field_validator("upstream_provenance")
    @classmethod
    def _upstream(cls, value: tuple[ArtifactIdentity, ...]) -> tuple[ArtifactIdentity, ...]:
        keys = tuple(item.qualified for item in value)
        if keys != tuple(sorted(set(keys), key=lambda item: item.encode("utf-8"))):
            raise ValueError("evidence upstream provenance must be sorted and unique")
        return value


class EvidenceControlComponentV1(_StrictVerdictModel):
    evidence_digests: tuple[str, ...]
    control_domains: tuple[str, ...]
    provider_identities: tuple[str, ...]


def _control_closure(
    *,
    control_domain: str,
    upstream: tuple[ArtifactIdentity, ...],
    providers: Mapping[str, ProviderV1],
) -> set[str]:
    closure = {f"control:{control_domain}"}
    pending = list(upstream)
    seen: set[str] = set()
    while pending:
        identity = pending.pop()
        if identity.qualified in seen:
            continue
        seen.add(identity.qualified)
        closure.add(f"provider:{identity.qualified}")
        provider = providers.get(identity.qualified)
        if provider is not None:
            closure.add(f"control:{provider.control_domain}")
            pending.extend(provider.upstream_provenance)
    return closure


def evidence_control_components(
    captures: tuple[CaptureVerdictEvidenceV1, ...],
    attestations: tuple[VerifiedClaimAttestationV1, ...],
    *,
    providers: Mapping[str, ProviderV1],
) -> tuple[EvidenceControlComponentV1, ...]:
    """Collapse shared ultimate control/upstream provenance into independence groups."""

    nodes: list[tuple[str, set[str]]] = []
    for capture in captures:
        nodes.append(
            (
                capture.capture_digest,
                _control_closure(
                    control_domain=capture.control_domain,
                    upstream=capture.upstream_provenance,
                    providers=providers,
                ),
            )
        )
    for attestation in attestations:
        nodes.append(
            (
                attestation.attestation_digest,
                _control_closure(
                    control_domain=attestation.control_domain,
                    upstream=attestation.upstream_provenance,
                    providers=providers,
                ),
            )
        )
    components: list[tuple[set[str], set[str]]] = []
    for evidence_digest, closure in sorted(nodes, key=lambda item: item[0]):
        touching = [index for index, (_, domains) in enumerate(components) if domains & closure]
        if not touching:
            components.append(({evidence_digest}, set(closure)))
            continue
        evidence = {evidence_digest}
        domains = set(closure)
        for index in reversed(touching):
            prior_evidence, prior_domains = components.pop(index)
            evidence.update(prior_evidence)
            domains.update(prior_domains)
        components.append((evidence, domains))
    result = tuple(
        EvidenceControlComponentV1(
            evidence_digests=tuple(sorted(evidence)),
            control_domains=tuple(
                sorted(
                    item.removeprefix("control:") for item in domains if item.startswith("control:")
                )
            ),
            provider_identities=tuple(
                sorted(
                    item.removeprefix("provider:")
                    for item in domains
                    if item.startswith("provider:")
                )
            ),
        )
        for evidence, domains in components
    )
    return tuple(sorted(result, key=lambda item: item.evidence_digests))


class ClaimVerdictResultV1(_StrictVerdictModel):
    tag: Literal["playbill-claim-verdict-v1"] = "playbill-claim-verdict-v1"
    claim_statement_digest: str
    adjudication_rule_digest: str
    evaluation_time: datetime
    verdict: EvidenceRelativeClaimVerdict
    currency: EvidenceCurrency
    basis_kinds: tuple[EvidenceBasisKind, ...]
    authority_basis: tuple[str, ...]
    provenance_grades: tuple[EvidenceProvenanceGrade, ...]
    epistemic_grades: tuple[EvidenceEpistemicGrade, ...]
    supporting_evidence_digests: tuple[str, ...]
    contradicting_evidence_digests: tuple[str, ...]
    unsure_evidence_digests: tuple[str, ...]
    control_components: tuple[EvidenceControlComponentV1, ...]
    refusal_codes: tuple[str, ...] = ()

    @field_validator("claim_statement_digest", "adjudication_rule_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("evaluation_time")
    @classmethod
    def _evaluation_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Claim verdict evaluation time must be timezone-aware")
        return value


def _capture_current(
    evidence: CaptureVerdictEvidenceV1,
    *,
    rule: ClaimAdjudicationRuleV1,
    evaluation_time: datetime,
) -> bool:
    if evaluation_time < evidence.observed_at:
        return False
    if evidence.source_effective_until is not None and (
        evaluation_time >= evidence.source_effective_until
    ):
        return False
    if rule.max_evidence_age is not None and evaluation_time >= evidence.observed_at + timedelta(
        microseconds=rule.max_evidence_age.microseconds
    ):
        return False
    if rule.require_current_replay and not evidence.current_replay_available:
        return False
    return True


def evaluate_claim_verdict(
    *,
    claim_statement_digest: str,
    rule: ClaimAdjudicationRuleV1,
    evaluation_time: datetime,
    captures: tuple[CaptureVerdictEvidenceV1, ...],
    attestations: tuple[VerifiedClaimAttestationV1, ...],
    providers: Mapping[str, ProviderV1],
    claim_effective_from: datetime | None = None,
    claim_effective_until: datetime | None = None,
    authority_basis: tuple[str, ...] = (),
) -> ClaimVerdictResultV1:
    """Compute one replayable verdict; acceptance and authority remain separate inputs."""

    Sha256Value.from_tagged(claim_statement_digest)
    if evaluation_time.tzinfo is None or evaluation_time.utcoffset() is None:
        raise ValueError("Claim verdict evaluation time must be timezone-aware")
    if authority_basis != tuple(sorted(set(authority_basis))):
        raise ValueError("Claim authority basis must be sorted and unique")
    rule_digest = claim_adjudication_rule_digest(rule)
    outside_claim_interval = (
        claim_effective_from is not None and evaluation_time < claim_effective_from
    ) or (claim_effective_until is not None and evaluation_time >= claim_effective_until)

    current_captures = tuple(
        item
        for item in captures
        if _capture_current(item, rule=rule, evaluation_time=evaluation_time)
    )
    current_attestations = tuple(
        item
        for item in attestations
        if item.statement.observed_at <= evaluation_time
        and (item.statement.valid_until is None or evaluation_time < item.statement.valid_until)
        and (not rule.shell_sensitive or item.current)
    )
    support_capture_digests = {
        item.capture_digest
        for item in current_captures
        if item.admission in {"direct", "derivational"}
    }
    support_attestations = {
        item.attestation_digest
        for item in current_attestations
        if item.statement.stance == "support"
    }
    contradicting = {
        item.attestation_digest
        for item in current_attestations
        if item.statement.stance == "contradict"
    }
    support = support_capture_digests | support_attestations
    all_support = {
        item.capture_digest for item in captures if item.admission in {"direct", "derivational"}
    } | {item.attestation_digest for item in attestations if item.statement.stance == "support"}
    all_contradicting = {
        item.attestation_digest for item in attestations if item.statement.stance == "contradict"
    }
    all_unsure = {
        item.attestation_digest for item in attestations if item.statement.stance == "unsure"
    }
    all_components = evidence_control_components(
        current_captures,
        current_attestations,
        providers=providers,
    )

    def independent_count(evidence: set[str]) -> int:
        return sum(bool(evidence.intersection(item.evidence_digests)) for item in all_components)

    support_satisfies = independent_count(support) >= rule.minimum_supporting_control_domains
    contradict_satisfies = (
        independent_count(contradicting) >= rule.minimum_contradicting_control_domains
    )
    if outside_claim_interval:
        verdict: EvidenceRelativeClaimVerdict = "stale"
    elif support_satisfies and contradict_satisfies:
        verdict = (
            "contradicted" if rule.conflict_behavior == "contradiction_precedence" else "unresolved"
        )
    elif contradict_satisfies:
        verdict = "contradicted"
    elif support_satisfies or authority_basis:
        verdict = "supported"
    elif captures or attestations:
        any_potentially_supporting = any(
            item.admission != "origin_only" for item in captures
        ) or any(item.statement.stance != "unsure" for item in attestations)
        any_current_potential = bool(support or contradicting)
        verdict = (
            "stale" if any_potentially_supporting and not any_current_potential else "uncovered"
        )
    else:
        verdict = "uncovered"
    current = verdict not in {"stale"}
    basis = {item.basis_kind for item in current_captures if item.capture_digest in support}
    if support_attestations or contradicting:
        basis.add("signature_verified")
    if authority_basis:
        basis.add("authority_ruled")
    provenance = {
        item.provenance_grade for item in current_captures if item.capture_digest in support
    }
    epistemic = {
        item.epistemic_grade for item in current_captures if item.capture_digest in support
    }
    return ClaimVerdictResultV1(
        claim_statement_digest=claim_statement_digest,
        adjudication_rule_digest=rule_digest,
        evaluation_time=evaluation_time,
        verdict=verdict,
        currency="current" if current else "stale",
        basis_kinds=tuple(sorted(basis)),
        authority_basis=authority_basis,
        provenance_grades=tuple(sorted(provenance)),
        epistemic_grades=tuple(sorted(epistemic)),
        supporting_evidence_digests=tuple(sorted(all_support)),
        contradicting_evidence_digests=tuple(sorted(all_contradicting)),
        unsure_evidence_digests=tuple(sorted(all_unsure)),
        control_components=all_components,
    )


__all__ = [
    "CaptureVerdictEvidenceV1",
    "ClaimAdjudicationRuleV1",
    "ClaimVerdictResultV1",
    "EvidenceBasisKind",
    "EvidenceControlComponentV1",
    "EvidenceCurrency",
    "EvidenceEpistemicGrade",
    "EvidenceProvenanceGrade",
    "EvidenceRelativeClaimVerdict",
    "claim_adjudication_rule",
    "claim_adjudication_rule_digest",
    "evaluate_claim_verdict",
    "evidence_control_components",
]
