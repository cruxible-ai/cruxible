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
    stored: list[set[str]] = []
    store = ledger._store_loose_objects

    def counted(objects):
        stored.append({oid for oid, (kind, _body) in objects.items() if kind == "blob"})
        return store(objects)

    monkeypatch.setattr(ledger, "_store_loose_objects", counted)
    assert ledger._write_tree(successor, accepted_parent=parent) == expected
    # Only the member the accepted parent lacks is stored.
    assert stored == [{ledger._blob_oid(b"c")}]


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


def test_a_commit_lists_from_its_written_tree_without_ls_tree(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path / "ledger.git")
    tree = {"a.json": b"1", "claims/x.json": b"22"}
    tree_oid = ledger._write_tree(tree)
    commit = ledger._git(["commit-tree", tree_oid, "-m", "one"]).decode().strip()
    expected = {
        sized: ledger._read_tree_listing(commit, with_sizes=sized, paths=None)
        for sized in (True, False)
    }
    listed = []
    read = GitLedger._read_tree_listing

    def counted(self, oid, **kwargs):
        listed.append(oid)
        return read(self, oid, **kwargs)

    monkeypatch.setattr(GitLedger, "_read_tree_listing", counted)
    for sized in (True, False):
        assert ledger._list_tree(commit, with_sizes=sized) == expected[sized]
    assert listed == []
    # A tree listed directly is not a commit and resolves to nothing further.
    assert ledger._commit_tree(tree_oid) is None
    assert ledger._commit_tree(commit) == tree_oid


def test_extending_a_stored_tree_writes_the_same_tree_as_a_full_write(tmp_path):
    ledger = _ledger(tmp_path / "ledger.git")
    base = {"a.json": b"1", "claims/x.json": b"22", "claims-z.json": b"z" * 70000}
    base_oid = ledger._write_tree(base)
    grown = {**base, "changesets/0001.json": b"record", "claims/w.json": b"new"}
    extended = ledger._extend_tree(base_oid, grown)
    assert extended == ledger._write_tree(grown)
    for sized in (True, False):
        assert ledger._list_tree(extended, with_sizes=sized) == ledger._read_tree_listing(
            extended, with_sizes=sized, paths=None
        )
    # A tree that drops a base member is not an extension of it.
    with pytest.raises(PlaybillGitError, match="every member"):
        ledger._extend_tree(base_oid, {"a.json": b"1"})


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_a_tree_written_over_its_parent_equals_a_write_from_empty(tmp_path, object_format):
    ledger = GitLedger.initialize(
        tmp_path / "ledger.git",
        object_format=object_format,
        signing_key_path=tmp_path / "unused-key",
        allowed_signers_path=tmp_path / "unused-signers",
    )
    base = {
        "a.json": b"1",
        "claims/x.json": b"22",
        "claims/y.json": b"33",
        "gone/only.json": b"removed with its directory",
        "claims-z.json": b"z" * 70000,
    }
    base_tree = ledger._write_tree(base)
    parent = ledger._git(["commit-tree", base_tree, "-m", "base"]).decode().strip()
    changed = {
        "a.json": b"1",  # unchanged
        "claims/x.json": b"changed",  # modified
        "claims/w.json": b"new",  # added beside an existing member
        "fresh/dir/v.json": b"new directory",  # added in a new directory
        "claims-z.json": b"z" * 70000,
    }  # claims/y.json and gone/only.json are removed
    over_parent = ledger._write_tree(changed, accepted_parent=parent)
    ledger_git._TREE_LISTINGS.clear()
    assert over_parent == ledger._write_tree(changed)
    for sized in (True, False):
        assert ledger._list_tree(over_parent, with_sizes=sized) == ledger._read_tree_listing(
            over_parent, with_sizes=sized, paths=None
        )


def test_a_path_restricted_listing_from_memory_matches_git(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path / "ledger.git")
    tree = {"a.json": b"1", "claims/x.json": b"22", "claims/y/z.json": b"3", "claims-z.json": b"4"}
    tree_oid = ledger._write_tree(tree)
    commit = ledger._git(["commit-tree", tree_oid, "-m", "one"]).decode().strip()
    selections = (("a.json",), ("claims",), ("claims/y",), ("claims/x.json", "missing.json"))
    expected = {
        (oid, paths): ledger._read_tree_listing(oid, with_sizes=False, paths=paths)
        for oid in (tree_oid, commit)
        for paths in selections
    }
    listed = []
    read = GitLedger._read_tree_listing

    def counted(self, oid, **kwargs):
        listed.append(oid)
        return read(self, oid, **kwargs)

    monkeypatch.setattr(GitLedger, "_read_tree_listing", counted)
    for (oid, paths), entries in expected.items():
        assert ledger._list_tree(oid, with_sizes=False, paths=paths) == entries
    assert listed == []


class _CountingPath(str):
    checks = 0

    def startswith(self, *args, **kwargs):  # type: ignore[override]
        _CountingPath.checks += 1
        return super().startswith(*args, **kwargs)


def _listing(count: int) -> tuple[ledger_git.GitTreeEntry, ...]:
    paths = sorted(
        {"claims/x.json", "claims/y/z.json", *(f"filler/{n:06d}.json" for n in range(count))}
    )
    return tuple(
        ledger_git.GitTreeEntry(
            path=_CountingPath(path), mode="100644", object_type="blob", oid="0" * 40, size=None
        )
        for path in paths
    )


def test_a_remembered_selection_does_not_grow_with_unrelated_entries():
    work = {}
    for count in (10, 10000):
        listing = _listing(count)
        ledger_git._select_from_listing(listing, ("warm",))  # build the index once
        _CountingPath.checks = 0
        selected = ledger_git._select_from_listing(listing, ("claims/x.json", "claims/y"))
        assert [entry.path for entry in selected] == ["claims/x.json", "claims/y/z.json"]
        work[count] = _CountingPath.checks
    assert work[10] == work[10000]
