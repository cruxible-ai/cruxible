"""Deterministic greppable file floor projected from accepted Playbill state.

This is the pre-OKF floor: a plain, byte-stable rendering of the F5 projection
artifacts (ClaimType cards, Subject profiles) and the accepted Documents, plus a
root manifest that binds every file to the accepted coordinate it came from.

The service writes nothing. It returns a path-to-bytes map that is a pure
function of the accepted coordinate, so the same coordinate always materializes
byte-identical files.

§11.7 makes the file-based context floor half of the reference coverage
surface, so the floor also carries its own coverage boundary: a
`coverage-manifest.json` naming the accepted coordinate, the evidence-index
generation, and exactly which logical sources accepted evidence cites there. It
is enumerated in the root manifest like every other floor file. An exported
floor observes no working snapshot, so it carries no epoch and proves no
freshness -- reading the boundary tells you what a coverage answer *could* be
about, and only the resolver can tell you what it *is*.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from cruxible_core.playbill.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.claim_types import claim_type_path, parse_claim_type
from cruxible_core.playbill.claims import ClaimArtifactAny
from cruxible_core.playbill.coverage.contracts import (
    CoverageManifestProfileV1,
    CoverageManifestProfileV2,
)
from cruxible_core.playbill.coverage.indexes import evidence_citation_index_digest
from cruxible_core.playbill.errors import ProposalIntegrityError
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedProjectionCoordinate
from cruxible_core.playbill.query.cards import (
    ClaimTypeUsageRowV1,
    SemanticRelationV1,
    build_claim_type_card,
    build_subject_profile,
    descriptor_relations,
)
from cruxible_core.playbill.query.semantic_discovery import DiscoveryEntryV1
from cruxible_core.playbill.semantic import SemanticAddress
from cruxible_core.playbill.service.documents import (
    PlaybillAcceptedCoordinate,
    service_list_playbill_documents,
)
from cruxible_core.playbill.source_readers import ExternalSourceReaderProtocol
from cruxible_core.playbill.subjects import parse_subject, subject_digest
from cruxible_core.service.playbill_claims import (
    _claim_from_view,
    service_list_playbill_claims,
)
from cruxible_core.service.playbill_coverage import (
    COVERAGE_ACCESS_PROFILE_ID,
    accepted_evidence_sources,
    build_accepted_evidence_index_v2,
)
from cruxible_core.service.playbill_discovery import (
    accepted_claim_types,
    build_accepted_discovery_vocabulary,
)
from cruxible_core.service.playbill_query import build_accepted_query_facts

FLOOR_FORMAT = "playbill-floor-export-v1"
MANIFEST_PATH = "manifest.json"
COVERAGE_MANIFEST_PATH = "coverage-manifest.json"
FLOOR_DIGEST_DOMAIN = "playbill-floor-export-v1"
DEFAULT_FLOOR_PRINCIPAL = "playbill-floor"
SUBJECT_PATH_PREFIX = "subjects/"

RelationIndex = Mapping[bytes, tuple[SemanticRelationV1, ...]]


class _StrictFloorModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlaybillFloorFileV1(_StrictFloorModel):
    """One materialized floor file bound to its exact content digest."""

    path: str
    content_digest: str
    byte_length: int


class PlaybillFloorManifestV1(_StrictFloorModel):
    """The root manifest binding one floor materialization to its coordinate."""

    tag: Literal["playbill-floor-manifest-v1"] = "playbill-floor-manifest-v1"
    format: Literal["playbill-floor-export-v1"] = "playbill-floor-export-v1"
    coordinate: PlaybillAcceptedCoordinate
    files: tuple[PlaybillFloorFileV1, ...]
    floor_digest: str


class PlaybillFloorCoverageManifestV1(CoverageManifestProfileV1):
    """The coverage boundary an exported floor carries with it (§11.6.3, §11.7).

    A **profile of the coverage-manifest family**: every §11.6.3 field is
    inherited from :class:`CoverageManifestProfileV1` rather than restated, so
    the floor boundary and every later profile cannot drift apart into two
    schemas. What this profile adds is the two counts that say how much accepted
    evidence the boundary was computed over.

    ``epoch`` and per-source commitments are absent by construction: an export
    observes no working snapshot, so it has nothing to be fresh *against*.
    ``watcher_health`` is `absent` for the same reason, and the floor alone
    therefore proves no `exact` match. Both are enforced below rather than
    narrowed in the annotation, because the family keeps one type per field.
    This is the §11.6.3 completeness boundary, stated in a file, so an agent
    reading the floor without a daemon knows exactly what a `none` would and
    would not have meant.
    """

    tag: Literal["playbill-floor-coverage-manifest-v1"] = "playbill-floor-coverage-manifest-v1"
    cited_commitment_count: int
    exact_bytes_commitment_count: int

    @model_validator(mode="after")
    def _export_observes_no_snapshot(self) -> "PlaybillFloorCoverageManifestV1":
        if self.epoch is not None or self.watcher_health != "absent":
            raise ValueError("an exported floor observes no working snapshot and proves no epoch")
        return self


class PlaybillFloorCoverageManifestV2(CoverageManifestProfileV2):
    """Association-native coverage boundary for a point-in-time floor export."""

    tag: Literal["playbill-floor-coverage-manifest-v2"] = "playbill-floor-coverage-manifest-v2"
    cited_commitment_count: int
    exact_bytes_commitment_count: int

    @model_validator(mode="after")
    def _export_observes_no_snapshot(self) -> "PlaybillFloorCoverageManifestV2":
        if self.epoch is not None or self.watcher_health != "absent":
            raise ValueError("an exported floor observes no working snapshot and proves no epoch")
        return self


def _resolve_coordinate(
    instance: PlaybillInstance,
    at: PlaybillAcceptedCoordinate | None,
) -> AcceptedProjectionCoordinate:
    if at is None:
        return instance.accepted_coordinate()
    return instance.resolve_accepted_coordinate(
        git_oid=at.git_oid,
        semantic_root=at.semantic_root,
        generation_root=at.generation_root,
        compiler_digest=at.compiler_digest,
    )


def _render(payload: object) -> bytes:
    return canonical_bytes(payload) + b"\n"


def _content_digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _relations_for(
    relations: RelationIndex, address: SemanticAddress
) -> tuple[SemanticRelationV1, ...]:
    return relations.get(canonical_bytes(address.model_dump(mode="json")), ())


def _subject_identity(tree: Mapping[str, bytes], path: str) -> str | None:
    content = tree.get(path)
    return None if content is None else parse_subject(content, path=path).identity.qualified


def _entry_index(entries: tuple[DiscoveryEntryV1, ...]) -> dict[bytes, DiscoveryEntryV1]:
    return {canonical_bytes(entry.address.model_dump(mode="json")): entry for entry in entries}


def _claim_type_cards(
    tree: Mapping[str, bytes],
    *,
    entries: Mapping[bytes, DiscoveryEntryV1],
    at: PlaybillAcceptedCoordinate,
    claims: tuple[ClaimArtifactAny, ...],
    relations: RelationIndex,
) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for claim_type in accepted_claim_types(tree):
        address = SemanticAddress.whole_artifact(claim_type_path(claim_type.predicate))
        entry = entries.get(canonical_bytes(address.model_dump(mode="json")))
        if entry is None:
            continue
        usage_rows = tuple(
            ClaimTypeUsageRowV1(
                subject_path=claim.statement.subject.artifact_path,
                subject_identity=identity,
            )
            for claim in claims
            if claim.statement.predicate == claim_type.predicate
            for identity in (_subject_identity(tree, claim.statement.subject.artifact_path),)
            if identity is not None
        )
        card = build_claim_type_card(
            claim_type,
            at=at,
            entry=entry,
            usage_rows=usage_rows,
            relations=_relations_for(relations, address),
        )
        path = claim_type_path(claim_type.predicate).removesuffix(".yaml") + ".card.json"
        files[path] = _render(card.model_dump(mode="json"))
    return files


def _subject_profiles(
    tree: Mapping[str, bytes],
    *,
    entries: Mapping[bytes, DiscoveryEntryV1],
    at: PlaybillAcceptedCoordinate,
    claims: tuple[ClaimArtifactAny, ...],
    relations: RelationIndex,
) -> dict[str, bytes]:
    cardinalities: dict[str, str] = {}
    for claim in claims:
        contract_path = claim_type_path(claim.statement.predicate)
        content = tree.get(contract_path)
        if content is not None:
            cardinalities[claim.statement.predicate] = parse_claim_type(
                content, path=contract_path
            ).cardinality
    files: dict[str, bytes] = {}
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not path.startswith(SUBJECT_PATH_PREFIX):
            continue
        shell = parse_subject(tree[path], path=path)
        address = SemanticAddress.whole_artifact(path)
        entry = entries.get(canonical_bytes(address.model_dump(mode="json")))
        if entry is None:
            continue
        profile = build_subject_profile(
            at=at,
            entry=entry,
            subject_kind=shell.subject_kind,
            subject_id=shell.subject_id,
            artifact_digest=subject_digest(shell).tagged,
            claims=tuple(claim for claim in claims if claim.statement.subject == address),
            cardinalities=cardinalities,
            relations=_relations_for(relations, address),
        )
        floor_path = f"{SUBJECT_PATH_PREFIX}{shell.subject_kind}/{shell.subject_id}.profile.json"
        files[floor_path] = _render(profile.model_dump(mode="json"))
    return files


def _coverage_manifest(
    instance: PlaybillInstance,
    *,
    at: PlaybillAcceptedCoordinate,
) -> PlaybillFloorCoverageManifestV2:
    """Summarize the coverage boundary of this export from the evidence index.

    Only identities, digests, and counts leave here. The index is built over
    accepted Capture envelopes, but no evidence bytes, no body content, and no
    selection material reaches the floor, so the boundary is publishable at the
    same access class the floor itself already is.
    """

    index = build_accepted_evidence_index_v2(instance, at=at)
    return PlaybillFloorCoverageManifestV2(
        instance_id=instance.descriptor.instance_id,
        coordinate=at,
        index_digest=evidence_citation_index_digest(index),
        access_profile_id=COVERAGE_ACCESS_PROFILE_ID,
        completeness="partial" if index.truncated else "complete",
        truncation_reason_codes=("evidence_index_truncated",) if index.truncated else (),
        scope=accepted_evidence_sources(index),
        cited_commitment_count=len(index.citations),
        exact_bytes_commitment_count=sum(
            1 for citation in index.citations if citation.digest_kind == "exact_bytes"
        ),
    )


def _documents(
    instance: PlaybillInstance,
    *,
    at: PlaybillAcceptedCoordinate,
    access: BodyAccessContext,
) -> dict[str, bytes]:
    listing = service_list_playbill_documents(instance, access=access, at=at)
    return {
        f"documents/{document.envelope['path']}.json": _render(document.envelope)
        for document in listing.documents
    }


def service_export_playbill_floor(
    instance: PlaybillInstance,
    *,
    at: PlaybillAcceptedCoordinate | None = None,
    access: BodyAccessContext | None = None,
    external_readers: Mapping[str, ExternalSourceReaderProtocol] | None = None,
) -> dict[str, bytes]:
    """Materialize the accepted floor as a deterministic path-to-bytes map.

    The map is keyed by byte-sorted floor path, and its root ``manifest.json``
    names the accepted coordinate together with every file's content digest.
    Cards and profiles are taken without an evaluation time: the floor is
    coordinate-pure accepted structure, never a verdict-relative read.
    """

    if at is not None and not isinstance(at, PlaybillAcceptedCoordinate):
        raise ProposalIntegrityError("floor export accepts only verified accepted coordinates")
    coordinate = _resolve_coordinate(instance, at)
    accepted = PlaybillAcceptedCoordinate.from_internal(coordinate)
    body_access = access or BodyAccessContext(principal_id=DEFAULT_FLOOR_PRINCIPAL)

    tree = instance.tree_at(coordinate.git_oid)
    facts = build_accepted_query_facts(
        instance,
        coordinate=coordinate,
        external_readers=external_readers,
    )
    vocabulary = build_accepted_discovery_vocabulary(
        instance,
        coordinate=coordinate,
        facts=facts,
    )
    entries = _entry_index(vocabulary.entries)
    claims = tuple(
        _claim_from_view(view)
        for view in service_list_playbill_claims(instance, at=accepted).claims
    )
    relations = descriptor_relations(claims)

    files: dict[str, bytes] = {}
    files.update(
        _claim_type_cards(tree, entries=entries, at=accepted, claims=claims, relations=relations)
    )
    files.update(
        _subject_profiles(tree, entries=entries, at=accepted, claims=claims, relations=relations)
    )
    files.update(_documents(instance, at=accepted, access=body_access))
    files[COVERAGE_MANIFEST_PATH] = _render(
        _coverage_manifest(instance, at=accepted).model_dump(mode="json")
    )

    ordered = {path: files[path] for path in sorted(files, key=lambda item: item.encode("utf-8"))}
    inventory = tuple(
        PlaybillFloorFileV1(
            path=path,
            content_digest=_content_digest(content),
            byte_length=len(content),
        )
        for path, content in ordered.items()
    )
    manifest = PlaybillFloorManifestV1(
        coordinate=accepted,
        files=inventory,
        floor_digest=typed_digest(
            Sha256Value,
            FLOOR_DIGEST_DOMAIN,
            {"files": [item.model_dump(mode="json") for item in inventory]},
        ).tagged,
    )
    return {MANIFEST_PATH: _render(manifest.model_dump(mode="json")), **ordered}


__all__ = [
    "COVERAGE_MANIFEST_PATH",
    "MANIFEST_PATH",
    "PlaybillFloorCoverageManifestV1",
    "PlaybillFloorCoverageManifestV2",
    "PlaybillFloorFileV1",
    "PlaybillFloorManifestV1",
    "service_export_playbill_floor",
]
