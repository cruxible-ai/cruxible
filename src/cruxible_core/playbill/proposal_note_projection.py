"""One evidence-derived note group for every original and advisory commit alias.

Callers hold the instance review-projection lock while building and using an
index. Git identity does not distinguish admissions with the same tree, prose,
actor and timestamp, so one note must carry all their records. Existing single
admissions retain the original two-line evaluation and signer-list byte shapes.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

from cruxible_client.contracts.attestations import ApprovalSubmission
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import CanonicalEncodingError, ProposalIntegrityError
from cruxible_client.contracts.proposal_models import (
    ProposalAdmissionRecord,
    ProposalEvaluationRecord,
    ProposalTransportProtocol,
)
from cruxible_core.playbill.candidate_review_summary import CandidateReviewSummary
from cruxible_core.playbill.proposal_notes import proposal_approval_note, proposal_evaluation_note


class ProposalNoteEvidence(Protocol):
    def list_admissions(self) -> tuple[ProposalAdmissionRecord, ...]: ...
    def list_evaluations(self) -> tuple[ProposalEvaluationRecord, ...]: ...
    def read_candidate_review_summary_if_present(
        self, digest: str
    ) -> CandidateReviewSummary | None: ...
    def read_approvals(self, digest: str) -> tuple[ApprovalSubmission, ...]: ...


@dataclass
class ProposalNoteIndex:
    evidence: ProposalNoteEvidence
    admissions: dict[str, ProposalAdmissionRecord]
    evaluations: dict[str, ProposalEvaluationRecord]
    candidates: dict[str, CandidateReviewSummary]
    review_oids: dict[str, str]
    proposal_ids_by_oid: dict[str, set[str]]

    @classmethod
    def build(
        cls, evidence: ProposalNoteEvidence, transport: ProposalTransportProtocol
    ) -> ProposalNoteIndex:
        all_admissions = evidence.list_admissions()
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
            # Admission precedes evaluation/candidate persistence. An interrupted
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
        return cls(evidence, admissions, evaluations, candidates, review_oids, groups)

    def oids_for_candidate(self, digest: str) -> set[str]:
        return {
            oid
            for oid, ids in self.proposal_ids_by_oid.items()
            if any(self.evaluations[item].candidate_digest == digest for item in ids)
        }

    def candidate_digests(self, oid: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    digest
                    for item in self.proposal_ids_by_oid.get(oid, ())
                    if (digest := self.evaluations[item].candidate_digest) is not None
                }
            )
        )

    def note_bytes(self, oid: str) -> dict[str, bytes]:
        ids = sorted(self.proposal_ids_by_oid.get(oid, ()))
        evaluations = b"".join(
            proposal_evaluation_note(
                admission=self.admissions[item], evaluation=self.evaluations[item]
            )
            for item in ids
        )
        # Candidate order followed by each store's signer order preserves the
        # historical one-candidate bytes and keeps distinct signed payloads.
        approvals = tuple(
            approval
            for digest in self.candidate_digests(oid)
            for approval in self.evidence.read_approvals(digest)
        )
        return {"evaluation": evaluations, "approval": proposal_approval_note(approvals)}

    def validate_and_snapshot(
        self, transport: ProposalTransportProtocol, oids: Iterable[str]
    ) -> dict[tuple[str, str], bytes | None]:
        """Prove the old projection before a durable addition changes its group."""
        previous: dict[tuple[str, str], bytes | None] = {}
        for oid in sorted(set(oids)):
            expected = self.note_bytes(oid)
            for kind in ("evaluation", "approval"):
                stored = transport.read_proposal_note(kind, oid)
                if stored is not None and stored != expected[kind]:
                    if not self._valid_subset(kind, stored, expected[kind]):
                        self._refuse(kind)
                previous[kind, oid] = stored
        return previous

    def publish(
        self,
        transport: ProposalTransportProtocol,
        oids: Iterable[str],
        *,
        previous: Mapping[tuple[str, str], bytes | None] | None = None,
    ) -> None:
        """Repair absent/incomplete projections or advance a verified old group."""
        for oid in sorted(set(oids)):
            if not transport.object_exists(oid):
                if any(
                    self.admissions[item].candidate_commit_oid == oid
                    for item in self.proposal_ids_by_oid.get(oid, ())
                ):
                    raise ProposalIntegrityError("original proposal commit is missing")
                # An unmaterialized advisory alias has no reader yet. Normal
                # reconciliation creates/retains it before publishing its notes.
                continue
            for kind, content in self.note_bytes(oid).items():
                stored = transport.read_proposal_note(kind, oid)
                if stored == content:
                    continue
                if stored is not None and (
                    previous is None or (kind, oid) not in previous or stored != previous[kind, oid]
                ):
                    if not self._valid_subset(kind, stored, content):
                        self._refuse(kind)
                if kind == "approval" and content == b"[]\n":
                    continue
                if kind == "evaluation" and not content:
                    continue
                transport.write_proposal_note(kind, oid, content)

    @staticmethod
    def _valid_subset(kind: str, stored: bytes, expected: bytes) -> bool:
        """Recognize an incomplete prior projection without accepting edited facts.

        Persistence can lead note publication across a crash. Only a nonempty,
        canonically ordered subset of exact present evidence is repairable.
        Empty approval notes were never emitted and remain corruption refusals.
        """
        if kind == "evaluation":
            actual_lines = stored.splitlines(keepends=True)
            expected_lines = expected.splitlines(keepends=True)
            if not actual_lines or len(actual_lines) % 2:
                return False
            actual_members = [
                b"".join(actual_lines[i : i + 2]) for i in range(0, len(actual_lines), 2)
            ]
            expected_members = [
                b"".join(expected_lines[i : i + 2]) for i in range(0, len(expected_lines), 2)
            ]
        else:
            try:
                actual = json.loads(stored)
                desired = json.loads(expected)
                if (
                    not isinstance(actual, list)
                    or not actual
                    or canonical_bytes(actual) + b"\n" != stored
                ):
                    return False
                actual_members = [canonical_bytes(item) for item in actual]
                expected_members = [canonical_bytes(item) for item in desired]
            except (ValueError, UnicodeError, CanonicalEncodingError):
                return False
        positions = {item: index for index, item in enumerate(expected_members)}
        if any(item not in positions for item in actual_members):
            return False
        indices = [positions[item] for item in actual_members]
        return indices == sorted(set(indices))

    @staticmethod
    def _refuse(kind: str) -> None:
        raise ProposalIntegrityError(
            "playbill.proposal.note_disagrees_with_evidence: the "
            f"{kind} note differs from the complete proposal evidence group"
        )
