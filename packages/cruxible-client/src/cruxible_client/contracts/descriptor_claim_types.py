"""Exact ordinary ClaimType artifacts for the four governed discovery descriptors."""

from __future__ import annotations

from typing import Literal

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    capture_contract_digest,
)
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicyV1,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionRuleV1,
    ClaimResolutionPolicyV1,
)

DescriptorPredicate = Literal[
    "semantic.alias",
    "semantic.distinct_from",
    "semantic.related_to",
    "semantic.tag",
]

_SEMANTIC_KINDS = ("semantic.claim_type", "semantic.subject")


def descriptor_claim_type(predicate: DescriptorPredicate) -> ClaimType:
    """Expand one reviewed descriptor seed into its complete ordinary ClaimType."""

    relation = predicate in {"semantic.distinct_from", "semantic.related_to"}
    capture_digest = capture_contract_digest(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT).tagged
    return ClaimType(
        identity=ArtifactIdentity(kind="ClaimType", name=predicate),
        predicate=predicate,
        allowed_subject_kinds=_SEMANTIC_KINDS,
        object_kind="subject" if relation else "literal",
        literal_schema=(
            None
            if relation
            else {
                "maxLength": 80,
                "minLength": 1,
                "type": "string",
            }
        ),
        allowed_object_subject_kinds=_SEMANTIC_KINDS if relation else (),
        cardinality="many",
        permitted_roles=("normative",),
        evidence_admission_policy=ClaimEvidenceAdmissionPolicyV1(
            rules=(
                ClaimEvidenceAdmissionRuleV1(
                    rule_id="direct-governed-descriptor",
                    claim_roles=("normative",),
                    capture_contract_digests=(capture_digest,),
                    evidence_kinds=("self_asserted",),
                    admission="direct",
                    subject_binding="exact_claim_subject",
                ),
            )
        ),
        admission_policy=ClaimAdmissionPolicyV1(),
        resolution_policy=ClaimResolutionPolicyV1(
            cardinality="many",
            eligible_verdicts=("supported",),
            selector="all",
        ),
    )


_DESCRIPTOR_PREDICATES: tuple[DescriptorPredicate, ...] = (
    "semantic.alias",
    "semantic.distinct_from",
    "semantic.related_to",
    "semantic.tag",
)
DESCRIPTOR_CLAIM_TYPES: tuple[ClaimType, ...] = tuple(
    descriptor_claim_type(predicate) for predicate in _DESCRIPTOR_PREDICATES
)


__all__ = [
    "DESCRIPTOR_CLAIM_TYPES",
    "DescriptorPredicate",
    "descriptor_claim_type",
]
