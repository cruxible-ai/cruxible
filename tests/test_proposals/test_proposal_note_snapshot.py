"""Request-owned note groups cannot combine independently acquired inventories."""

from contextlib import contextmanager

from cruxible_core.proposals.proposal_evidence import ProposalEvidenceStore
from tests.core_support._support import initialize_local
from tests.test_proposals.test_grouped_proposal_notes import _approve, _expected, _submit


def test_materialized_note_group_keeps_its_membership_after_later_admission(tmp_path):
    instance, owner = initialize_local(tmp_path)
    first = _submit(instance, "first")
    oid = first.admission.candidate_commit_oid
    captured = instance.proposal_note_index(oids=(oid,))
    second = _submit(instance, "second", timestamp="2026-08-11T12:30:00.000001Z")
    assert second.admission.candidate_commit_oid == oid
    _approve(instance, owner, second)

    assert captured.note_bytes(oid) == {"evaluation": _expected(first), "approval": b"[]\n"}
    current = instance.proposal_note_index(oids=(oid,))
    assert current.note_bytes(oid)["evaluation"] == _expected(first, second)
    assert current.note_bytes(oid)["approval"] != b"[]\n"


def test_selected_note_group_uses_one_snapshot_and_only_selected_sources(tmp_path, monkeypatch):
    instance, _ = initialize_local(tmp_path)
    selected = _submit(instance, "selected")
    for number in range(4):
        _submit(instance, f"other-{number}", timestamp=f"2026-08-11T12:31:0{number}.000000Z")
    evidence = instance.proposal_evidence()
    index = evidence.index
    read = index.read
    read_source = ProposalEvidenceStore.read_record_bytes
    acquisitions = []
    sources = []

    @contextmanager
    def counted(*args, **kwargs):
        acquisitions.append(1)
        with read(*args, **kwargs) as connection:
            yield connection

    def source(path):
        sources.append(path)
        return read_source(path)

    monkeypatch.setattr(index, "read", counted)
    monkeypatch.setattr(ProposalEvidenceStore, "read_record_bytes", staticmethod(source))
    oid = selected.admission.candidate_commit_oid
    snapshot = instance.proposal_note_index(oids=(oid,))
    assert snapshot.note_bytes(oid)["evaluation"] == _expected(selected)
    assert snapshot.candidate_digests(oid) == (selected.candidate.candidate_digest,)
    assert len(acquisitions) == 1
    assert len(sources) == 2
    assert {path.parent.name for path in sources} == {"proposals", "evaluations"}
