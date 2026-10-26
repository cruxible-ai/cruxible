"""Bounded exact-tree reader for the Python reference assembler."""

from __future__ import annotations

import functools
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from cruxible_client.contracts.artifacts import ArtifactKindRegistry
from cruxible_client.contracts.canonical import (
    CARD_NAMESPACE,
    is_candidate_card_path,
    normalize_ledger_path,
    normalize_manifest_paths,
)
from cruxible_client.contracts.errors import ProjectionFormatError
from cruxible_client.contracts.persistent import PersistentMap
from cruxible_core.compiler.projection_artifacts import registered_path_kind
from cruxible_core.ledger.git import GitLedger, GitTreeChange, GitTreeEntry
from cruxible_core.ledger.protocols import LedgerRepositoryProtocol

_LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1\n"


class TreeReadLimits(BaseModel):
    """Serialized resource bounds applied before parsing any artifact.

    The file-count and aggregate-byte defaults carry the pre-PC-G adoption
    posture: sharded Claim vocabularies reach six figures of members, so a
    10,000-file ceiling would refuse a real instance rather than a hostile one.
    Raising the count does not weaken any other gate -- path canonicality,
    registered-kind resolution, mode/symlink/submodule refusal, declared size
    and LFS-pointer refusal all stay exactly as they were.

    The per-blob ceiling is 64 MiB, raised from 4 MiB because 4 MiB was two
    budgets for one thing. The ledger writes its own record of a change set as
    a single blob costing up to 11,264 bytes per lowered entry, so a 4 MiB
    ceiling settled at most 372 entries while proposal admission advertised
    `max_changed_members: 5000` -- one number for what a submission may carry,
    a different one for what could then be recorded. At 64 MiB they are ONE
    number: 5,000 entries project to 56,320,000 bytes, about 53.7 MiB, which
    fits under the ceiling with room left. It closes a second gap in passing:
    receive admits a single member of up to 8 MiB against a read ceiling of 4,
    so a captured source over 4 MiB was admissible and then unreadable. Such a
    source may now back a Claim.

    Raising a READ limit is backward compatible: every blob already accepted was
    written under the 4 MiB ceiling and is therefore still under this one, so no
    accepted tree changes meaning and no accepted artifact changes bytes. It is
    still a bound -- it is what keeps a single member from exhausting memory --
    and operators may configure lower soft limits; the serialized hard ceiling
    stays bounded either way.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_files: int = Field(default=250_000, ge=1, le=1_000_000)
    max_total_bytes: int = Field(default=512 * 1024 * 1024, ge=1, le=2**44)
    max_blob_bytes: int = Field(default=64 * 1024 * 1024, ge=1)


@dataclass(frozen=True)
class GitTreeBlob:
    path: str
    oid: str
    content: bytes


def read_registered_tree(
    repository: LedgerRepositoryProtocol,
    oid: str,
    *,
    limits: TreeReadLimits,
    artifact_kinds: ArtifactKindRegistry,
    include_paths: frozenset[str] | None = None,
) -> tuple[GitTreeBlob, ...]:
    """Read regular registered blobs only, with all metadata gates first."""

    return _read_registered_entries(
        repository,
        repository.list_tree_with_sizes(oid),
        limits=limits,
        artifact_kinds=artifact_kinds,
        include_paths=include_paths,
    )


def _read_registered_entries(
    repository: LedgerRepositoryProtocol,
    entries: tuple[GitTreeEntry, ...],
    *,
    limits: TreeReadLimits,
    artifact_kinds: ArtifactKindRegistry,
    include_paths: frozenset[str] | None = None,
) -> tuple[GitTreeBlob, ...]:
    """Consume this operation's exact Git inventory; apply every reader gate."""
    if len(entries) > limits.max_files:
        raise ProjectionFormatError(
            f"ledger tree exceeds file-count limit ({len(entries)} > {limits.max_files})"
        )
    try:
        if any(normalize_ledger_path(entry.path) != entry.path for entry in entries):
            raise ProjectionFormatError("ledger tree contains a noncanonical path spelling")
        ordered_paths = normalize_manifest_paths([entry.path for entry in entries])
    except Exception as exc:
        raise ProjectionFormatError(
            "ledger tree paths are not canonical and collision-free"
        ) from exc
    by_path = {entry.path: entry for entry in entries}
    if len(by_path) != len(entries):
        raise ProjectionFormatError("ledger tree contains duplicate paths")

    declared_total = 0
    for path in ordered_paths:
        entry = by_path[path]
        try:
            if normalize_ledger_path(path) != path:
                raise ProjectionFormatError(f"ledger path is not canonical: {path}")
            if not is_candidate_card_path(path):
                registered_path_kind(path, artifact_kinds=artifact_kinds)
        except Exception as exc:
            if isinstance(exc, ProjectionFormatError):
                raise
            raise ProjectionFormatError(f"ledger path is not registered: {path}") from exc
        if entry.mode != "100644" or entry.object_type != "blob":
            if entry.mode == "120000":
                kind = "symlink"
            elif entry.mode == "160000" or entry.object_type == "commit":
                kind = "submodule"
            else:
                kind = f"{entry.mode} {entry.object_type}"
            raise ProjectionFormatError(f"ledger tree contains forbidden {kind}: {path}")
        if entry.size is None or entry.size < 0:
            raise ProjectionFormatError(f"ledger blob has no trustworthy size: {path}")
        if entry.size > limits.max_blob_bytes:
            raise ProjectionFormatError(
                f"ledger blob exceeds per-file byte limit: {path} ({entry.size})"
            )
        declared_total += entry.size
        if declared_total > limits.max_total_bytes:
            raise ProjectionFormatError(
                "ledger tree exceeds total-byte limit "
                f"({declared_total} > {limits.max_total_bytes})"
            )

    if include_paths is not None:
        ordered_paths = [path for path in ordered_paths if path in include_paths]

    blobs: list[GitTreeBlob] = []
    cached = repository.read_blobs(
        tuple(dict.fromkeys(by_path[path].oid for path in ordered_paths))
    )
    actual_total = 0
    for path in ordered_paths:
        entry = by_path[path]
        try:
            content = cached[entry.oid]
        except KeyError as exc:
            raise ProjectionFormatError(f"repository omitted a requested blob: {path}") from exc
        if len(content) != entry.size:
            raise ProjectionFormatError(f"ledger blob size changed while reading: {path}")
        if content.startswith(_LFS_POINTER_PREFIX):
            raise ProjectionFormatError(f"Git LFS pointer is not an artifact payload: {path}")
        actual_total += len(content)
        if actual_total > limits.max_total_bytes:
            raise ProjectionFormatError("ledger tree exceeded byte limit while reading")
        blobs.append(GitTreeBlob(path=path, oid=entry.oid, content=content))
    return tuple(blobs)


def _forbidden_kind(mode: str) -> str:
    if mode == "120000":
        return "symlink"
    if mode == "160000":
        return "submodule"
    return mode


def _refuse_case_fold_siblings(
    repository: GitLedger,
    changes: tuple[GitTreeChange, ...],
    *,
    base_oid: str,
    head_oid: str,
    case_rigid_tops: frozenset[str],
) -> None:
    """Refuse an added name whose case fold another child of its directory shares.

    Only names the parent did not hold are checked: the parent's siblings passed
    when its projection was built. Below a case-rigid top-level directory
    (``_case_rigid_tops``) no two names a registered path may hold share a fold,
    so those levels need no look at all. Every other directory that gains a name
    is checked against its folded child names (``_head_folds``): carried from the
    parent directory's by this delta's own additions and removals, or listed once.
    """
    added: dict[str, set[str]] = {}
    removed: dict[str, set[str]] = {}
    looked: set[str] = set()
    for change in changes:
        parts = change.path.split("/")
        if change.status == "D" and change.oid is None:
            # The deepest name the head no longer holds leaves its directory.
            if parts[0] in case_rigid_tops:
                continue
            for depth in range(len(parts)):
                prefix = "/".join(parts[: depth + 1])
                if not repository.tree_has_path(head_oid, prefix):
                    removed.setdefault("/".join(parts[:depth]), set()).add(parts[depth])
                    break
            continue
        if change.status != "A":
            continue
        rigid = parts[0] in case_rigid_tops
        for depth, name in enumerate(parts):
            if depth and rigid:
                break
            prefix = "/".join(parts[: depth + 1])
            if prefix in looked:
                continue
            looked.add(prefix)
            if repository.tree_has_path(base_oid, prefix):
                continue  # this name already passed as the parent's sibling
            added.setdefault("/".join(parts[:depth]), set()).add(name)
    for directory, names in added.items():
        folds = _head_folds(
            repository,
            directory,
            base_oid=base_oid,
            head_oid=head_oid,
            added=names,
            removed=removed.get(directory, set()),
        )
        for name in names:
            if any(sibling != name for sibling in folds.get(name.casefold(), ())):
                raise ProjectionFormatError(
                    "ledger tree paths are not canonical and collision-free"
                )


# A directory's child names by case fold, per directory tree object ID (its
# names are a function of that ID alone, in any repository): a successor's is
# its parent's carried by the delta, so a directory is listed only when cold.
# Bounded by the names held (a generation touches many small shard directories
# and a few wide ones) as well as by directories.
_FOLDS_CAPACITY = 4096
_FOLDS_NAME_CAPACITY = 400_000
_FOLDS: OrderedDict[str, PersistentMap[tuple[str, ...]]] = OrderedDict()
_FOLDS_NAMES = 0
_FOLDS_LOCK = threading.Lock()


def _remembered_folds(tree_oid: str | None) -> PersistentMap[tuple[str, ...]] | None:
    if tree_oid is None:
        return None
    with _FOLDS_LOCK:
        folds = _FOLDS.get(tree_oid)
        if folds is not None:
            _FOLDS.move_to_end(tree_oid)
        return folds


def _remember_folds(tree_oid: str, folds: PersistentMap[tuple[str, ...]]) -> None:
    global _FOLDS_NAMES
    with _FOLDS_LOCK:
        previous = _FOLDS.pop(tree_oid, None)
        if previous is not None:
            _FOLDS_NAMES -= len(previous)
        _FOLDS[tree_oid] = folds
        _FOLDS_NAMES += len(folds)
        while len(_FOLDS) > 1 and (
            len(_FOLDS) > _FOLDS_CAPACITY or _FOLDS_NAMES > _FOLDS_NAME_CAPACITY
        ):
            _old, evicted = _FOLDS.popitem(last=False)
            _FOLDS_NAMES -= len(evicted)


def _head_folds(
    repository: GitLedger,
    directory: str,
    *,
    base_oid: str,
    head_oid: str,
    added: set[str],
    removed: set[str],
) -> Mapping[str, tuple[str, ...]]:
    """The head directory's child names by case fold.

    Carried from the parent directory's remembered folds by exactly the names
    this delta adds and removes there (Git's diff names every changed entry), or,
    with none remembered, folded from one listing of the head directory.
    """
    directory_oid = getattr(repository, "directory_tree_oid", None)
    head_tree = None if directory_oid is None else directory_oid(head_oid, directory)
    remembered = _remembered_folds(head_tree)
    if remembered is not None:
        return remembered
    base_tree = None if directory_oid is None else directory_oid(base_oid, directory)
    base: PersistentMap[tuple[str, ...]] | None
    if directory_oid is not None and base_tree is None:
        base = PersistentMap()  # a new directory: every child name is an added one
    else:
        base = _remembered_folds(base_tree)
    if base is None or head_tree is None:
        listed: dict[str, list[str]] = {}
        for sibling in repository.tree_child_names(head_oid, directory):
            listed.setdefault(sibling.casefold(), []).append(sibling)
        folds = PersistentMap((fold, tuple(sorted(group))) for fold, group in listed.items())
    else:
        folds = base
        for name in removed:
            fold = name.casefold()
            kept = tuple(item for item in folds.get(fold, ()) if item != name)
            folds = folds.set(fold, kept) if kept else folds.delete(fold)
        for name in added:
            fold = name.casefold()
            group = folds.get(fold, ())
            if name not in group:
                folds = folds.set(fold, tuple(sorted((*group, name))))
    if head_tree is not None:
        _remember_folds(head_tree, folds)
    return folds


@functools.lru_cache(maxsize=32)
def _case_rigid_tops(artifact_kinds: ArtifactKindRegistry) -> frozenset[str]:
    """Top-level directories below which no two registrable names share a case fold.

    Every non-card path a successor holds resolves to a kind of this registry
    (its parent passed the same gates under the same compiler), so the entries of
    a directory below top-level directory ``t`` are names of paths of the kinds
    whose patterns begin with the literal ``t/``. ``t`` is rigid when every such
    kind's language is case-fixed (each character equals its own case fold, so
    distinct names have distinct folds), or when ``t`` has exactly one kind and
    its language is case-positional (fixed length, ``/`` only as a literal, and
    no two characters any one position admits share a fold). The card namespace
    holds unregistered paths and is never rigid. Any pattern this analysis cannot
    read leaves no directory rigid, so every level is checked exactly.
    """
    by_top: dict[str, list[str]] = {}
    for entry in artifact_kinds.entries():
        try:
            top, grammar = _pattern_case_grammar(entry.pattern)
        except _UnreadPattern:
            return frozenset()
        if top is not None:
            by_top.setdefault(top, []).append(grammar)
    card_top = CARD_NAMESPACE.rstrip("/")
    return frozenset(
        top
        for top, grammars in by_top.items()
        if top != card_top
        and (
            all(grammar == "fixed" for grammar in grammars)
            or grammars in (["fixed"], ["positional"])
        )
    )


class _UnreadPattern(Exception):
    """A registered pattern outside the grammar ``_pattern_case_grammar`` reads."""


def _pattern_case_grammar(pattern: re.Pattern[str]) -> tuple[str | None, str]:
    """(literal top-level directory or None for a root file, "fixed" | "positional" | "open").

    Raises ``_UnreadPattern`` when the top-level directory is not a literal, so
    the pattern could place a path anywhere.
    """
    try:
        from re import _constants as constants  # type: ignore[attr-defined]
        from re import _parser as parser  # type: ignore[attr-defined]

        items = list(parser.parse(pattern.pattern, pattern.flags))
    except Exception as exc:  # an interpreter without this parser checks every level
        raise _UnreadPattern from exc
    if (
        pattern.flags & re.IGNORECASE
        or not items
        or items[0]
        != (
            constants.AT,
            constants.AT_BEGINNING,
        )
    ):
        raise _UnreadPattern
    prefix = ""
    for opcode, argument in items[1:]:
        if opcode is not constants.LITERAL:
            break
        prefix += chr(argument)
    top: str | None
    if "/" in prefix:
        top = prefix.split("/", 1)[0]
    elif items[-1] == (constants.AT, constants.AT_END) and len(prefix) == len(items) - 2:
        top = None  # one literal root file: no directory holds it
    else:
        raise _UnreadPattern
    characters = _characters(items[1:], constants)
    if characters is not None and all(item.casefold() == item for item in characters):
        return top, "fixed"
    anchored = items[-1] == (constants.AT, constants.AT_END)
    positions = _fixed_positions(items[1:-1], constants) if anchored else None
    if positions is not None and all(_fold_injective(position) for position in positions):
        return top, "positional"
    return top, "open"


_MAX_CLASS = 512


def _class_members(argument: Any, constants: Any) -> set[str] | None:
    members: set[str] = set()
    for opcode, value in argument:
        if opcode is constants.LITERAL:
            members.add(chr(value))
        elif opcode is constants.RANGE and value[1] - value[0] < _MAX_CLASS:
            members.update(chr(code) for code in range(value[0], value[1] + 1))
        else:
            return None
    return members


def _characters(items: Any, constants: Any) -> set[str] | None:
    """Every character the items can match, or None when that is not a closed set."""
    found: set[str] = set()
    for opcode, argument in items:
        if opcode is constants.LITERAL:
            found.add(chr(argument))
        elif opcode is constants.IN:
            members = _class_members(argument, constants)
            if members is None:
                return None
            found |= members
        elif opcode in (constants.MAX_REPEAT, constants.MIN_REPEAT):
            inner = _characters(argument[2], constants)
            if inner is None:
                return None
            found |= inner
        elif opcode is constants.SUBPATTERN:
            if argument[1] or argument[2]:
                return None  # inline flags
            inner = _characters(argument[3], constants)
            if inner is None:
                return None
            found |= inner
        elif opcode is constants.BRANCH:
            for branch in argument[1]:
                inner = _characters(branch, constants)
                if inner is None:
                    return None
                found |= inner
        elif opcode is constants.AT and argument in (constants.AT_BEGINNING, constants.AT_END):
            continue
        else:
            return None
    return found


def _fixed_positions(items: Any, constants: Any) -> list[set[str]] | None:
    """The character set of each position of a fixed-length pattern, else None."""
    positions: list[set[str]] = []
    for opcode, argument in items:
        if opcode is constants.LITERAL:
            positions.append({chr(argument)})
        elif opcode is constants.IN:
            members = _class_members(argument, constants)
            if members is None or "/" in members:
                return None
            positions.append(members)
        elif opcode in (constants.MAX_REPEAT, constants.MIN_REPEAT):
            low, high, inner = argument
            unit = _fixed_positions(inner, constants)
            if low != high or unit is None:
                return None
            positions.extend(set(position) for _ in range(low) for position in unit)
        elif opcode is constants.SUBPATTERN and not argument[1] and not argument[2]:
            unit = _fixed_positions(argument[3], constants)
            if unit is None:
                return None
            positions.extend(unit)
        else:
            return None
    return positions


def _fold_injective(characters: set[str]) -> bool:
    folds = [character.casefold() for character in characters]
    return all(len(fold) == 1 for fold in folds) and len(set(folds)) == len(folds)


def read_registered_delta(
    repository: GitLedger,
    changes: tuple[GitTreeChange, ...],
    *,
    base_oid: str,
    head_oid: str,
    parent_inventory: tuple[int, int],
    limits: TreeReadLimits,
    artifact_kinds: ArtifactKindRegistry,
    include_paths: frozenset[str],
) -> tuple[tuple[GitTreeBlob, ...], tuple[int, int]]:
    """Apply every reader gate to a successor through its changed entries only.

    The parent's whole inventory passed these gates when its projection was
    built, and Git's diff proves each unreported path byte-identical, so the
    successor passes iff its changes do: per-entry gates here, the aggregate
    limits through the parent's carried (file count, byte total), and case-fold
    sibling collisions only at the levels where an added path introduces a name.
    Every metadata gate -- paths, modes, per-file and aggregate sizes,
    collisions -- runs before any payload is read. Returns the included blobs
    and the successor's inventory.
    """
    # Every gate that needs only metadata runs before any payload is read:
    # sizes come from Git's object headers, never from loading the bytes.
    wanted = [change.oid for change in changes if change.oid is not None]
    wanted += [change.previous_oid for change in changes if change.previous_oid is not None]
    sizes: dict[str, int] = {}
    for oid, info in repository.object_sizes(wanted).items():
        if info is None or info[0] != "blob":
            raise ProjectionFormatError(f"ledger tree names a missing or non-blob object: {oid}")
        sizes[oid] = info[1]
    files, total = parent_inventory
    for change in changes:
        if change.previous_oid is not None:
            files -= 1
            total -= sizes[change.previous_oid]
    admitted: list[GitTreeChange] = []
    for change in sorted(changes, key=lambda item: item.path.encode("utf-8")):
        if change.oid is None:
            continue
        path = change.path
        try:
            if normalize_ledger_path(path) != path:
                raise ProjectionFormatError(f"ledger path is not canonical: {path}")
            if not is_candidate_card_path(path):
                registered_path_kind(path, artifact_kinds=artifact_kinds)
        except Exception as exc:
            if isinstance(exc, ProjectionFormatError):
                raise
            raise ProjectionFormatError(f"ledger path is not registered: {path}") from exc
        if change.mode != "100644":
            raise ProjectionFormatError(
                f"ledger tree contains forbidden {_forbidden_kind(change.mode)}: {path}"
            )
        size = sizes[change.oid]
        if size > limits.max_blob_bytes:
            raise ProjectionFormatError(f"ledger blob exceeds per-file byte limit: {path} ({size})")
        files += 1
        total += size
        if path in include_paths:
            admitted.append(change)
    if files < 0 or total < 0:
        raise ProjectionFormatError("ledger tree delta removes more than its parent held")
    if files > limits.max_files:
        raise ProjectionFormatError(
            f"ledger tree exceeds file-count limit ({files} > {limits.max_files})"
        )
    if total > limits.max_total_bytes:
        raise ProjectionFormatError(
            f"ledger tree exceeds total-byte limit ({total} > {limits.max_total_bytes})"
        )
    _refuse_case_fold_siblings(
        repository,
        changes,
        base_oid=base_oid,
        head_oid=head_oid,
        case_rigid_tops=_case_rigid_tops(artifact_kinds),
    )
    admitted_oids = {change.path: change.oid for change in admitted if change.oid is not None}
    contents = repository.read_blobs(tuple(dict.fromkeys(admitted_oids.values())))
    blobs: list[GitTreeBlob] = []
    for change in admitted:
        oid = admitted_oids[change.path]
        content = contents[oid]
        if len(content) != sizes[oid]:
            raise ProjectionFormatError(f"ledger blob size changed while reading: {change.path}")
        if content.startswith(_LFS_POINTER_PREFIX):
            raise ProjectionFormatError(
                f"Git LFS pointer is not an artifact payload: {change.path}"
            )
        blobs.append(GitTreeBlob(path=change.path, oid=oid, content=content))
    return tuple(blobs), (files, total)


__all__ = ["GitTreeBlob", "TreeReadLimits", "read_registered_delta", "read_registered_tree"]
