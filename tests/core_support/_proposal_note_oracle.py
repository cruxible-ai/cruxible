"""Independent full-source grouping oracle for indexed proposal-note tests."""

from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.proposal_models import (
    ProposalAdmissionRecord,
    ProposalEvaluationRecord,
    ProposalTransportProtocol,
)
from cruxible_core.indexes.proposals.proposal_note_projection import ProposalNoteIndex
from cruxible_core.proposals.candidate_review_summary import CandidateReviewSummary
from cruxible_core.proposals.proposal_evidence import ProposalEvidenceStore


def build_proposal_note_oracle(
    evidence: ProposalEvidenceStore, transport: ProposalTransportProtocol
) -> ProposalNoteIndex:
    # Detach SQL so comparisons independently parse the complete source inventory.
    evidence = ProposalEvidenceStore(evidence.root)
    all_admissions = evidence.list_admissions()
    unique_admissions: dict[str, ProposalAdmissionRecord] = {}
    for admission in all_admissions:
        previous = unique_admissions.get(admission.proposal_id)
        if previous is not None and previous != admission:
            raise ProposalIntegrityError("proposal evidence contains conflicting admissions")
        unique_admissions[admission.proposal_id] = admission
    all_admissions = tuple(unique_admissions.values())
    admissions: dict[str, ProposalAdmissionRecord] = {}
    evaluations: dict[str, ProposalEvaluationRecord] = {}
    for record in evidence.list_evaluations():
        if record.proposal_id in evaluations:
            raise ProposalIntegrityError("proposal evidence contains multiple evaluations")
        evaluations[record.proposal_id] = record
    candidates: dict[str, CandidateReviewSummary] = {}
    review_oids: dict[str, str] = {}
    groups: dict[str, set[str]] = {}
    for admission in all_admissions:
        proposal_id = admission.proposal_id
        evaluation = evaluations.get(proposal_id)
        # Old or damaged inventories can contain incomplete admissions. An interrupted
        # unrelated write is not a complete note record and must not block
        # every subsequent authoring operation. Settlement reads its own
        # target strictly through the evidence store.
        if evaluation is None:
            continue
        digest = evaluation.candidate_digest
        if digest is not None and digest not in candidates:
            candidate = evidence.read_candidate_review_summary_if_present(digest)
            if candidate is None:
                continue
            candidates[digest] = candidate
        admissions[proposal_id] = admission
        groups.setdefault(admission.candidate_commit_oid, set()).add(proposal_id)
        if digest is None or evaluation.evaluated_tree_oid is None:
            continue
        oid = transport.proposal_review_commit_oid(
            tree_oid=evaluation.evaluated_tree_oid,
            base_oid=evaluation.evaluated_base_oid,
            actor_id=admission.actor_id,
            timestamp=admission.admitted_at,
            message=candidates[digest].message(rationale=admission.rationale),
        )
        review_oids[proposal_id] = oid
        groups.setdefault(oid, set()).add(proposal_id)
    return ProposalNoteIndex(evidence, admissions, evaluations, candidates, review_oids, groups)
