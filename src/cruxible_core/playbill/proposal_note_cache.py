"""Incremental review relationships over freshly verified evidence bytes.

The caller holds the review projection lock. Every load observes the complete
admission/evaluation inventory and freshly reads referenced candidate bytes, so
other writers, interrupted persistence, replacements and deletions remain visible.
Only unchanged decoding and alias derivation are reused. Git notes and approvals
are never cached. Cold reconstruction remains the independent oracle.
"""

from __future__ import annotations

import copy
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, TypeVar

from pydantic import BaseModel

from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.proposal_models import (
    ProposalAdmissionRecord,
    ProposalEvaluationRecord,
    ProposalTransportProtocol,
)
from cruxible_core.playbill.candidate_review_summary import CandidateReviewSummary
from cruxible_core.playbill.proposal_note_projection import ProposalNoteIndex
from cruxible_core.playbill.proposal_notes import admission_bytes

if TYPE_CHECKING:
    from cruxible_core.playbill.proposal_evidence import ProposalEvidenceStore

# Bounds account for source bytes and record count, not Python heap overhead.
MAX_RECORDS = 20_000
MAX_RECORD_BYTES = 16 * 1024 * 1024
T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class _Record:
    digest: bytes
    size: int
    value: BaseModel


def _read(path: Path) -> bytes:
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ProposalIntegrityError("proposal index evidence is not a regular file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            return stream.read()
    except OSError as exc:
        raise ProposalIntegrityError("proposal index evidence is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


class ProposalNoteCache:
    """Instance-owned disposable index; access only under its review lock."""

    def __init__(self) -> None:
        self._records: dict[Path, _Record] = {}
        self._index: ProposalNoteIndex | None = None

    def clear(self) -> None:
        self._records = {}
        self._index = None

    def load(
        self, evidence: ProposalEvidenceStore, transport: ProposalTransportProtocol
    ) -> ProposalNoteIndex:
        records: dict[Path, _Record] = {}

        def inventory(
            directory: Path, model: type[T], render: Callable[[Any], bytes] | None = None
        ) -> tuple[T, ...]:
            if directory.is_symlink() or not directory.is_dir():
                raise ProposalIntegrityError("proposal index directory is not trustworthy")
            values = []
            for path in sorted(directory.glob("*.json")):
                raw = _read(path)
                fingerprint = hashlib.sha256(raw).digest()
                old = self._records.get(path)
                if old is not None and old.digest == fingerprint and old.size == len(raw):
                    value = old.value
                else:
                    value = evidence.parse_model_bytes(
                        raw, model, label="proposal index", render=render
                    )
                records[path] = _Record(fingerprint, len(raw), value)
                assert isinstance(value, model)
                values.append(value)
            return tuple(values)

        try:
            admissions = inventory(evidence.proposals, ProposalAdmissionRecord, admission_bytes)
            evaluations = inventory(evidence.evaluations, ProposalEvaluationRecord)
            by_id = {r.proposal_id: r for r in evaluations}
            if len(by_id) != len(evaluations):
                raise ProposalIntegrityError("proposal evidence contains multiple evaluations")
            # The historical builder permits duplicate admission IDs under
            # foreign filenames. Preserve its behavior without guessing ownership.
            if len({r.proposal_id for r in admissions}) != len(admissions):
                self.clear()
                return ProposalNoteIndex.build(evidence, transport)
            observed_candidates: dict[str, CandidateReviewSummary | None] = {}
            complete = {}
            for admission in admissions:
                evaluation = by_id.get(admission.proposal_id)
                if evaluation is None:
                    continue
                digest = evaluation.candidate_digest
                if digest is not None:
                    if digest not in observed_candidates:
                        observed_candidates[digest] = (
                            evidence.read_candidate_review_summary_if_present(digest)
                        )
                    if observed_candidates[digest] is None:
                        continue
                complete[admission.proposal_id] = admission
            candidates = {
                key: value for key, value in observed_candidates.items() if value is not None
            }
            previous = self._index
            aliases = {} if previous is None else dict(previous.review_oids)
            groups = (
                {}
                if previous is None
                else {oid: set(ids) for oid, ids in previous.proposal_ids_by_oid.items()}
            )
            old_admissions = {} if previous is None else previous.admissions
            affected = set(old_admissions) | set(complete)
            if previous is not None:
                affected = {
                    pid
                    for pid in affected
                    if old_admissions.get(pid) != complete.get(pid)
                    or previous.evaluations.get(pid) != by_id.get(pid)
                    or (
                        (evaluation := by_id.get(pid)) is not None
                        and evaluation.candidate_digest is not None
                        and previous.candidates.get(evaluation.candidate_digest)
                        != candidates.get(evaluation.candidate_digest)
                    )
                }
            for pid in sorted(affected):
                old = old_admissions.get(pid)
                old_alias = aliases.pop(pid, None)
                if old is not None:
                    for oid in {old.candidate_commit_oid, old_alias}:
                        if oid is None:
                            continue
                        members = groups[oid]
                        members.discard(pid)
                        if not members:
                            del groups[oid]
                current_admission = complete.get(pid)
                if current_admission is None:
                    continue
                groups.setdefault(current_admission.candidate_commit_oid, set()).add(pid)
                evaluation = by_id[pid]
                digest = evaluation.candidate_digest
                if digest is None or evaluation.evaluated_tree_oid is None:
                    continue
                oid = transport.proposal_review_commit_oid(
                    tree_oid=evaluation.evaluated_tree_oid,
                    base_oid=evaluation.evaluated_base_oid,
                    actor_id=current_admission.actor_id,
                    timestamp=current_admission.admitted_at,
                    message=candidates[digest].message(rationale=current_admission.rationale),
                )
                aliases[pid] = oid
                groups.setdefault(oid, set()).add(pid)
            index = ProposalNoteIndex(evidence, complete, by_id, candidates, aliases, groups)
            # Keep the previous snapshot until the entire new inventory passes.
            if (
                len(records) <= MAX_RECORDS
                and sum(r.size for r in records.values())
                + sum(len((c.summary + c.member_roll).encode("utf-8")) for c in candidates.values())
                <= MAX_RECORD_BYTES
            ):
                self._records = records
                self._index = index
            else:
                self.clear()
            # Models contain nested mutable values. A caller cannot poison the
            # retained proof by modifying a returned record or membership set.
            return copy.deepcopy(index, memo={id(evidence): evidence})
        except Exception:
            self.clear()
            raise
