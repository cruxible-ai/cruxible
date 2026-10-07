"""Deterministic greppable file floor projected from accepted Cruxible state.

This is the pre-OKF floor: a plain, byte-stable rendering of accepted state,
plus a root manifest that binds every file to the accepted coordinate it came
from. Format v5 is the grep-first layer an agent reads
(``current/<kind>/<id>.yaml`` values, see ``floor_current``) and keeps every
digest and address out of it, in the manifest. It holds no source content:
Document and evidence bodies stay behind ``get``. The F5 projection artifacts
(ClaimType cards, Subject profiles) are an opt-in part for the tools that read
them.

The service writes nothing. It returns a path-to-bytes map that is a pure
function of the accepted coordinate and the explicitly pinned review notes
snapshot. The same inputs always materialize byte-identical files.

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
import json
from collections import defaultdict
from collections.abc import Mapping
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, model_validator

from cruxible_client.contracts import FloorExportPart
from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_types import claim_type_path, parse_claim_type
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
)
from cruxible_client.contracts.errors import ProjectionIntegrityError, ProposalIntegrityError
from cruxible_client.contracts.floor import (
    FloorManifest,
    build_floor_manifest,
    render_floor_manifest,
)
from cruxible_client.contracts.primitives import pretty_json
from cruxible_client.contracts.procedures.artifacts import (
    ProcedureArtifact,
    ProcedureRunnable,
    procedure_runnability,
)
from cruxible_client.contracts.procedures.models import (
    RUNG_AUTHORITY,
    ProcedureBudget,
    ProcedureHardCaps,
    ProcedurePinSlotRef,
)
from cruxible_client.contracts.projection_extensions import (
    ProjectionFact,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import parse_subject, subject_digest
from cruxible_core.coverage.contracts import CoverageManifestProfileV2
from cruxible_core.coverage.indexes import evidence_citation_index_digest
from cruxible_core.derived.memo import memo_get, memo_put
from cruxible_core.evidence.source_readers import ExternalSourceReaderProtocol
from cruxible_core.indexes.projection import (
    AcceptedProjectionCoordinate,
)
from cruxible_core.query.cards import (
    ClaimTypeUsageRowV1,
    SemanticRelationV1,
    build_claim_type_card,
    build_subject_profile,
    descriptor_relations,
)
from cruxible_core.query.semantic_discovery import DiscoveryEntryV1
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import (
    AcceptedCoordinate,
)
from cruxible_core.service.discovery.coverage import (
    COVERAGE_ACCESS_PROFILE_ID,
    accepted_evidence_sources,
    build_accepted_evidence_index_v2,
)
from cruxible_core.service.discovery.discovery import (
    accepted_claim_types,
    build_accepted_discovery_vocabulary,
)
from cruxible_core.service.discovery.query import _AcceptedQueryFactsRead
from cruxible_core.service.floor.floor_current import (
    bodies_unchanged,
    body_available,
)
from cruxible_core.service.floor.floor_index import floor_render_at
from cruxible_core.service.floor.renderer import floor_renderer
from cruxible_core.storage.cas import BodyAccessContext

MANIFEST_PATH = "manifest.json"
COVERAGE_MANIFEST_PATH = "coverage-manifest.json"
DEFAULT_FLOOR_PRINCIPAL = "playbill-floor"
SUBJECT_PATH_PREFIX = "subjects/"

RelationIndex = Mapping[bytes, tuple[SemanticRelationV1, ...]]


class _StrictFloorModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


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


class PlaybillProcedureInputContractV1(_StrictFloorModel):
    """The run input planes a Procedure declares, without resolving open slots."""

    input: ArtifactPin | ProcedurePinSlotRef
    parameters: ArtifactPin | ProcedurePinSlotRef | None = None


class PlaybillProcedureCapabilitiesV1(_StrictFloorModel):
    """Compact execution shape used when discovering a Procedure."""

    node_kinds: tuple[str, ...]
    # The most this Procedure's terminals can do: observe, propose or settle.
    authority: Literal["observe", "propose", "settle"]


class PlaybillProcedureGovernanceV1(_StrictFloorModel):
    """Lifecycle and activation policy, kept independent from operational evidence."""

    activation_policy: Literal["drain", "abort", "snapshot", "epoch-check"]
    lifecycle: ArtifactLifecycle


class PlaybillProcedureTrackRecordEntryV1(_StrictFloorModel):
    """One accepted promotion fact, never an observation from live exhaust."""

    fact_key: str
    value: object


class PlaybillProcedureFloorCardV1(_StrictFloorModel):
    """Frozen discovery shape for one accepted Procedure."""

    tag: Literal["playbill-procedure-floor-card-v1"] = "playbill-procedure-floor-card-v1"
    identity: ArtifactIdentity
    path: str
    artifact_digest: str
    accepted_coordinate: AcceptedCoordinate
    input_contract: PlaybillProcedureInputContractV1
    output_contract: ArtifactPin | ProcedurePinSlotRef
    runnable: ProcedureRunnable
    capabilities: PlaybillProcedureCapabilitiesV1
    budget: ProcedureBudget
    hard_caps: ProcedureHardCaps
    governance: PlaybillProcedureGovernanceV1
    track_record: tuple[PlaybillProcedureTrackRecordEntryV1, ...]


def _resolve_coordinate(
    instance: PlaybillInstance,
    at: AcceptedCoordinate | None,
) -> AcceptedProjectionCoordinate:
    if at is None:
        return instance.accepted_coordinate()
    return instance.resolve_accepted_coordinate(
        git_oid=at.git_oid,
        semantic_root=at.semantic_root,
        generation_root=at.generation_root,
        compiler_digest=at.compiler_digest,
    )


def render_floor_json_v2(payload: object) -> bytes:
    """Render one canonical JSON value as deterministic, greppable UTF-8."""

    value = json.loads(canonical_bytes(payload))
    return pretty_json(value).encode("utf-8") + b"\n"


def _render(payload: object) -> bytes:
    return render_floor_json_v2(payload)


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
    at: AcceptedCoordinate,
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
        path = claim_type_path(claim_type.predicate).removesuffix(".json") + ".card.json"
        files[path] = _render(card.model_dump(mode="json"))
    return files


def _subject_profiles(
    tree: Mapping[str, bytes],
    *,
    entries: Mapping[bytes, DiscoveryEntryV1],
    at: AcceptedCoordinate,
    claims: tuple[ClaimArtifactAny, ...],
    relations: RelationIndex,
) -> dict[str, bytes]:
    cardinalities: dict[str, str] = {}
    grouped: dict[bytes, list[ClaimArtifactAny]] = defaultdict(list)
    for claim in claims:
        grouped[canonical_bytes(claim.statement.subject.model_dump(mode="json"))].append(claim)
        contract_path = claim_type_path(claim.statement.predicate)
        content = tree.get(contract_path)
        if content is not None and claim.statement.predicate not in cardinalities:
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
            claims=tuple(grouped.get(canonical_bytes(address.model_dump(mode="json")), ())),
            cardinalities=cardinalities,
            relations=_relations_for(relations, address),
        )
        floor_path = f"{SUBJECT_PATH_PREFIX}{shell.subject_kind}/{shell.subject_id}.profile.json"
        files[floor_path] = _render(profile.model_dump(mode="json"))
    return files


def _coverage_manifest(
    instance: PlaybillInstance,
    *,
    at: AcceptedCoordinate,
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


def _procedure_track_records(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
) -> dict[str, tuple[ProjectionFact, ...]]:
    """Read only accepted, promoted track-record facts at this coordinate."""

    records: dict[str, list[ProjectionFact]] = {}
    with instance.bind_accepted_projection(coordinate) as projection:
        for fact in projection.typed.facts("cruxible.procedure.track_record"):
            records.setdefault(fact.subject_identity, []).append(fact)
    return {
        identity: tuple(sorted(facts, key=lambda item: item.fact_key.encode("utf-8")))
        for identity, facts in records.items()
    }


def _procedure_cards(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    at: AcceptedCoordinate,
) -> dict[str, bytes]:
    with instance.bind_accepted_projection(coordinate) as projection:
        rows = sorted(projection.typed.envelopes(kind="procedure"), key=lambda row: row.path)
        procedures = tuple((row, projection.typed.source(row.identity)) for row in rows)
    if not procedures:
        return {}
    track_records = _procedure_track_records(instance, coordinate=coordinate)
    files: dict[str, bytes] = {}
    for row, procedure in procedures:
        if not isinstance(procedure, ProcedureArtifact):
            raise ProjectionIntegrityError("Procedure floor source is unavailable")
        path = row.path
        definition = procedure.definition
        card = PlaybillProcedureFloorCardV1(
            identity=procedure.identity,
            path=path,
            artifact_digest=row.artifact_digest,
            accepted_coordinate=at,
            input_contract=PlaybillProcedureInputContractV1(
                input=definition.contract_in,
                parameters=definition.parameter_contract,
            ),
            output_contract=definition.contract_out,
            runnable=procedure_runnability(definition)[0],
            capabilities=PlaybillProcedureCapabilitiesV1(
                node_kinds=tuple(
                    sorted({node.kind for node in definition.nodes}, key=lambda item: item.encode())
                ),
                authority=RUNG_AUTHORITY[definition.terminal_capability],
            ),
            budget=definition.budget,
            hard_caps=definition.hard_caps,
            governance=PlaybillProcedureGovernanceV1(
                activation_policy=procedure.activation_policy,
                lifecycle=procedure.lifecycle,
            ),
            track_record=tuple(
                PlaybillProcedureTrackRecordEntryV1(
                    fact_key=fact.fact_key,
                    value=fact.value,
                )
                for fact in track_records.get(procedure.identity.qualified, ())
            ),
        )
        floor_path = path.removesuffix(".json") + ".card.json"
        files[floor_path] = _render(card.model_dump(mode="json"))
    return files


def _cited_captures(
    instance: PlaybillInstance, coordinate: AcceptedProjectionCoordinate
) -> tuple[str, ...]:
    """Every Capture an accepted Claim cites: the envelopes the coverage boundary reads."""

    with instance.bind_accepted_projection(coordinate) as projection:
        rows = projection.citations._rows(
            "SELECT DISTINCT capture_digest FROM citation_uses WHERE owner_kind='Claim' "
            "ORDER BY capture_digest"
        )
    return tuple(str(row["capture_digest"]) for row in rows)


def _discovery_layer(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    accepted: AcceptedCoordinate,
    external_readers: Mapping[str, ExternalSourceReaderProtocol] | None,
) -> tuple[dict[str, bytes], tuple[ClaimArtifactAny, ...]]:
    """The discovery cards: ClaimType cards, Subject profiles, Procedure cards, coverage.

    These carry the F5 projection shapes other tools read, digests and
    addresses included, and they need the whole accepted facts read. They are
    the frozen v2 layout, and an opt-in part of v4.
    """

    with instance.bind_accepted_projection(coordinate) as projection:
        paths = tuple(
            row.path
            for kind in ("claim-type", "subject")
            for row in projection.typed.envelopes(kind=kind)
        )
        projection.typed.prefetch_members(paths)
        tree = {path: projection.typed.member_bytes(path) for path in paths}
    read = _AcceptedQueryFactsRead(
        instance,
        coordinate=coordinate,
        external_readers=external_readers,
    )
    facts = read.build()
    vocabulary = build_accepted_discovery_vocabulary(
        instance,
        coordinate=coordinate,
        facts=facts,
    )
    entries = _entry_index(vocabulary.entries)
    claims = read.live_claims()
    relations = descriptor_relations(claims)
    files: dict[str, bytes] = {}
    files.update(
        _claim_type_cards(tree, entries=entries, at=accepted, claims=claims, relations=relations)
    )
    files.update(
        _subject_profiles(tree, entries=entries, at=accepted, claims=claims, relations=relations)
    )
    files.update(_procedure_cards(instance, coordinate=coordinate, at=accepted))
    files[COVERAGE_MANIFEST_PATH] = _render(
        _coverage_manifest(instance, at=accepted).model_dump(mode="json")
    )
    return files, claims


def _discovery_files(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    accepted: AcceptedCoordinate,
    access: BodyAccessContext,
    external_readers: Mapping[str, ExternalSourceReaderProtocol] | None,
) -> dict[str, bytes]:
    """The discovery cards, kept while every Capture they cite answers as it did."""

    structure_key = (coordinate.git_oid, access.principal_id, access.can_read_body)
    structure = None if external_readers else memo_get(instance.floor_structure_memo, structure_key)
    if isinstance(structure, tuple) and bodies_unchanged(instance, structure[1]):
        kept: dict[str, bytes] = structure[0]
        return kept.copy()
    # Bracket the layer's Capture reads: a Capture that answered one way
    # before and another after has no answer to record, so the cards built
    # over it are never reused.
    cited = _cited_captures(instance, coordinate)
    before = {digest: body_available(instance, digest) for digest in cited}
    files, _claims = _discovery_layer(
        instance, coordinate=coordinate, accepted=accepted, external_readers=external_readers
    )
    captures = tuple(
        (digest, held if held == body_available(instance, digest) else None)
        for digest, held in before.items()
    )
    if not external_readers and sum(map(len, files.values())) <= 32 * 1024 * 1024:
        memo_put(instance.floor_structure_memo, structure_key, (files.copy(), captures), capacity=2)
    return files


def service_export_playbill_floor(
    instance: PlaybillInstance,
    *,
    at: AcceptedCoordinate | None = None,
    include: tuple[FloorExportPart, ...] = (),
    access: BodyAccessContext | None = None,
    external_readers: Mapping[str, ExternalSourceReaderProtocol] | None = None,
) -> dict[str, bytes]:
    """Materialize the accepted floor as a deterministic path-to-bytes map.

    The map is keyed by byte-sorted floor path, and its root ``manifest.json``
    names the accepted coordinate together with every file's content digest
    and the generation each file last changed.

    The floor is grep-first: ``current/``, ``changes/`` and the README, read
    from the instance's floor index. ``include=("discovery",)`` adds the
    discovery cards (``subjects/``, ``claim-types/``, ``procedures/`` and
    ``coverage-manifest.json``), stamped with the export's own generation.
    Cards and profiles are taken without an evaluation time.
    """

    if at is not None and not isinstance(at, AcceptedCoordinate):
        raise ProposalIntegrityError("floor export accepts only verified accepted coordinates")
    coordinate = _resolve_coordinate(instance, at)
    accepted = AcceptedCoordinate.from_internal(coordinate)
    body_access = access or BodyAccessContext(principal_id=DEFAULT_FLOOR_PRINCIPAL)

    unknown = sorted(set(include) - set(get_args(FloorExportPart)))
    if unknown:
        raise ValueError(f"unsupported floor export part(s): {', '.join(unknown)}")
    parts = tuple(sorted(set(include)))

    render = floor_render_at(instance, coordinate)
    files = dict(render.files)
    if "discovery" in parts:
        for path, content in _discovery_files(
            instance,
            coordinate=coordinate,
            accepted=accepted,
            access=body_access,
            external_readers=external_readers,
        ).items():
            files[path] = (content, render.generation)
    manifest = build_floor_manifest(
        renderer=render.renderer,
        coordinate=render.inputs.coordinate,
        generation=render.generation,
        notes_digest=render.notes_digest,
        files={
            path: (_content_digest(content), len(content), changed)
            for path, (content, changed) in files.items()
        },
    )
    return {
        MANIFEST_PATH: render_floor_manifest(manifest),
        **{item.path: files[item.path][0] for item in manifest.files},
    }


__all__ = [
    "COVERAGE_MANIFEST_PATH",
    "MANIFEST_PATH",
    "PlaybillFloorCoverageManifestV2",
    "FloorManifest",
    "PlaybillProcedureCapabilitiesV1",
    "PlaybillProcedureFloorCardV1",
    "PlaybillProcedureGovernanceV1",
    "PlaybillProcedureInputContractV1",
    "PlaybillProcedureTrackRecordEntryV1",
    "floor_renderer",
    "render_floor_json_v2",
    "service_export_playbill_floor",
]
