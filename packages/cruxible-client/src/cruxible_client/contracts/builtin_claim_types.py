"""ClaimTypes core ships: new instances start with them, existing ones adopt them.

``semantic.alias`` states another name a Subject is known by. Its values are
stored in the canonical form ``nfc, casefold, collapse_whitespace, trim`` -- the
same rule the discovery normalizer applies -- so a stored alias is exactly the
term every Subject reference is looked up by. A reference ``kind/id`` that names
no Subject resolves through the aliases of that kind: one match resolves,
several refuse naming each, none leaves the reference unresolved.

``semantic.distinct_from`` states that two Subjects are different things,
whatever their names or keys share: the duplicates audit never groups an
asserted pair, a ``hint`` kind's second holder of a key cites it, and a
creation that would otherwise collide with an alias names it in the same change.

A new instance seeds these at generation zero (the ``triggers-3-workspace-file-
alias-distinct`` genesis seed set; ``semantic.alias`` alone before it); an
existing instance adopts one by proposing its exact envelope
(``cruxible claim-type propose --builtin semantic.distinct_from``).
"""

from __future__ import annotations

from typing import Final, Literal

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.captures import COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
from cruxible_client.contracts.claim_types import ClaimType, claim_type_path, render_claim_type
from cruxible_client.contracts.policies import (
    CAPTURE_CONTRACT_REF_ROLE,
    CaptureContractAdmission,
    ClaimAdmissionPolicy,
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
    ClaimResolutionPolicy,
)

#: The pseudo-kind a descriptor ClaimType admits: every Subject, whatever its kind.
SEMANTIC_SUBJECT_KIND: Final = "semantic.subject"
SEMANTIC_ALIAS_PREDICATE: Final = "semantic.alias"
SEMANTIC_DISTINCT_FROM_PREDICATE: Final = "semantic.distinct_from"
BuiltinClaimTypeName = Literal["semantic.alias", "semantic.distinct_from"]


def _self_evidence() -> ClaimEvidenceAdmissionPolicy:
    """The author's own words, as every builtin descriptor admits them."""

    return ClaimEvidenceAdmissionPolicy(
        rules=(
            ClaimEvidenceAdmissionRule(
                rule_id="self",
                claim_roles=("normative", "observation"),
                capture_contracts=(
                    CaptureContractAdmission(
                        role=CAPTURE_CONTRACT_REF_ROLE,
                        target=COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity,
                    ),
                ),
                admission="direct",
                subject_binding="exact_claim_subject",
            ),
        )
    )


SEMANTIC_ALIAS_CLAIM_TYPE: Final = ClaimType(
    artifact_format="playbill-claim-type-v8",
    identity=ArtifactIdentity(kind="ClaimType", name=SEMANTIC_ALIAS_PREDICATE),
    predicate=SEMANTIC_ALIAS_PREDICATE,
    allowed_subject_kinds=(SEMANTIC_SUBJECT_KIND,),
    object_kind="literal",
    literal_schema={"minLength": 1, "type": "string"},
    cardinality="many",
    permitted_roles=("normative", "observation"),
    evidence_admission_policy=_self_evidence(),
    admission_policy=ClaimAdmissionPolicy(),
    resolution_policy=ClaimResolutionPolicy(
        cardinality="many", eligible_verdicts=("supported",), selector="all"
    ),
    description=(
        "Another name the Subject is known by. A Subject reference that names no "
        "Subject resolves through the aliases of its kind: one match resolves, "
        "several refuse naming each."
    ),
    default_role="normative",
    evidence_requirement="self",
    revision_evidence="replace",
    canonical_form=("nfc", "casefold", "collapse_whitespace", "trim"),
)

SEMANTIC_DISTINCT_FROM_CLAIM_TYPE: Final = ClaimType(
    artifact_format="playbill-claim-type-v7",
    identity=ArtifactIdentity(kind="ClaimType", name=SEMANTIC_DISTINCT_FROM_PREDICATE),
    predicate=SEMANTIC_DISTINCT_FROM_PREDICATE,
    allowed_subject_kinds=(SEMANTIC_SUBJECT_KIND,),
    object_kind="subject",
    allowed_object_subject_kinds=(SEMANTIC_SUBJECT_KIND,),
    cardinality="many",
    permitted_roles=("normative", "observation"),
    evidence_admission_policy=_self_evidence(),
    admission_policy=ClaimAdmissionPolicy(),
    resolution_policy=ClaimResolutionPolicy(
        cardinality="many", eligible_verdicts=("supported",), selector="all"
    ),
    description=(
        "The object Subject is a different thing from this one, whatever names or keys "
        "they share: the duplicates audit never groups the pair."
    ),
    default_role="normative",
    evidence_requirement="self",
    revision_evidence="replace",
)

#: Pseudo-kinds a descriptor ClaimType may admit; never the kind of a Subject.
DESCRIPTOR_KINDS: Final = frozenset({SEMANTIC_SUBJECT_KIND, "semantic.claim_type"})

#: The descriptor predicates: their Claims address any Subject as the pseudo-kind
#: ``semantic.subject`` (subject and Subject object alike), whatever its own kind.
DESCRIPTOR_PREDICATES: Final = frozenset(
    {"semantic.alias", "semantic.distinct_from", "semantic.related_to", "semantic.tag"}
)


def admits_object_subject(
    predicate: str, allowed_object_subject_kinds: tuple[str, ...], kind: str
) -> bool:
    """Whether a ClaimType admits a Subject of ``kind`` as its object, as the Claim law reads it.

    A descriptor predicate's object is judged as ``semantic.subject``; any other
    predicate's by the object Subject's own kind.
    """

    if predicate in DESCRIPTOR_PREDICATES:
        return SEMANTIC_SUBJECT_KIND in allowed_object_subject_kinds
    return kind in allowed_object_subject_kinds


def applies_to_kind(allowed_subject_kinds: tuple[str, ...], kind: str) -> bool:
    """Whether a ClaimType admitting these kinds states something about ``kind``.

    A descriptor ClaimType admitting ``semantic.subject`` applies to every kind.
    """

    return kind in allowed_subject_kinds or SEMANTIC_SUBJECT_KIND in allowed_subject_kinds


def concrete_subject_kinds(kinds: tuple[str, ...]) -> tuple[str, ...]:
    """The Subject kinds named, without the descriptor pseudo-kinds."""

    return tuple(kind for kind in kinds if kind not in DESCRIPTOR_KINDS)


BUILTIN_CLAIM_TYPES: Final[dict[str, ClaimType]] = {
    SEMANTIC_ALIAS_PREDICATE: SEMANTIC_ALIAS_CLAIM_TYPE,
    SEMANTIC_DISTINCT_FROM_PREDICATE: SEMANTIC_DISTINCT_FROM_CLAIM_TYPE,
}


def builtin_claim_type(name: str) -> ClaimType:
    """The exact builtin ClaimType of this name, or a KeyError naming every builtin."""

    try:
        return BUILTIN_CLAIM_TYPES[name]
    except KeyError:
        raise KeyError(
            f"no builtin ClaimType is named {name!r}; the builtins are "
            + ", ".join(sorted(BUILTIN_CLAIM_TYPES))
        ) from None


def builtin_claim_type_files() -> dict[str, bytes]:
    """Every builtin ClaimType at its ledger path, in its exact wire bytes."""

    return {
        claim_type_path(claim_type.predicate): render_claim_type(claim_type)
        for claim_type in BUILTIN_CLAIM_TYPES.values()
    }


__all__ = [
    "BUILTIN_CLAIM_TYPES",
    "DESCRIPTOR_KINDS",
    "DESCRIPTOR_PREDICATES",
    "admits_object_subject",
    "applies_to_kind",
    "concrete_subject_kinds",
    "BuiltinClaimTypeName",
    "SEMANTIC_ALIAS_CLAIM_TYPE",
    "SEMANTIC_ALIAS_PREDICATE",
    "SEMANTIC_DISTINCT_FROM_CLAIM_TYPE",
    "SEMANTIC_DISTINCT_FROM_PREDICATE",
    "SEMANTIC_SUBJECT_KIND",
    "builtin_claim_type",
    "builtin_claim_type_files",
]
