"""Tree listings, deltas and history come from hash-checked objects, as Git reports them."""

from __future__ import annotations

import random
import zlib
from pathlib import Path

import pytest

from cruxible_client.contracts.errors import PlaybillGitError
from cruxible_core.ledger import git as ledger_git
from cruxible_core.ledger.git import GitLedger, GitTreeChange, GitTreeEntry


def _clear_caches() -> None:
    ledger_git._PARSED_TREES.clear()
    ledger_git._TREE_LISTINGS.clear()
    ledger_git._TREE_CHANGES.clear()
    ledger_git._COMMIT_ROOTS.clear()


@pytest.fixture(params=("sha1", "sha256"))
def ledger(tmp_path: Path, request: pytest.FixtureRequest) -> GitLedger:
    _clear_caches()
    return GitLedger.initialize(
        tmp_path / "ledger.git",
        object_format=request.param,
        signing_key_path=tmp_path / "unused-key",
        allowed_signers_path=tmp_path / "unused-signers",
    )


def _blob(ledger: GitLedger, content: bytes) -> str:
    return ledger._git(["hash-object", "-w", "--stdin"], input_bytes=content).decode().strip()


def _tree(ledger: GitLedger, rows: dict[str, tuple[str, bytes]], index: Path) -> str:
    """Write ``path -> (mode, content)`` with Git's own index, as an oracle-built tree."""

    environment = {"GIT_INDEX_FILE": str(index)}
    ledger._git(["read-tree", "--empty"], environment=environment)
    lines = b"".join(
        mode.encode() + b" " + _blob(ledger, content).encode() + b"\t" + path.encode() + b"\x00"
        for path, (mode, content) in rows.items()
    )
    if lines:
        ledger._git(
            ["update-index", "-z", "--index-info"], input_bytes=lines, environment=environment
        )
    return ledger._git(["write-tree"], environment=environment).decode().strip()


def _git_listing(ledger: GitLedger, oid: str, paths: list[str] | None) -> tuple[GitTreeEntry, ...]:
    scope = [] if paths is None else ["--", *(f":(literal){item}" for item in paths)]
    raw = ledger._git(["ls-tree", "-r", "-l", "-z", "--full-tree", oid, *scope])
    entries = []
    for row in raw.split(b"\x00"):
        if not row:
            continue
        metadata, path = row.split(b"\t", 1)
        mode, object_type, object_oid, size = metadata.decode().split()
        entries.append(
            GitTreeEntry(
                path=path.decode(),
                mode=mode,
                object_type=object_type,
                oid=object_oid,
                size=None if size == "-" else int(size),
            )
        )
    return tuple(entries)


def _git_changes(ledger: GitLedger, before: str, after: str) -> tuple[GitTreeChange, ...]:
    raw = ledger._git(
        ["diff-tree", "-r", "-z", "--no-renames", "--no-abbrev", "--no-commit-id", before, after]
    )
    fields = [field for field in raw.split(b"\x00") if field]
    changes = []
    for index in range(0, len(fields), 2):
        _source_mode, mode, source, destination, status = fields[index][1:].decode().split()
        changes.append(
            GitTreeChange(
                path=fields[index + 1].decode(),
                status=status,
                mode=mode,
                oid=None if status == "D" else destination,
                previous_oid=None if status == "A" else source,
            )
        )
    return tuple(changes)


_NAMES = ("a", "a.b", "a-b", "ab", "b", "claims", "x.json", "z")
_MODES = ("100644", "100644", "100644", "100755", "120000")


def _random_rows(rng: random.Random) -> dict[str, tuple[str, bytes]]:
    rows: dict[str, tuple[str, bytes]] = {}
    for _ in range(rng.randint(0, 25)):
        path = "/".join(rng.choice(_NAMES) for _ in range(rng.randint(1, 3)))
        # A path may not be both a file and a directory.
        if any(
            existing.startswith(path + "/") or path.startswith(existing + "/") for existing in rows
        ):
            continue
        rows[path] = (rng.choice(_MODES), rng.choice((b"one\n", b"two\n", b"three\n")))
    return rows


def test_listings_and_deltas_match_git(ledger: GitLedger, tmp_path: Path) -> None:
    rng = random.Random(7)
    trees = [_tree(ledger, _random_rows(rng), tmp_path / "index") for _ in range(30)]
    for tree in trees:
        _clear_caches()
        assert ledger._read_tree_listing(tree, with_sizes=True, paths=None) == _git_listing(
            ledger, tree, None
        )
        assert ledger._read_tree_listing(tree, with_sizes=False, paths=None) == tuple(
            GitTreeEntry(e.path, e.mode, e.object_type, e.oid, None)
            for e in _git_listing(ledger, tree, None)
        )
        selection = ["a", "claims", "a/b", "missing", "z/x.json"]
        assert ledger._read_tree_listing(tree, with_sizes=True, paths=selection) == _git_listing(
            ledger, tree, selection
        )
    for before, after in zip(trees, trees[1:] + trees[:1]):
        assert ledger._read_changed_entries(before, after) == _git_changes(ledger, before, after)


def test_a_file_that_becomes_a_directory_is_a_deletion_plus_additions(
    ledger: GitLedger, tmp_path: Path
) -> None:
    file_tree = _tree(ledger, {"a": ("100644", b"x\n"), "b": ("100644", b"y\n")}, tmp_path / "i")
    directory_tree = _tree(
        ledger,
        {"a/one": ("100644", b"x\n"), "a/two": ("100644", b"z\n"), "b": ("100755", b"y\n")},
        tmp_path / "i",
    )
    expected = _git_changes(ledger, file_tree, directory_tree)
    assert [(change.path, change.status) for change in expected] == [
        ("a", "D"),
        ("a/one", "A"),
        ("a/two", "A"),
        ("b", "M"),
    ]
    assert ledger._read_changed_entries(file_tree, directory_tree) == expected
    assert ledger._read_changed_entries(directory_tree, file_tree) == _git_changes(
        ledger, directory_tree, file_tree
    )


def _forge_subtree(ledger: GitLedger, subtree: str, body: bytes) -> None:
    loose = ledger.path / "objects" / subtree[:2] / subtree[2:]
    assert loose.is_file()
    loose.chmod(0o644)
    loose.write_bytes(zlib.compress(b"tree %d\x00" % len(body) + body))


def test_a_tree_object_replaced_on_disk_is_refused(ledger: GitLedger, tmp_path: Path) -> None:
    tree = _tree(
        ledger,
        {"documents/a.json": ("100644", b"accepted\n"), "z.json": ("100644", b"other\n")},
        tmp_path / "index",
    )
    forged_blob = _blob(ledger, b"forged\n")
    subtree = next(
        entry[1] for name, entry in ledger._tree_entries_of(tree).items() if name == "documents"
    )
    raw = bytes.fromhex(forged_blob)
    _forge_subtree(ledger, subtree, b"100644 a.json\x00" + raw)
    _clear_caches()
    # Git itself serves the forged member under the accepted tree's ID ...
    assert any(entry.oid == forged_blob for entry in _git_listing(ledger, tree, None))
    # ... the ledger's listing and delta refuse it.
    with pytest.raises(PlaybillGitError, match="do not hash to their ID"):
        ledger.list_tree(tree)
    empty = _tree(ledger, {}, tmp_path / "index")
    with pytest.raises(PlaybillGitError, match="do not hash to their ID"):
        ledger._read_changed_entries(empty, tree)


def test_a_forged_subtree_never_hides_a_generations_changes_from_history(
    tmp_path: Path,
) -> None:
    """History sync takes each generation's changed paths from checked trees too."""

    from tests.core_support._knowledge_loop_support import seed_claims

    instance, _owner = seed_claims(tmp_path)
    with instance.accepted_history_reader():
        pass
    ledger = instance._ledger
    head, parent = instance.accepted_history()[-1], instance.accepted_history()[-2]
    head_entries = ledger._tree_entries_of(ledger.tree_oid(head.oid))
    parent_entries = ledger._tree_entries_of(ledger.tree_oid(parent.oid))
    name = next(
        name
        for name in ("claims", "subjects")
        if name in head_entries and head_entries[name] != parent_entries.get(name)
    )
    source = parent_entries[name][1]
    body = ledger_git._batch_reader(ledger.path).objects((source,))[source]
    assert body is not None
    _forge_subtree(ledger, head_entries[name][1], body[1])
    _clear_caches()
    instance._accepted_history_index.invalidate()
    with pytest.raises(PlaybillGitError, match="do not hash to their ID"):
        with instance.accepted_history_reader():
            pass
