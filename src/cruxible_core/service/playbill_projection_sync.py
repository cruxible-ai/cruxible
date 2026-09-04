"""Read-only resolution of syncable publication-backed projection blocks."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import (
    PlaybillBlockSyncReadRequestV1,
    PlaybillBlockSyncReadResultV1,
    PlaybillBlockSyncSuccessorCandidateV1,
)
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    capture_contract_digest,
    capture_is_coordinator_self_source,
    parse_capture_envelope,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.claim_types import (
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
)
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    ClaimArtifactV2,
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
from cruxible_client.contracts.source_references import CasSourceReferenceV1
from cruxible_client.contracts.subjects import parse_subject, subject_digest, subject_path
from cruxible_core.playbill.candidate_cards import (
    render_candidate_card,
)
from cruxible_core.playbill.compiler import (
    artifact_kinds_for_compiler,
    candidate_card_renderer_digest_for_compiler,
)
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.service.playbill_publications import bound_publication_registrations


@dataclass(frozen=True)
class _ClaimNode:
    claim: ClaimArtifactAny
    artifact_digest: str
    coordinate: AcceptedCoordinate
    generation: int


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


def _retained_successor_body(
    instance: PlaybillInstance,
    *,
    node: _ClaimNode,
    predecessor: _ClaimNode | None,
) -> tuple[bytes, str] | str:
    claim = node.claim
    if not isinstance(claim, ClaimArtifactV2):
        return "missing"
    predecessor_citation_ids = (
        {citation.citation_id for citation in predecessor.claim.backing.citations}
        if predecessor is not None and isinstance(predecessor.claim, ClaimArtifactV2)
        else set()
    )
    introduced = tuple(
        citation
        for citation in claim.backing.citations
        if citation.citation_id not in predecessor_citation_ids and citation.origin == "self_source"
    )
    store = instance.body_store()
    access = BodyAccessContext(principal_id="playbill-block-sync", can_read_body=True)
    contract = COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
    contract_digest_value = capture_contract_digest(contract).tagged
    bodies: list[bytes] = []
    for citation in introduced:
        try:
            envelope = parse_capture_envelope(store.read(citation.capture_digest, access=access))
            if envelope.capture_contract_digest != contract_digest_value or not (
                capture_is_coordinator_self_source(
                    envelope,
                    contract=contract,
                    claim_id=claim.identity.name,
                )
            ):
                continue
            source = envelope.source
            if not isinstance(source, CasSourceReferenceV1):
                continue
            body = store.read(source.content_digest, access=access)
            body_digest = "sha256:" + hashlib.sha256(body).hexdigest()
            if body_digest != source.content_digest or body_digest != envelope.commitment.digest:
                continue
            bodies.append(body)
        except (PlaybillError, ValueError):
            continue
    if not bodies:
        return "missing"
    if len(bodies) != 1:
        return "ambiguous"
    body = bodies[0]
    return body, "sha256:" + hashlib.sha256(body).hexdigest()


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


def _read_artifact_backing(
    instance: PlaybillInstance,
    *,
    stamp_coordinate: AcceptedCoordinate,
    backing: ProjectionArtifactBackingV1,
) -> PlaybillBlockSyncReadResultV1:
    """Render the latest governed vocabulary/entity card with the pinned compiler."""

    path = _artifact_path(backing.identity)
    original_raw = instance.tree_at(stamp_coordinate.git_oid).get(path)
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

    current = instance.accepted_coordinate()
    current_raw = instance.tree_at(current.git_oid).get(path)
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
    if candidate_card_renderer_digest_for_compiler(current.compiler) is None:
        return _refusal(
            status="unsyncable",
            reason="block_backing_changed",
            detail="the accepted compiler has no pinned candidate-card renderer",
            original_artifact_digest=original_digest,
        )
    body = render_candidate_card(
        path,
        current_raw,
        artifact_kinds=artifact_kinds_for_compiler(current.compiler),
    )
    body_digest = "sha256:" + hashlib.sha256(body).hexdigest()
    generation = instance.accepted_history()[-1]
    return PlaybillBlockSyncReadResultV1(
        status="current" if current_digest == original_digest else "successor",
        original_artifact_digest=original_digest,
        artifact_digest=current_digest,
        coordinate=AcceptedCoordinate.from_internal(current),
        generation=generation.sequence,
        backing=ProjectionArtifactBackingV1(
            identity=backing.identity,
            artifact_digest=current_digest,
        ),
        body_content_base64=base64.b64encode(body).decode("ascii"),
        body_digest=body_digest,
    )


def service_read_playbill_block_sync_backing(
    instance: PlaybillInstance,
    *,
    request: PlaybillBlockSyncReadRequestV1,
) -> PlaybillBlockSyncReadResultV1:
    """Resolve one marker to its unique current accepted Claim body without writing."""

    stamp = request.stamp
    if len(stamp.backing) != 1:
        return _refusal(
            status="unsyncable",
            reason="block_backing_changed",
            detail="sync requires exactly one supported backing",
        )
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
    backing = stamp.backing[0]
    if isinstance(backing, ProjectionArtifactBackingV1):
        return _read_artifact_backing(
            instance,
            stamp_coordinate=declared,
            backing=backing,
        )
    if not isinstance(backing, ProjectionClaimBackingV1):
        return _refusal(
            status="unsyncable",
            reason="block_backing_changed",
            detail="sync requires a Claim, ClaimType, or Subject backing",
        )
    path = claim_path(backing.identity.name)
    raw = instance.tree_at(declared.git_oid).get(path)
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
    registrations = bound_publication_registrations(instance)
    if registrations is None:
        return _refusal(
            status="unsyncable",
            reason="block_publication_registry_unavailable",
            detail="the bound publication registry could not be read",
            original_artifact_digest=original_digest,
        )
    origin = tuple(
        registration
        for registration in registrations
        if registration.preparation.source_id == stamp.source_id
        and registration.preparation.block_id == stamp.block_id
        and registration.claim_identity == backing.identity.name
        and len(registration.preparation.stamp.backing) == 1
        and isinstance(registration.preparation.stamp.backing[0], ProjectionClaimBackingV1)
    )
    if not origin:
        return _refusal(
            status="unsyncable",
            reason="block_not_publication_origin",
            detail="the marker is not associated with a confirmed publication",
            original_artifact_digest=original_digest,
        )
    nodes = _claim_nodes(instance, path=path)
    if original_digest not in nodes:
        raise ProposalIntegrityError("accepted Claim block-sync origin disappeared from history")
    terminal = _terminal_node(
        nodes=nodes,
        original_digest=original_digest,
        preferred_successor_digest=request.preferred_successor_digest,
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
    predecessor = (
        None
        if terminal.claim.lifecycle.predecessor_digest is None
        else nodes.get(terminal.claim.lifecycle.predecessor_digest)
    )
    resolved_body = _retained_successor_body(instance, node=terminal, predecessor=predecessor)
    if isinstance(resolved_body, str):
        if resolved_body == "missing":
            return _refusal(
                status="unsyncable",
                reason="block_successor_body_missing",
                detail="the terminal Claim introduced no retained coordinator self-source body",
                original_artifact_digest=original_digest,
            )
        return _refusal(
            status="unsyncable",
            reason="block_successor_body_ambiguous",
            detail="the terminal Claim introduced multiple coordinator self-source bodies",
            original_artifact_digest=original_digest,
        )
    body, body_digest = resolved_body
    return PlaybillBlockSyncReadResultV1(
        status="current" if terminal.artifact_digest == original_digest else "successor",
        original_artifact_digest=original_digest,
        artifact_digest=terminal.artifact_digest,
        coordinate=terminal.coordinate,
        generation=terminal.generation,
        backing=ProjectionClaimBackingV1(
            identity=ArtifactIdentity(kind="Claim", name=terminal.claim.identity.name),
            statement_digest=claim_statement_digest(terminal.claim.statement).tagged,
        ),
        body_content_base64=base64.b64encode(body).decode("ascii"),
        body_digest=body_digest,
    )


__all__ = ["service_read_playbill_block_sync_backing"]
