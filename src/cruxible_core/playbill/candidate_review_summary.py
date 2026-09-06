"""Bounded, disposable review metadata proven by freshly read candidate bytes.

Full candidate validation remains the source of each summary. A hit skips only
decoding/revalidating unchanged canonical bytes; file metadata is never proof.
No candidate models, raw evidence bytes, admissions, or approvals are retained.
"""

from __future__ import annotations

import hashlib
import os
import stat
import threading
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from cruxible_client.contracts.candidates import CandidateRecordAnyVersion, render_candidate_record
from cruxible_client.contracts.canonical import CandidateDigest
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_core.playbill.proposal_message import (
    CandidateMember,
    change_set_summary,
    member_line,
    message_from_parts,
)

# Accounted retained UTF-8 fields, not a bound on interpreter heap overhead.
MAX_ENTRIES = 512
MAX_RETAINED_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CandidateReviewSummary:
    """Immutable metadata for review prose and open-versus-settled classification."""

    parent_semantic_root: str
    summary: str
    member_roll: str

    def message(self, *, rationale: str | None = None) -> str:
        return message_from_parts(
            self.summary if rationale is None else rationale, self.member_roll
        )


@dataclass(frozen=True, slots=True)
class _Entry:
    byte_length: int
    bytes_digest: bytes
    summary: CandidateReviewSummary
    weight: int


_cache: OrderedDict[tuple[str, str], _Entry] = OrderedDict()
_lock = threading.Lock()


def read_candidate_bytes(directory: Path, digest: str, *, missing_ok: bool = False) -> bytes | None:
    """Read one regular candidate through a non-symlink directory and leaf."""

    CandidateDigest.from_tagged(digest)
    directory_fd: int | None = None
    file_fd: int | None = None
    try:
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        name = digest.removeprefix("sha256:") + ".json"
        try:
            # O_NONBLOCK prevents a raced-in FIFO from blocking before fstat.
            file_fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
            )
        except FileNotFoundError:
            if missing_ok:
                return None
            raise ProposalIntegrityError(
                "validated candidate evidence is missing or not a regular file"
            ) from None
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise ProposalIntegrityError(
                "validated candidate evidence is missing or not a regular file"
            )
        with os.fdopen(file_fd, "rb") as stream:
            file_fd = None
            return stream.read()
    except OSError as exc:
        raise ProposalIntegrityError("validated candidate evidence is malformed") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def parse_candidate_evidence(raw: bytes, *, expected_digest: str) -> CandidateRecordAnyVersion:
    """Validate canonical candidate bytes and their requested digest identity."""

    try:
        adapter: TypeAdapter[CandidateRecordAnyVersion] = TypeAdapter(CandidateRecordAnyVersion)
        value = adapter.validate_json(raw)
    except (ValidationError, ValueError) as exc:
        raise ProposalIntegrityError("validated candidate evidence is malformed") from exc
    if render_candidate_record(value) != raw:
        raise ProposalIntegrityError("validated candidate evidence is not canonical")
    if value.candidate_digest != expected_digest:
        raise ProposalIntegrityError("validated candidate evidence names a different candidate")
    return value


def read_candidate_review_summary(directory: Path, digest: str) -> CandidateReviewSummary | None:
    """Read fresh evidence and reuse only compact metadata of its exact bytes."""

    raw = read_candidate_bytes(directory, digest, missing_ok=True)
    key = (str(directory.absolute()), digest)
    if raw is None:
        with _lock:
            _cache.pop(key, None)
        return None
    fingerprint = hashlib.sha256(raw).digest()
    with _lock:
        entry = _cache.get(key)
        if entry is not None:
            if entry.byte_length == len(raw) and entry.bytes_digest == fingerprint:
                _cache.move_to_end(key)
                return entry.summary
            # Never reuse a previous proof after observing changed bytes, even
            # if decoding the replacement below refuses.
            del _cache[key]
    candidate = parse_candidate_evidence(raw, expected_digest=digest)
    members: Sequence[CandidateMember] = candidate.members
    summary = CandidateReviewSummary(
        parent_semantic_root=candidate.candidate.parent_semantic_root,
        summary=change_set_summary(members),
        member_roll="\n".join(
            member_line(member)
            for member in sorted(members, key=lambda item: item.path.encode("utf-8"))
        ),
    )
    weight = sum(
        len(value.encode("utf-8"))
        for value in (*key, summary.parent_semantic_root, summary.summary, summary.member_roll)
    ) + len(fingerprint)
    if weight <= MAX_RETAINED_BYTES:
        with _lock:
            _cache[key] = _Entry(len(raw), fingerprint, summary, weight)
            _cache.move_to_end(key)
            while len(_cache) > MAX_ENTRIES or sum(item.weight for item in _cache.values()) > (
                MAX_RETAINED_BYTES
            ):
                _cache.popitem(last=False)
    return summary
