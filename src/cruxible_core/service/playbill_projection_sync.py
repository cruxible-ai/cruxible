"""Whether a declared block's held backings still read as its stamp says.

Nothing renders. This read used to resolve one Claim backing to the accepted
body a block would be rewritten to; a projection block is agent-authored prose
held to an explicit list, so the only question accepted state can answer about
it is whether every member of that list is still there, still live, and still
saying what it said. The answer is a currency verdict over the whole list, and
`block sync` reports it without writing a byte.
"""

from __future__ import annotations

from contextvars import ContextVar

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import (
    PlaybillBlockSyncReadRequestV1,
    PlaybillBlockSyncReadResultV1,
    PlaybillBlockSyncSuccessorCandidateV1,
)
from cruxible_client.contracts.claim_types import (
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
)
from cruxible_client.contracts.claims import (
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
    parse_claim,
)
from cruxible_client.contracts.declared_blocks import (
    ProjectionArtifactBackingV1,
    ProjectionClaimBackingV1,
)
from cruxible_client.contracts.errors import PlaybillError, ProposalIntegrityError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.subjects import parse_subject, subject_digest, subject_path
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.service.playbill_projection_lineage import (
    ClaimLineageNode as _ClaimNode,
)
from cruxible_core.service.playbill_projection_lineage import (
    read_claim_lineages,
)

_LINEAGES: ContextVar[
    tuple[PlaybillInstance, dict[str, dict[str, _ClaimNode] | PlaybillError]] | None
] = ContextVar("block_sync_lineages", default=None)


def _refusal(
    *,
    status: str,
    reason: str,
    detail: str,
    original_artifact_digest: str | None = None,
    candidates: tuple[PlaybillBlockSyncSuccessorCandidateV1, ...] = (),
) -> PlaybillBlockSyncReadResultV1:
    return PlaybillBlockSyncReadResultV1.model_validate(
        {
            "status": status,
            "reason": reason,
            "detail": detail,
            "original_artifact_digest": original_artifact_digest,
            "successor_candidates": [item.model_dump(mode="json") for item in candidates],
        }
    )


def _claim_nodes(instance: PlaybillInstance, *, path: str) -> dict[str, _ClaimNode]:
    prepared = _LINEAGES.get()
    if prepared is not None and prepared[0] is instance and path in prepared[1]:
        cached_nodes = prepared[1][path]
        if isinstance(cached_nodes, PlaybillError):
            raise cached_nodes
        return cached_nodes
    nodes: dict[str, _ClaimNode] = {}
    for generation in instance.accepted_history():
        raw = instance.blob_at(generation.oid, path)
        if raw is None:
            continue
        claim = parse_claim(raw, path=path)
        digest = claim_artifact_digest(claim).tagged
        nodes.setdefault(
            digest,
            _ClaimNode(
                claim=claim,
                artifact_digest=digest,
                coordinate=AcceptedCoordinate.from_internal(
                    instance.coordinate_for_oid(generation.oid)
                ),
                generation=generation.sequence,
            ),
        )
    return nodes


def _candidate(node: _ClaimNode) -> PlaybillBlockSyncSuccessorCandidateV1:
    return PlaybillBlockSyncSuccessorCandidateV1(
        identity=node.claim.identity,
        artifact_digest=node.artifact_digest,
        coordinate=node.coordinate,
        generation=node.generation,
    )


def _terminal_node(
    *,
    nodes: dict[str, _ClaimNode],
    original_digest: str,
    preferred_successor_digest: str | None,
) -> _ClaimNode | tuple[PlaybillBlockSyncSuccessorCandidateV1, ...]:
    successors: dict[str, dict[str, _ClaimNode]] = {}
    for node in nodes.values():
        predecessor = node.claim.lifecycle.predecessor_digest
        if predecessor is not None:
            successors.setdefault(predecessor, {})[node.artifact_digest] = node
    states: dict[str, int] = {}
    terminals: dict[str, _ClaimNode] = {}

    def visit(digest: str) -> None:
        state = states.get(digest, 0)
        if state == 1:
            raise ProposalIntegrityError("accepted Claim block-sync lineage contains a cycle")
        if state == 2:
            return
        states[digest] = 1
        children = successors.get(digest, {})
        if not children:
            terminals[digest] = nodes[digest]
        else:
            for child_digest in sorted(children, key=lambda item: item.encode("ascii")):
                visit(child_digest)
        states[digest] = 2

    visit(original_digest)
    live = tuple(
        terminals[digest]
        for digest in sorted(terminals, key=lambda item: item.encode("ascii"))
        if terminals[digest].claim.lifecycle.state == "live"
    )
    if preferred_successor_digest is not None:
        selected = tuple(
            node for node in live if node.artifact_digest == preferred_successor_digest
        )
        if len(selected) == 1:
            return selected[0]
    if len(live) > 1:
        return tuple(_candidate(item) for item in live)
    if live:
        return live[0]
    retired = tuple(terminals.values())
    if not retired:
        raise ProposalIntegrityError("accepted Claim block-sync lineage has no terminal")
    return max(retired, key=lambda item: (item.generation, item.artifact_digest))


def _artifact_path(identity: ArtifactIdentity) -> str:
    if identity.kind == "ClaimType":
        return claim_type_path(identity.name)
    subject_kind, separator, subject_id = identity.name.partition("/")
    if identity.kind != "Subject" or not separator:
        raise ValueError("unsupported projection artifact backing identity")
    return subject_path(subject_kind, subject_id)


def _artifact_digest(*, identity: ArtifactIdentity, path: str, raw: bytes) -> str:
    if identity.kind == "ClaimType":
        claim_type = parse_claim_type(raw, path=path)
        if claim_type.identity != identity:
            raise ValueError("ClaimType backing identity does not reproduce")
        return claim_type_digest(claim_type).tagged
    subject = parse_subject(raw, path=path)
    if subject.identity != identity:
        raise ValueError("Subject backing identity does not reproduce")
    return subject_digest(subject).tagged


def _artifact_backing_state(
    instance: PlaybillInstance,
    *,
    stamp_coordinate: AcceptedCoordinate,
    current: AcceptedCoordinate,
    backing: ProjectionArtifactBackingV1,
) -> ProjectionArtifactBackingV1 | PlaybillBlockSyncReadResultV1 | None:
    """The artifact backing's current spelling when it moved, ``None`` when it did not."""

    path = _artifact_path(backing.identity)
    original_raw = instance.blob_at(stamp_coordinate.git_oid, path)
    if original_raw is None:
        return _refusal(
            status="unsyncable",
            reason="block_backing_missing",
            detail="the declared artifact backing is absent at the marker coordinate",
        )
    try:
        original_digest = _artifact_digest(identity=backing.identity, path=path, raw=original_raw)
    except (PlaybillError, ValueError):
        return _refusal(
            status="unsyncable",
            reason="block_backing_changed",
            detail="the artifact backing does not reproduce at its declared coordinate",
        )
    if original_digest != backing.artifact_digest:
        return _refusal(
            status="unsyncable",
            reason="block_backing_changed",
            detail="the artifact backing digest does not reproduce at its declared coordinate",
            original_artifact_digest=original_digest,
        )
    current_raw = instance.blob_at(current.git_oid, path)
    if current_raw is None:
        return _refusal(
            status="unsyncable",
            reason="block_backing_missing",
            detail="the governed artifact backing is absent at the current coordinate",
            original_artifact_digest=original_digest,
        )
    try:
        current_digest = _artifact_digest(identity=backing.identity, path=path, raw=current_raw)
    except (PlaybillError, ValueError):
        return _refusal(
            status="unsyncable",
            reason="block_backing_changed",
            detail="the governed artifact backing does not reproduce at the current coordinate",
            original_artifact_digest=original_digest,
        )
    if current_digest == original_digest:
        return None
    return ProjectionArtifactBackingV1(
        identity=backing.identity,
        artifact_digest=current_digest,
    )


def _claim_backing_state(
    instance: PlaybillInstance,
    *,
    stamp_coordinate: AcceptedCoordinate,
    backing: ProjectionClaimBackingV1,
    preferred_successor_digest: str | None,
) -> tuple[ProjectionClaimBackingV1 | None, _ClaimNode] | PlaybillBlockSyncReadResultV1:
    """The Claim backing's terminal spelling, or the typed refusal its lineage earns."""

    path = claim_path(backing.identity.name)
    raw = instance.blob_at(stamp_coordinate.git_oid, path)
    if raw is None:
        return _refusal(
            status="unsyncable",
            reason="block_backing_missing",
            detail="the declared Claim backing is absent at the marker coordinate",
        )
    original = parse_claim(raw, path=path)
    if (
        original.identity != backing.identity
        or claim_statement_digest(original.statement).tagged != backing.statement_digest
    ):
        return _refusal(
            status="unsyncable",
            reason="block_backing_changed",
            detail="the marker backing does not reproduce at its declared coordinate",
        )
    original_digest = claim_artifact_digest(original).tagged
    nodes = _claim_nodes(instance, path=path)
    if original_digest not in nodes:
        raise ProposalIntegrityError("accepted Claim block-sync origin disappeared from history")
    terminal = _terminal_node(
        nodes=nodes,
        original_digest=original_digest,
        preferred_successor_digest=preferred_successor_digest,
    )
    if isinstance(terminal, tuple):
        return _refusal(
            status="refused",
            reason="block_successor_ambiguous",
            detail="the accepted Claim lineage has multiple live successor candidates",
            original_artifact_digest=original_digest,
            candidates=terminal,
        )
    if terminal.claim.lifecycle.state != "live":
        return _refusal(
            status="refused",
            reason="block_backing_retired",
            detail="the accepted Claim lineage terminates in retirement",
            original_artifact_digest=original_digest,
        )
    if terminal.artifact_digest == original_digest:
        return None, terminal
    return (
        ProjectionClaimBackingV1(
            identity=ArtifactIdentity(kind="Claim", name=terminal.claim.identity.name),
            statement_digest=claim_statement_digest(terminal.claim.statement).tagged,
        ),
        terminal,
    )


def _read_playbill_block_sync_backing(
    instance: PlaybillInstance,
    *,
    request: PlaybillBlockSyncReadRequestV1,
    accepted: AcceptedCoordinate,
    generation: int,
) -> PlaybillBlockSyncReadResultV1:
    """Say whether every backing a marker holds still reads as the marker says.

    A watched query is deliberately not consulted here. A query surfaces
    CANDIDATES for the held list; its result moving is not the block falling out
    of date with what it holds, and `next` reports that separately as
    `projection_candidates_changed`. Re-evaluating one is also the expensive
    part of the projection fold, and `block sync` is the cheap check an
    activation runs over a whole workspace.
    """

    stamp = request.stamp
    try:
        declared = AcceptedCoordinate.from_internal(
            instance.coordinate_for_oid(stamp.declared_coordinate.git_oid)
        )
    except PlaybillError:
        return _refusal(
            status="refused",
            reason="block_workspace_instance_mismatch",
            detail="the marker coordinate is not accepted by the attached instance",
        )
    if declared != stamp.declared_coordinate:
        return _refusal(
            status="refused",
            reason="block_workspace_instance_mismatch",
            detail="the marker coordinate differs from the attached instance coordinate",
        )
    moved: list[ProjectionArtifactBackingV1 | ProjectionClaimBackingV1] = []
    # The CURRENT spelling of every held backing, moved or not. `block repin
    # --backing DIGEST` asks this read to name one exact successor and re-stamp
    # it, and a block whose backing has not moved must still answer with the
    # backing it has rather than with nothing.
    current: list[ProjectionArtifactBackingV1 | ProjectionClaimBackingV1] = []
    original_digest: str | None = None
    current_digest: str | None = None
    held = 0
    for backing in stamp.backing:
        if isinstance(backing, ProjectionArtifactBackingV1):
            held += 1
            artifact = _artifact_backing_state(
                instance,
                stamp_coordinate=declared,
                current=accepted,
                backing=backing,
            )
            if isinstance(artifact, PlaybillBlockSyncReadResultV1):
                return artifact
            original_digest = backing.artifact_digest
            current_digest = (
                backing.artifact_digest if artifact is None else artifact.artifact_digest
            )
            current.append(backing if artifact is None else artifact)
            if artifact is not None:
                moved.append(artifact)
            continue
        if not isinstance(backing, ProjectionClaimBackingV1):
            continue
        held += 1
        claim_state = _claim_backing_state(
            instance,
            stamp_coordinate=declared,
            backing=backing,
            preferred_successor_digest=request.preferred_successor_digest,
        )
        if isinstance(claim_state, PlaybillBlockSyncReadResultV1):
            return claim_state
        successor, terminal = claim_state
        original_digest = (
            claim_artifact_digest(
                parse_claim(
                    instance.tree_at(declared.git_oid)[claim_path(backing.identity.name)],
                    path=claim_path(backing.identity.name),
                )
            ).tagged
            if original_digest is None
            else original_digest
        )
        current_digest = terminal.artifact_digest
        current.append(backing if successor is None else successor)
        if successor is not None:
            moved.append(successor)
    if held == 0:
        return _refusal(
            status="unsyncable",
            reason="block_backing_changed",
            detail="the marker holds no Claim or artifact backing to check",
        )
    single = current[0] if held == 1 else None
    return PlaybillBlockSyncReadResultV1(
        status="successor" if moved else "current",
        original_artifact_digest=original_digest if held == 1 else None,
        artifact_digest=current_digest if held == 1 else None,
        coordinate=accepted,
        generation=generation,
        backing=single,
        moved_backings=tuple(moved),
    )


def service_read_playbill_block_sync_backing(
    instance: PlaybillInstance, *, request: PlaybillBlockSyncReadRequestV1
) -> PlaybillBlockSyncReadResultV1:
    accepted = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    generation = next(g.sequence for g in instance.accepted_history() if g.oid == accepted.git_oid)
    # Validate the declared origin before loading historical backings so invalid
    # coordinates retain the existing typed refusal and do no unrelated work.
    try:
        declared = AcceptedCoordinate.from_internal(
            instance.coordinate_for_oid(request.stamp.declared_coordinate.git_oid)
        )
    except PlaybillError:
        declared = None
    paths = tuple(
        claim_path(b.identity.name)
        for b in request.stamp.backing
        if isinstance(b, ProjectionClaimBackingV1)
    )
    lineages = (
        read_claim_lineages(instance, paths=paths, at=accepted, defer_errors=True)
        if declared == request.stamp.declared_coordinate and paths
        else {}
    )
    token = _LINEAGES.set((instance, lineages))
    try:
        return _read_playbill_block_sync_backing(
            instance, request=request, accepted=accepted, generation=generation
        )
    finally:
        _LINEAGES.reset(token)


__all__ = ["service_read_playbill_block_sync_backing"]
