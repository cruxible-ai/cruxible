"""Structured candidate review and public approval-challenge services."""

from __future__ import annotations

import difflib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts import PlaybillSemanticFieldDelta
from cruxible_client.contracts.attestations import ApprovalStatement, approval_digest
from cruxible_client.contracts.candidates import (
    CandidateMemberEvidence,
    CandidateMemberLawEvidenceV2,
    CandidateRecord,
    CandidateRecordAnyVersion,
)
from cruxible_client.contracts.documents import parse_document
from cruxible_client.contracts.errors import ApprovalIntegrityError, ProposalIntegrityError
from cruxible_client.contracts.semantic import SourceMapping, whole_body_mapping
from cruxible_client.contracts.semantic_delta import semantic_field_delta
from cruxible_client.contracts.types import PrincipalRecord
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.service.documents import service_inspect_playbill_proposal


class _StrictReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlaybillReviewedDocument(_StrictReviewModel):
    path: str
    identity: str
    disposition: str
    title: str
    document_kind: str
    media_type: str
    base_body_digest: str | None
    candidate_body_digest: str
    links: tuple[dict[str, object], ...]
    pins: tuple[dict[str, object], ...]
    governance_scope: tuple[str, ...]
    base_source_mapping: SourceMapping | None
    candidate_source_mapping: SourceMapping | None
    readable_diff: str | None
    diff_unavailable_reason: str | None


class PlaybillReviewedMember(_StrictReviewModel):
    """One member in the atomic review; generated/invalidated members cannot disappear."""

    path: str
    artifact_kind: str
    disposition: str
    closure_role: Literal["authored", "generated_successor", "invalidation"]
    predecessor_artifact_digest: str | None
    candidate_artifact_digest: str | None
    base_semantic_artifact: dict[str, object] | None
    candidate_semantic_artifact: dict[str, object] | None
    semantic_delta: tuple[PlaybillSemanticFieldDelta, ...]
    law_identifier: str
    law_digest: str
    law_evidence: dict[str, object]
    dependency_proof_refs: tuple[dict[str, object], ...]


class PlaybillProposalReview(_StrictReviewModel):
    tag: Literal["playbill-proposal-review-v1"] = "playbill-proposal-review-v1"
    coordinate_kind: Literal["provisional"] = "provisional"
    proposal_id: str
    candidate: CandidateRecordAnyVersion
    candidate_digest: str
    parent_semantic_root: str
    settlement_base: AcceptedCoordinate
    base_oid: str
    complete_members: tuple[CandidateMemberEvidence | CandidateMemberLawEvidenceV2, ...]
    members: tuple[PlaybillReviewedMember, ...]
    governance: dict[str, object]
    provenance: dict[str, object]
    attestation_coverage: dict[str, object]
    documents: tuple[PlaybillReviewedDocument, ...]
    redactions: tuple[str, ...]


class PlaybillApprovalChallenge(_StrictReviewModel):
    tag: Literal["playbill-approval-challenge-v1"] = "playbill-approval-challenge-v1"
    proposal_id: str
    signer_principal: PrincipalRecord
    signer_key_history_ref: str
    statement: ApprovalStatement
    review: PlaybillProposalReview


def _text_body(media_type: str, body: bytes) -> str | None:
    textual = media_type.startswith("text/") or media_type in {
        "application/json",
        "application/toml",
        "application/xml",
        "application/yaml",
    }
    if not textual:
        return None
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _readable_diff(
    *,
    path: str,
    media_type: str,
    base_body: bytes,
    candidate_body: bytes,
) -> tuple[str | None, str | None]:
    base_text = _text_body(media_type, base_body)
    candidate_text = _text_body(media_type, candidate_body)
    if base_text is None or candidate_text is None:
        return None, "body is not a supported UTF-8 text media type"
    diff = "".join(
        difflib.unified_diff(
            base_text.splitlines(keepends=True),
            candidate_text.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="\n",
        )
    )
    return diff, None


def _review_document(
    instance: PlaybillInstance,
    *,
    member: CandidateMemberEvidence | CandidateMemberLawEvidenceV2,
    base_tree: dict[str, bytes],
    candidate_tree: dict[str, bytes],
    access: BodyAccessContext,
) -> PlaybillReviewedDocument:
    candidate_content = candidate_tree.get(member.path)
    if candidate_content is None:
        raise ProposalIntegrityError("candidate review member is missing from its exact tree")
    candidate = parse_document(candidate_content, path=member.path)
    base_content = base_tree.get(member.path)
    base = parse_document(base_content, path=member.path) if base_content is not None else None
    base_mapping: SourceMapping | None = None
    candidate_mapping: SourceMapping | None = None
    readable_diff: str | None = None
    unavailable: str | None = "body access is denied"
    if access.can_read_body:
        candidate_body = instance.body_store().read(candidate.body_digest, access=access)
        base_body = (
            b"" if base is None else instance.body_store().read(base.body_digest, access=access)
        )
        candidate_mapping = whole_body_mapping(
            member.path,
            candidate.body_digest,
            len(candidate_body),
        )
        if base is not None:
            base_mapping = whole_body_mapping(member.path, base.body_digest, len(base_body))
        readable_diff, unavailable = _readable_diff(
            path=member.path,
            media_type=candidate.media_type,
            base_body=base_body,
            candidate_body=candidate_body,
        )
    return PlaybillReviewedDocument(
        path=member.path,
        identity=candidate.identity,
        disposition=member.disposition,
        title=candidate.title,
        document_kind=candidate.document_kind,
        media_type=candidate.media_type,
        base_body_digest=None if base is None else base.body_digest,
        candidate_body_digest=candidate.body_digest,
        links=tuple(item.model_dump(mode="json") for item in candidate.links),
        pins=tuple(item.model_dump(mode="json") for item in candidate.pins),
        governance_scope=candidate.governance_scope,
        base_source_mapping=base_mapping,
        candidate_source_mapping=candidate_mapping,
        readable_diff=readable_diff,
        diff_unavailable_reason=unavailable,
    )


def _review_members(
    candidate: CandidateRecordAnyVersion,
    *,
    base_tree: dict[str, bytes],
    candidate_tree: dict[str, bytes],
) -> tuple[PlaybillReviewedMember, ...]:
    def semantic_artifact(content: bytes | None) -> dict[str, object] | None:
        if content is None:
            return None
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProposalIntegrityError("review member is not canonical structured data") from exc
        if not isinstance(value, dict):
            raise ProposalIntegrityError("review member must be a canonical object")
        return value

    def member_delta(
        base: dict[str, object] | None,
        candidate_value: dict[str, object] | None,
    ) -> tuple[PlaybillSemanticFieldDelta, ...]:
        return semantic_field_delta(base or {}, candidate_value or {})

    if not isinstance(candidate, CandidateRecord):
        evidence_by_path = {item.path: item for item in candidate.law_evidence}
        reviewed: list[PlaybillReviewedMember] = []
        for member in candidate.members:
            base_artifact = semantic_artifact(base_tree.get(member.path))
            candidate_artifact = semantic_artifact(candidate_tree.get(member.path))
            reviewed.append(
                PlaybillReviewedMember(
                    path=member.path,
                    artifact_kind=member.artifact_kind,
                    disposition=member.disposition,
                    closure_role=member.closure_role,
                    predecessor_artifact_digest=member.predecessor_artifact_digest,
                    candidate_artifact_digest=member.candidate_artifact_digest,
                    base_semantic_artifact=base_artifact,
                    candidate_semantic_artifact=candidate_artifact,
                    semantic_delta=member_delta(base_artifact, candidate_artifact),
                    law_identifier=member.law_identifier,
                    law_digest=member.law_digest,
                    law_evidence=evidence_by_path[member.path].model_dump(mode="json"),
                    dependency_proof_refs=tuple(
                        item.model_dump(mode="json") for item in member.dependency_proof_refs
                    ),
                )
            )
        return tuple(reviewed)
    reviewed = []
    for member in candidate.members:
        base_artifact = semantic_artifact(base_tree.get(member.path))
        candidate_artifact = semantic_artifact(candidate_tree.get(member.path))
        reviewed.append(
            PlaybillReviewedMember(
                path=member.path,
                artifact_kind=member.artifact_kind,
                disposition=member.disposition,
                closure_role="authored",
                predecessor_artifact_digest=None,
                candidate_artifact_digest=member.artifact_digest,
                base_semantic_artifact=base_artifact,
                candidate_semantic_artifact=candidate_artifact,
                semantic_delta=member_delta(base_artifact, candidate_artifact),
                law_identifier=member.law_identifier,
                law_digest=candidate.law_digests[member.law_identifier],
                law_evidence={
                    "artifact_digest": member.artifact_digest,
                    "governance_operation": member.governance_operation,
                    "tag": "playbill-singleton-law-evidence-v1",
                },
                dependency_proof_refs=(),
            )
        )
    return tuple(reviewed)


def service_review_playbill_proposal(
    instance: PlaybillInstance,
    *,
    proposal_id: str,
    access: BodyAccessContext,
) -> PlaybillProposalReview:
    """Render one immutable candidate from its recorded base and proposal tree."""

    inspection = service_inspect_playbill_proposal(instance, proposal_id=proposal_id)
    proposal = inspection.proposal
    candidate = proposal.candidate
    if candidate is None or proposal.evaluation.evaluated_tree_oid is None:
        raise ProposalIntegrityError("refused proposal has no reviewable candidate")
    base = instance.coordinate_for_oid(proposal.evaluation.evaluated_base_oid)
    base_public = AcceptedCoordinate.from_internal(base)
    base_tree = instance.tree_at(base.git_oid)
    candidate_tree = instance.proposal_tree(proposal.evaluation.evaluated_tree_oid)
    documents = tuple(
        _review_document(
            instance,
            member=member,
            base_tree=base_tree,
            candidate_tree=candidate_tree,
            access=access,
        )
        for member in candidate.members
        if member.artifact_kind == "document"
    )
    approvals = instance.proposal_evidence().read_approvals(candidate.candidate_digest)
    attestation_items = [
        {
            "attestation_digest": approval_digest(item.attestation).tagged,
            "coverage": "containing_change_set",
            "signed_payload_digest": item.attestation.payload_digest,
            "signer_id": item.attestation.signer_id,
            "signing_semantic_root": item.attestation.signing_semantic_root,
            "submitted_by": item.submitted_by,
        }
        for item in approvals
    ]
    redactions = () if access.can_read_body else ("body", "readable_diff", "source_mapping")
    return PlaybillProposalReview(
        proposal_id=proposal_id,
        candidate=candidate,
        candidate_digest=candidate.candidate_digest,
        parent_semantic_root=candidate.candidate.parent_semantic_root,
        settlement_base=base_public,
        base_oid=base.git_oid,
        complete_members=candidate.members,
        members=_review_members(
            candidate,
            base_tree=base_tree,
            candidate_tree=candidate_tree,
        ),
        governance={
            "activation_policy": candidate.activation_policy,
            "approval_requirements": [
                item.model_dump(mode="json") for item in candidate.approval_requirements
            ],
            "law_digests": candidate.law_digests,
            "required_tier": candidate.required_tier,
        },
        provenance={
            "actor_id": proposal.admission.actor_id,
            "source_compilation_digest": proposal.admission.source_compilation_digest,
        },
        attestation_coverage={
            "attestations": attestation_items,
            "coverage": "containing_change_set",
            "coverage_basis": {
                "candidate_digest": candidate.candidate_digest,
                "scope": list(candidate.candidate.scope),
            },
        },
        documents=documents,
        redactions=redactions,
    )


def service_prepare_playbill_approval(
    instance: PlaybillInstance,
    *,
    proposal_id: str,
    signer_id: str,
    access: BodyAccessContext,
) -> PlaybillApprovalChallenge:
    """Return the exact public statement a client signer may choose to approve."""

    review = service_review_playbill_proposal(instance, proposal_id=proposal_id, access=access)
    generation = instance.generation_for_semantic_root(review.parent_semantic_root)
    principal = generation.principals.require_active(signer_id)
    if principal.kind == "daemon":
        raise ApprovalIntegrityError("daemon identity cannot provide client approval")
    principal_lifecycle = all(
        member.artifact_kind == "principal-lifecycle" for member in review.complete_members
    )
    creator_principal_id = str(review.provenance["actor_id"])
    if (
        not principal_lifecycle
        and review.candidate.approval_requirements
        and signer_id == creator_principal_id
    ):
        raise ApprovalIntegrityError(
            "playbill.approval.creator_forbidden: independent_approval_required mode needs "
            "an active ordinary approver other than the candidate creator; after that "
            "eligible signer approves, run playbill proposal activate"
        )
    if not principal_lifecycle and principal.kind == "recovery":
        raise ApprovalIntegrityError("recovery principal cannot approve ordinary Documents")
    return PlaybillApprovalChallenge(
        proposal_id=proposal_id,
        signer_principal=principal,
        signer_key_history_ref=generation.principals.key_history_reference(signer_id),
        statement=ApprovalStatement(
            signer_id=signer_id,
            signing_semantic_root=review.parent_semantic_root,
            payload_digest=review.candidate_digest,
        ),
        review=review,
    )


def render_playbill_proposal_review(review: PlaybillProposalReview) -> str:
    """Small presentation renderer over the complete structured review contract."""

    lines = [
        f"Proposal: {review.proposal_id}",
        f"Candidate: {review.candidate_digest}",
        f"Parent semantic root: {review.parent_semantic_root}",
        f"Settlement base OID: {review.base_oid}",
        f"Proposal admission tier: {review.candidate.required_tier}",
        "Approve requires: graph_write",
        "Activate requires: graph_write",
        "Required approvals: "
        + (
            ", ".join(
                f"{item.minimum_distinct_signers} {item.role}"
                for item in review.candidate.approval_requirements
            )
            or (
                f"{review.provenance.get('actor_id', 'the proposing actor')}'s "
                "own signature (lifecycle actor binding)"
                if all(
                    member.artifact_kind == "principal-lifecycle"
                    for member in review.complete_members
                )
                and review.complete_members
                else "none"
            )
        ),
    ]
    for document in review.documents:
        lines.extend(
            (
                "",
                f"{document.title} ({document.document_kind})",
                f"Body: {document.base_body_digest or '<new>'} -> {document.candidate_body_digest}",
                f"Path: {document.path}",
            )
        )
        if document.readable_diff is not None:
            lines.extend(("", document.readable_diff.rstrip("\n")))
        elif document.diff_unavailable_reason is not None:
            lines.append(f"Diff unavailable: {document.diff_unavailable_reason}")
    non_documents = tuple(member for member in review.members if member.artifact_kind != "document")
    for member in non_documents:
        lines.extend(
            (
                "",
                f"{member.path} ({member.artifact_kind})",
                f"Closure role: {member.closure_role}",
                "Artifact: "
                f"{member.predecessor_artifact_digest or '<new>'} -> "
                f"{member.candidate_artifact_digest or '<deleted>'}",
                f"Law: {member.law_identifier} ({member.law_digest})",
            )
        )
    for member in review.members:
        lines.extend(("", f"Semantic delta: {member.path}"))
        if not member.semantic_delta:
            lines.append("  (no semantic field changes)")
            continue
        for row in member.semantic_delta:
            before = (
                "<absent>"
                if row.before.state == "absent"
                else json.dumps(row.before.value, sort_keys=True, ensure_ascii=False)
            )
            after = (
                "<absent>"
                if row.after.state == "absent"
                else json.dumps(row.after.value, sort_keys=True, ensure_ascii=False)
            )
            lines.append(f"  {row.field_path or '/'}: {before} -> {after}")
    return "\n".join(lines) + "\n"


__all__ = [
    "PlaybillApprovalChallenge",
    "PlaybillProposalReview",
    "PlaybillReviewedDocument",
    "PlaybillReviewedMember",
    "render_playbill_proposal_review",
    "service_prepare_playbill_approval",
    "service_review_playbill_proposal",
]
