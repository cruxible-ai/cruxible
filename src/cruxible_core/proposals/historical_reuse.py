"""Frozen reproducer for the retired ClaimType vocabulary reuse law.

Historical only. Maintainer ruling ``dev.decision/reuse-removal-laws-0929``
removed the reuse law from the current ClaimType law revisions. The revisions
that ran it (``HISTORICAL_REUSE_CLAIM_TYPE_LAWS``) stay installed so accepted
generations and pending proposals judged under them settle and replay, and each
of their ClaimType member results carries a ``reuse`` key that must reproduce
byte for byte. Nothing on a live authoring path reaches this module.

Every recorded evaluation used empty discovery hints and the ``new_distinct``
disposition, so this reproducer is specialized to exactly that input. Do not
change what it computes: its output is committed in accepted change-set records.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.claim_type_structure import claim_type_structural_signature
from cruxible_client.contracts.claim_types import ClaimType, parse_claim_type
from cruxible_client.contracts.claims import (
    LiteralClaimObject,
    SubjectClaimObject,
    claim_artifact_digest,
    claim_statement_address,
    parse_claim,
)
from cruxible_client.contracts.discovery import normalize_discovery_term
from cruxible_client.contracts.laws import (
    CLAIM_TYPE_LAW_REVISION_4,
    CLAIM_TYPE_LAW_V3_REVISION_4,
    CLAIM_TYPE_LAW_V4_REVISION_4,
    CLAIM_TYPE_LAW_V5_REVISION_4,
    CLAIM_TYPE_LAW_V6_REVISION_1,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import parse_subject, subject_reuse_signature
from cruxible_core.indexes.claims.claim_subject_index import CLAIM_PATH_RE
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate

# The ClaimType law coordinates that ran the vocabulary reuse law. Their member
# results carry a ``reuse`` key (evidence for a new ClaimType, null for a
# revision) that replay and settlement must reproduce byte for byte.
HISTORICAL_REUSE_CLAIM_TYPE_LAWS: frozenset[tuple[str, str]] = frozenset(
    (law.identifier, law.digest)
    for law in (
        CLAIM_TYPE_LAW_REVISION_4,
        CLAIM_TYPE_LAW_V3_REVISION_4,
        CLAIM_TYPE_LAW_V4_REVISION_4,
        CLAIM_TYPE_LAW_V5_REVISION_4,
        CLAIM_TYPE_LAW_V6_REVISION_1,
    )
)

# The digest of the empty ``playbill-discovery-hints-v1`` payload every recorded
# evaluation committed to.
_EMPTY_HINTS_DIGEST = typed_digest(
    Sha256Value,
    "playbill-discovery-hints-v1",
    {"alternate_phrases": [], "topical_tags": []},
).tagged

ReuseMatchBasis = Literal[
    "exact_identity",
    "canonical_token",
    "structural_signature",
    "accepted_alias",
    "accepted_tag",
    "accepted_relation",
]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _sorted_unique(value: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    normalized = tuple(unicodedata.normalize("NFC", item) for item in value)
    if normalized != value or value != tuple(
        sorted(set(value), key=lambda item: item.encode("utf-8"))
    ):
        raise ValueError(f"{label} must be NFC, sorted, and unique")
    return value


class SemanticReuseInterfaceV1(_Frozen):
    """One accepted ClaimType or Subject as the reuse law compared it."""

    address: SemanticAddress
    identity: ArtifactIdentity
    kind: str
    label: str
    canonical_tokens: tuple[str, ...]
    structural_signature_digest: str
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    relation_labels: tuple[str, ...] = ()

    @field_validator("canonical_tokens", "aliases", "tags", "relation_labels")
    @classmethod
    def _terms(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="semantic reuse terms")


class ProposedSemanticInterfaceV1(_Frozen):
    address: SemanticAddress
    identity: ArtifactIdentity
    kind: str
    label: str
    canonical_tokens: tuple[str, ...]
    structural_signature_digest: str

    @field_validator("canonical_tokens")
    @classmethod
    def _tokens(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("proposed semantic interface requires canonical tokens")
        return _sorted_unique(value, label="proposed canonical tokens")


class ReuseMatchV1(_Frozen):
    basis: ReuseMatchBasis
    term: str
    blocking: bool


class ReuseCandidateV1(_Frozen):
    address: SemanticAddress
    identity: ArtifactIdentity
    kind: str
    label: str
    match_basis: tuple[ReuseMatchV1, ...]

    @property
    def blocking(self) -> bool:
        return any(item.blocking for item in self.match_basis)


class ReuseDispositionV1(_Frozen):
    """The only disposition any recorded evaluation used."""

    kind: Literal["new_distinct"] = "new_distinct"
    target: None = None


class DistinctRelationMemberV1(_Frozen):
    """One governed distinction persisted in the same candidate closure."""

    claim_address: SemanticAddress
    claim_artifact_digest: str
    subject: SemanticAddress
    object: SemanticAddress


class VocabularyReuseLawEvidenceV1(_Frozen):
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

    @model_validator(mode="after")
    def _verdict_shape(self) -> VocabularyReuseLawEvidenceV1:
        if (self.verdict == "refused") != (self.refusal_code is not None):
            raise ValueError("reuse evidence refusal code must agree with its verdict")
        return self


def _match_candidate(
    proposal: ProposedSemanticInterfaceV1,
    candidate: SemanticReuseInterfaceV1,
) -> ReuseCandidateV1 | None:
    proposal_terms = {normalize_discovery_term(item) for item in proposal.canonical_tokens}
    matches: dict[tuple[str, str], ReuseMatchV1] = {}

    def add(basis: ReuseMatchBasis, term: str, *, blocking: bool) -> None:
        matches[(basis, term)] = ReuseMatchV1(basis=basis, term=term, blocking=blocking)

    if proposal.identity == candidate.identity:
        add("exact_identity", candidate.identity.qualified, blocking=True)
    for term in sorted(
        proposal_terms.intersection(
            normalize_discovery_term(item) for item in candidate.canonical_tokens
        )
    ):
        add("canonical_token", term, blocking=True)
    if proposal.kind == candidate.kind and (
        proposal.structural_signature_digest == candidate.structural_signature_digest
    ):
        add("structural_signature", proposal.structural_signature_digest, blocking=True)
    for alias in candidate.aliases:
        if normalize_discovery_term(alias) in proposal_terms:
            add("accepted_alias", alias, blocking=True)
    for tag in candidate.tags:
        if normalize_discovery_term(tag) in proposal_terms:
            add("accepted_tag", tag, blocking=False)
    for relation in candidate.relation_labels:
        if normalize_discovery_term(relation) in proposal_terms:
            add("accepted_relation", relation, blocking=False)
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
    proposal: ProposedSemanticInterfaceV1,
    *,
    accepted_interfaces: tuple[SemanticReuseInterfaceV1, ...],
    coordinate: AcceptedCoordinate,
    implementation_digest: str,
    distinct_relation_members: tuple[DistinctRelationMemberV1, ...] = (),
) -> VocabularyReuseLawEvidenceV1:
    """Reproduce one recorded parent-coordinate reuse lookup exactly."""

    Sha256Value.from_tagged(implementation_digest)
    candidates = tuple(
        sorted(
            (
                match
                for candidate in accepted_interfaces
                if (match := _match_candidate(proposal, candidate)) is not None
            ),
            key=lambda item: canonical_bytes(item.address.model_dump(mode="json")),
        )
    )
    encoded_relations = tuple(
        canonical_bytes(item.model_dump(mode="json")) for item in distinct_relation_members
    )
    if encoded_relations != tuple(sorted(set(encoded_relations))):
        raise ValueError("distinct relation members must be canonically sorted and unique")
    result_digest = typed_digest(
        Sha256Value,
        "playbill-vocabulary-reuse-result-v1",
        {
            "coordinate": coordinate.model_dump(mode="json"),
            "implementation_digest": implementation_digest,
            "proposal": proposal.model_dump(mode="json"),
            "candidates": [item.model_dump(mode="json") for item in candidates],
            "distinct_relation_members": [
                item.model_dump(mode="json") for item in distinct_relation_members
            ],
        },
    ).tagged
    refusal: str | None = None
    if any(
        any(item.basis == "exact_identity" for item in candidate.match_basis)
        for candidate in candidates
    ):
        refusal = "playbill.reuse.exact_collision"
    else:
        blocking = {
            canonical_bytes(item.address.model_dump(mode="json"))
            for item in candidates
            if item.blocking
        }
        proposal_address = canonical_bytes(proposal.address.model_dump(mode="json"))
        persisted = {
            canonical_bytes(item.object.model_dump(mode="json"))
            for item in distinct_relation_members
            if canonical_bytes(item.subject.model_dump(mode="json")) == proposal_address
        }
        if not blocking.issubset(persisted):
            refusal = "playbill.reuse.distinction_claim_missing"
    return VocabularyReuseLawEvidenceV1(
        coordinate=coordinate,
        implementation_digest=implementation_digest,
        hints_digest=_EMPTY_HINTS_DIGEST,
        result_digest=result_digest,
        candidates=candidates,
        disposition=ReuseDispositionV1(),
        distinct_relation_members=distinct_relation_members,
        verdict="refused" if refusal is not None else "satisfied",
        refusal_code=refusal,
    )


def _sorted_terms(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(values, key=lambda item: item.encode("utf-8")))


def _path_patterns() -> tuple[Any, Any]:
    # Imported late: the proposal evaluator imports this module.
    from cruxible_core.proposals.proposals import _CLAIM_TYPE_PATH_RE, _SUBJECT_PATH_RE

    return _CLAIM_TYPE_PATH_RE, _SUBJECT_PATH_RE


def reuse_interface(
    path: str, content: bytes, descriptors: Mapping[str, set[str]]
) -> SemanticReuseInterfaceV1 | None:
    """One live ClaimType or Subject's whole-artifact reuse interface."""

    claim_type_re, subject_re = _path_patterns()
    if claim_type_re.fullmatch(path):
        claim_type = parse_claim_type(content, path=path)
        if claim_type.lifecycle.state != "live":
            return None
        identity: ArtifactIdentity = claim_type.identity
        kind, label = "claim-type", claim_type.predicate
        tokens = _sorted_terms({claim_type.predicate, claim_type.predicate.rpartition(".")[2]})
        signature = claim_type_structural_signature(claim_type.structure)
    elif subject_re.fullmatch(path):
        subject = parse_subject(content, path=path)
        if subject.lifecycle.state != "live":
            return None
        identity = subject.identity
        kind, label = "subject", subject.identity.qualified
        tokens = (subject.subject_id,)
        signature = subject_reuse_signature(subject.identity)
    else:
        return None
    return SemanticReuseInterfaceV1(
        address=SemanticAddress.whole_artifact(path),
        identity=identity,
        kind=kind,
        label=label,
        canonical_tokens=tokens,
        structural_signature_digest=signature,
        aliases=_sorted_terms(descriptors["alias"]),
        tags=_sorted_terms(descriptors["tag"]),
        relation_labels=_sorted_terms(descriptors["relation"]),
    )


def reuse_interfaces(tree: Mapping[str, bytes]) -> tuple[SemanticReuseInterfaceV1, ...]:
    """Every accepted reuse interface, by reading the whole tree (the cold oracle)."""

    claim_type_re, subject_re = _path_patterns()
    descriptor_terms: dict[bytes, dict[str, set[str]]] = {}

    def terms_for(address: SemanticAddress) -> dict[str, set[str]]:
        key = canonical_bytes(address.model_dump(mode="json"))
        return descriptor_terms.setdefault(key, {"alias": set(), "tag": set(), "relation": set()})

    for descriptor_path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not CLAIM_PATH_RE.fullmatch(descriptor_path):
            continue
        descriptor = parse_claim(tree[descriptor_path], path=descriptor_path)
        if descriptor.lifecycle.state != "live":
            continue
        predicate = descriptor.statement.predicate
        if predicate in {"semantic.alias", "semantic.tag"} and isinstance(
            descriptor.statement.object, LiteralClaimObject
        ):
            value = descriptor.statement.object.value
            if not isinstance(value, str):
                continue
            field = "alias" if predicate == "semantic.alias" else "tag"
            terms_for(descriptor.statement.subject)[field].add(value)
        elif predicate in {"semantic.related_to", "semantic.distinct_from"} and isinstance(
            descriptor.statement.object, SubjectClaimObject
        ):
            relation_label = descriptor.statement.object.address.artifact_path
            terms_for(descriptor.statement.subject)["relation"].add(relation_label)
            terms_for(descriptor.statement.object.address)["relation"].add(
                descriptor.statement.subject.artifact_path
            )

    interfaces: list[SemanticReuseInterfaceV1] = []
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not (claim_type_re.fullmatch(path) or subject_re.fullmatch(path)):
            continue
        interface = reuse_interface(
            path, tree[path], terms_for(SemanticAddress.whole_artifact(path))
        )
        if interface is not None:
            interfaces.append(interface)
    return tuple(interfaces)


def indexed_reuse_interfaces(
    selection: Any, proposal: ProposedSemanticInterfaceV1, *, exclude_path: str
) -> tuple[SemanticReuseInterfaceV1, ...]:
    """Only the interfaces the vocabulary index says could match this proposal.

    Matching is exact equality on normalized terms, identity, or a same-kind
    structural signature, and every such key is indexed, so interfaces outside
    this set cannot match; each candidate is then built in full, exactly as the
    whole-tree oracle builds it, so the recorded result digest is unchanged.
    """

    def build(rows: Any) -> tuple[SemanticReuseInterfaceV1, ...]:
        paths = rows.vocabulary_matches(
            terms=(normalize_discovery_term(item) for item in proposal.canonical_tokens),
            identity=proposal.identity.qualified,
            signature_term=f"{proposal.kind}:{proposal.structural_signature_digest}",
        )
        interfaces = []
        for path in paths:
            if path == exclude_path:
                continue
            try:
                content = rows.source_bytes(path)
            except KeyError:
                continue
            interface = reuse_interface(path, content, rows.vocabulary_descriptors(path))
            if interface is not None:
                interfaces.append(interface)
        return tuple(interfaces)

    if hasattr(selection, "call"):
        return cast(tuple[SemanticReuseInterfaceV1, ...], selection.call(build))
    return build(selection)


def historical_claim_type_reuse_evidence(
    *,
    claim_type: ClaimType,
    path: str,
    lookup_tree: Mapping[str, bytes],
    candidate_scope: tuple[str, ...],
    current: AcceptedProjectionCoordinate,
    candidate_states: object = None,
) -> dict[str, object]:
    """The reuse evidence a historical ClaimType law recorded for a new ClaimType."""

    predicate = claim_type.predicate
    proposal = ProposedSemanticInterfaceV1(
        address=SemanticAddress.whole_artifact(path),
        identity=claim_type.identity,
        kind="claim-type",
        label=predicate,
        canonical_tokens=_sorted_terms({predicate, predicate.rpartition(".")[2]}),
        structural_signature_digest=claim_type_structural_signature(claim_type.structure),
    )
    relations: list[DistinctRelationMemberV1] = []
    for relation_path in candidate_scope:
        if not CLAIM_PATH_RE.fullmatch(relation_path):
            continue
        relation = parse_claim(lookup_tree[relation_path], path=relation_path)
        if relation.statement.predicate != "semantic.distinct_from" or not isinstance(
            relation.statement.object, SubjectClaimObject
        ):
            continue
        relations.append(
            DistinctRelationMemberV1(
                claim_address=claim_statement_address(relation_path),
                claim_artifact_digest=claim_artifact_digest(relation).tagged,
                subject=relation.statement.subject,
                object=relation.statement.object.address,
            )
        )
    from cruxible_core.indexes.evaluated_state import EvaluationRows, SelectionSpec

    selection = getattr(candidate_states, "owner", None)
    if isinstance(selection, (EvaluationRows, SelectionSpec)):
        accepted_interfaces = indexed_reuse_interfaces(selection, proposal, exclude_path=path)
    else:
        accepted_interfaces = tuple(
            item for item in reuse_interfaces(lookup_tree) if item.address.artifact_path != path
        )
    evidence = evaluate_vocabulary_reuse(
        proposal,
        accepted_interfaces=accepted_interfaces,
        coordinate=AcceptedCoordinate.from_internal(current),
        implementation_digest=current.compiler.rule_digest,
        distinct_relation_members=tuple(
            sorted(relations, key=lambda item: canonical_bytes(item.model_dump(mode="json")))
        ),
    )
    return evidence.model_dump(mode="json")


__all__ = [
    "HISTORICAL_REUSE_CLAIM_TYPE_LAWS",
    "historical_claim_type_reuse_evidence",
]
