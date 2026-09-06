"""Fresh batched review reads preserve Git bytes and evidence refusal boundaries."""

from __future__ import annotations

import zlib

import pytest

from cruxible_client.contracts.errors import PlaybillGitError, ProposalIntegrityError
from cruxible_core.playbill.git import NOTE_REFS, GitLedger
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_proposal_notes import _submit


@pytest.fixture(params=("sha1", "sha256"))
def ledger(tmp_path, request):
    result = GitLedger.initialize(
        tmp_path / "ledger.git",
        object_format=request.param,
        signing_key_path=tmp_path / "unused-key",
        allowed_signers_path=tmp_path / "unused-signers",
    )
    tree = result._git(["mktree"], input_bytes=b"").decode().strip()
    base = result._git(["commit-tree", tree, "-m", "base"]).decode().strip()
    result._git(["update-ref", "refs/heads/main", base])
    return result


def test_snapshot_matches_ordinary_notes_and_proves_dependencies(ledger):
    base = ledger.read_main()
    tree = ledger.tree_oid(base)
    missing = "0" * len(base)
    evaluation = b'{"exact":"record"}\n{"second":"record"}\n'
    ledger.write_proposal_note("evaluation", base, evaluation)
    presence, notes = ledger.read_review_projection(
        (base, missing, base), dependencies={tree: "tree", base: "commit"}
    )
    assert presence == {base: True, missing: False}
    assert notes == {
        ("evaluation", base): ledger.read_proposal_note("evaluation", base),
        ("approval", base): None,
        ("evaluation", missing): None,
        ("approval", missing): None,
    }
    ledger.write_proposal_note("evaluation", base, b"changed since last snapshot\n")
    assert (
        ledger.read_review_projection((base,), dependencies={})[1]["evaluation", base]
        == b"changed since last snapshot\n"
    )


def test_snapshot_refuses_missing_or_wrong_type_dependency(ledger):
    base = ledger.read_main()
    with pytest.raises(PlaybillGitError, match="tree or parent is missing"):
        ledger.read_review_projection((base,), dependencies={"0" * len(base): "tree"})
    with pytest.raises(PlaybillGitError, match="conflicting expected types"):
        ledger.read_review_projection((base,), dependencies={base: "tree"})
    with pytest.raises(PlaybillGitError, match="requested object"):
        ledger.read_review_projection((ledger.tree_oid(base),), dependencies={})


def test_snapshot_rehashes_corrupted_loose_commit_on_every_read(ledger):
    base = ledger.read_main()
    assert ledger.read_review_projection((base,), dependencies={})[0][base]
    path = ledger.path / "objects" / base[:2] / base[2:]
    raw = zlib.decompress(path.read_bytes())
    assert raw.endswith(b"base\n")
    path.chmod(0o600)
    path.write_bytes(zlib.compress(raw[:-5] + b"fake\n"))
    with pytest.raises(PlaybillGitError, match="do not reproduce its OID"):
        ledger.read_review_projection((base,), dependencies={})


@pytest.mark.parametrize(
    "output",
    [
        b"",
        b"wrong missing\n",
        b"{oid} tree 0\n\n",
        b"{oid} commit -1\n",
        b"{oid} commit 5\ncut",
        b"{oid} missing\nextra",
    ],
)
def test_snapshot_refuses_malformed_batch_output(ledger, monkeypatch, output):
    oid = ledger.read_main()
    monkeypatch.setattr(ledger, "_git", lambda *a, **kw: output.replace(b"{oid}", oid.encode()))
    with pytest.raises(PlaybillGitError):
        ledger._read_review_objects({oid: "commit"})


def test_snapshot_batches_large_object_inventory(ledger, monkeypatch):
    size = len(ledger.read_main())
    oids = {f"{i:0{size}x}": "commit" for i in range(300)}
    batches = []

    def missing(args, *, input_bytes, **kwargs):
        assert args == ["--no-replace-objects", "cat-file", "--batch"]
        batch = input_bytes.splitlines()
        batches.append(batch)
        return b"".join(oid + b" missing\n" for oid in batch)

    monkeypatch.setattr(ledger, "_git", missing)
    assert ledger._read_review_objects(oids) == {}
    assert list(map(len, batches)) == [128, 128, 44]


def test_reconciliation_reuses_verified_commits_but_still_refuses_note_tamper(
    tmp_path, monkeypatch
):
    instance, _ = initialize_local(tmp_path)
    proposal = _submit(instance)
    instance._reconcile_proposal_review_refs()
    refs = instance._ledger.mirror_refs()

    def unexpected(*args, **kwargs):
        raise AssertionError("existing exact review commit must not be rematerialized")

    monkeypatch.setattr(instance._ledger, "proposal_review_commit", unexpected)
    instance._reconcile_proposal_review_refs()
    assert instance._ledger.mirror_refs() == refs
    oid = next(oid for ref, oid in refs.items() if ref.startswith("refs/heads/proposals/"))
    instance.write_proposal_note("evaluation", oid, b"tampered\n")
    before = instance.accepted_coordinate()
    with pytest.raises(ProposalIntegrityError):
        instance._reconcile_proposal_review_refs()
    assert instance.accepted_coordinate() == before
    assert instance.read_proposal_note("evaluation", oid) == b"tampered\n"
    assert proposal.candidate is not None


def test_reconciliation_rebuilds_missing_notes_without_rematerializing_commits(
    tmp_path, monkeypatch
):
    instance, _ = initialize_local(tmp_path)
    _submit(instance)
    instance._reconcile_proposal_review_refs()
    refs = instance._ledger.mirror_refs()
    oid = next(oid for ref, oid in refs.items() if ref.startswith("refs/heads/proposals/"))
    expected = instance.read_proposal_note("evaluation", oid)
    instance._ledger._git(["update-ref", "-d", NOTE_REFS["evaluation"]])

    def unexpected(*args, **kwargs):
        raise AssertionError("existing exact review commit must not be rematerialized")

    monkeypatch.setattr(instance._ledger, "proposal_review_commit", unexpected)
    instance._reconcile_proposal_review_refs()
    assert instance.read_proposal_note("evaluation", oid) == expected


def test_snapshot_reads_actual_commit_despite_replace_ref(ledger):
    original = ledger.read_main()
    tree = ledger.tree_oid(original)
    replacement = ledger._git(["commit-tree", tree, "-m", "replacement"]).decode().strip()
    ledger._git(["update-ref", f"refs/replace/{original}", replacement])
    assert ledger.read_review_projection((original,), dependencies={tree: "tree"})[0] == {
        original: True
    }


def test_snapshot_rehashes_corrupted_note_blob(ledger):
    original = ledger.read_main()
    ledger.write_proposal_note("evaluation", original, b"good\n")
    assert (
        ledger.read_review_projection((original,), dependencies={})[1]["evaluation", original]
        == b"good\n"
    )
    note_oid = (
        ledger._git(["notes", f"--ref={NOTE_REFS['evaluation']}", "list", original])
        .decode()
        .strip()
    )
    path = ledger.path / "objects" / note_oid[:2] / note_oid[2:]
    raw = zlib.decompress(path.read_bytes())
    assert raw.endswith(b"good\n")
    path.chmod(0o600)
    path.write_bytes(zlib.compress(raw[:-5] + b"fake\n"))
    with pytest.raises(PlaybillGitError, match="do not reproduce its OID"):
        ledger.read_review_projection((original,), dependencies={})


def test_partial_note_snapshot_falls_back_to_fresh_read_and_refuses_tamper(tmp_path):
    from cruxible_core.playbill.proposal_note_projection import ProposalNoteIndex

    instance, _ = initialize_local(tmp_path)
    proposal = _submit(instance)
    oid = proposal.admission.candidate_commit_oid
    instance.write_proposal_note("evaluation", oid, b"edited\n")
    with instance.review_projection_lock():
        index = ProposalNoteIndex.build(instance.proposal_evidence(), instance._ledger)
        with pytest.raises(ProposalIntegrityError):
            index.publish(
                instance._ledger,
                (oid,),
                object_presence={},
                stored_notes={("approval", oid): None},
            )
    assert instance.read_proposal_note("evaluation", oid) == b"edited\n"
