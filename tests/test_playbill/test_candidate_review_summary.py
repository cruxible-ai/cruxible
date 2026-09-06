"""Compact review reuse never trusts timestamps or retains candidate models."""

from __future__ import annotations

import json
import os
from dataclasses import FrozenInstanceError
from unittest.mock import patch

import pytest

from cruxible_client.contracts.candidates import (
    CandidateRecord,
    SemanticCandidate,
    candidate_digest,
    render_candidate_record,
)
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_core.playbill import candidate_review_summary as cache
from cruxible_core.playbill.proposal_evidence import ProposalEvidenceStore
from cruxible_core.playbill.proposal_message import proposal_commit_message
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_proposal_notes import _submit


def _legacy():
    path = "documents/design.json"
    candidate = SemanticCandidate(
        parent_semantic_root="sha256:" + "11" * 32,
        candidate_manifest_root="sha256:" + "22" * 32,
        semantic_diff_digest="sha256:" + "33" * 32,
        scope=(path,),
        timestamp="2026-08-11T12:30:00.000000Z",
    )
    return CandidateRecord(
        candidate=candidate,
        candidate_digest=candidate_digest(candidate).tagged,
        required_tier="governed_write",
        approval_requirements=(),
        activation_policy="snapshot",
        closure_paths=(path,),
        members=(
            {
                "path": path,
                "artifact_kind": "document",
                "artifact_digest": "sha256:" + "66" * 32,
                "disposition": "replacement",
                "law_identifier": "playbill.document.v1",
            },
        ),
        law_digests={"playbill.document.v1": "sha256:" + "44" * 32},
        compiler_digest="sha256:" + "55" * 32,
    )


@pytest.fixture(scope="module")
def records(tmp_path_factory):
    instance, _ = initialize_local(tmp_path_factory.mktemp("review-summary-world"))
    current = _submit(instance).candidate
    assert current is not None
    return (_legacy(), current)


@pytest.fixture(autouse=True)
def clear_cache():
    cache._cache.clear()
    yield
    cache._cache.clear()


def _write(directory, record):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (record.candidate_digest.removeprefix("sha256:") + ".json")
    path.write_bytes(render_candidate_record(record))
    return path


@pytest.mark.parametrize("version", [0, 1], ids=["legacy", "current"])
def test_summary_reuses_exact_bytes_and_preserves_all_prose(records, tmp_path, version):
    record = records[version]
    _write(tmp_path, record)
    with patch.object(
        cache, "parse_candidate_evidence", wraps=cache.parse_candidate_evidence
    ) as parse:
        first = cache.read_candidate_review_summary(tmp_path, record.candidate_digest)
        second = cache.read_candidate_review_summary(tmp_path, record.candidate_digest)
    assert first is not None and second == first
    assert parse.call_count == 1
    assert first.parent_semantic_root == record.candidate.parent_semantic_root
    for rationale in (None, "Why", "A" * 90, "First line\nSecond paragraph", "Résumé 日本語", ""):
        assert first.message(rationale=rationale) == proposal_commit_message(
            record.members, rationale=rationale
        )
    with pytest.raises(FrozenInstanceError):
        first.member_roll = "poisoned"
    assert not hasattr(first, "__dict__")
    assert cache.read_candidate_review_summary(tmp_path, record.candidate_digest) == second


def test_hit_still_reads_bytes_and_detects_restored_mtime_corruption(tmp_path):
    record = _legacy()
    path = _write(tmp_path, record)
    cache.read_candidate_review_summary(tmp_path, record.candidate_digest)
    original = path.read_bytes()
    before = path.stat()
    changed = original.replace(b'"candidate_digest":"sha256:1', b'"candidate_digest":"sha256:0')
    if changed == original:
        at = original.index(b'"candidate_digest":"sha256:') + len(b'"candidate_digest":"sha256:')
        changed = (
            original[:at] + (b"0" if original[at : at + 1] != b"0" else b"1") + original[at + 1 :]
        )
    assert len(changed) == len(original) and changed != original
    path.write_bytes(changed)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ProposalIntegrityError, match="malformed"):
        cache.read_candidate_review_summary(tmp_path, record.candidate_digest)
    assert not cache._cache
    path.write_bytes(original)
    assert cache.read_candidate_review_summary(tmp_path, record.candidate_digest) is not None


def test_valid_changed_metadata_is_revalidated_even_with_same_candidate_digest(tmp_path):
    record = _legacy()
    path = _write(tmp_path, record)
    cache.read_candidate_review_summary(tmp_path, record.candidate_digest)
    changed = record.model_copy(update={"compiler_digest": "sha256:" + "77" * 32})
    path.write_bytes(render_candidate_record(changed))
    with patch.object(
        cache, "parse_candidate_evidence", wraps=cache.parse_candidate_evidence
    ) as parse:
        summary = cache.read_candidate_review_summary(tmp_path, record.candidate_digest)
    assert parse.call_count == 1
    assert summary is not None


def test_canonical_candidate_under_another_digest_filename_refuses(tmp_path):
    evidence = ProposalEvidenceStore(tmp_path)
    record = _legacy()
    path = _write(evidence.candidates, record)
    evidence.read_candidate_review_summary_if_present(record.candidate_digest)
    candidate = record.candidate.model_copy(update={"timestamp": "2026-08-12T12:30:00.000000Z"})
    other = record.model_copy(
        update={"candidate": candidate, "candidate_digest": candidate_digest(candidate).tagged}
    )
    raw = render_candidate_record(other)
    # B is valid in its own right; its candidate preimage is not A's.
    assert cache.parse_candidate_evidence(raw, expected_digest=other.candidate_digest) == other
    path.write_bytes(raw)
    with pytest.raises(ProposalIntegrityError, match="different candidate"):
        evidence.read_candidate_review_summary_if_present(record.candidate_digest)
    with pytest.raises(ProposalIntegrityError, match="different candidate"):
        evidence.read_candidate(record.candidate_digest)


@pytest.mark.parametrize("failure", ["noncanonical", "truncated", "symlink", "directory"])
def test_present_invalid_evidence_refuses_instead_of_becoming_absent(tmp_path, failure):
    record = _legacy()
    path = _write(tmp_path, record)
    cache.read_candidate_review_summary(tmp_path, record.candidate_digest)
    raw = path.read_bytes()
    if failure == "noncanonical":
        path.write_text(json.dumps(json.loads(raw), indent=2))
    elif failure == "truncated":
        path.write_bytes(raw[:50])
    else:
        path.unlink()
        if failure == "directory":
            path.mkdir()
        else:
            target = tmp_path / "actual.json"
            target.write_bytes(raw)
            path.symlink_to(target)
    with pytest.raises(ProposalIntegrityError):
        cache.read_candidate_review_summary(tmp_path, record.candidate_digest)


def test_absence_evicts_prior_proof_and_directory_symlink_is_refused(tmp_path):
    record = _legacy()
    directory = tmp_path / "candidates"
    path = _write(directory, record)
    cache.read_candidate_review_summary(directory, record.candidate_digest)
    path.unlink()
    assert cache.read_candidate_review_summary(directory, record.candidate_digest) is None
    assert not cache._cache
    directory.rmdir()
    target = tmp_path / "moved"
    _write(target, record)
    directory.symlink_to(target, target_is_directory=True)
    with pytest.raises(ProposalIntegrityError):
        cache.read_candidate_review_summary(directory, record.candidate_digest)


def test_entry_and_byte_limits_evict_and_revalidate_on_next_read(tmp_path, monkeypatch):
    record = _legacy()
    monkeypatch.setattr(cache, "MAX_ENTRIES", 2)
    directories = [tmp_path / str(index) for index in range(3)]
    for directory in directories:
        _write(directory, record)
        cache.read_candidate_review_summary(directory, record.candidate_digest)
    assert len(cache._cache) == 2
    with patch.object(
        cache, "parse_candidate_evidence", wraps=cache.parse_candidate_evidence
    ) as parse:
        cache.read_candidate_review_summary(directories[0], record.candidate_digest)
    assert parse.call_count == 1
    weight = next(iter(cache._cache.values())).weight
    monkeypatch.setattr(cache, "MAX_RETAINED_BYTES", weight)
    cache.read_candidate_review_summary(directories[1], record.candidate_digest)
    assert sum(entry.weight for entry in cache._cache.values()) <= weight
    cache._cache.clear()
    monkeypatch.setattr(cache, "MAX_RETAINED_BYTES", 1)
    assert cache.read_candidate_review_summary(directories[0], record.candidate_digest) is not None
    assert not cache._cache


def test_unchanged_hit_does_not_decode_and_never_reuses_a_different_path(tmp_path, monkeypatch):
    record = _legacy()
    directory = tmp_path / "first"
    _write(directory, record)
    cache.read_candidate_review_summary(directory, record.candidate_digest)
    other = tmp_path / "second"
    _write(other, record)

    def no_decode(raw, **kwargs):
        pytest.fail("unexpected candidate decoding")

    monkeypatch.setattr(cache, "parse_candidate_evidence", no_decode)
    assert cache.read_candidate_review_summary(directory, record.candidate_digest) is not None
    with pytest.raises(pytest.fail.Exception, match="unexpected candidate decoding"):
        cache.read_candidate_review_summary(other, record.candidate_digest)
