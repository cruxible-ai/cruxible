"""The daemon's glob listing for ``workspace.file`` (D-R41): sorted, bounded, no links."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.workspace_file import (
    WorkspaceFileGlobRequestV1,
    is_workspace_glob,
    parse_authored_workspace_glob,
)
from cruxible_core.documents.workspace_file import (
    DirectoryEntryCache,
    WorkspaceFileReader,
    WorkspaceFileReadRefused,
)


def _request(glob: str, **changes: object) -> WorkspaceFileGlobRequestV1:
    return WorkspaceFileGlobRequestV1.model_validate(
        {
            "logical_source": "test.files",
            "glob": glob,
            "coordinate_type": "database-snapshot-v1",
            "coordinate": {},
            "selector_type": "relation-primary-key-v1",
            "selector": {},
            **changes,
        }
    )


def _reader(root: Path) -> WorkspaceFileReader:
    return WorkspaceFileReader(
        instance_id="inst_test", operating_profile="local", attached_roots=(root,)
    )


def test_a_glob_lists_its_matches_sorted_by_real_name(tmp_path: Path) -> None:
    (tmp_path / "data" / "b").mkdir(parents=True)
    (tmp_path / "data" / "a").mkdir()
    for path in ("data/b/2.json", "data/a/1.json", "data/a/z.json", "data/a/x.txt"):
        (tmp_path / path).write_text("{}")
    (tmp_path / "data" / "a" / ".hidden.json").write_text("{}")
    (tmp_path / "data" / "a" / "dir.json").mkdir()  # a directory never matches
    matches = _reader(tmp_path).expand(_request("data/*/*.json"))
    assert matches == ("data/a/1.json", "data/a/z.json", "data/b/2.json")
    assert _reader(tmp_path).expand(_request("data/a/.*.json")) == ("data/a/.hidden.json",)


def test_a_glob_never_walks_into_a_link_and_never_matches_control_paths(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "f.json").write_text("{}")
    os.symlink(tmp_path / "real", tmp_path / "linked")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "f.json").write_text("{}")
    assert _reader(tmp_path).expand(_request("*/f.json")) == ("real/f.json",)
    assert _reader(tmp_path).expand(_request(".*/f.json")) == ()


def test_more_matches_than_the_cap_refuse(tmp_path: Path) -> None:
    for index in range(3):
        (tmp_path / f"{index}.json").write_text("{}")
    assert len(_reader(tmp_path).expand(_request("*.json", max_files=3))) == 3
    with pytest.raises(WorkspaceFileReadRefused) as refused:
        _reader(tmp_path).expand(_request("*.json", max_files=2))
    assert refused.value.path_class == "glob_over_cap"


@pytest.mark.parametrize("glob", ["data/**/x.json", "data/x.json", "../*.json", "/abs/*.json"])
def test_a_glob_names_a_pattern_within_its_workspace(glob: str) -> None:
    with pytest.raises(ValidationError):
        _request(glob)


def test_a_glob_input_is_told_apart_and_never_carries_the_binding_digest() -> None:
    assert is_workspace_glob({"glob": "*.json"})
    assert not is_workspace_glob({"relative_path": "a.json"})
    with pytest.raises(ValueError, match="resolved by the daemon"):
        parse_authored_workspace_glob({"glob": "*.json", "workspace_binding_digest": "x"})


# -- directory-entry cache correctness (F3): a stale candidate is never consumed -----

#: Never a real descriptor; the fakes below act only on this sentinel so every
#: other ``os`` call in the process (including pytest's own) passes through.
FAKE_FD = 999_999


@dataclass
class _FakeStat:
    st_dev: int
    st_ino: int
    st_mtime_ns: int = 0
    st_ctime_ns: int = 0


class _FakeDirectory:
    """A directory's live ``name -> inode`` state, independent of any real filesystem.

    Lets the cache-staleness regressions construct exact before/after states
    (a rename immediately followed by a distinct recreate under the freed
    name, a directory inode reused for unrelated content) deterministically,
    without depending on the host filesystem's own case-sensitivity or
    timer resolution -- the same effect the review's own mocked-filesystem
    probes used.
    """

    def __init__(self, *, dev: int = 1, ino: int = 42) -> None:
        self.dev = dev
        self.ino = ino
        self.mtime_ns = 1_000
        self.ctime_ns = 1_000
        self.entries: dict[str, int] = {}
        self._next_ino = 100

    def create(self, name: str) -> int:
        self._next_ino += 1
        self.entries[name] = self._next_ino
        self.mtime_ns += 1
        self.ctime_ns += 1
        return self.entries[name]

    def remove(self, name: str) -> None:
        del self.entries[name]
        self.mtime_ns += 1
        self.ctime_ns += 1

    def identity(self) -> tuple[int, int, int, int]:
        return (self.dev, self.ino, self.mtime_ns, self.ctime_ns)

    def stat(self) -> _FakeStat:
        return _FakeStat(self.dev, self.ino, self.mtime_ns, self.ctime_ns)

    def stat_of(self, name: str) -> _FakeStat:
        return _FakeStat(self.dev, self.entries[name])


@pytest.fixture
def fake_directory(monkeypatch: pytest.MonkeyPatch) -> _FakeDirectory:
    directory = _FakeDirectory()
    real_fstat, real_listdir, real_stat = os.fstat, os.listdir, os.stat

    def fake_fstat(fd: int, *a: Any, **kw: Any) -> Any:
        if fd == FAKE_FD:
            return directory.stat()
        return real_fstat(fd, *a, **kw)

    def fake_listdir(path: Any = ".", *a: Any, **kw: Any) -> list[str]:
        if path == FAKE_FD:
            return list(directory.entries)
        return real_listdir(path, *a, **kw)

    def fake_stat(path: Any, *a: Any, dir_fd: int | None = None, **kw: Any) -> Any:
        if dir_fd == FAKE_FD:
            name = path if isinstance(path, str) else os.fsdecode(path)
            if name not in directory.entries:
                raise FileNotFoundError(name)
            return directory.stat_of(name)
        return real_stat(path, *a, dir_fd=dir_fd, **kw)

    monkeypatch.setattr(os, "fstat", fake_fstat)
    monkeypatch.setattr(os, "listdir", fake_listdir)
    monkeypatch.setattr(os, "stat", fake_stat)
    return directory


def test_a_renamed_and_recreated_name_is_never_consumed_from_a_stale_cache(
    fake_directory: _FakeDirectory,
) -> None:
    """``FOO.json`` renamed to ``Foo.json``, then a new ``FOO.json`` created (F3).

    The cache is seeded *after* the rename, so its listing holds only
    ``Foo.json`` -- never the literal string ``FOO.json`` -- which is what
    makes the fast exact-match path miss and fall to the case-folding
    fallback below. A new, distinct ``FOO.json`` is then created. Even when
    the directory-level staleness check is defeated on purpose (its
    recorded identity is forced to agree, simulating coarse clock
    resolution), the per-candidate identity check -- a fresh stat of the
    requested name compared against a fresh stat of the cached candidate --
    must still catch the mismatch and force a relist: the read must consume
    the live ``FOO.json`` (its new inode), never the stale ``Foo.json``.
    """

    old_ino = fake_directory.create("FOO.json")
    fake_directory.remove("FOO.json")
    fake_directory.entries["Foo.json"] = old_ino  # the rename, under another name

    cache: DirectoryEntryCache = {}
    WorkspaceFileReader._directory_entries(FAKE_FD, cache=cache)
    key = (fake_directory.dev, fake_directory.ino)
    _, stale_entries, stale_by_name = cache[key]
    assert stale_entries == ("Foo.json",)  # the stale listing holds only the renamed name
    assert "FOO.json" not in stale_by_name  # so the exact-match fast path cannot hit

    new_ino = fake_directory.create("FOO.json")
    assert new_ino != old_ino

    # Defeat the directory-level staleness check on purpose: only the
    # per-candidate check protects the read from here.
    cache[key] = (fake_directory.identity(), stale_entries, stale_by_name)

    resolved = WorkspaceFileReader._resolve_component(FAKE_FD, "FOO.json", cache)
    assert resolved is None, "a stale candidate must never be trusted on its word alone"

    actual, verified_identity = WorkspaceFileReader._actual_component(
        FAKE_FD, "FOO.json", cache=cache
    )
    assert actual == "FOO.json"
    assert verified_identity is None  # resolved by exact match once relisted, nothing substituted
    opened = os.stat("FOO.json", dir_fd=FAKE_FD, follow_symlinks=False)
    assert opened.st_ino == new_ino  # the live file, not the renamed original

    # The cache is fresh now: the renamed file resolves correctly too.
    renamed, _ = WorkspaceFileReader._actual_component(FAKE_FD, "Foo.json", cache=cache)
    assert renamed == "Foo.json"


def test_a_listing_that_changes_mid_glob_relists_once_for_the_new_name(
    fake_directory: _FakeDirectory,
) -> None:
    """A file created after the shared cache was seeded is still read correctly."""

    fake_directory.create("a.json")
    cache: DirectoryEntryCache = {}
    WorkspaceFileReader._directory_entries(FAKE_FD, cache=cache)
    resolved_a, _ = WorkspaceFileReader._actual_component(FAKE_FD, "a.json", cache=cache)
    assert resolved_a == "a.json"

    fake_directory.create("b.json")
    resolved_b, _ = WorkspaceFileReader._actual_component(FAKE_FD, "b.json", cache=cache)
    assert resolved_b == "b.json"
    # The relist is itself cached: a further lookup costs no further listing.
    key = (fake_directory.dev, fake_directory.ino)
    assert cache[key][0] == fake_directory.identity()
    resolved_a_again, _ = WorkspaceFileReader._actual_component(FAKE_FD, "a.json", cache=cache)
    assert resolved_a_again == "a.json"


def test_a_directory_inode_reused_for_different_content_is_never_trusted(
    fake_directory: _FakeDirectory,
) -> None:
    """A stale cache entry keyed by a reused ``(device, inode)`` is never reused."""

    key = (fake_directory.dev, fake_directory.ino)
    cache: DirectoryEntryCache = {
        key: (
            (fake_directory.dev, fake_directory.ino, 1, 1),
            ("ghost.json",),
            frozenset({"ghost.json"}),
        )
    }
    fake_directory.create("real.json")
    fake_directory.mtime_ns = fake_directory.ctime_ns = 99_999  # a later, unrelated directory

    entries, by_name = WorkspaceFileReader._directory_entries(FAKE_FD, cache=cache)
    assert entries == ("real.json",)
    assert "ghost.json" not in by_name
