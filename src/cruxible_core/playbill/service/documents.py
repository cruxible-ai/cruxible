"""Typed service operations for the governed Playbill Document lifecycle."""

from __future__ import annotations

import base64
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.attestations import (
    ApprovalAttestation,
    ApprovalSubmission,
    VerifiedApproval,
    verify_approval,
)
from cruxible_client.contracts.candidates import CandidateRecordAnyVersion
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.documents import (
    DocumentShell,
    document_digest,
    document_path,
    parse_document,
    render_document,
)
from cruxible_client.contracts.errors import (
    DocumentNotFoundError,
    ProposalIntegrityError,
    SettlementIntegrityError,
)
from cruxible_client.contracts.principal_rendering import render_principal
from cruxible_client.contracts.proposal_models import canonical_proposal_ref_name
from cruxible_client.contracts.types import PrincipalRecord
from cruxible_core.errors import DataValidationError
from cruxible_core.playbill.cas import BodyAccessContext, CasObjectMetadata
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.playbill.projection_documents import DocumentProjectionView
from cruxible_core.playbill.proposals import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
    ProposalResult,
)
from cruxible_core.playbill.settlement import ChangeActorBinding


class _StrictServiceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


PlaybillAcceptedCoordinate = AcceptedCoordinate


class PlaybillDocumentView(_StrictServiceModel):
    tag: Literal["playbill-document-read-v1"] = "playbill-document-read-v1"
    coordinate_kind: Literal["canonical"] = "canonical"
    coordinate: PlaybillAcceptedCoordinate
    envelope: dict[str, object]
    facts: tuple[dict[str, object], ...]


class PlaybillDocumentList(_StrictServiceModel):
    tag: Literal["playbill-document-list-v1"] = "playbill-document-list-v1"
    coordinate: PlaybillAcceptedCoordinate
    documents: tuple[PlaybillDocumentView, ...]


class PlaybillPrincipalList(_StrictServiceModel):
    tag: Literal["playbill-principal-list-v1"] = "playbill-principal-list-v1"
    coordinate: PlaybillAcceptedCoordinate
    principals: tuple[PrincipalRecord, ...]


class PlaybillBodyRead(_StrictServiceModel):
    tag: Literal["playbill-document-body-v1"] = "playbill-document-body-v1"
    identity: str
    coordinate: PlaybillAcceptedCoordinate
    body_digest: str
    media_type: str
    content_base64: str


class PlaybillDocumentHistoryEntry(_StrictServiceModel):
    sequence: int
    coordinate: PlaybillAcceptedCoordinate
    envelope_digest: str
    body_digest: str
    predecessor_digest: str | None
    revision: int
    change_set_path: str
    changeset_digest: str
    candidate_digest: str


class PlaybillDocumentHistory(_StrictServiceModel):
    tag: Literal["playbill-document-history-v1"] = "playbill-document-history-v1"
    identity: str
    entries: tuple[PlaybillDocumentHistoryEntry, ...]


class PlaybillProposalInspection(_StrictServiceModel):
    tag: Literal["playbill-proposal-inspection-v1"] = "playbill-proposal-inspection-v1"
    proposal: ProposalResult
    accepted_coordinate: PlaybillAcceptedCoordinate


class PlaybillApprovalReceipt(_StrictServiceModel):
    tag: Literal["playbill-approval-receipt-v1"] = "playbill-approval-receipt-v1"
    proposal_id: str
    candidate_digest: str
    signer_id: str
    submitted_by: str
    signing_semantic_root: str
    attestation_digest: str
    key_history_ref: str


class PlaybillActivationReceipt(_StrictServiceModel):
    tag: Literal["playbill-activation-receipt-v1"] = "playbill-activation-receipt-v1"
    proposal_id: str
    status: Literal["accepted", "lost_cas"]
    accepted_coordinate: PlaybillAcceptedCoordinate | None


class PlaybillRefusalInspection(_StrictServiceModel):
    tag: Literal["playbill-refusal-v1"] = "playbill-refusal-v1"
    proposal_id: str
    verdict: Literal["candidate", "refused"]
    diagnostics: tuple[CompilerDiagnostic, ...]


def _public_document(view: DocumentProjectionView) -> PlaybillDocumentView:
    if view.coordinate_kind != "canonical" or not isinstance(
        view.coordinate,
        AcceptedProjectionCoordinate,
    ):
        raise ProposalIntegrityError("canonical Document service received a provisional view")
    return PlaybillDocumentView(
        coordinate=PlaybillAcceptedCoordinate.from_internal(view.coordinate),
        envelope=view.envelope.model_dump(mode="json"),
        facts=tuple(fact.model_dump(mode="json") for fact in view.facts),
    )


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


def service_store_playbill_body(
    instance: PlaybillInstance,
    *,
    content: bytes,
) -> CasObjectMetadata:
    """Store inert exact bytes; this operation creates no proposal or authority."""

    return instance.store_document_body(content)


def service_propose_playbill_document(
    instance: PlaybillInstance,
    *,
    shell: DocumentShell,
    actor_id: str,
    proposal_name: str,
    timestamp: str,
    base: PlaybillAcceptedCoordinate | None = None,
    source_compilation_digest: str | None = None,
) -> PlaybillProposalInspection:
    """Admit and deterministically evaluate one exact Document envelope change."""

    proposed_base = _resolve_coordinate(instance, base)
    candidate_tree = instance.tree_at(proposed_base.git_oid)
    candidate_tree[document_path(shell.document_id)] = render_document(shell)
    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id=actor_id),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/{actor_id}/{proposal_name}",
            proposed_base_oid=proposed_base.git_oid,
            source_compilation_digest=source_compilation_digest,
        ),
        candidate_tree=candidate_tree,
        timestamp=timestamp,
    )
    return PlaybillProposalInspection(
        proposal=result,
        accepted_coordinate=PlaybillAcceptedCoordinate.from_internal(
            instance.accepted_coordinate()
        ),
    )


def service_propose_playbill_principal_change(
    instance: PlaybillInstance,
    *,
    principal: PrincipalRecord,
    actor_id: str,
    proposal_name: str,
    timestamp: str,
    base: PlaybillAcceptedCoordinate | None = None,
) -> PlaybillProposalInspection:
    """Use the distinct principal-lifecycle law; never the ordinary Document path."""

    proposed_base = _resolve_coordinate(instance, base)
    candidate_tree = instance.tree_at(proposed_base.git_oid)
    candidate_tree[f"principals/{principal.principal_id}.yaml"] = render_principal(principal)
    try:
        ref_name = canonical_proposal_ref_name(proposal_name)
    except ValueError as exc:
        raise DataValidationError(
            "Playbill principal proposal name is invalid",
            errors=[str(exc)],
        ) from exc
    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id=actor_id),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/{actor_id}/{ref_name}",
            proposed_base_oid=proposed_base.git_oid,
        ),
        candidate_tree=candidate_tree,
        timestamp=timestamp,
    )
    return PlaybillProposalInspection(
        proposal=result,
        accepted_coordinate=PlaybillAcceptedCoordinate.from_internal(
            instance.accepted_coordinate()
        ),
    )


def service_inspect_playbill_proposal(
    instance: PlaybillInstance,
    *,
    proposal_id: str,
) -> PlaybillProposalInspection:
    evidence = instance.proposal_evidence()
    admission = evidence.read_admission(proposal_id)
    evaluation = evidence.read_evaluation(proposal_id)
    candidate = (
        evidence.read_candidate(evaluation.candidate_digest)
        if evaluation.candidate_digest is not None
        else None
    )
    return PlaybillProposalInspection(
        proposal=ProposalResult(
            admission=admission,
            evaluation=evaluation,
            candidate=candidate,
        ),
        accepted_coordinate=PlaybillAcceptedCoordinate.from_internal(
            instance.accepted_coordinate()
        ),
    )


def service_inspect_playbill_refusal(
    instance: PlaybillInstance,
    *,
    proposal_id: str,
) -> PlaybillRefusalInspection:
    evaluation = instance.proposal_evidence().read_evaluation(proposal_id)
    return PlaybillRefusalInspection(
        proposal_id=proposal_id,
        verdict=evaluation.verdict,
        diagnostics=evaluation.diagnostics,
    )


def _candidate_for_proposal(
    instance: PlaybillInstance,
    proposal_id: str,
) -> tuple[ProposalResult, CandidateRecordAnyVersion]:
    inspection = service_inspect_playbill_proposal(instance, proposal_id=proposal_id)
    candidate = inspection.proposal.candidate
    if candidate is None:
        raise ProposalIntegrityError("refused proposal has no approvable candidate")
    return inspection.proposal, candidate


def service_submit_playbill_approval(
    instance: PlaybillInstance,
    *,
    proposal_id: str,
    attestation: ApprovalAttestation,
    authenticated_submitter: str,
) -> PlaybillApprovalReceipt:
    """Verify and persist only a public attestation under historical key state."""

    _proposal, candidate = _candidate_for_proposal(instance, proposal_id)
    if attestation.payload_digest != candidate.candidate_digest:
        raise ProposalIntegrityError("attestation payload differs from proposal candidate")
    signing_generation = instance.generation_for_semantic_root(
        candidate.candidate.parent_semantic_root
    )
    submission = ApprovalSubmission(
        submitted_by=authenticated_submitter,
        attestation=attestation,
    )
    principal_lifecycle = all(
        member.artifact_kind == "principal-lifecycle" for member in candidate.members
    )
    verified = verify_approval(
        submission,
        candidate=candidate.candidate,
        principals=signing_generation.principals,
        purpose="principal-lifecycle" if principal_lifecycle else "ordinary-artifact",
    )
    instance.proposal_evidence().write_approval(candidate.candidate_digest, submission)
    return _approval_receipt(proposal_id, candidate, verified)


def _approval_receipt(
    proposal_id: str,
    candidate: CandidateRecordAnyVersion,
    verified: VerifiedApproval,
) -> PlaybillApprovalReceipt:
    return PlaybillApprovalReceipt(
        proposal_id=proposal_id,
        candidate_digest=candidate.candidate_digest,
        signer_id=verified.signer_id,
        submitted_by=verified.submission.submitted_by,
        signing_semantic_root=verified.submission.attestation.signing_semantic_root,
        attestation_digest=verified.digest.tagged,
        key_history_ref=verified.signer_key_history_ref,
    )


def service_activate_playbill_proposal(
    instance: PlaybillInstance,
    *,
    proposal_id: str,
) -> PlaybillActivationReceipt:
    """Settle, prebuild, and atomically activate one independently approved candidate."""

    proposal, candidate = _candidate_for_proposal(instance, proposal_id)
    evaluation = proposal.evaluation
    if evaluation.evaluated_tree_oid is None:
        raise ProposalIntegrityError("candidate evaluation is missing its exact tree")
    base = instance.coordinate_for_oid(evaluation.evaluated_base_oid)
    if candidate.candidate.parent_semantic_root != base.semantic_root:
        raise SettlementIntegrityError("candidate parent root differs from evaluated base")
    approvals = instance.proposal_evidence().read_approvals(candidate.candidate_digest)
    bundle = instance.prepare_generation(
        base=base,
        candidate_tree=instance.proposal_tree(evaluation.evaluated_tree_oid),
        candidate=candidate,
        approvals=approvals,
        actor_binding=ChangeActorBinding(
            actor_id=proposal.admission.actor_id,
            source_compilation_digest=proposal.admission.source_compilation_digest,
        ),
        sequence=instance.accepted_history()[-1].sequence + 1,
    )
    publisher = instance.activation_publisher()
    projection = publisher.prebuild(bundle, base=base)
    activation = publisher.activate(bundle, projection, base=base)
    if activation.status not in {"accepted", "lost_cas"}:
        raise SettlementIntegrityError("activation returned an unsupported terminal status")
    status = cast(Literal["accepted", "lost_cas"], activation.status)
    instance.refresh()
    return PlaybillActivationReceipt(
        proposal_id=proposal_id,
        status=status,
        accepted_coordinate=(
            PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())
            if activation.status == "accepted"
            else None
        ),
    )


def service_get_playbill_document(
    instance: PlaybillInstance,
    *,
    identity: str,
    access: BodyAccessContext,
    at: PlaybillAcceptedCoordinate | None = None,
) -> PlaybillDocumentView:
    coordinate = _resolve_coordinate(instance, at)
    generation = next(
        item for item in instance.accepted_history() if item.oid == coordinate.git_oid
    )
    if generation.sequence == 0:
        raise DocumentNotFoundError(identity)
    with instance.bind_accepted_projection(coordinate) as projection:
        document = projection.document(identity, access=access)
    if document is None:
        raise DocumentNotFoundError(identity)
    return _public_document(document)


def service_list_playbill_documents(
    instance: PlaybillInstance,
    *,
    access: BodyAccessContext,
    at: PlaybillAcceptedCoordinate | None = None,
) -> PlaybillDocumentList:
    coordinate = _resolve_coordinate(instance, at)
    generation = next(
        item for item in instance.accepted_history() if item.oid == coordinate.git_oid
    )
    if generation.sequence == 0:
        documents: tuple[PlaybillDocumentView, ...] = ()
    else:
        with instance.bind_accepted_projection(coordinate) as projection:
            documents = tuple(
                _public_document(item) for item in projection.list_documents(access=access)
            )
    return PlaybillDocumentList(
        coordinate=PlaybillAcceptedCoordinate.from_internal(coordinate),
        documents=documents,
    )


def service_list_playbill_principals(instance: PlaybillInstance) -> PlaybillPrincipalList:
    generation = instance.accepted_history()[-1]
    return PlaybillPrincipalList(
        coordinate=PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        principals=generation.principals.principals,
    )


def _fact_value(document: PlaybillDocumentView, schema_id: str, fact_key: str) -> object:
    matches = tuple(
        fact["value"]
        for fact in document.facts
        if fact["schema_id"] == schema_id and fact["fact_key"] == fact_key
    )
    if len(matches) != 1:
        raise ProposalIntegrityError("Document projection is missing one required typed fact")
    return matches[0]


def service_dereference_playbill_document(
    instance: PlaybillInstance,
    *,
    identity: str,
    access: BodyAccessContext,
    at: PlaybillAcceptedCoordinate | None = None,
) -> PlaybillBodyRead:
    document = service_get_playbill_document(instance, identity=identity, access=access, at=at)
    subject = _fact_value(document, "playbill.document.subject", "whole_document")
    metadata = _fact_value(document, "playbill.document.metadata", "metadata")
    if not isinstance(subject, dict) or not isinstance(metadata, dict):
        raise ProposalIntegrityError("Document projection facts have invalid public shapes")
    body_value = subject.get("body_digest")
    if not isinstance(body_value, dict) or not isinstance(body_value.get("$digest"), str):
        raise ProposalIntegrityError("Document subject fact is missing its body digest")
    media_type = metadata.get("media_type")
    if not isinstance(media_type, str):
        raise ProposalIntegrityError("Document metadata fact is missing its media type")
    body_digest = body_value["$digest"]
    content = instance.body_store().read(body_digest, access=access)
    return PlaybillBodyRead(
        identity=identity,
        coordinate=document.coordinate,
        body_digest=body_digest,
        media_type=media_type,
        content_base64=base64.b64encode(content).decode("ascii"),
    )


def service_playbill_document_history(
    instance: PlaybillInstance,
    *,
    identity: str,
) -> PlaybillDocumentHistory:
    document_id = identity.removeprefix("document:")
    if identity != f"document:{document_id}":
        raise DocumentNotFoundError(identity)
    path = document_path(document_id)
    entries: list[PlaybillDocumentHistoryEntry] = []
    for generation in instance.accepted_history()[1:]:
        record = generation.record
        if record is None or not any(member.path == path for member in record.members):
            continue
        content = instance.tree_at(generation.oid).get(path)
        if content is None:
            continue
        shell = parse_document(content, path=path)
        entries.append(
            PlaybillDocumentHistoryEntry(
                sequence=generation.sequence,
                coordinate=PlaybillAcceptedCoordinate.from_internal(
                    instance.coordinate_for_oid(generation.oid)
                ),
                envelope_digest=document_digest(shell).tagged,
                body_digest=shell.body_digest,
                predecessor_digest=shell.predecessor_digest,
                revision=shell.lifecycle.revision,
                change_set_path=f"changesets/cs-{record.sequence:020d}.json",
                changeset_digest=record.changeset_digest,
                candidate_digest=record.candidate_digest,
            )
        )
    if not entries:
        raise DocumentNotFoundError(identity)
    return PlaybillDocumentHistory(identity=identity, entries=tuple(entries))


__all__ = [
    "PlaybillAcceptedCoordinate",
    "PlaybillActivationReceipt",
    "PlaybillApprovalReceipt",
    "PlaybillBodyRead",
    "PlaybillDocumentHistory",
    "PlaybillDocumentHistoryEntry",
    "PlaybillDocumentList",
    "PlaybillDocumentView",
    "PlaybillProposalInspection",
    "PlaybillPrincipalList",
    "PlaybillRefusalInspection",
    "service_activate_playbill_proposal",
    "service_dereference_playbill_document",
    "service_get_playbill_document",
    "service_inspect_playbill_proposal",
    "service_inspect_playbill_refusal",
    "service_list_playbill_documents",
    "service_list_playbill_principals",
    "service_playbill_document_history",
    "service_propose_playbill_document",
    "service_propose_playbill_principal_change",
    "service_store_playbill_body",
    "service_submit_playbill_approval",
]
