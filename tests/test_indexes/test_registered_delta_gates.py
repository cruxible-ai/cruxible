"""A successor's reader gates run over its changed entries and carried totals."""

from __future__ import annotations

import hashlib

import pytest

from cruxible_client.contracts.errors import ProjectionFormatError
from cruxible_core.compiler.compiler import SUPPORTED_COMPILERS, artifact_kinds_for_compiler
from cruxible_core.compiler.projection_tree import TreeReadLimits, read_registered_delta
from cruxible_core.ledger.git import (
    GitTreeChange,
    GitTreeEntry,
    _listing_child_names,
    _listing_has_path,
)

KINDS = artifact_kinds_for_compiler(SUPPORTED_COMPILERS[-1])


def _oid(content: bytes) -> str:
    return hashlib.sha1(content).hexdigest()


def _listing(*paths: str) -> tuple[GitTreeEntry, ...]:
    return tuple(
        GitTreeEntry(path=path, mode="100644", object_type="blob", oid=_oid(path.encode()), size=1)
        for path in sorted(paths)
    )


class _Repository:
    def __init__(self, base: tuple[str, ...], head: tuple[str, ...], blobs: dict[str, bytes]):
        self._trees = {"base": _listing(*base), "head": _listing(*head)}
        self._blobs = blobs
        self.read: list[str] = []

    def read_blobs(self, oids):
        self.read.extend(oids)
        return {oid: self._blobs[oid] for oid in oids}

    def object_sizes(self, oids):
        return {oid: ("blob", len(self._blobs[oid])) for oid in oids}

    def tree_has_path(self, oid, path):
        return _listing_has_path(self._trees[oid], path)

    def tree_child_names(self, oid, directory):
        return _listing_child_names(self._trees[oid], directory)


def test_child_names_skip_subtrees_and_existence_sees_directories() -> None:
    listing = _listing("a/b/c.json", "a/b.json", "a/b-x/d.json", "a/z.json", "q.json")
    assert _listing_child_names(listing, "a") == ("b-x", "b.json", "b", "z.json")
    assert _listing_child_names(listing, "") == ("a", "q.json")
    assert _listing_has_path(listing, "a/b")
    assert _listing_has_path(listing, "a/b/c.json")
    assert not _listing_has_path(listing, "a/c")


def _change(path: str, content: bytes | None, *, previous: bytes | None = None, mode="100644"):
    status = "A" if previous is None else ("D" if content is None else "M")
    return GitTreeChange(
        path=path,
        status=status,
        mode=mode,
        oid=None if content is None else _oid(content),
        previous_oid=None if previous is None else _oid(previous),
    )


def test_carried_totals_follow_additions_and_removals() -> None:
    old, new = b"x" * 10, b"y" * 4
    repository = _Repository((), (), {_oid(old): old, _oid(new): new})
    changes = (
        _change("cards/a.json", new, previous=old),
        _change("cards/b.json", None, previous=old),
    )
    _blobs, inventory = read_registered_delta(
        repository,
        changes,
        base_oid="base",
        head_oid="head",
        parent_inventory=(5, 100),
        limits=TreeReadLimits(),
        artifact_kinds=KINDS,
        include_paths=frozenset(),
    )
    assert inventory == (4, 100 - 10 + 4 - 10)

    with pytest.raises(ProjectionFormatError, match="file-count limit"):
        read_registered_delta(
            repository,
            (_change("cards/c.json", new),),
            base_oid="base",
            head_oid="head",
            parent_inventory=(3, 0),
            limits=TreeReadLimits(max_files=3),
            artifact_kinds=KINDS,
            include_paths=frozenset(),
        )
    with pytest.raises(ProjectionFormatError, match="total-byte limit"):
        read_registered_delta(
            repository,
            (_change("cards/c.json", new),),
            base_oid="base",
            head_oid="head",
            parent_inventory=(0, 97),
            limits=TreeReadLimits(max_total_bytes=100),
            artifact_kinds=KINDS,
            include_paths=frozenset(),
        )


def test_forbidden_modes_and_case_fold_siblings_are_refused() -> None:
    content = b"{}"
    repository = _Repository(
        ("cards/a/One.json",),
        ("cards/a/One.json", "cards/a/one.json", "cards/B/x.json", "cards/b/y.json"),
        {_oid(content): content},
    )
    with pytest.raises(ProjectionFormatError, match="forbidden symlink"):
        read_registered_delta(
            repository,
            (_change("cards/a/two.json", content, mode="120000"),),
            base_oid="base",
            head_oid="head",
            parent_inventory=(1, 2),
            limits=TreeReadLimits(),
            artifact_kinds=KINDS,
            include_paths=frozenset(),
        )
    for added in ("cards/a/one.json", "cards/b/y.json"):
        with pytest.raises(ProjectionFormatError, match="collision-free"):
            read_registered_delta(
                repository,
                (_change(added, content),),
                base_oid="base",
                head_oid="head",
                parent_inventory=(1, 2),
                limits=TreeReadLimits(),
                artifact_kinds=KINDS,
                include_paths=frozenset(),
            )


def test_a_file_child_never_hides_a_later_sibling_directory() -> None:
    # "A.MD" is a file; "A.MD-" is a directory sorting after it. Skipping from
    # the file as if it were a directory would miss "A.MD-" entirely.
    listing = _listing("cards/A.MD", "cards/A.MD-/x.md", "cards/a.md-/y.md")
    assert _listing_child_names(listing, "cards") == ("A.MD", "A.MD-", "a.md-")
    content = b"{}"
    repository = _Repository(
        ("cards/A.MD", "cards/A.MD-/x.md"),
        ("cards/A.MD", "cards/A.MD-/x.md", "cards/a.md-/y.md"),
        {_oid(content): content},
    )
    with pytest.raises(ProjectionFormatError, match="collision-free"):
        read_registered_delta(
            repository,
            (_change("cards/a.md-/y.md", content),),
            base_oid="base",
            head_oid="head",
            parent_inventory=(2, 4),
            limits=TreeReadLimits(),
            artifact_kinds=KINDS,
            include_paths=frozenset(),
        )
    # The whole-inventory reader refuses the same tree.
    from cruxible_client.contracts.canonical import normalize_manifest_paths

    with pytest.raises(Exception, match="case-fold"):
        normalize_manifest_paths(["cards/A.MD", "cards/A.MD-/x.md", "cards/a.md-/y.md"])


def test_rejected_deltas_never_read_a_payload() -> None:
    big, small = b"x" * 64, b"{}"
    repository = _Repository((), (), {_oid(big): big, _oid(small): small})
    for changes, limits, message in (
        ((_change("cards/big.json", big),), TreeReadLimits(max_blob_bytes=10), "per-file"),
        ((_change("cards/a.json", small),), TreeReadLimits(max_total_bytes=1), "total-byte"),
        ((_change("cards/a.json", small, mode="120000"),), TreeReadLimits(), "symlink"),
    ):
        with pytest.raises(ProjectionFormatError, match=message):
            read_registered_delta(
                repository,
                changes,
                base_oid="base",
                head_oid="head",
                parent_inventory=(0, 0),
                limits=limits,
                artifact_kinds=KINDS,
                include_paths=frozenset({"cards/big.json", "cards/a.json"}),
            )
    assert repository.read == []
