"""Materialize one request's review groups from a single proposal snapshot."""

from __future__ import annotations

from collections.abc import Iterable

from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.proposal_models import (
    ProposalAdmissionRecord,
    ProposalEvaluationRecord,
)
from cruxible_core.indexes.proposals.proposal_note_projection import ProposalNoteIndex
from cruxible_core.proposals.proposal_evidence import ProposalEvidenceStore
from cruxible_core.proposals.proposal_notes import admission_bytes

_COMPLETE = (
    "admission_path IS NOT NULL AND evaluation_status!='missing' AND "
    "(candidate_digest IS NULL OR candidate_parent_semantic_root IS NOT NULL)"
)


def proposal_note_snapshot(
    evidence: ProposalEvidenceStore,
    *,
    oids: Iterable[str] | None = None,
    candidate_digests: Iterable[str] = (),
) -> ProposalNoteIndex:
    """Read exact groups once; approvals remain fresh under their existing locks.

    Passing no OIDs selects the full inventory for explicit review-ref export.
    Ordinary note operations select aliases/candidates and read only those groups.
    """
    assert evidence.index is not None
    with evidence.index.read(evidence, review_context=True) as connection:
        if oids is None:
            rows = connection.execute(f"SELECT * FROM proposals WHERE {_COMPLETE}").fetchall()
        else:
            aliases = set(oids)
            for digest in sorted(set(candidate_digests)):
                for row in connection.execute(
                    "SELECT candidate_commit_oid,review_commit_oid FROM proposals "
                    f"WHERE {_COMPLETE} AND candidate_digest=?",
                    (digest,),
                ):
                    aliases.update(value for value in row if value is not None)
            # Each branch uses its owned commit index. UNION coalesces proposals
            # whose submitted and advisory aliases both belong to this request.
            selected = {}
            for oid in sorted(aliases):
                for row in connection.execute(
                    f"SELECT * FROM proposals WHERE {_COMPLETE} AND candidate_commit_oid=? "
                    f"UNION SELECT * FROM proposals WHERE {_COMPLETE} AND review_commit_oid=?",
                    (oid, oid),
                ):
                    selected[row["proposal_id"]] = row
            rows = list(selected.values())
        admissions = {}
        evaluations = {}
        candidates = {}
        review_oids = {}
        groups: dict[str, set[str]] = {}
        for located in rows:
            row = dict(located)
            pid = row["proposal_id"]
            admission = evidence._read_located(
                row, "admission", ProposalAdmissionRecord, render=admission_bytes
            )
            evaluation = evidence._read_located(row, "evaluation", ProposalEvaluationRecord)
            candidate_digest = evaluation.candidate_digest
            if candidate_digest is not None and candidate_digest not in candidates:
                candidate = evidence.read_candidate_review_summary_if_present(candidate_digest)
                if candidate is None:
                    raise ProposalIntegrityError("selected proposal candidate evidence is missing")
                candidates[candidate_digest] = candidate
            admissions[pid], evaluations[pid] = admission, evaluation
            groups.setdefault(admission.candidate_commit_oid, set()).add(pid)
            if row["review_commit_oid"] is not None:
                review_oids[pid] = row["review_commit_oid"]
                groups.setdefault(row["review_commit_oid"], set()).add(pid)
    return ProposalNoteIndex(evidence, admissions, evaluations, candidates, review_oids, groups)
