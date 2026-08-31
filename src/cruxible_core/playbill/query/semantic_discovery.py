"""Deterministic exact/lexical discovery over the accepted semantic vocabulary.

Discovery is a read. It never writes, never resolves a conflict, and never
scores: every hit names the closed set of bases that produced it, and the page
order is a total function of those bases and the kind-qualified semantic-address
bytes. Learned ranking may later add a separately labeled advisory lane; this
path stays canonical.

The vocabulary this module ranges over is the accepted *naming* layer -- the
Subjects, ClaimTypes, QueryDefinitions, Procedures, and LineSpecs an agent has
to find before it can ask anything useful. It deliberately does not range over
individual Claims: a Claim's standing is verdict- and time-relative, and a hit
that carried a verdict would be a coordinate-pure answer wearing a
time-relative one. Hits therefore report ``currency="not_applicable"`` and no
verdict; the verdict-bearing read is :func:`evaluate_claim_query`, whose
execution receipts are journalled, or ``expand`` at an explicit evaluation time.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, field_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.claim_type_structure import claim_type_structural_signature
from cruxible_client.contracts.claim_types import ClaimType, claim_type_path
from cruxible_client.contracts.claims import LiteralClaimObject, SubjectClaimObject
from cruxible_client.contracts.discovery import (
    DiscoveryHitV1,
    DiscoveryMatchBasis,
    DiscoveryMatchBasisV1,
    DiscoveryPageV1,
    DiscoveryRequestV1,
    normalize_discovery_term,
    reject_locator_or_secret,
)
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedureV1
from cruxible_client.contracts.procedures.line_specs import AcceptedLineSpecV1
from cruxible_client.contracts.query.definitions import AcceptedQueryDefinitionV1
from cruxible_client.contracts.query.grammar import byte_sorted
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.source_references import CoverageDescriptorV1
from cruxible_client.contracts.subjects import subject_reuse_signature
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.query.backends import ClaimQueryFactsV1, SubjectQueryViewV1

VOCABULARY_DIGEST_DOMAIN = "playbill-discovery-vocabulary-v1"
SELECTION_BASIS_DIGEST_DOMAIN = "playbill-discovery-selection-basis-v1"
PAGE_RECEIPT_DIGEST_DOMAIN = "playbill-discovery-page-receipt-v1"

DISCOVERY_ENTRY_KINDS: tuple[str, ...] = (
    "ClaimType",
    "LineSpec",
    "Procedure",
    "QueryDefinition",
    "Subject",
)

DISCOVERY_PROFILE_KINDS: Mapping[str, tuple[str, ...]] = {
    "all": DISCOVERY_ENTRY_KINDS,
    "interfaces": ("ClaimType", "LineSpec", "Procedure", "QueryDefinition"),
    "subjects": ("Subject",),
}

MATCH_BASIS_PRIORITY: Mapping[str, int] = {
    "exact_address": 0,
    "named_entrypoint": 1,
    "exact_alias": 2,
    "content_equivalent": 3,
    "structural_signature": 4,
    "dependency_walk": 5,
    "tag": 6,
    "lexical": 7,
}
"""The frozen v1 basis order: an accepted tag or a lexical hit can never outrank
an exact address or an accepted alias, and no opaque score participates at all.

The order grades *strength of deterministic evidence*, not trust, and the
equivalence grade below is the separate axis that decides what a basis may
conclude. ``content_equivalent`` therefore sits immediately below the three
equivalence-resolving bases and above every recall-only one: byte-for-byte
digest equality of cited material is a stronger deterministic signal than a
shared structural shape (which is a shape collision) and categorically stronger
than a tag or a lexical token, so when a bounded card list is clipped from the
low-priority tail it is the byte-identical foreign occurrence a reviewer keeps.
Ranking it high grants it nothing: `dd-match-basis-content-equivalent` resolves
it to ``False`` below, and §11.6.1 makes copied bytes precisely not identity.
"""

MATCH_BASIS_RESOLVES_EQUIVALENCE: Mapping[str, bool] = {
    "exact_address": True,
    "named_entrypoint": True,
    "exact_alias": True,
    "content_equivalent": False,
    "structural_signature": False,
    "dependency_walk": False,
    "tag": False,
    "lexical": False,
}
"""Which bases may resolve an expression to the exact target.

This is the §6.3.1 law made structural rather than conventional. An alias is
admitted under the target namespace's own authority, so it may resolve; a tag
and a lexical token are recall-only and can only add or rank candidates. A
shared structural signature is a blocking near candidate for review, never a
proof of equivalence -- two ClaimTypes can have the same shape and mean
different things. Identical bytes at a foreign source occurrence are the same
kind of near candidate: §11.6.1 gives them no inherited governance, so
``content_equivalent`` can never merge Subjects or satisfy identity resolution.
"""

_DESCRIPTOR_LITERAL_PREDICATES = frozenset({"semantic.alias", "semantic.tag"})
_DESCRIPTOR_RELATION_PREDICATES = frozenset({"semantic.distinct_from", "semantic.related_to"})
_TOKEN_SPLIT_RE = re.compile(r"[^0-9a-z]+")


class DiscoveryError(PlaybillError):
    """A discovery request could not be answered deterministically."""


class _StrictDiscoveryEngineModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def discovery_tokens(value: str) -> tuple[str, ...]:
    """Split one normalized term into the deterministic lexical token set."""

    return byte_sorted(
        tuple({token for token in _TOKEN_SPLIT_RE.split(normalize_discovery_term(value)) if token})
    )


class DiscoveryEntryV1(_StrictDiscoveryEngineModel):
    """One accepted vocabulary item, compact enough to return before a body."""

    tag: Literal["playbill-discovery-entry-v1"] = "playbill-discovery-entry-v1"
    kind: str
    address: SemanticAddress
    identity: str
    label: str
    entrypoint_name: str | None = None
    description: str | None = None
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    lexical_terms: tuple[str, ...] = ()
    structural_signature_digest: str | None = None
    dependency_addresses: tuple[SemanticAddress, ...] = ()
    dependent_addresses: tuple[SemanticAddress, ...] = ()

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if value not in DISCOVERY_ENTRY_KINDS:
            supported = ", ".join(DISCOVERY_ENTRY_KINDS)
            raise ValueError(f"unknown discovery entry kind {value!r}; supported: {supported}")
        return value

    @field_validator("aliases", "tags", "lexical_terms")
    @classmethod
    def _terms(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("discovery entry terms must be sorted and unique")
        return value

    @field_validator("structural_signature_digest")
    @classmethod
    def _signature(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @property
    def order_key(self) -> tuple[bytes, bytes]:
        """Return the kind-qualified semantic-address bytes hits are ordered by."""

        return (
            self.kind.encode("utf-8"),
            canonical_bytes(self.address.model_dump(mode="json")),
        )

    @property
    def normalized_aliases(self) -> frozenset[str]:
        return frozenset(normalize_discovery_term(item) for item in self.aliases)

    @property
    def normalized_tags(self) -> frozenset[str]:
        return frozenset(normalize_discovery_term(item) for item in self.tags)

    @property
    def token_set(self) -> frozenset[str]:
        tokens: set[str] = set()
        for term in (self.label, self.identity, *self.aliases, *self.tags, *self.lexical_terms):
            tokens.update(discovery_tokens(term))
        return frozenset(tokens)


class DiscoveryVocabularyV1(_StrictDiscoveryEngineModel):
    """The complete discoverable naming layer at exactly one accepted coordinate."""

    tag: Literal["playbill-discovery-vocabulary-v1"] = "playbill-discovery-vocabulary-v1"
    at: AcceptedCoordinate
    entries: tuple[DiscoveryEntryV1, ...] = ()
    excluded_claim_count: int = 0
    """Accepted Claims read for descriptor terms whose statement Subject is absent
    at the coordinate. F3 keeps them out of the materialized Subject view, so the
    view alone would silently understate what discovery consulted."""

    @field_validator("entries")
    @classmethod
    def _entries(cls, value: tuple[DiscoveryEntryV1, ...]) -> tuple[DiscoveryEntryV1, ...]:
        keys = tuple(item.order_key for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("discovery entries must be sorted and unique by kind and address")
        return value

    def entrypoints(self) -> tuple[DiscoveryEntryV1, ...]:
        """Return the accepted named QueryDefinition entrypoints in stable order."""

        return tuple(item for item in self.entries if item.entrypoint_name is not None)

    def entrypoint(self, name: str) -> DiscoveryEntryV1 | None:
        """Resolve one named entrypoint by its exact accepted name."""

        for item in self.entrypoints():
            if item.entrypoint_name == name:
                return item
        return None


def discovery_vocabulary_digest(vocabulary: DiscoveryVocabularyV1) -> str:
    """Digest the vocabulary so a selection basis names exactly what was searched."""

    payload = vocabulary.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(Sha256Value, VOCABULARY_DIGEST_DOMAIN, payload).tagged


# -- vocabulary construction ---------------------------------------------


def _descriptor_terms(
    facts: ClaimQueryFactsV1,
    *,
    resolved_subject_paths: frozenset[str],
) -> tuple[dict[bytes, dict[str, set[str]]], int]:
    """Collect accepted alias/tag/relation terms and count the view's exclusions."""

    terms: dict[bytes, dict[str, set[str]]] = {}
    excluded = 0

    def bucket(address: SemanticAddress) -> dict[str, set[str]]:
        return terms.setdefault(
            canonical_bytes(address.model_dump(mode="json")),
            {"aliases": set(), "tags": set(), "relations": set()},
        )

    for row in facts.claims:
        statement = row.accepted.claim.statement
        if row.accepted.claim.lifecycle.state != "live":
            continue
        if statement.subject.artifact_path not in resolved_subject_paths:
            # F3 law (b): the materialized view never carries this Claim, so a
            # discovery count derived from the view alone would understate the
            # accepted facts that were actually read.
            excluded += 1
        predicate = statement.predicate
        if predicate in _DESCRIPTOR_LITERAL_PREDICATES and isinstance(
            statement.object, LiteralClaimObject
        ):
            value = statement.object.value
            if not isinstance(value, str):
                continue
            field = "aliases" if predicate == "semantic.alias" else "tags"
            bucket(statement.subject)[field].add(value)
        elif predicate in _DESCRIPTOR_RELATION_PREDICATES and isinstance(
            statement.object, SubjectClaimObject
        ):
            bucket(statement.subject)["relations"].add(statement.object.address.artifact_path)
            bucket(statement.object.address)["relations"].add(statement.subject.artifact_path)
    return terms, excluded


def _entry_descriptors(
    terms: Mapping[bytes, dict[str, set[str]]],
    address: SemanticAddress,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    found = terms.get(canonical_bytes(address.model_dump(mode="json")))
    if found is None:
        return ((), (), ())
    return (
        byte_sorted(tuple(found["aliases"])),
        byte_sorted(tuple(found["tags"])),
        byte_sorted(tuple(found["relations"])),
    )


def _predicate_tokens(predicate: str) -> tuple[str, ...]:
    return byte_sorted((predicate, predicate.rpartition(".")[2]))


def build_discovery_vocabulary(
    *,
    view: SubjectQueryViewV1,
    facts: ClaimQueryFactsV1,
    claim_types: Iterable[ClaimType] = (),
    definitions: Iterable[AcceptedQueryDefinitionV1] = (),
    procedures: Iterable[AcceptedProcedureV1] = (),
    line_specs: Iterable[AcceptedLineSpecV1] = (),
) -> DiscoveryVocabularyV1:
    """Project the accepted naming layer at one coordinate into discovery entries.

    Subjects come from the F3 materialized view rather than a second walk of the
    accepted facts, so the index and the view cannot disagree about what exists.
    Descriptor terms come from the Claim facts, because the view deliberately
    omits Claims whose statement Subject is absent.
    """

    if canonical_bytes(view.coordinate.model_dump(mode="json")) != canonical_bytes(
        facts.coordinate.model_dump(mode="json")
    ):
        raise DiscoveryError("discovery vocabulary requires one accepted coordinate")
    resolved = frozenset(row.path for row in view.subjects)
    terms, excluded = _descriptor_terms(facts, resolved_subject_paths=resolved)
    entries: list[DiscoveryEntryV1] = []

    for row in view.subjects:
        address = SemanticAddress.whole_artifact(row.path)
        aliases, tags, relations = _entry_descriptors(terms, address)
        entries.append(
            DiscoveryEntryV1(
                kind="Subject",
                address=address,
                identity=row.identity,
                label=row.identity,
                aliases=aliases,
                tags=tags,
                lexical_terms=byte_sorted((row.subject_id, row.subject_kind)),
                structural_signature_digest=subject_reuse_signature(
                    ArtifactIdentity(kind="Subject", name=f"{row.subject_kind}/{row.subject_id}")
                ),
                dependency_addresses=tuple(
                    SemanticAddress.whole_artifact(path) for path in relations
                ),
            )
        )

    for claim_type in claim_types:
        if claim_type.lifecycle.state != "live":
            continue
        address = SemanticAddress.whole_artifact(claim_type_path(claim_type.predicate))
        aliases, tags, relations = _entry_descriptors(terms, address)
        entries.append(
            DiscoveryEntryV1(
                kind="ClaimType",
                address=address,
                identity=claim_type.identity.qualified,
                label=claim_type.predicate,
                aliases=aliases,
                tags=tags,
                lexical_terms=byte_sorted(
                    (*_predicate_tokens(claim_type.predicate), *claim_type.allowed_subject_kinds)
                ),
                structural_signature_digest=claim_type_structural_signature(claim_type.structure),
            )
        )

    for definition in definitions:
        if definition.query.lifecycle.state != "live":
            continue
        address = SemanticAddress.whole_artifact(definition.path)
        aliases, tags, _relations = _entry_descriptors(terms, address)
        entries.append(
            DiscoveryEntryV1(
                kind="QueryDefinition",
                address=address,
                identity=definition.query.identity.qualified,
                label=definition.query.identity.name,
                entrypoint_name=definition.query.identity.name,
                description=definition.query.description,
                aliases=aliases,
                tags=tags,
                lexical_terms=byte_sorted(
                    (
                        definition.query.identity.name,
                        *definition.query.subject_kinds,
                        *definition.query.referenced_predicates,
                    )
                ),
                dependency_addresses=byte_sorted_addresses(
                    SemanticAddress.whole_artifact(claim_type_path(predicate))
                    for predicate in definition.query.referenced_predicates
                ),
            )
        )

    for procedure in procedures:
        if procedure.procedure.lifecycle.state != "live":
            continue
        address = SemanticAddress.procedure_unit(procedure.path)
        aliases, tags, _relations = _entry_descriptors(terms, address)
        entries.append(
            DiscoveryEntryV1(
                kind="Procedure",
                address=address,
                identity=procedure.procedure.identity.qualified,
                label=procedure.procedure.identity.name,
                description=procedure.procedure.definition.description,
                aliases=aliases,
                tags=tags,
                lexical_terms=byte_sorted(
                    (
                        procedure.procedure.identity.name,
                        *(node.node_id for node in procedure.procedure.definition.nodes),
                    )
                ),
            )
        )

    for line_spec in line_specs:
        if line_spec.line.lifecycle.state != "live":
            continue
        address = SemanticAddress.line(line_spec.path)
        aliases, tags, _relations = _entry_descriptors(terms, address)
        entries.append(
            DiscoveryEntryV1(
                kind="LineSpec",
                address=address,
                identity=line_spec.line.identity.qualified,
                label=line_spec.line.identity.name,
                aliases=aliases,
                tags=tags,
                lexical_terms=byte_sorted((line_spec.line.identity.name,)),
                dependency_addresses=(
                    SemanticAddress.procedure_unit(
                        f"procedures/{line_spec.line.procedure.target.name}.json"
                    ),
                ),
            )
        )

    ordered = tuple(sorted(entries, key=lambda item: item.order_key))
    return DiscoveryVocabularyV1(
        at=AcceptedCoordinate.from_internal(view.coordinate),
        entries=_with_reverse_dependencies(ordered),
        excluded_claim_count=excluded,
    )


def byte_sorted_addresses(addresses: Iterable[SemanticAddress]) -> tuple[SemanticAddress, ...]:
    """Return semantic addresses in canonical byte order without duplicates."""

    seen: dict[bytes, SemanticAddress] = {}
    for address in addresses:
        seen[canonical_bytes(address.model_dump(mode="json"))] = address
    return tuple(seen[key] for key in sorted(seen))


def _with_reverse_dependencies(
    entries: tuple[DiscoveryEntryV1, ...],
) -> tuple[DiscoveryEntryV1, ...]:
    """Close the declared dependency edges so a walk works in both directions."""

    dependents: dict[bytes, list[SemanticAddress]] = {}
    for entry in entries:
        for dependency in entry.dependency_addresses:
            dependents.setdefault(canonical_bytes(dependency.model_dump(mode="json")), []).append(
                entry.address
            )
    return tuple(
        entry.model_copy(
            update={
                "dependent_addresses": byte_sorted_addresses(
                    dependents.get(canonical_bytes(entry.address.model_dump(mode="json")), ())
                )
            }
        )
        for entry in entries
    )


# -- matching -------------------------------------------------------------


def _basis(basis: str, matched_text: str | None) -> DiscoveryMatchBasisV1:
    return DiscoveryMatchBasisV1(
        basis=cast(DiscoveryMatchBasis, basis),
        matched_text=matched_text,
    )


def _direct_bases(
    entry: DiscoveryEntryV1,
    *,
    query: str,
    entrypoint: str | None,
) -> tuple[DiscoveryMatchBasisV1, ...]:
    found: dict[tuple[str, str | None], DiscoveryMatchBasisV1] = {}

    def add(basis: str, matched_text: str | None) -> None:
        found[(basis, matched_text)] = _basis(basis, matched_text)

    if entrypoint is not None:
        if entry.entrypoint_name == entrypoint:
            add("named_entrypoint", entrypoint)
        return tuple(found.values())
    normalized = normalize_discovery_term(query)
    if query in {entry.address.artifact_path, entry.identity}:
        add("exact_address", query)
    if entry.entrypoint_name is not None and normalized == normalize_discovery_term(
        entry.entrypoint_name
    ):
        add("named_entrypoint", entry.entrypoint_name)
    if normalized in entry.normalized_aliases:
        add("exact_alias", normalized)
    if entry.structural_signature_digest is not None and query == entry.structural_signature_digest:
        add("structural_signature", query)
    if normalized in entry.normalized_tags:
        add("tag", normalized)
    tokens = discovery_tokens(query)
    if tokens and set(tokens).issubset(entry.token_set):
        add("lexical", normalized)
    return tuple(
        sorted(
            found.values(),
            key=lambda item: (
                MATCH_BASIS_PRIORITY[item.basis],
                (item.matched_text or "").encode("utf-8"),
            ),
        )
    )


def _hit_priority(bases: tuple[DiscoveryMatchBasisV1, ...]) -> int:
    return min(MATCH_BASIS_PRIORITY[item.basis] for item in bases)


def _match_entries(
    vocabulary: DiscoveryVocabularyV1,
    *,
    query: str,
    entrypoint: str | None,
    kinds: frozenset[str],
) -> tuple[tuple[DiscoveryEntryV1, tuple[DiscoveryMatchBasisV1, ...]], ...]:
    direct: dict[bytes, tuple[DiscoveryEntryV1, list[DiscoveryMatchBasisV1]]] = {}
    for entry in vocabulary.entries:
        bases = _direct_bases(entry, query=query, entrypoint=entrypoint)
        if not bases:
            continue
        direct[canonical_bytes(entry.address.model_dump(mode="json"))] = (entry, list(bases))

    # One deterministic hop from an exactly addressed item: it advertises what it
    # depends on and what depends on it, so the next read is never a guess. A
    # named entrypoint does not seed the walk -- its handle already carries the
    # dependency addresses, and re-listing them as hits would spend the budget on
    # material the caller already holds.
    walked: set[bytes] = set()
    for entry, matched in tuple(direct.values()):
        if not any(item.basis == "exact_address" for item in matched):
            continue
        for address in (*entry.dependency_addresses, *entry.dependent_addresses):
            walked.add(canonical_bytes(address.model_dump(mode="json")))
    for candidate in vocabulary.entries:
        key = canonical_bytes(candidate.address.model_dump(mode="json"))
        if key not in walked:
            continue
        existing = direct.get(key)
        if existing is None:
            direct[key] = (candidate, [_basis("dependency_walk", None)])
        else:
            existing[1].append(_basis("dependency_walk", None))

    hits = [
        (entry, tuple(sorted(bases, key=lambda item: MATCH_BASIS_PRIORITY[item.basis])))
        for entry, bases in direct.values()
        if entry.kind in kinds
    ]
    return tuple(
        sorted(
            hits,
            key=lambda item: (_hit_priority(item[1]), *item[0].order_key),
        )
    )


# -- the discovery page ---------------------------------------------------


def _selection_basis_digest(
    request: DiscoveryRequestV1,
    *,
    vocabulary_digest: str,
) -> str:
    return typed_digest(
        Sha256Value,
        SELECTION_BASIS_DIGEST_DOMAIN,
        {
            "at": request.at.model_dump(mode="json"),
            "budget": request.budget.model_dump(mode="json"),
            "entrypoint": request.entrypoint,
            "evaluation_time": request.evaluation_time,
            "profile": request.profile,
            "query": request.query,
            "vocabulary_digest": vocabulary_digest,
        },
    ).tagged


def _hit(
    entry: DiscoveryEntryV1,
    bases: tuple[DiscoveryMatchBasisV1, ...],
    *,
    at: AcceptedCoordinate,
) -> DiscoveryHitV1:
    return DiscoveryHitV1(
        address=entry.address,
        at=at,
        kind=entry.kind,
        label=entry.label,
        aliases=entry.aliases,
        tags=entry.tags,
        match_basis=bases,
        role=None,
        verdict=None,
        currency="not_applicable",
        dependency_addresses=entry.dependency_addresses,
        dependent_addresses=entry.dependent_addresses,
    )


def _hit_bytes(hits: tuple[DiscoveryHitV1, ...]) -> int:
    return len(canonical_bytes([item.model_dump(mode="json") for item in hits]))


def discover(
    request: DiscoveryRequestV1,
    *,
    vocabulary: DiscoveryVocabularyV1,
) -> DiscoveryPageV1:
    """Answer one exact/lexical discovery request without writing anything.

    The page is a pure function of the request and the accepted vocabulary
    digest, so the same coordinate always yields byte-identical hits. Budgets
    clip only from the low-priority tail, and every clip is stated: a silently
    narrowed page is unrepresentable.
    """

    if not isinstance(request.at, AcceptedCoordinate):
        raise DiscoveryError("PC-F discovery accepts only verified accepted coordinates")
    if request.at != vocabulary.at:
        raise DiscoveryError("discovery request coordinate differs from the built vocabulary")
    if request.query is not None and not request.query.strip():
        raise DiscoveryError("a blank discovery query is refused; list entrypoints explicitly")
    if request.entrypoint is not None:
        reject_locator_or_secret(request.entrypoint, label="discovery entrypoint")
    if request.query is not None:
        reject_locator_or_secret(request.query, label="discovery query")

    kinds = frozenset(DISCOVERY_PROFILE_KINDS[request.profile])
    matched = _match_entries(
        vocabulary,
        query=request.query or "",
        entrypoint=request.entrypoint,
        kinds=kinds,
    )
    hits = tuple(_hit(entry, bases, at=request.at) for entry, bases in matched)

    truncated: set[str] = set()
    reasons: set[str] = set()
    if len(hits) > request.budget.max_hits:
        hits = hits[: request.budget.max_hits]
        truncated.add("hits")
        reasons.add("hit_budget_exceeded")
    while hits and _hit_bytes(hits) > request.budget.max_bytes:
        hits = hits[:-1]
        truncated.add("hits")
        reasons.add("byte_budget_exceeded")

    alias_targets = tuple(
        entry for entry, bases in matched if any(item.basis == "exact_alias" for item in bases)
    )
    if len(alias_targets) > 1:
        # An alias that validly names several subjects is an ambiguity to show,
        # never a choice for the server to make.
        reasons.add("alias_ambiguous")

    available = byte_sorted(tuple({hit.kind for hit in hits}))
    coverage = CoverageDescriptorV1(
        requested_facets=byte_sorted(DISCOVERY_PROFILE_KINDS[request.profile]),
        available_facets=available,
        truncated_facets=byte_sorted(tuple(truncated)),
        reason_codes=byte_sorted(tuple(reasons)),
    )
    selection_basis_digest = _selection_basis_digest(
        request,
        vocabulary_digest=discovery_vocabulary_digest(vocabulary),
    )
    receipt_digest = typed_digest(
        Sha256Value,
        PAGE_RECEIPT_DIGEST_DOMAIN,
        {
            "coverage": coverage.model_dump(mode="json"),
            "hits": [item.model_dump(mode="json") for item in hits],
            "selection_basis_digest": selection_basis_digest,
        },
    ).tagged
    return DiscoveryPageV1(
        coordinate_kind="accepted",
        at=request.at,
        evaluation_time=request.evaluation_time,
        hits=hits,
        selection_basis_digest=selection_basis_digest,
        receipt_digest=receipt_digest,
        coverage=coverage,
    )


def resolved_equivalence_address(page: DiscoveryPageV1) -> SemanticAddress | None:
    """Return the one address an equivalence-grade basis resolved, or None.

    A page whose only hits came from tags, lexical tokens, structural signatures,
    or a dependency walk resolves nothing: those bases broaden recall and are
    rendered for review. An ambiguous alias resolves nothing either, because
    choosing between two validly named targets is not the server's call.
    """

    resolved = byte_sorted_addresses(
        hit.address
        for hit in page.hits
        if any(MATCH_BASIS_RESOLVES_EQUIVALENCE[item.basis] for item in hit.match_basis)
    )
    return resolved[0] if len(resolved) == 1 else None


__all__ = [
    "DISCOVERY_ENTRY_KINDS",
    "DISCOVERY_PROFILE_KINDS",
    "MATCH_BASIS_PRIORITY",
    "MATCH_BASIS_RESOLVES_EQUIVALENCE",
    "PAGE_RECEIPT_DIGEST_DOMAIN",
    "SELECTION_BASIS_DIGEST_DOMAIN",
    "VOCABULARY_DIGEST_DOMAIN",
    "DiscoveryEntryV1",
    "DiscoveryError",
    "DiscoveryVocabularyV1",
    "build_discovery_vocabulary",
    "byte_sorted_addresses",
    "discover",
    "discovery_tokens",
    "discovery_vocabulary_digest",
    "resolved_equivalence_address",
]
