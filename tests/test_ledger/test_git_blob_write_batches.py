"""In-process tree writes preserve Git's exact trees without a Git process."""

from __future__ import annotations

import tempfile
from unittest.mock import Mock

import pytest

from cruxible_client.contracts.canonical import normalize_manifest_paths
from cruxible_client.contracts.errors import CanonicalEncodingError, PlaybillGitError
from cruxible_core.ledger.git import GitLedger


@pytest.fixture(params=("sha1", "sha256"))
def ledger(tmp_path, request):
    return GitLedger.initialize(
        tmp_path / "ledger.git",
        object_format=request.param,
        signing_key_path=tmp_path / "unused-key",
        allowed_signers_path=tmp_path / "unused-signers",
    )


def _member_by_member_tree(ledger, tree, tmp_path):
    """The former Git write path, used as a format-independent tree oracle."""
    rows = []
    for path in normalize_manifest_paths(list(tree)):
        oid = ledger._git(["hash-object", "-w", "--stdin"], input_bytes=tree[path]).strip()
        rows.append(b"100644 " + oid + b"\t" + path.encode() + b"\x00")
    environment = {"GIT_INDEX_FILE": str(tmp_path / "oracle-index")}
    ledger._git(["read-tree", "--empty"], environment=environment)
    if rows:
        ledger._git(
            ["update-index", "-z", "--index-info"],
            input_bytes=b"".join(rows),
            environment=environment,
        )
    return ledger._git(["write-tree"], environment=environment).decode().strip()


def test_exact_tree_parity_weird_paths_and_bytes_without_filters(ledger, tmp_path, monkeypatch):
    temporary_root = tmp_path / 'temporary "quoted"\nline\ttab\\slash-雪'
    temporary_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temporary_root))
    ledger._git(["config", "core.autocrlf", "true"])
    (ledger.path / "info" / "attributes").write_text("* text eol=lf\n")
    tree = {
        'nested/space tab\tnewline\nquote"-雪.txt': b"first\r\nsecond\r\n",
        "other/binary": bytes(range(256)) + b"\x00\xff",
        "duplicate": bytes(range(256)) + b"\x00\xff",
        "zero": b"",
        "cafe\u0301": b"normalized filename",
        "--leading-option": b"literal filename",
    }
    normalized = {normalize_manifest_paths([path])[0]: body for path, body in tree.items()}
    actual = ledger._write_tree(tree)
    assert ledger.read_tree(actual) == normalized
    assert actual == _member_by_member_tree(ledger, normalized, tmp_path)
    assert list(temporary_root.iterdir()) == []


def test_a_tree_write_runs_no_git_process_and_passes_fsck(ledger, monkeypatch):
    ledger.object_format()
    calls = []
    original = ledger._git

    def tracked(arguments, **kwargs):
        calls.append(arguments)
        return original(arguments, **kwargs)

    monkeypatch.setattr(ledger, "_git", tracked)
    tree = {f"dir-{i % 3}/file-{i}": f"content-{i}".encode() for i in range(7)}
    tree["duplicate"] = tree["dir-0/file-0"]
    first = ledger._write_tree(tree)
    assert calls == []
    assert ledger._write_tree(tree) == first
    monkeypatch.setattr(ledger, "_git", original)
    ledger._git(["fsck", "--strict", "--no-dangling", first])
    assert ledger.read_tree(first) == tree


def test_parent_based_writes_match_the_oracle_across_adds_edits_and_removals(ledger, tmp_path):
    identity = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    tree = {
        "a.b": b"1",
        "a/c.json": b"22",
        "a0": b"",
        "claims/x/y.json": b"nested",
        "claims/x/z.json": b"sibling",
        "claims-z.json": b"z" * 700,
    }
    parent = None
    steps = (
        {"claims/x/y.json": b"revised"},  # edit inside a subtree
        {"claims/w/new.json": b"new directory"},  # a new directory between siblings
        {"claims/x/z.json": None, "claims/x/y.json": None},  # a directory emptied away
        {"a/c.json": None, "a/d/e.json": b"deeper"},  # remove and add under one parent
        {"z-last": b"last", "a.b": None},
    )
    for step in (None, *steps):
        if step is not None:
            for path, body in step.items():
                if body is None:
                    tree.pop(path)
                else:
                    tree[path] = body
        oid = ledger._write_tree(dict(tree), accepted_parent=parent)
        assert oid == _member_by_member_tree(ledger, tree, tmp_path)
        assert ledger.read_tree(oid) == tree
        parent = (
            ledger._git(["commit-tree", "--no-gpg-sign", oid, "-m", "step"], environment=identity)
            .decode()
            .strip()
        )
    ledger._git(["fsck", "--strict", "--no-dangling"])


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ({"a/b": b"inside", "keep": b"k"}, {"a": b"now a file", "keep": b"k"}),
        ({"a": b"a file", "keep": b"k"}, {"a/b": b"now inside", "keep": b"k"}),
        ({"x/a/b/c": b"deep", "x/z": b"z"}, {"x/a": b"flattened", "x/z": b"z"}),
    ],
)
def test_a_path_may_turn_between_file_and_directory(ledger, tmp_path, before, after):
    identity = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    parent_tree = ledger._write_tree(before)
    parent = (
        ledger._git(["commit-tree", "--no-gpg-sign", parent_tree, "-m", "p"], environment=identity)
        .decode()
        .strip()
    )
    oid = ledger._write_tree(after, accepted_parent=parent)
    assert oid == _member_by_member_tree(ledger, after, tmp_path)
    assert ledger.read_tree(oid) == after
    ledger._git(["fsck", "--strict", "--no-dangling"])


def test_a_path_that_is_both_file_and_directory_is_refused(ledger):
    base = ledger._write_tree({"a": b"file"})
    with pytest.raises(PlaybillGitError, match="both a file and a directory"):
        ledger._commit_changes_to_tree(base, {"a/b": ledger._blob_oid(b"x")}, {})


def test_normalization_collisions_refuse_before_writing(ledger, monkeypatch):
    writer = Mock()
    monkeypatch.setattr(ledger, "_store_loose_objects", writer)
    with pytest.raises(PlaybillGitError, match="collide after normalization"):
        ledger._write_tree({"café": b"one", "cafe\u0301": b"two"})
    with pytest.raises(CanonicalEncodingError, match="case-fold-colliding"):
        ledger._write_tree({"A": b"one", "a": b"two"})
    writer.assert_not_called()


def test_different_bytes_with_same_computed_address_are_not_deduplicated(ledger, monkeypatch):
    oid = "1" * (40 if ledger.object_format() == "sha1" else 64)
    monkeypatch.setattr(ledger, "_blob_oid", lambda content: oid)
    writer = Mock()
    monkeypatch.setattr(ledger, "_store_loose_objects", writer)
    with pytest.raises(PlaybillGitError, match="different blob bytes"):
        ledger._write_tree({"a": b"one", "b": b"two"})
    writer.assert_not_called()


def test_admitted_and_evaluated_commits_extend_proposal_ancestry(ledger, tmp_path):
    base_tree = {"base": b"accepted bytes"}
    base_tree_oid = ledger._write_tree(base_tree)
    base = ledger._git(["commit-tree", base_tree_oid, "-m", "test base"]).decode().strip()
    ref = "refs/proposals/owner/batched"
    admitted_tree = {**base_tree, "authored": b"authored bytes"}
    admitted, admitted_oid = ledger.create_proposal_commit(
        admitted_tree,
        base_oid=base,
        target_ref=ref,
        actor_id="owner",
        message="Add the authored proposal and its evaluated projection.",
        timestamp="2026-09-05T12:00:00.000000Z",
        expected_ref_oid=None,
    )
    evaluated_tree = {**admitted_tree, "derived/card": b"derived bytes"}
    evaluated, evaluated_oid = ledger.create_proposal_commit(
        evaluated_tree,
        base_oid=admitted,
        target_ref=ref,
        actor_id="owner",
        message="Add the authored proposal and its evaluated projection.",
        timestamp="2026-09-05T12:00:00.000000Z",
        expected_ref_oid=admitted,
    )
    assert ledger.parent_of(admitted) == base
    assert ledger.parent_of(evaluated) == admitted
    assert ledger.read_proposal_ref(ref) == evaluated
    assert admitted_oid == _member_by_member_tree(ledger, admitted_tree, tmp_path)
    assert evaluated_oid == _member_by_member_tree(ledger, evaluated_tree, tmp_path)
