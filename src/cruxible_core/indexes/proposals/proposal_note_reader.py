"""Scoped review-note reads over the shared proposal locators.

The retained cold builder is an explicit recovery oracle. Ordinary grouping uses
SQLite indexes; these mappings contain no inventories, parsed records or groups.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import Any, TypeVar

from cruxible_client.contracts.proposal_models import (
    ProposalAdmissionRecord,
    ProposalEvaluationRecord,
)
from cruxible_core.indexes.proposals.proposal_note_projection import ProposalNoteIndex
from cruxible_core.proposals.candidate_review_summary import CandidateReviewSummary
from cruxible_core.proposals.proposal_evidence import ProposalEvidenceStore

T = TypeVar("T")
_COMPLETE = (
    "evaluation_status!='missing' AND "
    "(candidate_digest IS NULL OR candidate_parent_semantic_root IS NOT NULL)"
)


class _Mapping(Mapping[str, T]):
    def __init__(self, keys: Callable[[], tuple[str, ...]], get: Callable[[str], T]) -> None:
        self._keys, self._get = keys, get

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys())

    def __len__(self) -> int:
        return len(self._keys())

    def __getitem__(self, key: str) -> T:
        return self._get(key)


class IndexedProposalNotes(ProposalNoteIndex):
    """The existing note publisher with SQL-backed, source-verifying accessors."""

    def __init__(self, evidence: ProposalEvidenceStore) -> None:
        assert evidence.index is not None
        self.evidence = evidence
        self._store = evidence
        self._index = evidence.index
        # Establish current source and Git review context before handing out a
        # reader. Every subsequent selected source read still verifies its bytes.
        with self._index.read(evidence, review_context=True):
            pass

        def ids() -> tuple[str, ...]:
            return self._keys("proposal_id")

        self.admissions = _Mapping(ids, self._admission)
        self.evaluations = _Mapping(ids, self._evaluation)
        self.candidates = _Mapping(
            lambda: self._keys("candidate_digest", "candidate_digest IS NOT NULL"), self._candidate
        )
        self.review_oids = _Mapping(
            lambda: self._keys("proposal_id", "review_commit_oid IS NOT NULL"), self._review_oid
        )
        self.proposal_ids_by_oid = _Mapping(self._oids, self._group)
        self.proposal_ids_by_candidate = _Mapping(
            lambda: self._keys("candidate_digest", "candidate_digest IS NOT NULL"),
            self._candidate_group,
        )

    def _keys(self, column: str, where: str = "1") -> tuple[str, ...]:
        with self._index.read(self._store) as connection:
            return tuple(
                row[0]
                for row in connection.execute(
                    f"SELECT DISTINCT {column} FROM proposals WHERE {_COMPLETE} "
                    f"AND {where} ORDER BY {column}"
                )
            )

    def _row(self, pid: str) -> dict[str, Any]:
        rows = self._index.rows(self._store, _COMPLETE + " AND proposal_id=?", (pid,))
        if not rows:
            raise KeyError(pid)
        return rows[0]

    def _admission(self, pid: str) -> ProposalAdmissionRecord:
        self._row(pid)
        return self._store.read_admission(pid)

    def _evaluation(self, pid: str) -> ProposalEvaluationRecord:
        self._row(pid)
        return self._store.read_evaluation(pid)

    def _candidate(self, digest: str) -> CandidateReviewSummary:
        if not self._index.rows(self._store, _COMPLETE + " AND candidate_digest=?", (digest,)):
            raise KeyError(digest)
        value = self._store.read_candidate_review_summary_if_present(digest)
        if value is None:
            raise KeyError(digest)
        return value

    def _review_oid(self, pid: str) -> str:
        value = self._row(pid)["review_commit_oid"]
        if value is None:
            raise KeyError(pid)
        return str(value)

    def _oids(self) -> tuple[str, ...]:
        with self._index.read(self._store) as connection:
            return tuple(
                row[0]
                for row in connection.execute(
                    f"SELECT candidate_commit_oid FROM proposals WHERE {_COMPLETE} "
                    f"UNION SELECT review_commit_oid FROM proposals WHERE {_COMPLETE} "
                    "AND review_commit_oid IS NOT NULL ORDER BY 1"
                )
            )

    def _group(self, oid: str) -> set[str]:
        with self._index.read(self._store) as connection:
            result = {
                row[0]
                for row in connection.execute(
                    f"SELECT proposal_id FROM proposals WHERE {_COMPLETE} "
                    "AND candidate_commit_oid=? "
                    f"UNION SELECT proposal_id FROM proposals WHERE {_COMPLETE} "
                    "AND review_commit_oid=?",
                    (oid, oid),
                )
            }
        if not result:
            raise KeyError(oid)
        return result

    def _candidate_group(self, digest: str) -> set[str]:
        result = {
            row["proposal_id"]
            for row in self._index.rows(
                self._store, _COMPLETE + " AND candidate_digest=?", (digest,)
            )
        }
        if not result:
            raise KeyError(digest)
        return result

    def note_bytes(self, oid: str) -> dict[str, bytes]:
        for digest in self.candidate_digests(oid):
            self._candidate(digest)
        return super().note_bytes(oid)

    def oids_for_candidate(self, digest: str) -> set[str]:
        return {
            oid
            for row in self._index.rows(
                self._store, _COMPLETE + " AND candidate_digest=?", (digest,)
            )
            for oid in (row["candidate_commit_oid"], row["review_commit_oid"])
            if oid is not None
        }
