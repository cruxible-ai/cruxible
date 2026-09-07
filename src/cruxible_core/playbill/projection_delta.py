"""Internal changeset-driven successor construction; cold reconstruction is the oracle.

Only ActivationPublisher supplies this verified bundle/prefix. No serialized delta
or caller-supplied SQL is accepted. The immutable parent is verified before copying;
all changed artifacts still pass the ordinary compiler. Unsupported ownership
shapes deliberately use the existing full assembler.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, TypeVar

from cruxible_client.contracts.canonical import is_candidate_card_path
from cruxible_client.contracts.errors import ProjectionIntegrityError
from cruxible_core.playbill.citation_relations import build_citation_relation_facts
from cruxible_core.playbill.projection import (
    AcceptedProjectionCoordinate,
    AssemblerRequest,
    CandidateGenerationProjectionCoordinate,
    projection_manifest_name,
)
from cruxible_core.playbill.projection_artifacts import parse_projection_tree
from cruxible_core.playbill.projection_tree import _read_registered_entries
from cruxible_core.playbill.settlement import (
    ChangeSetRecordAnyVersion,
    ChangeSetRecordV2,
    ChangeSetRecordV3,
    VerifiedGenerationBundle,
)
from cruxible_core.storage.playbill_projection import bind_projection, update_projection_database

if TYPE_CHECKING:
    from cruxible_core.playbill.assembler import ProjectionAssembler

# These compilers emit rows owned by their artifact identity. Promotion outputs
# can emit Procedure-owned rows; fixtures allow extensions and presentation can
# name arbitrary owners.
# Their updates need an explicit row-ownership adapter before using this path.
_LOCAL_KINDS = frozenset(
    {
        "document",
        "capture-contract",
        "claim",
        "subject",
        "claim-type",
        "procedure",
        "line",
        "query-definition",
        "provider",
        "provider-interface",
        "source-acquisition-policy",
        "standing-mandate",
        "procedure-mandate",
        "approval-policy",
        "procedure-runtime-policy",
        "principal",
    }
)
_RELATIONS = (
    "playbill.citation_relation.capture_contract",
    "playbill.citation_relation.use",
    "playbill.citation_relation.retired_conflict",
)
T = TypeVar("T")


@dataclass(frozen=True)
class GenerationDelta:
    base: AcceptedProjectionCoordinate
    bundle: VerifiedGenerationBundle
    verified_prefix: tuple[tuple[str, ChangeSetRecordAnyVersion], ...]


def _timed(timings: dict[str, int], phase: str, fn: Callable[[], T]) -> T:
    start = time.perf_counter_ns()
    try:
        return fn()
    finally:
        timings[phase] += time.perf_counter_ns() - start


def populate_successor(
    assembler: ProjectionAssembler,
    *,
    request: AssemblerRequest,
    destination: Path,
    delta: GenerationDelta,
    timings: dict[str, int],
) -> dict[str, int] | None:
    """Update an independently staged database, or select the cold path before writing."""
    bundle, base = delta.bundle, delta.base
    if not isinstance(assembler.accepted, CandidateGenerationProjectionCoordinate):
        raise ProjectionIntegrityError("an accepted rebuild cannot consume a candidate delta")
    if bundle.projection_coordinate(base=base) != assembler.accepted:
        raise ProjectionIntegrityError("derived-index delta names another successor")
    if base.compiler != assembler.accepted.compiler:
        return None
    if not isinstance(bundle.record, (ChangeSetRecordV2, ChangeSetRecordV3)):
        return None
    if tuple(r.sequence for _, r in delta.verified_prefix) != tuple(
        range(1, bundle.record.sequence)
    ):
        raise ProjectionIntegrityError("derived-index delta has a noncontiguous verified prefix")
    if any(member.artifact_kind not in _LOCAL_KINDS for member in bundle.record.members):
        return None

    parent_request = AssemblerRequest(
        instance_id=base.instance_id,
        repository_path=base.repository_path,
        git_object_format=base.git_object_format,
        git_oid=base.git_oid,
        semantic_root=base.semantic_root,
        generation_root=base.generation_root,
        compiler_digest=base.compiler.rule_digest,
        schema_version=base.compiler.schema_version,
        output_staging_directory=str(destination.parent / ".parent"),
        limits=request.limits,
    )
    parent_manifest = assembler.publication_directory / projection_manifest_name(parent_request)
    # Genesis or an explicitly missing derivative can always rebuild from authority.
    if not parent_manifest.is_file():
        return None

    def changed_inputs() -> tuple[frozenset[str], dict[str, bytes]]:
        repository = assembler._repository
        parent_entries = {e.path: e for e in repository.list_tree_with_sizes(base.git_oid)}
        current_inventory = repository.list_tree_with_sizes(request.git_oid)
        current_entries = {entry.path: entry for entry in current_inventory}
        changed = frozenset(
            path
            for path in parent_entries.keys() | current_entries.keys()
            if parent_entries.get(path) != current_entries.get(path)
        )
        members = frozenset(member.path for member in bundle.record.members)
        extra = {p for p in changed - members if not is_candidate_card_path(p)}
        if extra != {bundle.record_path}:
            raise ProjectionIntegrityError("Git successor differs outside its changeset")
        # This validates the whole inventory's modes, paths, collisions and resource
        # bounds, but opens only affected payloads and the small contract inventory.
        selected = members | {p for p in current_entries if p.startswith("capture-contracts/")}
        blobs = _read_registered_entries(
            repository,
            current_inventory,
            limits=request.limits,
            artifact_kinds=assembler.artifact_kinds,
            include_paths=selected,
        )
        return members, {blob.path: blob.content for blob in blobs}

    members, inputs = _timed(timings, "git_traversal", changed_inputs)
    with bind_projection(parent_manifest, expected=base) as parent:
        # Fixture extensions and presentation rows can be owned by arbitrary
        # subjects, including a changed artifact. They need full reconstruction
        # even when their own source files are unchanged.
        if (
            parent._connection.execute(
                "SELECT 1 FROM artifact_envelopes WHERE kind='fixture' LIMIT 1"
            ).fetchone()
            or parent._connection.execute("SELECT 1 FROM presentation_facts LIMIT 1").fetchone()
        ):
            return None
        records = (*delta.verified_prefix, (bundle.record_path, bundle.record))
        parsed = _timed(
            timings,
            "parse_normalize",
            lambda: parse_projection_tree(
                {p: body for p, body in inputs.items() if p in members},
                registry=assembler.registry,
                artifact_kinds=assembler.artifact_kinds,
                artifact_codec=assembler.artifact_codec,
                bodies=assembler.bodies,
                coordinate=request,
                accepted_coordinates_by_sequence=assembler.accepted_coordinates_by_sequence,
                claim_compilation_cache=assembler.claim_compilation_cache,
                verified_change_sets=records,
            ),
        )
        # Verify the compiled member identities/digests against the changeset, not
        # a heuristic based on filename. Principal members have no envelope.
        envelope_by_path = {row.path: row for row in parsed.envelopes}
        for member in bundle.record.members:
            row = envelope_by_path.get(member.path)
            if row is not None and row.artifact_digest != getattr(
                member, "candidate_artifact_digest", None
            ):
                raise ProjectionIntegrityError("compiled delta member differs from its changeset")
        relations = None
        bodies = assembler.bodies
        if bodies is not None and assembler.registry.supports(
            "playbill.citation_relation.use", 1, classification="semantic"
        ):
            relations = _timed(
                timings,
                "parse_normalize",
                lambda: build_citation_relation_facts(
                    inputs,
                    bodies=bodies,
                    previous_use_facts=parent.semantic_facts(_RELATIONS[1]),
                    previous_conflict_facts=parent.semantic_facts(_RELATIONS[2]),
                    changed_claim_paths=frozenset(p for p in members if p.startswith("claims/")),
                ),
            )
            relations = assembler.registry.validate(relations, classification="semantic")
        return _timed(
            timings,
            "sqlite_load",
            lambda: update_projection_database(
                destination,
                parent=parent,
                request=request,
                parsed=parsed,
                changed_paths=members,
                relation_facts=relations,
            ),
        )
