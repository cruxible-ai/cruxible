"""Incremental review grouping preserves the cold builder's evidence semantics."""

from __future__ import annotations

import os

import pytest

from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_core.playbill import proposal_note_cache as cache_module
from cruxible_core.playbill.proposal_evidence import ProposalEvidenceStore
from cruxible_core.playbill.proposal_note_projection import ProposalNoteIndex
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_grouped_proposal_notes import _submit


def _current(instance):
    with instance.review_projection_lock():
        return instance.proposal_note_index()


def _assert_oracle(instance, actual):
    expected = ProposalNoteIndex.build(instance.proposal_evidence(), instance._ledger)
    for name in (
        "admissions",
        "evaluations",
        "candidates",
        "review_oids",
        "proposal_ids_by_oid",
        "proposal_ids_by_candidate",
    ):
        assert getattr(actual, name) == getattr(expected, name)
    for oid in expected.proposal_ids_by_oid:
        assert actual.note_bytes(oid) == expected.note_bytes(oid)
    return expected


def test_unchanged_load_has_no_decoding_or_git_alias_work_and_cannot_be_poisoned(
    tmp_path, monkeypatch
):
    instance, _ = initialize_local(tmp_path)
    first = _submit(instance, "first")
    _submit(instance, "second")
    before = _assert_oracle(instance, _current(instance))
    with monkeypatch.context() as patch:

        def forbidden(*args, **kwargs):
            raise AssertionError("unchanged evidence must reuse validation and alias derivation")

        patch.setattr(ProposalEvidenceStore, "parse_model_bytes", forbidden)
        patch.setattr(instance._ledger, "proposal_review_commit_oid", forbidden)
        actual = _current(instance)
    actual.admissions[first.admission.proposal_id].__dict__["rationale"] = "poison"
    actual.proposal_ids_by_oid.clear()
    actual.review_oids.clear()
    assert _current(instance).admissions == before.admissions
    _assert_oracle(instance, _current(instance))


def test_external_revision_updates_only_its_alias_and_deletion_removes_membership(
    tmp_path, monkeypatch
):
    instance, _ = initialize_local(tmp_path)
    first = _submit(instance, "first")
    evidence = instance.proposal_evidence()
    _current(instance)
    # An external replacement changes canonical review prose without notifying
    # this process. Fresh bytes must update both old and new alias membership.
    second_admission = first.admission.model_copy(update={"rationale": "Another review alias"})
    path = evidence.proposals / f"{first.admission.proposal_id.removeprefix('sha256:')}.json"
    from cruxible_core.playbill.proposal_notes import admission_bytes

    path.write_bytes(admission_bytes(second_admission))
    calls = []
    original = instance._ledger.proposal_review_commit_oid

    def counted(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(instance._ledger, "proposal_review_commit_oid", counted)
        actual = _current(instance)
    assert len(calls) == 1
    _assert_oracle(instance, actual)
    path.unlink()
    assert _assert_oracle(instance, _current(instance)).admissions == {}


@pytest.mark.parametrize("kind", ["proposals", "evaluations", "candidates"])
def test_same_size_same_mtime_corruption_is_not_hidden_by_reuse(tmp_path, kind):
    instance, _ = initialize_local(tmp_path)
    _submit(instance, "first")
    _current(instance)
    directory = getattr(instance.proposal_evidence(), kind)
    path = next(directory.glob("*.json"))
    old_stat = path.stat()
    raw = path.read_bytes()
    path.write_bytes(b"!" + raw[1:])
    os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    with pytest.raises(ProposalIntegrityError):
        _current(instance)
    assert instance._proposal_note_cache._index is None
    path.write_bytes(raw)
    _assert_oracle(instance, _current(instance))


def test_candidate_completion_wakes_all_interrupted_admissions(tmp_path, monkeypatch):
    instance, _ = initialize_local(tmp_path)
    with monkeypatch.context() as patch:

        def crash(*args, **kwargs):
            raise OSError("candidate persistence interrupted")

        patch.setattr(ProposalEvidenceStore, "write_candidate", crash)
        with pytest.raises(OSError, match="interrupted"):
            _submit(instance, "interrupted", rationale="Different original and advisory aliases")
    assert _current(instance).admissions == {}
    later = _submit(instance, "completes-shared-candidate")
    actual = _current(instance)
    assert len(actual.admissions) == 2
    _assert_oracle(instance, actual)
    originals = {admission.candidate_commit_oid for admission in actual.admissions.values()}
    assert len(originals) == 2
    assert later.admission.candidate_commit_oid in originals
    for oid in originals:
        assert (
            instance.read_proposal_note("evaluation", oid) == actual.note_bytes(oid)["evaluation"]
        )


def test_duplicate_evaluation_and_symlink_are_not_hidden_by_reuse(tmp_path):
    instance, _ = initialize_local(tmp_path)
    _submit(instance, "first")
    _current(instance)
    evidence = instance.proposal_evidence()
    original = next(evidence.evaluations.glob("*.json"))
    duplicate = evidence.evaluations / "duplicate.json"
    duplicate.write_bytes(original.read_bytes())
    with pytest.raises(ProposalIntegrityError, match="multiple evaluations"):
        _current(instance)
    duplicate.unlink()
    raw = original.read_bytes()
    elsewhere = tmp_path / "evaluation.json"
    elsewhere.write_bytes(raw)
    original.unlink()
    original.symlink_to(elsewhere)
    with pytest.raises(ProposalIntegrityError):
        _current(instance)


def test_budget_and_cold_restart_reconstruct_same_groups(tmp_path, monkeypatch):
    instance, _ = initialize_local(tmp_path)
    _submit(instance, "first")
    _submit(instance, "second")
    _assert_oracle(instance, _current(instance))
    instance._proposal_note_cache = cache_module.ProposalNoteCache()
    _assert_oracle(instance, _current(instance))
    monkeypatch.setattr(cache_module, "MAX_RECORD_BYTES", 0)
    _assert_oracle(instance, _current(instance))
    assert instance._proposal_note_cache._index is None
    _assert_oracle(instance, _current(instance))
