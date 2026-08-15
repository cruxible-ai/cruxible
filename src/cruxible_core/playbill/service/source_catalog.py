"""Client-side source-catalog compilation and server-safe frozen-bundle proposal."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from cruxible_core.playbill.documents import (
    DocumentShell,
    document_digest,
    parse_document,
    render_document,
)
from cruxible_core.playbill.errors import PlaybillFormatError, ProposalIntegrityError
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.service.documents import (
    PlaybillAcceptedCoordinate,
    PlaybillProposalInspection,
    service_propose_playbill_document,
    service_store_playbill_body,
)
from cruxible_core.playbill.source_catalog import (
    CompiledSourceDocument,
    SourceAlignment,
    SourceAlignmentState,
    SourceCatalog,
    SourceCompilationBundle,
    compile_source_catalog,
    content_digest_bytes,
)


class _StrictSourceServiceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlaybillSourceCheckResult(_StrictSourceServiceModel):
    tag: Literal["playbill-source-check-v1"] = "playbill-source-check-v1"
    compilation_digest: str
    accepted_coordinate: PlaybillAcceptedCoordinate
    alignments: tuple[SourceAlignment, ...]


class PlaybillSourceContext(_StrictSourceServiceModel):
    """Path-free accepted inputs needed for deterministic client-side compilation."""

    tag: Literal["playbill-source-context-v1"] = "playbill-source-context-v1"
    accepted_coordinate: PlaybillAcceptedCoordinate
    documents: tuple[DocumentShell, ...]


def _accepted_documents(instance: PlaybillInstance) -> dict[str, DocumentShell]:
    result: dict[str, DocumentShell] = {}
    for path, content in instance.tree_at(instance.accepted_coordinate().git_oid).items():
        if not path.startswith("documents/"):
            continue
        shell = parse_document(content, path=path)
        result[shell.document_id] = shell
    return result


def service_playbill_source_context(instance: PlaybillInstance) -> PlaybillSourceContext:
    documents = _accepted_documents(instance)
    return PlaybillSourceContext(
        accepted_coordinate=PlaybillAcceptedCoordinate.from_internal(
            instance.accepted_coordinate()
        ),
        documents=tuple(
            documents[key] for key in sorted(documents, key=lambda item: item.encode())
        ),
    )


def service_compile_playbill_sources(
    instance: PlaybillInstance,
    *,
    catalog: SourceCatalog,
    repository_root: Path,
    root_aliases: dict[str, Path] | None = None,
) -> SourceCompilationBundle:
    """Compile local declared files without changing CAS, exhaust, or accepted state."""

    return compile_source_catalog(
        catalog,
        repository_root=repository_root,
        root_aliases=root_aliases or {},
        accepted_base=PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        accepted_documents=_accepted_documents(instance),
    )


def _pending_body_digests(instance: PlaybillInstance) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    evidence = instance.proposal_evidence()
    settled_candidates = {
        generation.record.candidate_digest
        for generation in instance.accepted_history()
        if generation.record is not None
    }
    for evaluation in evidence.list_evaluations():
        if (
            evaluation.verdict != "candidate"
            or evaluation.evaluated_tree_oid is None
            or evaluation.candidate_digest is None
            or evaluation.candidate_digest in settled_candidates
        ):
            continue
        candidate = evidence.read_candidate(evaluation.candidate_digest)
        for member in candidate.members:
            if member.artifact_kind != "document":
                continue
            content = instance.proposal_tree(evaluation.evaluated_tree_oid).get(member.path)
            if content is None:
                continue
            shell = parse_document(content, path=member.path)
            result.setdefault(shell.document_id, set()).add(shell.body_digest)
    return result


def service_check_playbill_source_bundle(
    instance: PlaybillInstance,
    *,
    bundle: SourceCompilationBundle,
) -> PlaybillSourceCheckResult:
    """Compare one exact frozen compile with current accepted and pending coordinates."""

    current_coordinate = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())
    accepted = _accepted_documents(instance)
    pending = _pending_body_digests(instance)
    alignments: list[SourceAlignment] = []
    base_is_current = bundle.manifest.accepted_base == current_coordinate
    for document in bundle.documents:
        document_id = document.source.document_id
        current = accepted.get(document_id)
        current_body = None if current is None else current.body_digest
        current_envelope = None if current is None else document_digest(current).tagged
        local_body = document.source.body_digest
        pending_body = local_body if local_body in pending.get(document_id, set()) else None
        if local_body == current_body:
            state = "aligned"
        elif pending_body is not None:
            state = "pending"
        elif current is None:
            state = "untracked"
        elif base_is_current:
            state = "modified"
        elif current.predecessor_digest == document.envelope_digest:
            state = "behind"
        elif document.envelope.predecessor_digest == current_envelope:
            state = "ahead"
        else:
            state = "diverged"
        alignment_state = cast(SourceAlignmentState, state)
        alignments.append(
            SourceAlignment(
                name=document.source.name,
                document_id=document_id,
                state=alignment_state,
                local_body_digest=local_body,
                accepted_body_digest=current_body,
                accepted_envelope_digest=current_envelope,
                pending_body_digest=pending_body,
                accepted_coordinate=current_coordinate,
            )
        )
    return PlaybillSourceCheckResult(
        compilation_digest=bundle.manifest.compilation_digest,
        accepted_coordinate=current_coordinate,
        alignments=tuple(alignments),
    )


def service_check_playbill_sources(
    instance: PlaybillInstance,
    *,
    catalog: SourceCatalog,
    repository_root: Path,
    root_aliases: dict[str, Path] | None = None,
) -> PlaybillSourceCheckResult:
    """Snapshot/hash declared bytes, then compare the frozen result with state."""

    bundle = service_compile_playbill_sources(
        instance,
        catalog=catalog,
        repository_root=repository_root,
        root_aliases=root_aliases,
    )
    return service_check_playbill_source_bundle(instance, bundle=bundle)


def _compiled_document(
    bundle: SourceCompilationBundle,
    source_name: str,
) -> CompiledSourceDocument:
    matches = tuple(item for item in bundle.documents if item.source.name == source_name)
    if len(matches) != 1:
        raise PlaybillFormatError("source compilation does not contain exactly one named source")
    return matches[0]


def service_propose_playbill_source_bundle(
    instance: PlaybillInstance,
    *,
    bundle: SourceCompilationBundle,
    source_name: str,
    actor_id: str,
    proposal_name: str,
    timestamp: str,
) -> PlaybillProposalInspection:
    """Submit only the bundle's frozen bytes; no source path is accepted or read here."""

    document = _compiled_document(bundle, source_name)
    try:
        body = base64.b64decode(document.body_base64, validate=True)
        envelope_bytes = base64.b64decode(document.envelope_bytes_base64, validate=True)
    except ValueError as exc:  # defensive: bundle validation already proves this
        raise ProposalIntegrityError("compiled source bundle contains invalid base64") from exc
    if content_digest_bytes(body) != document.source.body_digest:
        raise ProposalIntegrityError("compiled body bytes changed after compilation")
    if envelope_bytes != render_document(document.envelope):
        raise ProposalIntegrityError("compiled envelope bytes changed after compilation")
    instance.proposal_evidence().write_source_compilation(bundle.manifest)
    stored = service_store_playbill_body(instance, content=body)
    if stored.digest != document.source.body_digest:
        raise ProposalIntegrityError("stored body digest differs from compiled source")
    return service_propose_playbill_document(
        instance,
        shell=document.envelope,
        actor_id=actor_id,
        proposal_name=proposal_name,
        timestamp=timestamp,
        base=bundle.manifest.accepted_base,
        source_compilation_digest=bundle.manifest.compilation_digest,
    )


__all__ = [
    "PlaybillSourceContext",
    "PlaybillSourceCheckResult",
    "service_check_playbill_source_bundle",
    "service_check_playbill_sources",
    "service_compile_playbill_sources",
    "service_propose_playbill_source_bundle",
    "service_playbill_source_context",
]
