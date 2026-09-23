"""The resident `cat-file --batch` reader returns exactly what Git stores."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from cruxible_client.contracts.errors import PlaybillGitError
from cruxible_core.ledger import git as ledger_git
from cruxible_core.ledger.git import GitLedger


def _ledger(path: Path) -> GitLedger:
    return GitLedger.initialize(
        path,
        object_format="sha1",
        signing_key_path=path.parent / "unused-key",
        allowed_signers_path=path.parent / "unused-signers",
    )


def _write(ledger: GitLedger, content: bytes) -> str:
    return ledger._git(["hash-object", "-w", "--stdin"], input_bytes=content).decode().strip()


def test_resident_reader_matches_git_and_is_shared_across_handles(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path / "ledger.git")
    contents = [b"", b"one\n", b"\x00binary\xff" * 5000, b"no trailing newline"]
    oids = [_write(ledger, content) for content in contents]
    spawned = []
    popen = subprocess.Popen

    def counted(args, *a, **k):
        spawned.append(tuple(args))
        return popen(args, *a, **k)

    monkeypatch.setattr(ledger_git.subprocess, "Popen", counted)
    assert ledger.read_blobs(oids) == dict(zip(oids, contents, strict=True))
    other = GitLedger(
        ledger.path, signing_key_path=tmp_path / "k", allowed_signers_path=tmp_path / "s"
    )
    assert other.read_blobs(list(reversed(oids))) == dict(zip(oids, contents, strict=True))
    assert len([args for args in spawned if "cat-file" in args]) == 1


def test_missing_object_refuses_and_the_next_read_recovers(tmp_path):
    ledger = _ledger(tmp_path / "ledger.git")
    present = _write(ledger, b"present")
    with pytest.raises(PlaybillGitError):
        ledger.read_blobs(["0" * 40])
    assert ledger.read_blobs([present]) == {present: b"present"}


def test_a_repository_replaced_at_the_same_path_is_read_fresh(tmp_path):
    path = tmp_path / "ledger.git"
    ledger = _ledger(path)
    first = _write(ledger, b"original")
    assert ledger.read_blobs([first]) == {first: b"original"}
    shutil.rmtree(path)
    replacement = _ledger(path)
    second = _write(replacement, b"replacement")
    assert replacement.read_blobs([second]) == {second: b"replacement"}
    with pytest.raises(PlaybillGitError):
        replacement.read_blobs([first])


def test_tree_writes_check_only_members_the_accepted_parent_lacks(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path / "ledger.git")
    base = {"claims/a.json": b"a", "claims/b.json": b"b"}
    identity = {name: "tester" for name in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")} | {
        name: "t@example.invalid" for name in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL")
    }
    parent = (
        ledger._git(
            ["commit-tree", "--no-gpg-sign", ledger._write_tree(base), "-m", "base"],
            environment=identity,
        )
        .decode()
        .strip()
    )
    successor = {**base, "claims/c.json": b"c"}
    expected = ledger._write_tree(successor)
    checked: list[tuple[str, ...]] = []
    absent = ledger._absent_objects

    def counted(oids):
        checked.append(tuple(oids))
        return absent(oids)

    monkeypatch.setattr(ledger, "_absent_objects", counted)
    assert ledger._write_tree(successor, accepted_parent=parent) == expected
    assert checked == [(ledger._blob_oid(b"c"),)]


def test_a_written_tree_records_exactly_the_listing_git_reports(tmp_path):
    ledger = _ledger(tmp_path / "ledger.git")
    tree = {
        "a.b": b"1",
        "a/c.json": b"22",
        "a0": b"",
        "claims/x/y.json": b"nested",
        "claims-z.json": b"z" * 70000,
    }
    oid = ledger._write_tree(tree)
    for with_sizes in (True, False):
        assert ledger._list_tree(oid, with_sizes=with_sizes) == ledger._read_tree_listing(
            oid, with_sizes=with_sizes, paths=None
        )
