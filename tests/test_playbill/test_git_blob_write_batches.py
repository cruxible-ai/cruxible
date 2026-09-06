"""System Git batches preserve exact trees, bounded work and refusal behavior."""

from __future__ import annotations

import tempfile
from unittest.mock import Mock

import pytest

from cruxible_client.contracts.canonical import normalize_manifest_paths
from cruxible_client.contracts.errors import CanonicalEncodingError, PlaybillGitError
from cruxible_core.playbill import git as git_module
from cruxible_core.playbill.git import GitLedger


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


def test_process_count_deduplication_and_existing_blobs(ledger, monkeypatch):
    monkeypatch.setattr(git_module, "_BLOB_WRITE_BATCH_OBJECTS", 3)
    monkeypatch.setattr(git_module, "_BLOB_WRITE_BATCH_BYTES", 1024)
    original = ledger._git
    calls = []

    def tracked(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return original(arguments, **kwargs)

    monkeypatch.setattr(ledger, "_git", tracked)
    tree = {f"file-{i}": f"content-{i}".encode() for i in range(7)}
    tree["duplicate"] = tree["file-0"]
    first = ledger._write_tree(tree)
    writes = [kwargs["input_bytes"] for args, kwargs in calls if args[0] == "hash-object"]
    assert [len(value.splitlines()) for value in writes] == [3, 3, 1]
    assert len(calls) == 7  # existence + three writes + three index/tree operations
    calls.clear()
    assert ledger._write_tree(tree) == first
    assert not any(args[0] == "hash-object" for args, _ in calls)
    assert len(calls) == 4


def test_byte_bound_and_larger_single_blob_use_separate_batches(ledger, monkeypatch):
    monkeypatch.setattr(git_module, "_BLOB_WRITE_BATCH_BYTES", 5)
    original = ledger._write_blob_batch
    sizes = []

    def write(batch):
        sizes.append([len(body) for _oid, body in batch])
        return original(batch)

    monkeypatch.setattr(ledger, "_write_blob_batch", write)
    tree = {"a": b"111", "b": b"22", "c": b"333", "d": b"oversize", "e": b"5"}
    oid = ledger._write_tree(tree)
    assert sizes == [[3, 2], [3], [8], [1]]
    assert ledger.read_tree(oid) == tree


@pytest.mark.parametrize(
    "failure", ("short", "extra", "reordered", "wrong", "malformed", "nonascii", "failed")
)
def test_bad_git_response_refuses_before_index_or_ref_and_cleans_temps(
    ledger, tmp_path, monkeypatch, failure
):
    temporary_root = tmp_path / "private-batches"
    temporary_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temporary_root))
    original = ledger._git
    calls = []

    def faulty(arguments, **kwargs):
        calls.append(arguments)
        if arguments[0] != "hash-object":
            return original(arguments, **kwargs)
        directories = list(temporary_root.iterdir())
        assert len(directories) == 1
        assert directories[0].stat().st_mode & 0o777 == 0o700
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in directories[0].iterdir())
        if failure == "failed":
            # Git can fail after only a prefix has become inert objects. No
            # index/ref may publish, and retry must store the remaining suffix.
            first = sorted(directories[0].iterdir())[0].read_bytes()
            original(["hash-object", "-w", "--stdin"], input_bytes=first)
            raise PlaybillGitError("injected failure after one inert object was written")
        output = original(arguments, **kwargs)
        lines = output.splitlines(keepends=True)
        if failure == "short":
            return b"".join(lines[:-1])
        if failure == "extra":
            return output + lines[0]
        if failure == "reordered":
            return b"".join(reversed(lines))
        if failure == "wrong":
            return b"0" * len(lines[0].strip()) + b"\n" + b"".join(lines[1:])
        if failure == "malformed":
            return b"not-an-oid\n" + b"".join(lines[1:])
        if failure == "nonascii":
            return b"\xff\n"
        raise AssertionError(f"unhandled failure mode: {failure}")

    monkeypatch.setattr(ledger, "_git", faulty)
    tree = {"a": b"first", "b": b"second"}
    with pytest.raises(PlaybillGitError):
        ledger._write_tree(tree)
    assert not any(
        args[0] in {"read-tree", "update-index", "write-tree", "update-ref"} for args in calls
    )
    assert list(temporary_root.iterdir()) == []
    # A failed attempt may have stored inert objects; ordinary retry recovers.
    monkeypatch.setattr(ledger, "_git", original)
    if failure == "failed":
        assert ledger._absent_objects(tuple(ledger._blob_oid(body) for body in tree.values())) == {
            ledger._blob_oid(tree["b"])
        }
    assert ledger.read_tree(ledger._write_tree(tree)) == tree


def test_normalization_collisions_refuse_before_writing(ledger, monkeypatch):
    writer = Mock()
    monkeypatch.setattr(ledger, "_write_missing_blobs", writer)
    with pytest.raises(PlaybillGitError, match="collide after normalization"):
        ledger._write_tree({"café": b"one", "cafe\u0301": b"two"})
    with pytest.raises(CanonicalEncodingError, match="case-fold-colliding"):
        ledger._write_tree({"A": b"one", "a": b"two"})
    writer.assert_not_called()


def test_different_bytes_with_same_computed_address_are_not_deduplicated(ledger, monkeypatch):
    oid = "1" * (40 if ledger.object_format() == "sha1" else 64)
    monkeypatch.setattr(ledger, "_blob_oid", lambda content: oid)
    writer = Mock()
    monkeypatch.setattr(ledger, "_write_missing_blobs", writer)
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
        timestamp="2026-09-05T12:00:00.000000Z",
        expected_ref_oid=None,
    )
    evaluated_tree = {**admitted_tree, "derived/card": b"derived bytes"}
    evaluated, evaluated_oid = ledger.create_proposal_commit(
        evaluated_tree,
        base_oid=admitted,
        target_ref=ref,
        actor_id="owner",
        timestamp="2026-09-05T12:00:00.000000Z",
        expected_ref_oid=admitted,
    )
    assert ledger.parent_of(admitted) == base
    assert ledger.parent_of(evaluated) == admitted
    assert ledger.read_proposal_ref(ref) == evaluated
    assert admitted_oid == _member_by_member_tree(ledger, admitted_tree, tmp_path)
    assert evaluated_oid == _member_by_member_tree(ledger, evaluated_tree, tmp_path)
