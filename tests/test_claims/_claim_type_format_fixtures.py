"""One ClaimType per historical format, built only from pre-v7 contract names.

The byte-identity test pins the render, digest and ``model_dump`` of each of
these to the values the code before ClaimType v7 produced. This module must
therefore stay importable by that code: it names nothing v7 introduced.
"""

from __future__ import annotations

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactRef
from cruxible_client.contracts.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    capture_contract_digest,
    foreign_source_capture_contract,
)
from cruxible_client.contracts.claim_types import (
    ClaimAttestationConsequencePolicy,
    ClaimAttestationConsequenceRule,
    ClaimEvidenceFreshness,
    ClaimFreshnessDuration,
    ClaimType,
)
from cruxible_client.contracts.policies import (
    CAPTURE_CONTRACT_REF_ROLE,
    ClaimAdmissionPolicy,
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionPolicyV2,
    ClaimEvidenceAdmissionRule,
    ClaimEvidenceAdmissionRuleV1,
    ClaimEvidenceAdmissionRuleV2,
    ClaimResolutionPolicy,
)

_PREDICATE = "project.work_item.status"
_SOURCE = foreign_source_capture_contract("repo.work-items")


def _base(**update: object) -> ClaimType:
    base = ClaimType(
        identity=ArtifactIdentity(kind="ClaimType", name=_PREDICATE),
        predicate=_PREDICATE,
        allowed_subject_kinds=("project.work_item",),
        object_kind="literal",
        literal_schema={"enum": ["blocked", "done", "ready"], "type": "string"},
        cardinality="one",
        permitted_roles=("normative", "observation"),
        evidence_admission_policy=ClaimEvidenceAdmissionPolicyV1(
            rules=(
                ClaimEvidenceAdmissionRuleV1(
                    rule_id="direct-self-asserted",
                    claim_roles=("normative", "observation"),
                    capture_contract_digests=(
                        capture_contract_digest(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT).tagged,
                    ),
                    evidence_kinds=("self_asserted",),
                    admission="direct",
                    subject_binding="exact_claim_subject",
                ),
            )
        ),
        admission_policy=ClaimAdmissionPolicy(),
        resolution_policy=ClaimResolutionPolicy(
            cardinality="one",
            eligible_verdicts=("supported",),
            selector="only_contender",
        ),
    )
    return ClaimType.model_validate({**base.model_dump(mode="python"), **update})


_FRESHNESS = ClaimEvidenceFreshness(stale_after=ClaimFreshnessDuration(microseconds=86_400_000_000))
_CONSEQUENCES = ClaimAttestationConsequencePolicy(
    rules=(
        ClaimAttestationConsequenceRule(
            rule_id="contradict-once",
            stance="contradict",
            minimum_independent_control_components=1,
        ),
    )
)
_V2_RULE = ClaimEvidenceAdmissionRuleV2(
    rule_id="source",
    claim_roles=("normative", "observation"),
    capture_contract_digests=(capture_contract_digest(_SOURCE).tagged,),
    evidence_kinds=("self_asserted",),
    admission="direct",
    subject_binding="exact_claim_subject",
)
_V3_RULE = ClaimEvidenceAdmissionRule(
    rule_id="source",
    claim_roles=("normative", "observation"),
    capture_contracts=(ArtifactRef(role=CAPTURE_CONTRACT_REF_ROLE, target=_SOURCE.identity),),
    evidence_kinds=("self_asserted",),
    admission="direct",
    subject_binding="exact_claim_subject",
)


def historical_claim_types() -> dict[str, ClaimType]:
    """Every pre-v7 format, including the optional fields each one may carry."""

    return {
        "v1": _base(),
        "v3": _base(artifact_format="playbill-claim-type-v3", evidence_freshness=_FRESHNESS),
        "v4": _base(
            artifact_format="playbill-claim-type-v4",
            evidence_freshness=_FRESHNESS,
            attestation_consequence_policy=_CONSEQUENCES,
        ),
        "v5": _base(
            artifact_format="playbill-claim-type-v5",
            evidence_admission_policy=ClaimEvidenceAdmissionPolicyV2(rules=(_V2_RULE,)),
        ),
        "v5-hold": _base(
            artifact_format="playbill-claim-type-v5",
            evidence_admission_policy=ClaimEvidenceAdmissionPolicyV2(rules=(_V2_RULE,)),
            unsure_hold_for=ClaimFreshnessDuration(microseconds=3_600_000_000),
            attestation_consequence_policy=_CONSEQUENCES,
        ),
        "v6": _base(
            artifact_format="playbill-claim-type-v6",
            evidence_admission_policy=ClaimEvidenceAdmissionPolicy(rules=(_V3_RULE,)),
            evidence_freshness=_FRESHNESS,
        ),
    }


def fingerprints() -> dict[str, tuple[str, str, str]]:
    """``(sha256(render), digest, sha256(canonical model_dump))`` per fixture."""

    import hashlib

    from cruxible_client.contracts.canonical import canonical_bytes
    from cruxible_client.contracts.claim_types import claim_type_digest, render_claim_type

    return {
        name: (
            hashlib.sha256(render_claim_type(claim_type)).hexdigest(),
            claim_type_digest(claim_type).tagged,
            hashlib.sha256(canonical_bytes(claim_type.model_dump(mode="json"))).hexdigest(),
        )
        for name, claim_type in historical_claim_types().items()
    }
