"""ClaimType cards and Subject profiles: the compact claim-side interface.

The Procedure-side compression boundary is a Contract. The Claim-side one is a
ClaimType, and an agent has to be able to decide whether to reuse that interface
*before* expanding every Claim and evidence edge that uses it. These two records
are that decision surface: a ``ClaimTypeCardV1`` states the proposition
interface, and a ``SubjectProfileV1`` states what is currently said about one
referent, predicate by predicate.

Three properties keep them honest:

*coordinate purity* -- a card and a profile are pure functions of the accepted
facts at one accepted coordinate, so rebuilding them reproduces the same bytes.
A profile carries a verdict or a currency only when it was built at an explicit
evaluation time, and the model refuses the combination that would let a
time-relative fact masquerade as accepted state.

*match bases, not scores* -- every card and profile carries the deterministic
per-basis vocabulary it can be found through, in the frozen
:data:`~cruxible_core.playbill.query.semantic_discovery.MATCH_BASIS_PRIORITY`
order, each row stating whether that basis may resolve equivalence. A tag row
never can: a tag is recall-only, and equivalence needs an alias admitted under
the target namespace's own authority.

*stated budgets* -- these are header files, not bodies. Policies appear as
digests plus one-line facts, objects appear as digests plus a bounded preview,
and full backing, history, governance, and source material stay behind
``expand``/``open_source``. Every clip is named in coverage, so a silently
narrowed card is unrepresentable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_core.playbill.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_core.playbill.claim_type_structure import claim_type_structural_signature
from cruxible_core.playbill.claim_types import ClaimType, claim_type_digest, claim_type_path
from cruxible_core.playbill.claim_verdicts import evaluate_claim_verdict
from cruxible_core.playbill.claims import ClaimArtifact, SubjectClaimObject
from cruxible_core.playbill.diagnostics import GovernedOperationReference
from cruxible_core.playbill.discovery import (
    DiscoveryMatchBasis,
    DiscoveryPageV1,
    DiscoveryRequestV1,
    reject_locator_or_secret,
)
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.providers import ProviderV1
from cruxible_core.playbill.query.backends import ClaimFactRowV1, ClaimQueryFactsV1
from cruxible_core.playbill.query.grammar import byte_sorted
from cruxible_core.playbill.query.semantic_discovery import (
    MATCH_BASIS_PRIORITY,
    MATCH_BASIS_RESOLVES_EQUIVALENCE,
    DiscoveryEntryV1,
    DiscoveryError,
    DiscoveryVocabularyV1,
    discover,
    resolved_equivalence_address,
)
from cruxible_core.playbill.semantic import SemanticAddress
from cruxible_core.playbill.source_references import CoverageDescriptorV1

CLAIM_TYPE_CARD_DIGEST_DOMAIN = "playbill-claim-type-card-v1"
SUBJECT_PROFILE_DIGEST_DOMAIN = "playbill-subject-profile-v1"
INTERFACE_POLICY_DIGEST_DOMAIN = "playbill-interface-policy-summary-v1"
INTERFACE_PAGE_RECEIPT_DIGEST_DOMAIN = "playbill-interface-discovery-page-v1"

CLAIM_TYPE_CARD_FACETS: tuple[str, ...] = ("interface", "match_bases", "policies", "usage")
SUBJECT_PROFILE_FACETS: tuple[str, ...] = ("match_bases", "predicates", "vocabulary")

_RELATION_PREDICATES = frozenset({"semantic.distinct_from", "semantic.related_to"})


class _StrictCardModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InterfaceProjectionBudgetV1(_StrictCardModel):
    """The declared ceiling that keeps a card cheaper than graph archaeology."""

    tag: Literal["playbill-interface-projection-budget-v1"] = (
        "playbill-interface-projection-budget-v1"
    )
    max_terms_per_basis: int = Field(default=20, ge=1)
    max_predicates: int = Field(default=40, ge=1)
    max_contended_subjects: int = Field(default=10, ge=0)
    max_object_preview_bytes: int = Field(default=120, ge=0)
    max_bytes: int = Field(default=8_192, ge=1)


class InterfaceMatchBasisV1(_StrictCardModel):
    """One basis this interface can be found through, and what it may conclude."""

    tag: Literal["playbill-interface-match-basis-v1"] = "playbill-interface-match-basis-v1"
    basis: DiscoveryMatchBasis
    terms: tuple[str, ...]
    resolves_equivalence: bool

    @field_validator("terms")
    @classmethod
    def _terms(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or value != byte_sorted(value):
            raise ValueError("interface match-basis terms must be nonempty, sorted, and unique")
        return value

    @model_validator(mode="after")
    def _equivalence_law(self) -> "InterfaceMatchBasisV1":
        if self.resolves_equivalence != MATCH_BASIS_RESOLVES_EQUIVALENCE[self.basis]:
            raise ValueError(
                "interface match-basis equivalence grade differs from the frozen v1 law"
            )
        return self


class SemanticRelationV1(_StrictCardModel):
    """One accepted typed relation edge, named by its registered descriptor predicate.

    Relations use registered ClaimTypes rather than free-form edge labels, so the
    predicate here is always one of the reserved v1 descriptor predicates and
    ``inbound`` states which end of the stored Claim this interface sits on.
    """

    tag: Literal["playbill-interface-relation-v1"] = "playbill-interface-relation-v1"
    predicate: Literal["semantic.distinct_from", "semantic.related_to"]
    target: SemanticAddress
    inbound: bool = False


class InterfacePolicySummaryV1(_StrictCardModel):
    """One governing policy as a digest plus one line of exact counted facts."""

    tag: Literal["playbill-interface-policy-summary-v1"] = "playbill-interface-policy-summary-v1"
    policy: Literal["admission", "evidence_admission", "resolution"]
    policy_digest: str
    summary: str

    @field_validator("policy_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class ClaimTypeUsageRowV1(_StrictCardModel):
    """One accepted Claim of a predicate, reduced to what a usage count needs."""

    tag: Literal["playbill-claim-type-usage-row-v1"] = "playbill-claim-type-usage-row-v1"
    subject_path: str
    subject_identity: str


class ClaimTypeUsageV1(_StrictCardModel):
    """Coordinate-pure usage: counts of accepted Claims, never a verdict."""

    tag: Literal["playbill-claim-type-usage-v1"] = "playbill-claim-type-usage-v1"
    claim_count: int = Field(ge=0)
    subject_count: int = Field(ge=0)
    contended_subject_count: int = Field(ge=0)
    contended_subject_identities: tuple[str, ...] = ()

    @field_validator("contended_subject_identities")
    @classmethod
    def _identities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("contended Subject identities must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "ClaimTypeUsageV1":
        if len(self.contended_subject_identities) > self.contended_subject_count:
            raise ValueError("contended Subject identities cannot exceed their own count")
        return self


class ClaimTypeCardV1(_StrictCardModel):
    """The compact reusable proposition interface of one accepted ClaimType."""

    tag: Literal["playbill-claim-type-card-v1"] = "playbill-claim-type-card-v1"
    at: AcceptedCoordinate
    address: SemanticAddress
    identity: str
    artifact_digest: str
    predicate: str
    allowed_subject_kinds: tuple[str, ...]
    object_kind: Literal["literal", "subject", "exact_content"]
    allowed_object_subject_kinds: tuple[str, ...] = ()
    literal_schema_digest: str | None = None
    literal_schema_summary: str | None = None
    cardinality: Literal["one", "many"]
    permitted_roles: tuple[str, ...]
    referent_sensitivity: Literal["identity", "shell"]
    structural_signature_digest: str
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    relations: tuple[SemanticRelationV1, ...] = ()
    match_bases: tuple[InterfaceMatchBasisV1, ...] = ()
    policies: tuple[InterfacePolicySummaryV1, ...] = ()
    usage: ClaimTypeUsageV1
    expansion_links: tuple[GovernedOperationReference, ...] = ()
    coverage: CoverageDescriptorV1

    @field_validator("artifact_digest", "structural_signature_digest", "literal_schema_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @field_validator("match_bases")
    @classmethod
    def _bases(cls, value: tuple[InterfaceMatchBasisV1, ...]) -> tuple[InterfaceMatchBasisV1, ...]:
        return _validated_bases(value)


class SubjectProfilePredicateV1(_StrictCardModel):
    """What one predicate currently says about one Subject, compactly.

    ``verdict``/``currency`` are the only time-relative fields in this record and
    exist only when the owning profile was built at an explicit evaluation time.
    Everything else is coordinate-pure structure.
    """

    tag: Literal["playbill-subject-profile-predicate-v1"] = "playbill-subject-profile-predicate-v1"
    predicate: str
    claim_type_address: SemanticAddress
    pinned_claim_type_digests: tuple[str, ...]
    cardinality: Literal["one", "many"] | None = None
    claim_count: int = Field(ge=1)
    contender_count: int = Field(ge=1)
    resolution: Literal["single", "unresolved"]
    object_kind: str | None = None
    object_digest: str | None = None
    object_preview: str | None = None
    verdict: str | None = None
    currency: str | None = None

    @field_validator("pinned_claim_type_digests")
    @classmethod
    def _pinned(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or value != byte_sorted(value):
            raise ValueError("pinned ClaimType digests must be nonempty, sorted, and unique")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "SubjectProfilePredicateV1":
        if (self.resolution == "single") != (self.contender_count == 1):
            raise ValueError("a resolved predicate row has exactly one distinct effective object")
        if self.resolution == "unresolved" and (
            self.object_digest is not None or self.object_preview is not None
        ):
            raise ValueError("an unresolved predicate row cannot render one effective object")
        if self.object_preview is not None and self.object_digest is None:
            raise ValueError("an object preview requires the exact object digest beside it")
        return self


class SubjectProfileV1(_StrictCardModel):
    """The predicate-indexed profile of one accepted Subject at one coordinate."""

    tag: Literal["playbill-subject-profile-v1"] = "playbill-subject-profile-v1"
    at: AcceptedCoordinate
    address: SemanticAddress
    identity: str
    artifact_digest: str
    subject_kind: str
    subject_id: str
    evaluation_time: datetime | None = None
    verdict_relative: bool = False
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    relations: tuple[SemanticRelationV1, ...] = ()
    match_bases: tuple[InterfaceMatchBasisV1, ...] = ()
    predicates: tuple[SubjectProfilePredicateV1, ...] = ()
    expansion_links: tuple[GovernedOperationReference, ...] = ()
    coverage: CoverageDescriptorV1

    @field_validator("artifact_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("evaluation_time")
    @classmethod
    def _time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("a Subject profile evaluation time must be an absolute instant")
        return value

    @field_validator("match_bases")
    @classmethod
    def _bases(cls, value: tuple[InterfaceMatchBasisV1, ...]) -> tuple[InterfaceMatchBasisV1, ...]:
        return _validated_bases(value)

    @field_validator("predicates")
    @classmethod
    def _predicates(
        cls, value: tuple[SubjectProfilePredicateV1, ...]
    ) -> tuple[SubjectProfilePredicateV1, ...]:
        names = tuple(item.predicate for item in value)
        if names != byte_sorted(names):
            raise ValueError("Subject profile predicate rows must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _time_relative_law(self) -> "SubjectProfileV1":
        timed = any(
            item.verdict is not None or item.currency is not None for item in self.predicates
        )
        if timed and self.evaluation_time is None:
            raise ValueError("a verdict-bearing Subject profile requires an explicit read time")
        if self.verdict_relative != (self.evaluation_time is not None):
            raise ValueError("Subject profile verdict relativity must agree with its read time")
        return self


def _validated_bases(
    value: tuple[InterfaceMatchBasisV1, ...],
) -> tuple[InterfaceMatchBasisV1, ...]:
    keys = tuple(MATCH_BASIS_PRIORITY[item.basis] for item in value)
    if keys != tuple(sorted(set(keys))):
        raise ValueError("interface match bases must follow the frozen priority order, once each")
    return value


def claim_type_card_digest(card: ClaimTypeCardV1) -> str:
    """Digest one card so a rebuild can be compared without re-reading the facts."""

    payload = card.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(Sha256Value, CLAIM_TYPE_CARD_DIGEST_DOMAIN, payload).tagged


def subject_profile_digest(profile: SubjectProfileV1) -> str:
    """Digest one profile so a rebuild can be compared without re-reading the facts."""

    payload = profile.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(Sha256Value, SUBJECT_PROFILE_DIGEST_DOMAIN, payload).tagged


# -- policy and schema one-liners -----------------------------------------


def _policy_digest(policy: str, payload: object) -> str:
    return typed_digest(
        Sha256Value,
        INTERFACE_POLICY_DIGEST_DOMAIN,
        {"policy": policy, "payload": payload},
    ).tagged


def _policy_summaries(claim_type: ClaimType) -> tuple[InterfacePolicySummaryV1, ...]:
    """Render each governing policy as counted facts, never as a policy body."""

    admission = claim_type.admission_policy
    evidence = claim_type.evidence_admission_policy
    resolution = claim_type.resolution_policy
    signers = tuple(item.minimum_distinct_signers for item in admission.actor_requirements)
    return (
        InterfacePolicySummaryV1(
            policy="admission",
            policy_digest=_policy_digest("admission", admission.model_dump(mode="json")),
            summary=(
                f"transition={len(admission.transition_requirements)} "
                f"actor={len(admission.actor_requirements)} "
                f"evidence={len(admission.evidence_requirements)} "
                f"freeze={len(admission.freeze_requirements)} "
                f"min_distinct_signers={max(signers) if signers else 0}"
            ),
        ),
        InterfacePolicySummaryV1(
            policy="evidence_admission",
            policy_digest=_policy_digest("evidence_admission", evidence.model_dump(mode="json")),
            summary=(
                f"rules={len(evidence.rules)} "
                f"admission={','.join(byte_sorted(tuple(r.admission for r in evidence.rules)))} "
                "attestation="
                + ",".join(byte_sorted(tuple(r.attestation_requirement for r in evidence.rules)))
            ),
        ),
        InterfacePolicySummaryV1(
            policy="resolution",
            policy_digest=_policy_digest("resolution", resolution.model_dump(mode="json")),
            summary=(
                f"cardinality={resolution.cardinality} selector={resolution.selector} "
                f"eligible_verdicts={','.join(resolution.eligible_verdicts)} "
                f"require_current={'true' if resolution.require_current else 'false'} "
                f"conflict_result={resolution.conflict_result}"
            ),
        ),
    )


def _literal_schema_summary(claim_type: ClaimType) -> tuple[str | None, str | None]:
    """Return the literal schema digest and a one-line shape, never the schema body."""

    schema = claim_type.literal_schema
    if schema is None:
        return (None, None)
    declared = schema.get("type")
    parts = [f"type={declared}" if declared is not None else "type=unconstrained"]
    if "enum" in schema:
        enumerated = schema["enum"]
        parts.append(f"enum={len(enumerated) if isinstance(enumerated, list) else 1}")
    if "const" in schema:
        parts.append("const=1")
    if "format" in schema:
        parts.append(f"format={schema['format']}")
    return (
        typed_digest(Sha256Value, INTERFACE_POLICY_DIGEST_DOMAIN, schema).tagged,
        " ".join(parts),
    )


# -- match bases -----------------------------------------------------------


def _basis_row(
    basis: str,
    terms: Sequence[str],
    *,
    limit: int,
) -> tuple[InterfaceMatchBasisV1, bool]:
    ordered = byte_sorted(tuple(terms))
    clipped = len(ordered) > limit
    return (
        InterfaceMatchBasisV1(
            basis=basis,  # type: ignore[arg-type]
            terms=ordered[:limit],
            resolves_equivalence=MATCH_BASIS_RESOLVES_EQUIVALENCE[basis],
        ),
        clipped,
    )


def _match_bases(
    entry: DiscoveryEntryV1,
    *,
    budget: InterfaceProjectionBudgetV1,
    reasons: set[str],
) -> tuple[InterfaceMatchBasisV1, ...]:
    """Project the deterministic per-basis vocabulary in frozen priority order."""

    candidates: list[tuple[str, tuple[str, ...]]] = [
        ("exact_address", (entry.address.artifact_path, entry.identity)),
        ("exact_alias", entry.aliases),
        (
            "structural_signature",
            ()
            if entry.structural_signature_digest is None
            else (entry.structural_signature_digest,),
        ),
        ("tag", entry.tags),
        ("lexical", entry.lexical_terms),
    ]
    if entry.entrypoint_name is not None:
        candidates.insert(1, ("named_entrypoint", (entry.entrypoint_name,)))
    rows: list[InterfaceMatchBasisV1] = []
    for basis, terms in sorted(candidates, key=lambda item: MATCH_BASIS_PRIORITY[item[0]]):
        if not terms:
            continue
        row, clipped = _basis_row(basis, terms, limit=budget.max_terms_per_basis)
        if clipped:
            reasons.add("term_budget_exceeded")
        rows.append(row)
    return tuple(rows)


def descriptor_relations(
    claims: Iterable[ClaimArtifact],
) -> Mapping[bytes, tuple[SemanticRelationV1, ...]]:
    """Index the accepted typed relation edges by the address each end sits on.

    A reviewed distinction is stored once with the new item as its subject and
    indexed in both directions for discovery, so the reverse edge is rendered as
    inbound rather than silently reattributed to the other end.
    """

    found: dict[bytes, dict[bytes, SemanticRelationV1]] = {}

    def add(owner: SemanticAddress, relation: SemanticRelationV1) -> None:
        key = canonical_bytes(owner.model_dump(mode="json"))
        found.setdefault(key, {})[canonical_bytes(relation.model_dump(mode="json"))] = relation

    for claim in claims:
        statement = claim.statement
        if claim.lifecycle.state != "live":
            continue
        if statement.predicate not in _RELATION_PREDICATES or not isinstance(
            statement.object, SubjectClaimObject
        ):
            continue
        predicate = cast(
            Literal["semantic.distinct_from", "semantic.related_to"], statement.predicate
        )
        add(
            statement.subject,
            SemanticRelationV1(
                predicate=predicate,
                target=statement.object.address,
                inbound=False,
            ),
        )
        add(
            statement.object.address,
            SemanticRelationV1(predicate=predicate, target=statement.subject, inbound=True),
        )
    return {owner: tuple(edges[key] for key in sorted(edges)) for owner, edges in found.items()}


def _relations_for(
    relations: Mapping[bytes, tuple[SemanticRelationV1, ...]],
    address: SemanticAddress,
) -> tuple[SemanticRelationV1, ...]:
    return relations.get(canonical_bytes(address.model_dump(mode="json")), ())


def _expansion_links(address: SemanticAddress) -> tuple[GovernedOperationReference, ...]:
    return (GovernedOperationReference(operation="expand", subject=address),)


def _coverage(
    *,
    facets: tuple[str, ...],
    truncated: Iterable[str],
    reasons: Iterable[str],
) -> CoverageDescriptorV1:
    truncated_facets = byte_sorted(tuple(truncated))
    return CoverageDescriptorV1(
        requested_facets=byte_sorted(facets),
        available_facets=byte_sorted(
            tuple(item for item in facets if item not in set(truncated_facets))
        ),
        truncated_facets=truncated_facets,
        reason_codes=byte_sorted(tuple(reasons)),
    )


def _payload_bytes(payload: object) -> int:
    return len(canonical_bytes(payload))


# -- the ClaimType card ---------------------------------------------------


def _usage(
    rows: Sequence[ClaimTypeUsageRowV1],
    *,
    budget: InterfaceProjectionBudgetV1,
    reasons: set[str],
) -> ClaimTypeUsageV1:
    """Count accepted use structurally: contention is not a verdict."""

    per_subject: dict[str, int] = {}
    identities: dict[str, str] = {}
    for row in rows:
        per_subject[row.subject_path] = per_subject.get(row.subject_path, 0) + 1
        identities[row.subject_path] = row.subject_identity
    contended = byte_sorted(
        tuple(identities[path] for path, count in per_subject.items() if count > 1)
    )
    retained = contended[: budget.max_contended_subjects]
    if len(retained) != len(contended):
        reasons.add("contended_subject_budget_exceeded")
    return ClaimTypeUsageV1(
        claim_count=len(rows),
        subject_count=len(per_subject),
        contended_subject_count=len(contended),
        contended_subject_identities=retained,
    )


def build_claim_type_card(
    claim_type: ClaimType,
    *,
    at: AcceptedCoordinate,
    entry: DiscoveryEntryV1,
    usage_rows: Sequence[ClaimTypeUsageRowV1] = (),
    relations: tuple[SemanticRelationV1, ...] = (),
    budget: InterfaceProjectionBudgetV1 = InterfaceProjectionBudgetV1(),
) -> ClaimTypeCardV1:
    """Project one accepted ClaimType into its compact reusable interface.

    ``usage_rows`` name the accepted Claims of this predicate at the same
    coordinate; they contribute counts only. The card never carries a Claim
    body, a policy body, or a verdict.
    """

    if entry.kind != "ClaimType":
        raise DiscoveryError("a ClaimType card requires the ClaimType vocabulary entry")
    truncated: set[str] = set()
    reasons: set[str] = set()
    schema_digest, schema_summary = _literal_schema_summary(claim_type)
    bases = _match_bases(entry, budget=budget, reasons=reasons)
    if "term_budget_exceeded" in reasons:
        truncated.add("match_bases")
    usage = _usage(usage_rows, budget=budget, reasons=reasons)
    if "contended_subject_budget_exceeded" in reasons:
        truncated.add("usage")

    def payload(rows: tuple[InterfaceMatchBasisV1, ...]) -> dict[str, object]:
        return {
            "bases": [item.model_dump(mode="json") for item in rows],
            "identity": claim_type.identity.qualified,
            "policies": [item.model_dump(mode="json") for item in _policy_summaries(claim_type)],
            "structure": claim_type.structure.model_dump(mode="json"),
            "usage": usage.model_dump(mode="json"),
        }

    # The reduction ladder drops recall vocabulary before identity vocabulary:
    # the lowest-precedence basis goes first, so an over-tight budget can cost a
    # lexical or tag term but never the exact address that names the interface.
    while len(bases) > 1 and _payload_bytes(payload(bases)) > budget.max_bytes:
        bases = bases[:-1]
        truncated.add("match_bases")
        reasons.add("byte_budget_exceeded")
    if _payload_bytes(payload(bases)) > budget.max_bytes:
        raise DiscoveryError("interface card byte budget is smaller than its minimum interface")

    return ClaimTypeCardV1(
        at=at,
        address=entry.address,
        identity=claim_type.identity.qualified,
        artifact_digest=claim_type_digest(claim_type).tagged,
        predicate=claim_type.predicate,
        allowed_subject_kinds=claim_type.allowed_subject_kinds,
        object_kind=claim_type.object_kind,
        allowed_object_subject_kinds=claim_type.allowed_object_subject_kinds,
        literal_schema_digest=schema_digest,
        literal_schema_summary=schema_summary,
        cardinality=claim_type.cardinality,
        permitted_roles=claim_type.permitted_roles,
        referent_sensitivity=claim_type.referent_sensitivity,
        structural_signature_digest=claim_type_structural_signature(claim_type.structure),
        aliases=entry.aliases,
        tags=entry.tags,
        relations=relations,
        match_bases=bases,
        policies=_policy_summaries(claim_type),
        usage=usage,
        expansion_links=_expansion_links(entry.address),
        coverage=_coverage(
            facets=CLAIM_TYPE_CARD_FACETS,
            truncated=truncated,
            reasons=reasons,
        ),
    )


# -- the Subject profile --------------------------------------------------


def _object_preview(
    payload: object,
    *,
    budget: InterfaceProjectionBudgetV1,
    reasons: set[str],
) -> str | None:
    """Return the exact canonical object bytes, or nothing, never a lossy render."""

    encoded = canonical_bytes(payload)
    if len(encoded) > budget.max_object_preview_bytes:
        reasons.add("object_preview_omitted")
        return None
    rendered = encoded.decode("utf-8")
    try:
        reject_locator_or_secret(rendered, label="Subject profile object preview")
    except ValueError:
        # An accepted Claim may legitimately state a locator; a compact profile is
        # not the surface that hands one out, so the digest stands alone.
        reasons.add("object_preview_withheld")
        return None
    return rendered


def claim_predicate_verdicts(
    rows: Iterable[ClaimFactRowV1],
    *,
    evaluation_time: datetime,
    providers: Mapping[str, ProviderV1] | None = None,
) -> Mapping[str, tuple[str, str]]:
    """Adjudicate one Subject's Claims per predicate at one explicit instant.

    Adjudication stays outside the projection: a card or a profile renders what
    it is handed, and the single verdict path in
    :mod:`cruxible_core.playbill.claim_verdicts` remains the only place a
    verdict is computed.
    """

    grouped: dict[str, list[ClaimFactRowV1]] = {}
    for row in rows:
        if row.accepted.claim.lifecycle.state != "live":
            continue
        grouped.setdefault(row.accepted.claim.statement.predicate, []).append(row)
    return {
        predicate: _predicate_verdict(
            claims,
            evaluation_time=evaluation_time,
            providers=providers or {},
        )
        for predicate, claims in grouped.items()
    }


def _predicate_verdict(
    rows: Sequence[ClaimFactRowV1],
    *,
    evaluation_time: datetime,
    providers: Mapping[str, ProviderV1],
) -> tuple[str, str]:
    verdicts = {
        (result.verdict, result.currency)
        for result in (
            evaluate_claim_verdict(
                claim_statement_digest=row.accepted.statement_digest,
                rule=row.rule,
                evaluation_time=evaluation_time,
                captures=row.captures,
                attestations=row.attestations,
                providers=providers,
                claim_effective_from=row.accepted.claim.statement.effective_from,
                claim_effective_until=row.accepted.claim.statement.effective_until,
                referent_current=row.referent_current,
                resolved_authority_basis=row.resolved_authority_basis,
            )
            for row in rows
        )
    }
    if len(verdicts) == 1:
        return next(iter(verdicts))
    # Competing Claims that adjudicate differently are a conflict to show, never
    # a winner to pick inside a compact projection.
    return ("unresolved", "not_applicable")


def _predicate_row(
    predicate: str,
    rows: Sequence[ClaimArtifact],
    *,
    cardinalities: Mapping[str, str],
    verdict: str | None,
    currency: str | None,
    budget: InterfaceProjectionBudgetV1,
    reasons: set[str],
) -> SubjectProfilePredicateV1:
    distinct: dict[bytes, object] = {}
    for claim in rows:
        payload = claim.statement.object.model_dump(mode="json")
        distinct[canonical_bytes(payload)] = payload
    single = next(iter(distinct.values())) if len(distinct) == 1 else None
    kinds = byte_sorted(tuple({str(claim.statement.object.kind) for claim in rows}))
    declared = cardinalities.get(predicate)
    return SubjectProfilePredicateV1(
        predicate=predicate,
        claim_type_address=SemanticAddress.whole_artifact(claim_type_path(predicate)),
        pinned_claim_type_digests=byte_sorted(
            tuple(claim.statement.claim_type_digest for claim in rows)
        ),
        cardinality=declared,  # type: ignore[arg-type]
        claim_count=len(rows),
        contender_count=len(distinct),
        resolution="single" if len(distinct) == 1 else "unresolved",
        object_kind=kinds[0] if len(kinds) == 1 else None,
        object_digest=(
            None
            if single is None
            else typed_digest(Sha256Value, "playbill-claim-object-v1", {"object": single}).tagged
        ),
        object_preview=(
            None if single is None else _object_preview(single, budget=budget, reasons=reasons)
        ),
        verdict=verdict,
        currency=currency,
    )


def build_subject_profile(
    *,
    at: AcceptedCoordinate,
    entry: DiscoveryEntryV1,
    subject_kind: str,
    subject_id: str,
    artifact_digest: str,
    claims: Iterable[ClaimArtifact] = (),
    cardinalities: Mapping[str, str] | None = None,
    relations: tuple[SemanticRelationV1, ...] = (),
    predicate_verdicts: Mapping[str, tuple[str, str]] | None = None,
    evaluation_time: datetime | None = None,
    budget: InterfaceProjectionBudgetV1 = InterfaceProjectionBudgetV1(),
) -> SubjectProfileV1:
    """Project one accepted Subject into its predicate-indexed compact profile.

    Passing ``evaluation_time`` together with ``predicate_verdicts`` makes the
    profile verdict-relative and is the only way a verdict or a currency can
    appear on it; without a read time the profile is a coordinate-pure
    projection of accepted structure.
    """

    if entry.kind != "Subject":
        raise DiscoveryError("a Subject profile requires the Subject vocabulary entry")
    if predicate_verdicts and evaluation_time is None:
        raise DiscoveryError("a verdict-bearing Subject profile requires an explicit read time")
    verdicts = predicate_verdicts or {}
    truncated: set[str] = set()
    reasons: set[str] = set()
    grouped: dict[str, list[ClaimArtifact]] = {}
    for claim in claims:
        if claim.lifecycle.state != "live":
            continue
        grouped.setdefault(claim.statement.predicate, []).append(claim)
    built: list[SubjectProfilePredicateV1] = []
    for predicate in sorted(grouped, key=lambda item: item.encode("utf-8")):
        adjudicated = verdicts.get(predicate)
        built.append(
            _predicate_row(
                predicate,
                grouped[predicate],
                cardinalities=cardinalities or {},
                verdict=None if adjudicated is None else adjudicated[0],
                currency=None if adjudicated is None else adjudicated[1],
                budget=budget,
                reasons=reasons,
            )
        )
    rows = tuple(built)
    if len(rows) > budget.max_predicates:
        rows = rows[: budget.max_predicates]
        truncated.add("predicates")
        reasons.add("predicate_budget_exceeded")
    bases = _match_bases(entry, budget=budget, reasons=reasons)
    if "term_budget_exceeded" in reasons:
        truncated.add("match_bases")
    if {"object_preview_omitted", "object_preview_withheld"} & reasons:
        truncated.add("predicates")

    def payload(kept: tuple[SubjectProfilePredicateV1, ...]) -> list[object]:
        return [item.model_dump(mode="json") for item in kept] + [
            item.model_dump(mode="json") for item in bases
        ]

    while rows and _payload_bytes(payload(rows)) > budget.max_bytes:
        rows = rows[:-1]
        truncated.add("predicates")
        reasons.add("byte_budget_exceeded")

    return SubjectProfileV1(
        at=at,
        address=entry.address,
        identity=entry.identity,
        artifact_digest=artifact_digest,
        subject_kind=subject_kind,
        subject_id=subject_id,
        evaluation_time=evaluation_time,
        verdict_relative=evaluation_time is not None,
        aliases=entry.aliases,
        tags=entry.tags,
        relations=relations,
        match_bases=bases,
        predicates=rows,
        expansion_links=_expansion_links(entry.address),
        coverage=_coverage(
            facets=SUBJECT_PROFILE_FACETS,
            truncated=truncated,
            reasons=reasons,
        ),
    )


# -- the projection index and the interface discovery page ----------------


def _require_address_order(value: Sequence[ClaimTypeCardV1 | SubjectProfileV1]) -> None:
    keys = tuple(canonical_bytes(item.address.model_dump(mode="json")) for item in value)
    if keys != tuple(sorted(set(keys))):
        raise ValueError("interface projections must be sorted and unique by address")


class InterfaceProjectionIndexV1(_StrictCardModel):
    """Every card and profile at exactly one accepted coordinate."""

    tag: Literal["playbill-interface-projection-index-v1"] = (
        "playbill-interface-projection-index-v1"
    )
    at: AcceptedCoordinate
    cards: tuple[ClaimTypeCardV1, ...] = ()
    profiles: tuple[SubjectProfileV1, ...] = ()

    @field_validator("cards")
    @classmethod
    def _cards(cls, value: tuple[ClaimTypeCardV1, ...]) -> tuple[ClaimTypeCardV1, ...]:
        _require_address_order(value)
        return value

    @field_validator("profiles")
    @classmethod
    def _profiles(cls, value: tuple[SubjectProfileV1, ...]) -> tuple[SubjectProfileV1, ...]:
        _require_address_order(value)
        return value

    def card(self, address: SemanticAddress) -> ClaimTypeCardV1 | None:
        """Return the card at one exact semantic address, if this index holds it."""

        key = canonical_bytes(address.model_dump(mode="json"))
        return next(
            (
                item
                for item in self.cards
                if canonical_bytes(item.address.model_dump(mode="json")) == key
            ),
            None,
        )

    def profile(self, address: SemanticAddress) -> SubjectProfileV1 | None:
        """Return the profile at one exact semantic address, if this index holds it."""

        key = canonical_bytes(address.model_dump(mode="json"))
        return next(
            (
                item
                for item in self.profiles
                if canonical_bytes(item.address.model_dump(mode="json")) == key
            ),
            None,
        )


class InterfaceDiscoveryPageV1(_StrictCardModel):
    """One discovery page whose hits carry their compact interface projection."""

    tag: Literal["playbill-interface-discovery-page-v1"] = "playbill-interface-discovery-page-v1"
    page: DiscoveryPageV1
    cards: tuple[ClaimTypeCardV1, ...] = ()
    profiles: tuple[SubjectProfileV1, ...] = ()
    handle_addresses: tuple[SemanticAddress, ...] = ()
    resolved_address: SemanticAddress | None = None
    coverage: CoverageDescriptorV1
    receipt_digest: str

    @field_validator("receipt_digest")
    @classmethod
    def _receipt(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _covers_every_hit(self) -> "InterfaceDiscoveryPageV1":
        projected = [canonical_bytes(item.address.model_dump(mode="json")) for item in self.cards]
        projected.extend(
            canonical_bytes(item.address.model_dump(mode="json")) for item in self.profiles
        )
        projected.extend(
            canonical_bytes(item.model_dump(mode="json")) for item in self.handle_addresses
        )
        hits = [canonical_bytes(hit.address.model_dump(mode="json")) for hit in self.page.hits]
        if sorted(projected) != sorted(hits):
            raise ValueError("an interface page projects each hit exactly once")
        return self


def build_interface_projections(
    *,
    vocabulary: DiscoveryVocabularyV1,
    facts: ClaimQueryFactsV1,
    claim_types: Iterable[ClaimType] = (),
    evaluation_time: datetime | None = None,
    budget: InterfaceProjectionBudgetV1 = InterfaceProjectionBudgetV1(),
) -> InterfaceProjectionIndexV1:
    """Project every card and profile the accepted coordinate supports.

    The vocabulary supplies the accepted descriptor terms so a card, a profile,
    and a discovery hit can never disagree about what an interface is called.
    """

    if vocabulary.at != AcceptedCoordinate.from_internal(facts.coordinate):
        raise DiscoveryError("interface projections require one accepted coordinate")
    live = tuple(row for row in facts.claims if row.accepted.claim.lifecycle.state == "live")
    contracts = {item.predicate: item for item in claim_types if item.lifecycle.state == "live"}
    cardinalities = {name: item.cardinality for name, item in contracts.items()}
    providers = {item.identity.qualified: item for item in facts.providers}
    subject_identities = {item.path: item.shell.identity.qualified for item in facts.subjects}
    digests = {item.path: item.artifact_digest for item in facts.subjects}
    kinds = {item.path: (item.shell.subject_kind, item.shell.subject_id) for item in facts.subjects}
    relations = descriptor_relations(row.accepted.claim for row in facts.claims)

    usage: dict[str, list[ClaimTypeUsageRowV1]] = {}
    by_subject: dict[str, list[ClaimFactRowV1]] = {}
    for row in live:
        usage.setdefault(row.accepted.claim.statement.predicate, []).append(
            ClaimTypeUsageRowV1(
                subject_path=row.subject_path,
                subject_identity=subject_identities.get(row.subject_path, row.subject_path),
            )
        )
        by_subject.setdefault(row.subject_path, []).append(row)

    cards: list[ClaimTypeCardV1] = []
    profiles: list[SubjectProfileV1] = []
    for entry in vocabulary.entries:
        if entry.kind == "ClaimType":
            contract = contracts.get(entry.label)
            if contract is None:
                continue
            cards.append(
                build_claim_type_card(
                    contract,
                    at=vocabulary.at,
                    entry=entry,
                    usage_rows=tuple(usage.get(contract.predicate, ())),
                    relations=_relations_for(relations, entry.address),
                    budget=budget,
                )
            )
        elif entry.kind == "Subject":
            path = entry.address.artifact_path
            if path not in kinds:
                continue
            subject_kind, subject_id = kinds[path]
            rows = tuple(by_subject.get(path, ()))
            profiles.append(
                build_subject_profile(
                    at=vocabulary.at,
                    entry=entry,
                    subject_kind=subject_kind,
                    subject_id=subject_id,
                    artifact_digest=digests[path],
                    claims=tuple(item.accepted.claim for item in rows),
                    cardinalities=cardinalities,
                    relations=_relations_for(relations, entry.address),
                    predicate_verdicts=(
                        None
                        if evaluation_time is None
                        else claim_predicate_verdicts(
                            rows,
                            evaluation_time=evaluation_time,
                            providers=providers,
                        )
                    ),
                    evaluation_time=evaluation_time,
                    budget=budget,
                )
            )
    return InterfaceProjectionIndexV1(
        at=vocabulary.at,
        cards=tuple(cards),
        profiles=tuple(profiles),
    )


def discover_interfaces(
    request: DiscoveryRequestV1,
    *,
    vocabulary: DiscoveryVocabularyV1,
    projections: InterfaceProjectionIndexV1,
) -> InterfaceDiscoveryPageV1:
    """Answer one discovery request with compact interfaces instead of bare handles.

    Every hit the projection index covers comes back as a card or a profile;
    a hit whose kind has no v1 projection stays an explicit handle rather than
    being dropped, so the page still accounts for the whole result set.
    """

    if projections.at != vocabulary.at:
        raise DiscoveryError("interface discovery requires one accepted coordinate")
    page = discover(request, vocabulary=vocabulary)
    cards: list[ClaimTypeCardV1] = []
    profiles: list[SubjectProfileV1] = []
    handles: list[SemanticAddress] = []
    for hit in page.hits:
        card = projections.card(hit.address)
        profile = projections.profile(hit.address)
        if card is not None:
            cards.append(card)
        elif profile is not None:
            profiles.append(profile)
        else:
            handles.append(hit.address)
    coverage = CoverageDescriptorV1(
        requested_facets=("claim_type_card", "handle", "subject_profile"),
        available_facets=byte_sorted(
            tuple(
                name
                for name, present in (
                    ("claim_type_card", bool(cards)),
                    ("handle", bool(handles)),
                    ("subject_profile", bool(profiles)),
                )
                if present
            )
        ),
        truncated_facets=page.coverage.truncated_facets,
        reason_codes=page.coverage.reason_codes,
    )
    receipt_digest = typed_digest(
        Sha256Value,
        INTERFACE_PAGE_RECEIPT_DIGEST_DOMAIN,
        {
            "cards": [claim_type_card_digest(item) for item in cards],
            "coverage": coverage.model_dump(mode="json"),
            "handles": [item.model_dump(mode="json") for item in handles],
            "page_receipt_digest": page.receipt_digest,
            "profiles": [subject_profile_digest(item) for item in profiles],
        },
    ).tagged
    return InterfaceDiscoveryPageV1(
        page=page,
        cards=tuple(cards),
        profiles=tuple(profiles),
        handle_addresses=tuple(handles),
        resolved_address=resolved_equivalence_address(page),
        coverage=coverage,
        receipt_digest=receipt_digest,
    )


__all__ = [
    "CLAIM_TYPE_CARD_DIGEST_DOMAIN",
    "CLAIM_TYPE_CARD_FACETS",
    "INTERFACE_PAGE_RECEIPT_DIGEST_DOMAIN",
    "INTERFACE_POLICY_DIGEST_DOMAIN",
    "SUBJECT_PROFILE_DIGEST_DOMAIN",
    "SUBJECT_PROFILE_FACETS",
    "ClaimTypeCardV1",
    "ClaimTypeUsageRowV1",
    "ClaimTypeUsageV1",
    "InterfaceDiscoveryPageV1",
    "InterfaceMatchBasisV1",
    "InterfacePolicySummaryV1",
    "InterfaceProjectionBudgetV1",
    "InterfaceProjectionIndexV1",
    "SemanticRelationV1",
    "SubjectProfilePredicateV1",
    "SubjectProfileV1",
    "build_claim_type_card",
    "build_interface_projections",
    "build_subject_profile",
    "claim_predicate_verdicts",
    "claim_type_card_digest",
    "descriptor_relations",
    "discover_interfaces",
    "subject_profile_digest",
]
