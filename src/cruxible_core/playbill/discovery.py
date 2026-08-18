"""Frozen semantic discovery, reuse evidence, and bounded expansion contracts."""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_core.playbill.artifacts import ArtifactIdentity
from cruxible_core.playbill.canonical import (
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_core.playbill.diagnostics import GovernedOperationReference
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.semantic import SemanticAddress
from cruxible_core.playbill.source_references import (
    AttestationCoverage,
    CoverageDescriptorV1,
    SemanticReadCoordinateV1,
    SourceDereferenceResultV1,
    SourceHandleV1,
)

ReuseDisposition = Literal["reuse_existing", "extend_existing_vocabulary", "new_distinct"]
ReuseMatchBasis = Literal[
    "exact_identity",
    "canonical_token",
    "structural_signature",
    "accepted_alias",
    "accepted_tag",
    "accepted_relation",
    "source_label",
    "proposer_hint",
]
DiscoveryMatchBasis = Literal[
    "exact_address",
    "exact_alias",
    "structural_signature",
    "tag",
    "lexical",
    "named_entrypoint",
    "dependency_walk",
]
"""The closed v1 discovery match bases; ranking may reorder, never extend."""

DescriptorAuthorityFloor = Literal[
    "target_namespace_authority",
    "recall_only",
    "namespace_creation_plus_cross_namespace",
]

_TERM_LIMIT = 80
_FORBIDDEN_HINT_RE = re.compile(
    r"(?:https?://|file://|api[_ -]?key|bearer\s|password|private[_ -]?key|"
    r"benchmark[_ -]?task|customer[_ -]?(?:task|scratch)|ignore\s+(?:all\s+)?previous|"
    r"system\s+prompt|developer\s+message)",
    re.IGNORECASE,
)


class _StrictDiscoveryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _term(value: str, *, label: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if normalized != value or not value.strip() or len(value) > _TERM_LIMIT:
        raise ValueError(f"{label} must be nonblank NFC text of at most {_TERM_LIMIT} scalars")
    if _FORBIDDEN_HINT_RE.search(value) or value.startswith(("/", "~", "../")):
        raise ValueError(f"{label} contains forbidden locator, secret, task, or instruction text")
    return value


def _ordered_terms(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    normalized = tuple(_term(value, label=label) for value in values)
    if normalized != tuple(sorted(set(normalized), key=lambda item: item.encode("utf-8"))):
        raise ValueError(f"{label} must be sorted and unique")
    return normalized


def _normalized_match_term(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def normalize_discovery_term(value: str) -> str:
    """Normalize one match term deterministically; never a similarity score.

    This is the single normalization vocabulary shared by write-time reuse
    checking and read-time exact/lexical discovery.
    """

    return _normalized_match_term(value)


def reject_locator_or_secret(value: str, *, label: str) -> str:
    """Refuse rendered or indexed text that carries a locator, secret, or lure.

    Discovery output is read by agents, so the exclusion vocabulary that keeps
    proposal hints clean also governs every index and capsule this layer emits.
    """

    if _FORBIDDEN_HINT_RE.search(value):
        raise ValueError(f"{label} contains forbidden locator, secret, task, or instruction text")
    return value


class DiscoveryHintsV1(_StrictDiscoveryModel):
    """Bounded untrusted proposal/query terms; never accepted metadata."""

    tag: Literal["playbill-discovery-hints-v1"] = "playbill-discovery-hints-v1"
    alternate_phrases: tuple[str, ...] = ()
    topical_tags: tuple[str, ...] = ()

    @field_validator("alternate_phrases", "topical_tags")
    @classmethod
    def _terms(cls, value: tuple[str, ...], info: object) -> tuple[str, ...]:
        if len(value) > 5:
            raise ValueError("DiscoveryHintsV1 permits at most five values per field")
        return _ordered_terms(value, label=str(getattr(info, "field_name", "discovery hints")))


class SemanticReuseInterfaceV1(_StrictDiscoveryModel):
    address: SemanticAddress
    identity: ArtifactIdentity
    kind: str
    label: str
    canonical_tokens: tuple[str, ...]
    structural_signature_digest: str
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    relation_labels: tuple[str, ...] = ()
    source_labels: tuple[str, ...] = ()

    @field_validator("structural_signature_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator(
        "canonical_tokens",
        "aliases",
        "tags",
        "relation_labels",
        "source_labels",
    )
    @classmethod
    def _terms(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(unicodedata.normalize("NFC", item) for item in value)
        if normalized != value or value != tuple(
            sorted(set(value), key=lambda item: item.encode("utf-8"))
        ):
            raise ValueError("semantic reuse terms must be NFC, sorted, and unique")
        return value


class ProposedSemanticInterfaceV1(_StrictDiscoveryModel):
    address: SemanticAddress
    identity: ArtifactIdentity
    kind: str
    label: str
    canonical_tokens: tuple[str, ...]
    structural_signature_digest: str

    @field_validator("structural_signature_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("canonical_tokens")
    @classmethod
    def _tokens(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(unicodedata.normalize("NFC", item) for item in value)
        if normalized != value or value != tuple(
            sorted(set(value), key=lambda item: item.encode("utf-8"))
        ):
            raise ValueError("proposed canonical tokens must be NFC, sorted, and unique")
        if not value:
            raise ValueError("proposed semantic interface requires canonical tokens")
        return value


class ReuseMatchV1(_StrictDiscoveryModel):
    basis: ReuseMatchBasis
    term: str
    blocking: bool


class ReuseCandidateV1(_StrictDiscoveryModel):
    address: SemanticAddress
    identity: ArtifactIdentity
    kind: str
    label: str
    match_basis: tuple[ReuseMatchV1, ...]

    @field_validator("match_basis")
    @classmethod
    def _basis(cls, value: tuple[ReuseMatchV1, ...]) -> tuple[ReuseMatchV1, ...]:
        ordered = tuple(
            sorted(value, key=lambda item: (item.basis.encode("utf-8"), item.term.encode("utf-8")))
        )
        identities = {(item.basis, item.term) for item in value}
        if value != ordered or len(identities) != len(value):
            raise ValueError("reuse match bases must be sorted and unique")
        return value

    @property
    def blocking(self) -> bool:
        return any(item.blocking for item in self.match_basis)


class ReuseDispositionV1(_StrictDiscoveryModel):
    kind: ReuseDisposition
    target: SemanticAddress | None = None

    @model_validator(mode="after")
    def _target_shape(self) -> "ReuseDispositionV1":
        if self.kind in {"reuse_existing", "extend_existing_vocabulary"}:
            if self.target is None:
                raise ValueError("reuse/extend disposition requires an exact target")
        elif self.target is not None:
            raise ValueError("new_distinct disposition cannot carry a reuse target")
        return self


class DistinctRelationMemberV1(_StrictDiscoveryModel):
    """One exact governed distinction persisted in the same candidate closure."""

    claim_address: SemanticAddress
    claim_artifact_digest: str
    subject: SemanticAddress
    object: SemanticAddress

    @field_validator("claim_artifact_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _address_kinds(self) -> "DistinctRelationMemberV1":
        if self.claim_address.selector.scheme != "claim-statement-v1":
            raise ValueError("distinct relation member must identify one exact Claim statement")
        if self.subject.selector.scheme != "artifact-v1" or (
            self.object.selector.scheme != "artifact-v1"
        ):
            raise ValueError("distinct relation endpoints must use stable artifact identities")
        return self


class VocabularyReuseRequestV1(_StrictDiscoveryModel):
    """Caller input intentionally has no result digest or selectable search profile."""

    tag: Literal["playbill-vocabulary-reuse-request-v1"] = "playbill-vocabulary-reuse-request-v1"
    proposal: ProposedSemanticInterfaceV1
    hints: DiscoveryHintsV1 = DiscoveryHintsV1()
    disposition: ReuseDispositionV1


class VocabularyReuseLawEvidenceV1(_StrictDiscoveryModel):
    tag: Literal["playbill-vocabulary-reuse-law-evidence-v1"] = (
        "playbill-vocabulary-reuse-law-evidence-v1"
    )
    coordinate: AcceptedCoordinate
    implementation_digest: str
    hints_digest: str
    result_digest: str
    candidates: tuple[ReuseCandidateV1, ...]
    disposition: ReuseDispositionV1
    distinct_relation_members: tuple[DistinctRelationMemberV1, ...] = ()
    verdict: Literal["satisfied", "refused"]
    refusal_code: str | None = None

    @field_validator("implementation_digest", "hints_digest", "result_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("distinct_relation_members")
    @classmethod
    def _relation_members(
        cls,
        value: tuple[DistinctRelationMemberV1, ...],
    ) -> tuple[DistinctRelationMemberV1, ...]:
        encoded = tuple(canonical_bytes(item.model_dump(mode="json")) for item in value)
        if encoded != tuple(sorted(set(encoded))):
            raise ValueError("distinct relation members must be canonically sorted and unique")
        return value

    @model_validator(mode="after")
    def _verdict_shape(self) -> "VocabularyReuseLawEvidenceV1":
        if (self.verdict == "refused") != (self.refusal_code is not None):
            raise ValueError("reuse evidence refusal code must agree with its verdict")
        return self


def _match_candidate(
    proposal: ProposedSemanticInterfaceV1,
    candidate: SemanticReuseInterfaceV1,
    hints: DiscoveryHintsV1,
) -> ReuseCandidateV1 | None:
    proposal_terms = {_normalized_match_term(item) for item in proposal.canonical_tokens}
    hint_phrases = {_normalized_match_term(item) for item in hints.alternate_phrases}
    hint_tags = {_normalized_match_term(item) for item in hints.topical_tags}
    all_terms = proposal_terms | hint_phrases | hint_tags
    matches: dict[tuple[str, str], ReuseMatchV1] = {}

    def add(basis: ReuseMatchBasis, term: str, *, blocking: bool) -> None:
        matches[(basis, term)] = ReuseMatchV1(basis=basis, term=term, blocking=blocking)

    if proposal.identity == candidate.identity:
        add("exact_identity", candidate.identity.qualified, blocking=True)
    for term in sorted(
        proposal_terms.intersection(
            _normalized_match_term(item) for item in candidate.canonical_tokens
        )
    ):
        add("canonical_token", term, blocking=True)
    if proposal.kind == candidate.kind and (
        proposal.structural_signature_digest == candidate.structural_signature_digest
    ):
        add("structural_signature", proposal.structural_signature_digest, blocking=True)
    for alias in candidate.aliases:
        normalized = _normalized_match_term(alias)
        if normalized in all_terms:
            add("accepted_alias", alias, blocking=True)
    for tag in candidate.tags:
        normalized = _normalized_match_term(tag)
        if normalized in all_terms:
            add("accepted_tag", tag, blocking=False)
    for relation in candidate.relation_labels:
        normalized = _normalized_match_term(relation)
        if normalized in all_terms:
            add("accepted_relation", relation, blocking=False)
    for label in candidate.source_labels:
        normalized = _normalized_match_term(label)
        if normalized in all_terms:
            add("source_label", label, blocking=False)
    candidate_terms = {
        _normalized_match_term(item)
        for item in (*candidate.canonical_tokens, *candidate.aliases, *candidate.tags)
    }
    for hint in sorted((hint_phrases | hint_tags).intersection(candidate_terms)):
        add("proposer_hint", hint, blocking=False)
    if not matches:
        return None
    return ReuseCandidateV1(
        address=candidate.address,
        identity=candidate.identity,
        kind=candidate.kind,
        label=candidate.label,
        match_basis=tuple(
            sorted(
                matches.values(),
                key=lambda item: (item.basis.encode("utf-8"), item.term.encode("utf-8")),
            )
        ),
    )


def evaluate_vocabulary_reuse(
    request: VocabularyReuseRequestV1,
    *,
    accepted_interfaces: tuple[SemanticReuseInterfaceV1, ...],
    coordinate: AcceptedCoordinate,
    implementation_digest: str,
    distinct_relation_members: tuple[DistinctRelationMemberV1, ...] = (),
    descriptor_claims_available: bool = False,
) -> VocabularyReuseLawEvidenceV1:
    """Run the mandatory parent-coordinate reuse lookup; hints can only add terms."""

    Sha256Value.from_tagged(implementation_digest)
    candidates = tuple(
        sorted(
            (
                match
                for candidate in accepted_interfaces
                if (match := _match_candidate(request.proposal, candidate, request.hints))
                is not None
            ),
            key=lambda item: canonical_bytes(item.address.model_dump(mode="json")),
        )
    )
    hint_payload = request.hints.model_dump(mode="json")
    hint_payload.pop("tag")
    hints_digest = typed_digest(
        Sha256Value,
        "playbill-discovery-hints-v1",
        hint_payload,
    ).tagged
    result_digest = typed_digest(
        Sha256Value,
        "playbill-vocabulary-reuse-result-v1",
        {
            "coordinate": coordinate.model_dump(mode="json"),
            "implementation_digest": implementation_digest,
            "proposal": request.proposal.model_dump(mode="json"),
            "candidates": [item.model_dump(mode="json") for item in candidates],
            "distinct_relation_members": [
                item.model_dump(mode="json") for item in distinct_relation_members
            ],
        },
    ).tagged
    exact = [
        candidate
        for candidate in candidates
        if any(item.basis == "exact_identity" for item in candidate.match_basis)
    ]
    blocking = tuple(candidate for candidate in candidates if candidate.blocking)
    target_bytes = (
        None
        if request.disposition.target is None
        else canonical_bytes(request.disposition.target.model_dump(mode="json"))
    )
    candidate_addresses = {
        canonical_bytes(item.address.model_dump(mode="json")) for item in candidates
    }
    ordered_relations = tuple(
        sorted(
            distinct_relation_members,
            key=lambda item: canonical_bytes(item.model_dump(mode="json")),
        )
    )
    if ordered_relations != distinct_relation_members or len(
        {canonical_bytes(item.model_dump(mode="json")) for item in distinct_relation_members}
    ) != len(distinct_relation_members):
        raise ValueError("distinct relation members must be canonically sorted and unique")
    refusal: str | None = None
    if exact:
        refusal = "playbill.reuse.exact_collision"
    elif target_bytes is not None and target_bytes not in candidate_addresses:
        refusal = "playbill.reuse.target_not_in_result"
    elif request.disposition.kind == "reuse_existing":
        refusal = "playbill.reuse.existing_target_required"
    elif request.disposition.kind == "extend_existing_vocabulary":
        if not descriptor_claims_available:
            refusal = "playbill.reuse.descriptor_claim_unavailable"
    elif blocking:
        proposal_address = canonical_bytes(request.proposal.address.model_dump(mode="json"))
        required = {canonical_bytes(item.address.model_dump(mode="json")) for item in blocking}
        persisted = {
            canonical_bytes(item.object.model_dump(mode="json"))
            for item in distinct_relation_members
            if canonical_bytes(item.subject.model_dump(mode="json")) == proposal_address
        }
        if not required.issubset(persisted):
            refusal = "playbill.reuse.distinction_claim_missing"
    return VocabularyReuseLawEvidenceV1(
        coordinate=coordinate,
        implementation_digest=implementation_digest,
        hints_digest=hints_digest,
        result_digest=result_digest,
        candidates=candidates,
        disposition=request.disposition,
        distinct_relation_members=distinct_relation_members,
        verdict="refused" if refusal is not None else "satisfied",
        refusal_code=refusal,
    )


class DescriptorClaimTypeSeedV1(_StrictDiscoveryModel):
    identity: ArtifactIdentity
    predicate: Literal[
        "semantic.alias",
        "semantic.tag",
        "semantic.related_to",
        "semantic.distinct_from",
    ]
    authority_floor: DescriptorAuthorityFloor
    resolves_identity: bool
    recall_only: bool
    indexed_bidirectionally: bool

    @model_validator(mode="after")
    def _identity_and_floor(self) -> "DescriptorClaimTypeSeedV1":
        if self.identity != ArtifactIdentity(kind="ClaimType", name=self.predicate):
            raise ValueError("descriptor seed identity must equal its predicate")
        expected = {
            "semantic.alias": ("target_namespace_authority", True, False, False),
            "semantic.tag": ("recall_only", False, True, False),
            "semantic.related_to": ("recall_only", False, True, False),
            "semantic.distinct_from": (
                "namespace_creation_plus_cross_namespace",
                False,
                True,
                True,
            ),
        }[self.predicate]
        if (
            self.authority_floor,
            self.resolves_identity,
            self.recall_only,
            self.indexed_bidirectionally,
        ) != expected:
            raise ValueError("descriptor seed semantics differ from the frozen v1 floor")
        return self


DESCRIPTOR_CLAIM_TYPE_SEEDS: tuple[DescriptorClaimTypeSeedV1, ...] = (
    DescriptorClaimTypeSeedV1(
        identity=ArtifactIdentity(kind="ClaimType", name="semantic.alias"),
        predicate="semantic.alias",
        authority_floor="target_namespace_authority",
        resolves_identity=True,
        recall_only=False,
        indexed_bidirectionally=False,
    ),
    DescriptorClaimTypeSeedV1(
        identity=ArtifactIdentity(kind="ClaimType", name="semantic.distinct_from"),
        predicate="semantic.distinct_from",
        authority_floor="namespace_creation_plus_cross_namespace",
        resolves_identity=False,
        recall_only=True,
        indexed_bidirectionally=True,
    ),
    DescriptorClaimTypeSeedV1(
        identity=ArtifactIdentity(kind="ClaimType", name="semantic.related_to"),
        predicate="semantic.related_to",
        authority_floor="recall_only",
        resolves_identity=False,
        recall_only=True,
        indexed_bidirectionally=False,
    ),
    DescriptorClaimTypeSeedV1(
        identity=ArtifactIdentity(kind="ClaimType", name="semantic.tag"),
        predicate="semantic.tag",
        authority_floor="recall_only",
        resolves_identity=False,
        recall_only=True,
        indexed_bidirectionally=False,
    ),
)


class DescriptorAuthorityContextV1(_StrictDiscoveryModel):
    actor_roles: tuple[str, ...]
    target_namespace_roles: tuple[str, ...] = ()
    recall_descriptor_roles: tuple[str, ...] = ()
    new_item_namespace_roles: tuple[str, ...] = ()
    blocking_cross_namespace_roles: tuple[str, ...] = ()

    @field_validator(
        "actor_roles",
        "target_namespace_roles",
        "recall_descriptor_roles",
        "new_item_namespace_roles",
        "blocking_cross_namespace_roles",
    )
    @classmethod
    def _roles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("descriptor authority roles must be sorted and unique")
        return value


class DescriptorAuthorityResultV1(_StrictDiscoveryModel):
    tag: Literal["playbill-descriptor-authority-result-v1"] = (
        "playbill-descriptor-authority-result-v1"
    )
    verdict: Literal["authorized", "refused"]
    refusal_code: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> "DescriptorAuthorityResultV1":
        if (self.verdict == "refused") != (self.refusal_code is not None):
            raise ValueError("descriptor authority refusal must carry one stable code")
        return self


def evaluate_descriptor_authority(
    predicate: Literal[
        "semantic.alias",
        "semantic.tag",
        "semantic.related_to",
        "semantic.distinct_from",
    ],
    context: DescriptorAuthorityContextV1,
) -> DescriptorAuthorityResultV1:
    """Apply the descriptor-specific floor; the proposed Claim cannot author it."""

    actor = set(context.actor_roles)
    if predicate == "semantic.alias":
        authorized = bool(actor.intersection(context.target_namespace_roles))
        code = "playbill.descriptor.alias_target_authority_required"
    elif predicate in {"semantic.tag", "semantic.related_to"}:
        authorized = bool(actor.intersection(context.recall_descriptor_roles))
        code = "playbill.descriptor.recall_authority_required"
    else:
        authorized = bool(actor.intersection(context.new_item_namespace_roles)) and (
            not context.blocking_cross_namespace_roles
            or bool(actor.intersection(context.blocking_cross_namespace_roles))
        )
        code = "playbill.descriptor.distinct_namespace_authority_required"
    return DescriptorAuthorityResultV1(
        verdict="authorized" if authorized else "refused",
        refusal_code=None if authorized else code,
    )


class DiscoveryBudgetV1(_StrictDiscoveryModel):
    tag: Literal["playbill-discovery-budget-v1"] = "playbill-discovery-budget-v1"
    max_hits: int = Field(default=20, ge=1)
    max_bytes: int = Field(default=16_384, ge=1)


class ExpansionBudgetV1(_StrictDiscoveryModel):
    tag: Literal["playbill-expansion-budget-v1"] = "playbill-expansion-budget-v1"
    max_bytes: int = Field(default=65_536, ge=1)
    max_relations: int = Field(default=100, ge=0)
    max_source_handles: int = Field(default=20, ge=0)


class DiscoveryMatchBasisV1(_StrictDiscoveryModel):
    basis: DiscoveryMatchBasis
    matched_text: str | None = None


class DiscoveryHitV1(_StrictDiscoveryModel):
    tag: Literal["playbill-discovery-hit-v1"] = "playbill-discovery-hit-v1"
    address: SemanticAddress
    at: SemanticReadCoordinateV1
    kind: str
    label: str
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    match_basis: tuple[DiscoveryMatchBasisV1, ...]
    role: str | None = None
    verdict: str | None = None
    currency: Literal["current", "stale", "not_applicable"]
    source_handles: tuple[SourceHandleV1, ...] = ()
    dependency_addresses: tuple[SemanticAddress, ...] = ()
    dependent_addresses: tuple[SemanticAddress, ...] = ()


class DiscoveryRequestV1(_StrictDiscoveryModel):
    tag: Literal["playbill-discovery-request-v1"] = "playbill-discovery-request-v1"
    query: str | None = None
    entrypoint: str | None = None
    at: SemanticReadCoordinateV1
    evaluation_time: str
    profile: Literal["interfaces", "subjects", "all"] = "interfaces"
    budget: DiscoveryBudgetV1 = DiscoveryBudgetV1()

    @model_validator(mode="after")
    def _selection(self) -> "DiscoveryRequestV1":
        if (self.query is None) == (self.entrypoint is None):
            raise ValueError("discover requires exactly one query or entrypoint")
        return self


class DiscoveryPageV1(_StrictDiscoveryModel):
    tag: Literal["playbill-discovery-page-v1"] = "playbill-discovery-page-v1"
    coordinate_kind: Literal["accepted", "candidate", "local_only"]
    at: SemanticReadCoordinateV1 | None
    evaluation_time: str
    hits: tuple[DiscoveryHitV1, ...]
    selection_basis_digest: str
    receipt_digest: str
    coverage: CoverageDescriptorV1


class ExpandRequestV1(_StrictDiscoveryModel):
    tag: Literal["playbill-expand-request-v1"] = "playbill-expand-request-v1"
    address: SemanticAddress
    at: SemanticReadCoordinateV1
    evaluation_time: str
    facets: tuple[str, ...]
    budget: ExpansionBudgetV1 = ExpansionBudgetV1()


class ContextCapsuleV1(_StrictDiscoveryModel):
    tag: Literal["playbill-context-capsule-v1"] = "playbill-context-capsule-v1"
    address: SemanticAddress
    at: SemanticReadCoordinateV1
    evaluation_time: str
    canonical_summary: object
    governance: object
    provenance: object
    attestation_coverage: AttestationCoverage
    claim_context: object | None = None
    procedure_context: object | None = None
    claim_type_card: object | None = None
    subject_profile: object | None = None
    source_material: tuple[SourceDereferenceResultV1, ...] = ()
    relations: tuple[object, ...] = ()
    next_reads: tuple[GovernedOperationReference, ...] = ()
    coverage: CoverageDescriptorV1
    receipt_digest: str

    @field_validator("canonical_summary", "governance", "provenance")
    @classmethod
    def _canonical_objects(cls, value: object) -> object:
        return normalize_canonical(value)

    @field_validator("claim_context", "procedure_context", "claim_type_card", "subject_profile")
    @classmethod
    def _optional_canonical(cls, value: object | None) -> object | None:
        return None if value is None else normalize_canonical(value)

    @field_validator("relations")
    @classmethod
    def _relations(cls, value: tuple[object, ...]) -> tuple[object, ...]:
        return tuple(normalize_canonical(item) for item in value)

    @field_validator("receipt_digest")
    @classmethod
    def _receipt_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class ContextMaterialV1(_StrictDiscoveryModel):
    """Explicit instruction/data boundary for any later client injection."""

    tag: Literal["playbill-context-material-v1"] = "playbill-context-material-v1"
    classification: Literal["untrusted_data", "eligible_instruction"]
    subject: SemanticAddress
    at: SemanticReadCoordinateV1
    content_digest: str
    accepted_context_policy_digest: str | None = None

    @field_validator("content_digest", "accepted_context_policy_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _instruction_authority(self) -> "ContextMaterialV1":
        if (self.classification == "eligible_instruction") != (
            self.accepted_context_policy_digest is not None
        ):
            raise ValueError("instruction eligibility requires an exact accepted context policy")
        return self


__all__ = [
    "ContextCapsuleV1",
    "ContextMaterialV1",
    "DESCRIPTOR_CLAIM_TYPE_SEEDS",
    "DescriptorAuthorityContextV1",
    "DescriptorAuthorityResultV1",
    "DescriptorClaimTypeSeedV1",
    "DiscoveryBudgetV1",
    "DiscoveryHintsV1",
    "DiscoveryHitV1",
    "DiscoveryMatchBasis",
    "DiscoveryMatchBasisV1",
    "DiscoveryPageV1",
    "DiscoveryRequestV1",
    "DistinctRelationMemberV1",
    "ExpandRequestV1",
    "ExpansionBudgetV1",
    "ProposedSemanticInterfaceV1",
    "ReuseCandidateV1",
    "ReuseDispositionV1",
    "SemanticReuseInterfaceV1",
    "VocabularyReuseLawEvidenceV1",
    "VocabularyReuseRequestV1",
    "evaluate_descriptor_authority",
    "evaluate_vocabulary_reuse",
    "normalize_discovery_term",
    "reject_locator_or_secret",
]
