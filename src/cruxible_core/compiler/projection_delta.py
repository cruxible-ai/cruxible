"""Internal changeset-driven successor construction; cold reconstruction is the oracle.

Only ActivationPublisher supplies this verified bundle/prefix. No serialized delta
or caller-supplied SQL is accepted. The immutable parent is verified before copying;
all changed artifacts still pass the ordinary compiler. Unsupported ownership
shapes deliberately use the existing full assembler.
"""

from __future__ import annotations

import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, TypeVar

from cruxible_client.contracts.canonical import is_candidate_card_path
from cruxible_client.contracts.errors import ProjectionIntegrityError
from cruxible_core.compiler.projection_artifacts import parse_projection_tree
from cruxible_core.compiler.projection_tree import _read_registered_entries
from cruxible_core.indexes.evidence.citation_index import (
    CitationDelta,
    CitationIndex,
    rebuild_citation_index,
)
from cruxible_core.indexes.projection import (
    AcceptedProjectionCoordinate,
    AssemblerRequest,
    CandidateGenerationProjectionCoordinate,
    projection_manifest_name,
)
from cruxible_core.indexes.sqlite import bind_projection, update_projection_database
from cruxible_core.proposals.settlement import (
    ChangeSetRecordAnyVersion,
    ChangeSetRecordV2,
    ChangeSetRecordV3,
    VerifiedGenerationBundle,
)

if TYPE_CHECKING:
    from cruxible_core.compiler.assembler import ProjectionAssembler

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

    citation_inputs_changed = any(
        member.path.startswith(("claims/", "capture-contracts/"))
        for member in bundle.record.members
    )

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
        # Validate the whole inventory boundary, but read only changeset members.
        selected = members
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
        successor_relations = None
        relation_cache = assembler.citation_index_cache
        cache_epoch = relation_cache.cache.generation if relation_cache is not None else 0
        bodies = assembler.bodies
        # Relations depend on Claim citations and CaptureContracts, not the
        # generation coordinate. Other member kinds carry those rows unchanged.
        if (
            citation_inputs_changed
            and bodies is not None
            and assembler.registry.supports(
                "playbill.citation_relation.use", 1, classification="semantic"
            )
        ):

            def citation_delta() -> tuple[CitationIndex, CitationDelta] | None:
                prior = (
                    relation_cache.parent(parent)
                    if relation_cache is not None
                    else rebuild_citation_index(parent)
                )
                if prior is None:
                    return None
                with (
                    relation_cache.owner.build(("citation-delta", request.git_oid))
                    if relation_cache is not None
                    else nullcontext()
                ):
                    return prior.advance(inputs, changed_paths=members, bodies=bodies)

            planned = _timed(timings, "parse_normalize", citation_delta)
            if planned is None:
                return None
            successor_relations, relations = planned
            assembler.registry.validate(
                tuple(f.materialize() for f in relations.inserts), classification="semantic"
            )
        elif relation_cache is not None:
            # Carry a warm root over unrelated edits without bootstrapping a cold one.
            successor_relations = relation_cache.peek(base)
        result = _timed(
            timings,
            "sqlite_load",
            lambda: update_projection_database(
                destination,
                parent=parent,
                request=request,
                parsed=parsed,
                changed_paths=members,
                relation_delta=relations,
            ),
        )
        if relation_cache is not None and successor_relations is not None:
            relation_cache.remember(request, successor_relations, epoch=cache_epoch)
        return result
