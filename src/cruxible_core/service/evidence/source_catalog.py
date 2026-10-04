"""Client-side source-catalog compilation and server-safe frozen-bundle proposal."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.documents import (
    DocumentShell,
    document_digest,
    parse_document,
    render_document,
)
from cruxible_client.contracts.errors import FormatError, ProposalIntegrityError
from cruxible_client.contracts.source_catalog import (
    CompiledSourceDocument,
    SourceAlignment,
    SourceAlignmentState,
    SourceCatalog,
    SourceCompilationBundle,
    compile_source_catalog,
    content_digest_bytes,
)
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import (
    AcceptedCoordinate,
    ProposalInspection,
    service_propose_playbill_document,
    service_store_playbill_body,
)
from cruxible_core.service.change_preview import change_entry


class _StrictSourceServiceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceCheckResult(_StrictSourceServiceModel):
    tag: Literal["playbill-source-check-v1"] = "playbill-source-check-v1"
    compilation_digest: str
    accepted_coordinate: AcceptedCoordinate
    alignments: tuple[SourceAlignment, ...]


class SourceContext(_StrictSourceServiceModel):
    """Path-free accepted inputs needed for deterministic client-side compilation."""

    tag: Literal["playbill-source-context-v1"] = "playbill-source-context-v1"
    accepted_coordinate: AcceptedCoordinate
    documents: tuple[DocumentShell, ...]


def _accepted_documents(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
) -> dict[str, DocumentShell]:
    with instance.bind_accepted_projection(coordinate) as projection:
        documents = {}
        for row in projection.typed.envelopes(kind="document"):
            shell = projection.typed.source(row.identity)
            assert shell is not None
            documents[shell.document_id] = shell
        return documents


def service_playbill_source_context(instance: PlaybillInstance) -> SourceContext:
    coordinate = instance.accepted_coordinate()
    documents = _accepted_documents(instance, coordinate)
    return SourceContext(
        accepted_coordinate=AcceptedCoordinate.from_internal(coordinate),
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

    coordinate = instance.accepted_coordinate()
    return compile_source_catalog(
        catalog,
        repository_root=repository_root,
        root_aliases=root_aliases or {},
        accepted_base=AcceptedCoordinate.from_internal(coordinate),
        accepted_documents=_accepted_documents(instance, coordinate),
    )


def _pending_body_digests(
    instance: PlaybillInstance, coordinate: AcceptedProjectionCoordinate
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    evidence = instance.proposal_evidence()
    assert evidence.index is not None
    with instance.accepted_history_reader(
        at=AcceptedCoordinate.from_internal(coordinate)
    ) as history:
        # A missing candidate is an interrupted operation, not pending document work.
        with evidence.index.read(evidence) as connection:
            generation = connection.execute(
                "SELECT git_oid,semantic_root,generation_root,compiler_digest "
                "FROM accepted_generations WHERE sequence=?",
                (history.sequence,),
            ).fetchone()
            if tuple(generation or ()) != (
                coordinate.git_oid,
                coordinate.semantic_root,
                coordinate.generation_root,
                coordinate.compiler.rule_digest,
            ):
                raise ProposalIntegrityError("pending source history binding differs")
            rows = connection.execute(
                "SELECT DISTINCT candidate_digest,evaluated_tree_oid FROM proposals p "
                "WHERE admission_path IS NOT NULL AND evaluation_status='candidate' "
                "AND candidate_parent_semantic_root IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM accepted_generations g "
                "WHERE g.candidate_digest=p.candidate_digest AND g.sequence<=?)",
                (history.sequence,),
            ).fetchall()
    for digest, tree_oid in rows:
        candidate = evidence.read_candidate(digest)
        paths = tuple(
            member.path for member in candidate.members if member.artifact_kind == "document"
        )
        for path, content in instance._ledger.blobs_at(tree_oid, paths).items():
            shell = parse_document(content, path=path)
            result.setdefault(shell.document_id, set()).add(shell.body_digest)
    return result


def service_check_playbill_source_bundle(
    instance: PlaybillInstance,
    *,
    bundle: SourceCompilationBundle,
) -> SourceCheckResult:
    """Compare one exact frozen compile with current accepted and pending coordinates."""

    coordinate = instance.accepted_coordinate()
    current_coordinate = AcceptedCoordinate.from_internal(coordinate)
    accepted = _accepted_documents(instance, coordinate)
    pending = _pending_body_digests(instance, coordinate)
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
    return SourceCheckResult(
        compilation_digest=bundle.manifest.compilation_digest,
        accepted_coordinate=current_coordinate,
        alignments=tuple(alignments),
    )


def _compiled_document(
    bundle: SourceCompilationBundle,
    source_name: str,
) -> CompiledSourceDocument:
    matches = tuple(item for item in bundle.documents if item.source.name == source_name)
    if len(matches) != 1:
        raise FormatError("source compilation does not contain exactly one named source")
    return matches[0]


def service_propose_playbill_source_bundle(
    instance: PlaybillInstance,
    *,
    bundle: SourceCompilationBundle,
    source_name: str,
    actor_id: str,
    proposal_name: str,
    timestamp: str,
    dry_run: bool | None = None,
    at: str | None = None,
) -> ProposalInspection:
    """Submit only the bundle's frozen bytes; no source path is accepted or read here.

    ``dry_run`` runs the same path behind the preview guards: the body is held
    in memory, the compilation manifest is not recorded, and the Document
    change is evaluated on the admission path and admitted nowhere (R12).
    """

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
    with change_entry(dry_run, "direct") as previewing:
        if not previewing:
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
            dry_run=dry_run,
            at=at,
        )


__all__ = [
    "SourceContext",
    "SourceCheckResult",
    "service_check_playbill_source_bundle",
    "service_compile_playbill_sources",
    "service_propose_playbill_source_bundle",
    "service_playbill_source_context",
]
