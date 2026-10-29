"""A successor's reader gates run over its changed entries and carried totals."""

from __future__ import annotations

import hashlib

import pytest

from cruxible_client.contracts.errors import ProjectionFormatError
from cruxible_core.compiler.compiler import ARTIFACT_KINDS
from cruxible_core.compiler.projection_tree import TreeReadLimits, read_registered_delta
from cruxible_core.ledger.git import (
    GitTreeChange,
    GitTreeEntry,
    _listing_child_names,
    _listing_has_path,
)

KINDS = ARTIFACT_KINDS


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


class _CountingRepository(_Repository):
    """Counts the directory listings the collision check asks for and the names it walks."""

    def __init__(self, base: tuple[str, ...], head: tuple[str, ...], blobs: dict[str, bytes]):
        super().__init__(base, head, blobs)
        self.listings: list[str] = []
        self.listed_names = 0

    def tree_child_names(self, oid, directory):
        names = super().tree_child_names(oid, directory)
        self.listings.append(directory)
        self.listed_names += len(names)
        return names


def _flat_delta(directory: str, siblings: int, added: int, *, suffix: str = ".json"):
    content = b"{}"
    existing = tuple(f"{directory}/s{index:05d}{suffix}" for index in range(siblings))
    new = tuple(f"{directory}/n{index:05d}{suffix}" for index in range(added))
    repository = _CountingRepository(existing, existing + new, {_oid(content): content})
    changes = tuple(_change(path, content) for path in new)
    read_registered_delta(
        repository,
        changes,
        base_oid="base",
        head_oid="head",
        parent_inventory=(siblings, 2 * siblings),
        limits=TreeReadLimits(),
        artifact_kinds=KINDS,
        include_paths=frozenset(),
    )
    return repository


def test_adding_to_a_flat_case_rigid_directory_costs_the_same_at_any_width() -> None:
    # Subject file names are lowercase by grammar: no sibling can share a fold, so
    # neither width lists the directory, and the work is the delta's alone.
    counts = [
        (repository.listings, repository.listed_names)
        for repository in (
            _flat_delta("subjects/project.work_item", width, 20) for width in (100, 5_000)
        )
    ]
    assert counts[0] == counts[1] == ([], 0)


def test_an_open_directory_is_listed_once_per_delta_however_many_names_it_gains() -> None:
    # Card paths are unregistered: their directory is checked exactly, once.
    repository = _flat_delta("cards/flat", 300, 40)
    assert repository.listings == ["cards/flat"]
    assert repository.listed_names == 340


def test_case_rigid_tops_follow_the_registered_grammars() -> None:
    from cruxible_client.contracts.artifacts import ArtifactKindRegistry, ArtifactPathKind
    from cruxible_core.compiler.projection_tree import _case_rigid_tops

    tops = _case_rigid_tops(KINDS)
    # ``CLM-`` is upper case, but each of its positions admits one case only.
    assert {"subjects", "claims", "changesets", "attestations", "governance"} <= tops
    assert "cards" not in tops
    import re

    mixed = ArtifactKindRegistry(
        (
            ArtifactPathKind("subject", re.compile(r"^notes/[a-z]+\.json$")),
            ArtifactPathKind("document", re.compile(r"^notes/[A-Z]+\.json$")),
            ArtifactPathKind("line", re.compile(r"^marks/[A-Za-z]{3}\.json$")),
            ArtifactPathKind("claim", re.compile(r"^upper/X[0-9]{2}\.json$")),
        )
    )
    # Two positional or case-free kinds sharing a directory, or one admitting both
    # cases at a position, leave it open; one positional kind alone is rigid.
    assert _case_rigid_tops(mixed) == frozenset({"upper"})
    unanchored = ArtifactKindRegistry(
        (ArtifactPathKind("subject", re.compile(r"^[a-z]+/x\.json$")),)
    )
    assert _case_rigid_tops(unanchored) == frozenset()


def test_open_kinds_still_refuse_case_fold_siblings_exactly() -> None:
    import re

    from cruxible_client.contracts.artifacts import ArtifactKindRegistry, ArtifactPathKind

    kinds = ArtifactKindRegistry(
        (ArtifactPathKind("subject", re.compile(r"^notes/[A-Za-z]+\.json$")),)
    )
    content = b"{}"
    base = tuple(f"notes/s{'a' * index}.json" for index in range(1, 50)) + ("notes/Abc.json",)
    for added, refused in (("notes/abc.json", True), ("notes/abd.json", False)):
        repository = _Repository(base, (*base, added), {_oid(content): content})
        call = lambda: read_registered_delta(  # noqa: E731
            repository,
            (_change(added, content),),
            base_oid="base",
            head_oid="head",
            parent_inventory=(len(base), 2 * len(base)),
            limits=TreeReadLimits(),
            artifact_kinds=kinds,
            include_paths=frozenset(),
        )
        if refused:
            with pytest.raises(ProjectionFormatError, match="collision-free"):
                call()
        else:
            call()


def _git_ledger(path):
    from cruxible_core.ledger.git import GitLedger

    return GitLedger.initialize(
        path,
        object_format="sha1",
        signing_key_path=path.parent / "unused-key",
        allowed_signers_path=path.parent / "unused-signers",
    )


def _counting_listings(monkeypatch):
    from cruxible_core.ledger.git import GitLedger

    listed = [0]
    child_names = GitLedger.tree_child_names

    def counted(self, oid, directory):
        names = child_names(self, oid, directory)
        listed[0] += len(names)
        return names

    monkeypatch.setattr(GitLedger, "tree_child_names", counted)
    return listed


def _raw_tree(ledger, files: dict[str, bytes]) -> str:
    """A tree Git writes without the ledger's own collision refusal."""

    blob = ledger._git(["hash-object", "-w", "--stdin"], input_bytes=b"card").decode().strip()
    children: dict[str, dict[str, bytes]] = {}
    rows = []
    for path in files:
        head, _sep, rest = path.partition("/")
        if rest:
            children.setdefault(head, {})[rest] = b"card"
        else:
            rows.append(f"100644 blob {blob}\t{head}")
    for name, nested in children.items():
        rows.append(f"040000 tree {_raw_tree(ledger, nested)}\t{name}")
    listing = "".join(f"{row}\n" for row in rows).encode()
    return ledger._git(["mktree"], input_bytes=listing).decode().strip()


def _ledger_delta(ledger, base: dict[str, bytes], head: dict[str, bytes]):
    base_oid, head_oid = _raw_tree(ledger, base), _raw_tree(ledger, head)
    read_registered_delta(
        ledger,
        ledger.changed_entries(base_oid, head_oid),
        base_oid=base_oid,
        head_oid=head_oid,
        parent_inventory=(len(base), sum(len(body) for body in base.values())),
        limits=TreeReadLimits(),
        artifact_kinds=KINDS,
        include_paths=frozenset(),
    )


def test_an_open_directory_carries_its_folds_from_the_parent_generation(
    tmp_path, monkeypatch
) -> None:
    """Card paths are checked exactly; a warm successor never re-lists its directory."""

    listed = _counting_listings(monkeypatch)
    warm = []
    for width in (100, 2_000):
        ledger = _git_ledger(tmp_path / f"ledger-{width}.git")
        tree = {f"cards/flat/s{index:05d}.md": b"card" for index in range(width)}
        grown = {**tree, "cards/flat/first.md": b"card"}
        _ledger_delta(ledger, tree, grown)  # cold: one listing
        listed[0] = 0
        successor = {**grown, **{f"cards/flat/n{index:03d}.md": b"card" for index in range(20)}}
        del successor["cards/flat/s00003.md"]
        _ledger_delta(ledger, grown, successor)
        warm.append(listed[0])
        # The carried folds are exact: a name the delta removed may come back in
        # another case, and one still held refuses its other spelling.
        _ledger_delta(ledger, successor, {**successor, "cards/flat/S00003.md": b"card"})
        with pytest.raises(ProjectionFormatError, match="collision-free"):
            _ledger_delta(ledger, successor, {**successor, "cards/flat/N001.md": b"card"})
    assert warm == [0, 0]


def test_a_root_name_removed_from_a_rigid_top_leaves_the_root_folds_exact(tmp_path) -> None:
    """One delta drops the last path under a rigid top and adds a root name sharing its fold.

    The root's folds are carried from the parent's, so the removed top-level name
    must leave them even though nothing below a rigid top is folded.
    """

    import re

    from cruxible_client.contracts.artifacts import ArtifactKindRegistry, ArtifactPathKind
    from cruxible_core.compiler.projection_tree import _case_rigid_tops

    kinds = ArtifactKindRegistry(
        (
            ArtifactPathKind("subject", re.compile(r"^notes/[a-z]+\.json$")),
            ArtifactPathKind("document", re.compile(r"^NOTES/[a-z]+\.json$")),
            ArtifactPathKind("line", re.compile(r"^other/[a-z]+\.json$")),
        )
    )
    assert {"notes", "other"} <= _case_rigid_tops(kinds)
    ledger = _git_ledger(tmp_path / "ledger.git")

    def delta(base: dict[str, bytes], head: dict[str, bytes]) -> None:
        base_oid, head_oid = _raw_tree(ledger, base), _raw_tree(ledger, head)
        read_registered_delta(
            ledger,
            ledger.changed_entries(base_oid, head_oid),
            base_oid=base_oid,
            head_oid=head_oid,
            parent_inventory=(len(base), 4 * len(base)),
            limits=TreeReadLimits(),
            artifact_kinds=kinds,
            include_paths=frozenset(),
        )

    first = {"notes/a.json": b"card"}
    second = {**first, "other/a.json": b"card"}
    delta(first, second)  # a new root name: the root's folds are remembered
    third = {"other/a.json": b"card", "NOTES/a.json": b"card"}
    delta(second, third)  # "notes" left the root as "NOTES" joined it: no collision
    with pytest.raises(ProjectionFormatError, match="collision-free"):
        delta(third, {**third, "notes/b.json": b"card"})  # and it is still held
