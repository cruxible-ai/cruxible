"""Deterministic evidence-relative Claim verdicts at explicit evaluation times."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Literal, cast

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
ObservationTrustGrade = Literal[
    "proposer_observed",
    "daemon_fetched",
    "provider_receipted",
    "witnessed",
]
EvidenceEpistemicGrade = Literal["observed", "derived", "predicted"]
EvidenceCurrency = Literal["current", "stale", "not_applicable"]
EvidenceRelativeClaimVerdictV1 = Literal[
    "supported", "uncovered", "stale", "contradicted", "unresolved"
]
EvidenceRelativeClaimVerdictV2 = Literal[
    "supported",
    "uncovered",
    "stale",
    "stale_evidence",
    "contradicted",
    "unresolved",
]
EvidenceRelativeClaimVerdict = EvidenceRelativeClaimVerdictV1


class _StrictVerdictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def observation_trust_grade(
    provenance_grade: EvidenceProvenanceGrade,
) -> ObservationTrustGrade:
    """Present observation trust separately without rewriting verdict receipt values."""

    values: dict[EvidenceProvenanceGrade, ObservationTrustGrade] = {
        "self-asserted": "proposer_observed",
        "daemon-fetched": "daemon_fetched",
        "provider-signed": "provider_receipted",
        "witnessed": "witnessed",
    }
    return values[provenance_grade]


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
        max_evidence_age=(
            None
            if claim_type.evidence_freshness is None
            else CanonicalDurationV1.model_validate(
                claim_type.evidence_freshness.stale_after.model_dump(mode="json")
            )
        ),
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

    _require_unique_evidence_digests(captures, attestations)

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


def _require_unique_evidence_digests(
    captures: tuple[CaptureVerdictEvidenceV1, ...],
    attestations: tuple[VerifiedClaimAttestationV1, ...],
) -> None:
    """Refuse one CAS object presented as more than one evidence node."""

    digests = tuple(item.capture_digest for item in captures) + tuple(
        item.attestation_digest for item in attestations
    )
    if len(digests) != len(set(digests)):
        raise ValueError("Claim verdict evidence digests must be globally unique")


class ClaimVerdictResultV1(_StrictVerdictModel):
    """Verdict at one time; evidence digest tuples are lifetime stance inventory."""

    tag: Literal["playbill-claim-verdict-v1"] = "playbill-claim-verdict-v1"
    claim_statement_digest: str
    adjudication_rule_digest: str
    evaluation_time: datetime
    verdict: EvidenceRelativeClaimVerdictV1
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


class EvidenceFreshnessExpirationV1(_StrictVerdictModel):
    tag: Literal["playbill-evidence-freshness-expiration-v1"] = (
        "playbill-evidence-freshness-expiration-v1"
    )
    capture_digest: str
    observed_at: datetime
    expires_at: datetime

    @field_validator("capture_digest")
    @classmethod
    def _capture_digest(cls, value: str) -> str:
        CasDigest.from_tagged(value)
        return value

    @field_validator("observed_at", "expires_at")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evidence freshness times must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _positive_interval(self) -> "EvidenceFreshnessExpirationV1":
        if self.expires_at <= self.observed_at:
            raise ValueError("evidence freshness expiration must follow observation")
        return self


class ClaimVerdictResultV2(_StrictVerdictModel):
    """Verdict carrying the exact horizon-expiration vector for ClaimType v3."""

    tag: Literal["playbill-claim-verdict-v2"] = "playbill-claim-verdict-v2"
    claim_statement_digest: str
    adjudication_rule_digest: str
    evaluation_time: datetime
    verdict: EvidenceRelativeClaimVerdictV2
    currency: EvidenceCurrency
    basis_kinds: tuple[EvidenceBasisKind, ...]
    authority_basis: tuple[str, ...]
    provenance_grades: tuple[EvidenceProvenanceGrade, ...]
    epistemic_grades: tuple[EvidenceEpistemicGrade, ...]
    supporting_evidence_digests: tuple[str, ...]
    contradicting_evidence_digests: tuple[str, ...]
    unsure_evidence_digests: tuple[str, ...]
    control_components: tuple[EvidenceControlComponentV1, ...]
    freshness_expirations: tuple[EvidenceFreshnessExpirationV1, ...]
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

    @field_validator("freshness_expirations")
    @classmethod
    def _freshness_expirations(
        cls, value: tuple[EvidenceFreshnessExpirationV1, ...]
    ) -> tuple[EvidenceFreshnessExpirationV1, ...]:
        digests = tuple(item.capture_digest for item in value)
        if digests != tuple(sorted(set(digests), key=lambda item: item.encode("ascii"))):
            raise ValueError("freshness expirations must be byte-sorted and unique")
        return value


ClaimVerdictResultAny = ClaimVerdictResultV1 | ClaimVerdictResultV2


def claim_verdict_v1_compat(result: ClaimVerdictResultAny) -> ClaimVerdictResultV1:
    """Project v2 freshness into the conservative v1 stale vocabulary."""

    if isinstance(result, ClaimVerdictResultV1):
        return result
    return ClaimVerdictResultV1(
        claim_statement_digest=result.claim_statement_digest,
        adjudication_rule_digest=result.adjudication_rule_digest,
        evaluation_time=result.evaluation_time,
        verdict=("stale" if result.verdict == "stale_evidence" else result.verdict),
        currency=result.currency,
        basis_kinds=result.basis_kinds,
        authority_basis=result.authority_basis,
        provenance_grades=result.provenance_grades,
        epistemic_grades=result.epistemic_grades,
        supporting_evidence_digests=result.supporting_evidence_digests,
        contradicting_evidence_digests=result.contradicting_evidence_digests,
        unsure_evidence_digests=result.unsure_evidence_digests,
        control_components=result.control_components,
        refusal_codes=result.refusal_codes,
    )


def verify_claim_verdict_freshness(
    result: ClaimVerdictResultAny,
    *,
    rule: ClaimAdjudicationRuleV1,
    captures: tuple[CaptureVerdictEvidenceV1, ...],
) -> None:
    """Refuse a v3 verdict whose committed expiration vector cannot reproduce."""

    if rule.max_evidence_age is None:
        if isinstance(result, ClaimVerdictResultV2):
            raise ValueError(
                "playbill.claim.evidence_freshness_invalid: v2 verdict has no freshness rule"
            )
        return
    if not isinstance(result, ClaimVerdictResultV2):
        raise ValueError(
            "playbill.claim.evidence_freshness_invalid: freshness rule requires a v2 verdict"
        )
    expected = tuple(
        EvidenceFreshnessExpirationV1(
            capture_digest=item.capture_digest,
            observed_at=item.observed_at,
            expires_at=item.observed_at
            + timedelta(microseconds=rule.max_evidence_age.microseconds),
        )
        for item in sorted(captures, key=lambda item: item.capture_digest.encode("ascii"))
        if item.admission in {"direct", "derivational"}
    )
    if result.freshness_expirations != expected:
        raise ValueError(
            "playbill.claim.evidence_freshness_invalid: expiration vector does not reproduce"
        )


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
    referent_current: bool = True,
    resolved_authority_basis: tuple[str, ...] = (),
) -> ClaimVerdictResultAny:
    """Compute one replayable verdict from current evidence and resolved authority.

    ``resolved_authority_basis`` must already have been revalidated at
    ``evaluation_time`` and the evaluated accepted coordinate. Historical
    verdict output is never a valid input to this parameter.
    """

    Sha256Value.from_tagged(claim_statement_digest)
    if evaluation_time.tzinfo is None or evaluation_time.utcoffset() is None:
        raise ValueError("Claim verdict evaluation time must be timezone-aware")
    _require_unique_evidence_digests(captures, attestations)
    if resolved_authority_basis != tuple(sorted(set(resolved_authority_basis))):
        raise ValueError("Claim authority basis must be sorted and unique")
    rule_digest = claim_adjudication_rule_digest(rule)
    before_claim_interval = (
        claim_effective_from is not None and evaluation_time < claim_effective_from
    )
    after_claim_interval = (
        claim_effective_until is not None and evaluation_time >= claim_effective_until
    )

    current_captures = tuple(
        item
        for item in captures
        if _capture_current(item, rule=rule, evaluation_time=evaluation_time)
        and (not rule.shell_sensitive or referent_current)
    )
    current_attestations = tuple(
        item
        for item in attestations
        if item.statement.observed_at <= evaluation_time
        and (item.statement.valid_until is None or evaluation_time < item.statement.valid_until)
        and (not rule.shell_sensitive or item.current)
    )
    admitted_capture_digests = {
        item.capture_digest
        for item in current_captures
        if item.admission in {"direct", "derivational"}
    }
    started_admitted_capture_digests = {
        item.capture_digest
        for item in captures
        if item.admission in {"direct", "derivational"} and item.observed_at <= evaluation_time
    }

    def attestation_has_relevant_captures(item: VerifiedClaimAttestationV1) -> bool:
        cited = set(item.statement.capture_digests)
        return bool(cited) and cited.issubset(admitted_capture_digests)

    support_capture_digests = {
        item.capture_digest
        for item in current_captures
        if item.admission in {"direct", "derivational"}
    }
    support_attestations = {
        item.attestation_digest
        for item in current_attestations
        if item.statement.stance == "support" and attestation_has_relevant_captures(item)
    }
    contradicting = {
        item.attestation_digest
        for item in current_attestations
        if item.statement.stance == "contradict" and attestation_has_relevant_captures(item)
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
    if before_claim_interval:
        verdict: EvidenceRelativeClaimVerdictV2 = "uncovered"
    elif after_claim_interval:
        verdict = "stale"
    elif support_satisfies and contradict_satisfies:
        verdict = (
            "contradicted" if rule.conflict_behavior == "contradiction_precedence" else "unresolved"
        )
    elif contradict_satisfies:
        verdict = "contradicted"
    elif support_satisfies or resolved_authority_basis:
        verdict = "supported"
    elif rule.max_evidence_age is not None and any(
        item.admission in {"direct", "derivational"}
        and item.observed_at <= evaluation_time
        and evaluation_time
        >= item.observed_at + timedelta(microseconds=rule.max_evidence_age.microseconds)
        and (item.source_effective_until is None or evaluation_time < item.source_effective_until)
        and (not rule.require_current_replay or item.current_replay_available)
        and (not rule.shell_sensitive or referent_current)
        for item in captures
    ):
        verdict = "stale_evidence"
    elif captures or attestations:
        any_potentially_supporting = any(
            item.admission != "origin_only" for item in captures
        ) or any(item.statement.stance != "unsure" for item in attestations)
        any_started_potential = any(
            item.observed_at <= evaluation_time and item.admission != "origin_only"
            for item in captures
        ) or any(
            item.statement.observed_at <= evaluation_time
            and item.statement.stance != "unsure"
            and bool(item.statement.capture_digests)
            and set(item.statement.capture_digests).issubset(started_admitted_capture_digests)
            for item in attestations
        )
        any_current_potential = bool(support or contradicting)
        verdict = (
            "stale"
            if any_potentially_supporting and any_started_potential and not any_current_potential
            else "uncovered"
        )
    else:
        verdict = "uncovered"
    basis = {item.basis_kind for item in current_captures if item.capture_digest in support}
    if support_attestations or contradicting:
        basis.add("signature_verified")
    if resolved_authority_basis:
        basis.add("authority_ruled")
    provenance = {
        item.provenance_grade for item in current_captures if item.capture_digest in support
    }
    epistemic = {
        item.epistemic_grade for item in current_captures if item.capture_digest in support
    }
    currency: EvidenceCurrency = (
        "not_applicable"
        if before_claim_interval
        else ("stale" if verdict in {"stale", "stale_evidence"} else "current")
    )
    if rule.max_evidence_age is not None:
        result = ClaimVerdictResultV2(
            claim_statement_digest=claim_statement_digest,
            adjudication_rule_digest=rule_digest,
            evaluation_time=evaluation_time,
            verdict=verdict,
            currency=currency,
            basis_kinds=tuple(sorted(basis)),
            authority_basis=resolved_authority_basis,
            provenance_grades=tuple(sorted(provenance)),
            epistemic_grades=tuple(sorted(epistemic)),
            supporting_evidence_digests=tuple(sorted(all_support)),
            contradicting_evidence_digests=tuple(sorted(all_contradicting)),
            unsure_evidence_digests=tuple(sorted(all_unsure)),
            control_components=all_components,
            freshness_expirations=tuple(
                EvidenceFreshnessExpirationV1(
                    capture_digest=item.capture_digest,
                    observed_at=item.observed_at,
                    expires_at=item.observed_at
                    + timedelta(microseconds=rule.max_evidence_age.microseconds),
                )
                for item in sorted(captures, key=lambda item: item.capture_digest.encode("ascii"))
                if item.admission in {"direct", "derivational"}
            ),
        )
        verify_claim_verdict_freshness(result, rule=rule, captures=captures)
        return result
    return ClaimVerdictResultV1(
        claim_statement_digest=claim_statement_digest,
        adjudication_rule_digest=rule_digest,
        evaluation_time=evaluation_time,
        verdict=cast(EvidenceRelativeClaimVerdictV1, verdict),
        currency=currency,
        basis_kinds=tuple(sorted(basis)),
        authority_basis=resolved_authority_basis,
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
    "ClaimVerdictResultV2",
    "ClaimVerdictResultAny",
    "EvidenceBasisKind",
    "EvidenceControlComponentV1",
    "EvidenceCurrency",
    "EvidenceEpistemicGrade",
    "EvidenceProvenanceGrade",
    "EvidenceRelativeClaimVerdict",
    "EvidenceRelativeClaimVerdictV1",
    "EvidenceRelativeClaimVerdictV2",
    "EvidenceFreshnessExpirationV1",
    "ObservationTrustGrade",
    "claim_adjudication_rule",
    "claim_adjudication_rule_digest",
    "claim_verdict_v1_compat",
    "evaluate_claim_verdict",
    "evidence_control_components",
    "observation_trust_grade",
    "verify_claim_verdict_freshness",
]
