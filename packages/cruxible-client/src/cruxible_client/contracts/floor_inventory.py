"""The floor manifest carried from one floor to the next, so a change costs the change.

A floor's ``manifest.json`` lists every file, byte-sorted, and is named by two
digests over that whole list: ``floor_digest`` (``floor_inventory_digest``, over
the canonical rows) and the manifest digest (``floor_manifest_digest``, over the
rendered bytes). ``build_floor_manifest`` and ``render_floor_manifest`` rebuild,
re-validate and re-encode every entry each time; this module is the same
manifest, kept between floors.

``FloorInventory`` holds the byte-sorted entries in short blocks. Each entry
carries its two encodings (its canonical row and its indented manifest row),
taken once when it enters, and each block its joined bytes. Beside them sit the
two indexes the manifest's path rules (``check_floor_paths``) are checked
against: every file's filesystem key, and how many files live under each
directory key. ``patch`` validates only the paths it adds, re-encodes only the
blocks they land in, and shares every other block and index node with the
inventory it came from, so a prior floor stays intact for concurrent readers
and delta bases. Every entry was validated once, when it entered; carried ones
are never rebuilt.

The digests stay the format's. Both are SHA-256 over bytes naming every file,
so a floor's bytes are joined and hashed whole, but at the speed of a copy:
no entry is touched unless it changed.

``FloorManifestState`` is one floor: the inventory with its header (renderer,
coordinate, generation, notes), its ``FloorManifest``, its exact
``manifest.json`` bytes and its manifest digest, each computed once. The few
most recent states are remembered by manifest digest, so an apply that reads
back the manifest a floor was last given finds that floor already validated.
"""

from __future__ import annotations

import json
import threading
from bisect import bisect_left, bisect_right
from collections import Counter, OrderedDict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property
from hashlib import sha256
from itertools import chain
from unicodedata import is_normalized

from pydantic import BaseModel

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.floor import (
    FLOOR_FORMAT,
    FLOOR_LOCAL_PATHS,
    FLOOR_MANIFEST_PATH,
    FloorEntry,
    FloorManifest,
    check_floor_paths,
    content_digest,
    floor_inventory_digest,
    floor_path_key,
    render_floor_manifest,
)
from cruxible_client.contracts.persistent import PersistentMap
from cruxible_client.contracts.primitives import pretty_json
from cruxible_client.contracts.projection import AcceptedCoordinate

# Entries per block when built; a block is split past twice this and merged
# into its neighbour below a quarter of it.
_BLOCK = 128
_LABEL = "a floor manifest"
_RESERVED = frozenset(floor_path_key(path) for path in (FLOOR_MANIFEST_PATH, *FLOOR_LOCAL_PATHS))
# The directories the reserved paths live under: no floor file may be one.
_RESERVED_DIRECTORIES = frozenset(
    floor_path_key("/".join(parts[:end]))
    for path in (FLOOR_MANIFEST_PATH, *FLOOR_LOCAL_PATHS)
    for parts in (path.split("/"),)
    for end in range(1, len(parts))
)
# ``canonical_bytes({"tag": FLOOR_FORMAT, "files": rows})``, around the rows.
_DIGEST_HEAD = b'{"files":['
_DIGEST_TAIL = b'],"tag":' + json.dumps(FLOOR_FORMAT).encode("utf-8") + b"}"
_FILES_MARKER = b'"files": []'
_EMPTY_FLOOR_DIGEST = floor_inventory_digest(())


def _directory_keys(path: str) -> Iterator[str]:
    parts = path.split("/")
    for end in range(1, len(parts)):
        yield floor_path_key("/".join(parts[:end]))


@dataclass(frozen=True, slots=True)
class _Row:
    """One entry with its two encodings, taken once when it enters a floor."""

    entry: FloorEntry
    # The canonical row ``floor_inventory_digest`` hashes.
    compact: bytes
    # The row as ``render_floor_manifest`` indents it inside ``files``.
    pretty: bytes
    # Whether the path is already NFC: the rendered manifest reads back as itself.
    canonical: bool


def _row(entry: FloorEntry) -> _Row:
    compact = canonical_bytes(entry.model_dump(mode="json"))
    pretty = "    " + pretty_json(json.loads(compact)).replace("\n", "\n    ")
    path = entry.path
    return _Row(
        entry, compact, pretty.encode("utf-8"), path.isascii() or is_normalized("NFC", path)
    )


@dataclass(frozen=True, slots=True)
class _Block:
    paths: tuple[str, ...]
    rows: tuple[_Row, ...]
    compact: bytes
    pretty: bytes
    latest: int
    canonical: bool


def _block(rows: Sequence[_Row]) -> _Block:
    return _Block(
        tuple(row.entry.path for row in rows),
        tuple(rows),
        b",".join(row.compact for row in rows),
        b",\n".join(row.pretty for row in rows),
        max(row.entry.changed_at for row in rows),
        all(row.canonical for row in rows),
    )


def _blocks(rows: Sequence[_Row]) -> list[_Block]:
    """Blocks of ``_BLOCK`` rows, or one block when fewer than two blocks' worth."""

    if len(rows) <= 2 * _BLOCK:
        return [_block(rows)] if rows else []
    return [_block(rows[start : start + _BLOCK]) for start in range(0, len(rows), _BLOCK)]


class FloorInventory:
    """A floor's byte-sorted, validated file inventory, persistent across floors.

    Built whole once (``build`` validates every path, ``from_entries`` adopts
    the entries of an already validated manifest); then each floor is the one
    before it ``patch``-ed with the paths that changed.
    """

    __slots__ = ("_blocks", "_directories", "_digest", "_keys", "_lows", "_size")

    _blocks: tuple[_Block, ...]
    _lows: tuple[str, ...]
    # filesystem key -> the one path holding it
    _keys: PersistentMap[str]
    # directory key -> how many files live under it
    _directories: PersistentMap[int]
    _size: int
    _digest: str | None

    def __init__(
        self,
        blocks: Sequence[_Block],
        keys: PersistentMap[str],
        directories: PersistentMap[int],
    ) -> None:
        self._blocks = tuple(blocks)
        self._lows = tuple(block.paths[0] for block in self._blocks)
        self._keys = keys
        self._directories = directories
        self._size = sum(len(block.rows) for block in self._blocks)
        self._digest = None

    # -- building ------------------------------------------------------------------

    @classmethod
    def build(cls, files: Mapping[str, tuple[str, int, int]]) -> FloorInventory:
        """The inventory of ``path -> (digest, byte_length, changed_at)``, validated whole."""

        ordered = sorted(files, key=lambda item: item.encode("utf-8"))
        entries = [
            FloorEntry(path=path, content_digest=digest, byte_length=size, changed_at=changed)
            for path in ordered
            for digest, size, changed in (files[path],)
        ]
        check_floor_paths(ordered, label=_LABEL)
        return cls._assemble(entries)

    @classmethod
    def from_entries(cls, entries: Sequence[FloorEntry]) -> FloorInventory:
        """The inventory of a validated manifest's entries, adopted without validating again."""

        return cls._assemble(entries)

    @classmethod
    def _assemble(cls, entries: Sequence[FloorEntry]) -> FloorInventory:
        keys = PersistentMap((floor_path_key(entry.path), entry.path) for entry in entries)
        counts = Counter(key for entry in entries for key in _directory_keys(entry.path))
        return cls(_blocks([_row(entry) for entry in entries]), keys, PersistentMap(counts))

    # -- reading -------------------------------------------------------------------

    def __len__(self) -> int:
        return self._size

    def __contains__(self, path: object) -> bool:
        return isinstance(path, str) and self.get(path) is not None

    def __iter__(self) -> Iterator[str]:
        """Every path, byte-sorted."""

        return chain.from_iterable(block.paths for block in self._blocks)

    def get(self, path: str) -> FloorEntry | None:
        index = bisect_right(self._lows, path) - 1
        if index < 0:
            return None
        block = self._blocks[index]
        position = bisect_left(block.paths, path)
        if position < len(block.paths) and block.paths[position] == path:
            return block.rows[position].entry
        return None

    def entries(self) -> tuple[FloorEntry, ...]:
        """Every entry, byte-sorted (shared objects; nothing is rebuilt)."""

        return tuple(row.entry for block in self._blocks for row in block.rows)

    def by_path(self) -> dict[str, FloorEntry]:
        """Every entry by path: one pass over the shared rows, for a caller visiting them all."""

        return {row.entry.path: row.entry for block in self._blocks for row in block.rows}

    @property
    def latest(self) -> int:
        """The largest ``changed_at`` of any file, 0 for an empty floor."""

        return max((block.latest for block in self._blocks), default=0)

    @property
    def canonical(self) -> bool:
        """Whether every path is NFC, so the rendered manifest reads back as this inventory."""

        return all(block.canonical for block in self._blocks)

    @property
    def floor_digest(self) -> str:
        """``floor_inventory_digest`` of the entries, from the bytes kept per block."""

        if self._digest is None:
            hasher = sha256(_DIGEST_HEAD)
            first = True
            for block in self._blocks:
                if not first:
                    hasher.update(b",")
                hasher.update(block.compact)
                first = False
            hasher.update(_DIGEST_TAIL)
            self._digest = "sha256:" + hasher.hexdigest()
        return self._digest

    def manifest_rows(self) -> bytes:
        """The entries as ``render_floor_manifest`` writes them inside ``files``."""

        return b",\n".join(block.pretty for block in self._blocks)

    # -- patching ------------------------------------------------------------------

    def patch(
        self,
        files: Mapping[str, tuple[str, int, int]],
        removed: Iterable[str] = (),
    ) -> FloorInventory:
        """This inventory without ``removed`` and with ``files`` written over it.

        ``files`` is ``path -> (content_digest, byte_length, changed_at)``;
        a removed path not held is ignored, and a path in both is written.
        Only the paths written or removed are validated, keyed or encoded:
        the work is the patch's, never the floor's. Raises ``ValueError``
        exactly when ``check_floor_paths`` would refuse the patched floor.
        """

        changes: dict[str, _Row | None] = {
            path: None for path in removed if path not in files and self.get(path) is not None
        }
        for path, (digest, size, changed) in files.items():
            path.encode("utf-8")  # a lone surrogate has no byte order
            entry = FloorEntry(
                path=path, content_digest=digest, byte_length=size, changed_at=changed
            )
            held = self.get(path)
            if held != entry:
                changes[path] = _row(entry)
        if not changes:
            return self
        keys, directories = self._keys, self._directories
        added = [
            path for path, row in changes.items() if row is not None and self.get(path) is None
        ]
        for path, row in changes.items():
            if row is None:
                keys = keys.delete(floor_path_key(path))
                for key in _directory_keys(path):
                    count = directories[key] - 1
                    directories = (
                        directories.delete(key) if count == 0 else directories.set(key, count)
                    )
        for path in added:
            key = floor_path_key(path)
            other = keys.get(key)
            if other is not None:
                raise ValueError(f"{_LABEL} names one file twice: {other!r} and {path!r}")
            keys = keys.set(key, path)
            for directory in _directory_keys(path):
                directories = directories.set(directory, directories.get(directory, 0) + 1)
        # Every rule the whole floor obeys, read off the indexes for the new paths:
        # an old path cannot clash with another old one.
        for path in added:
            key = floor_path_key(path)
            if key in directories or key in _RESERVED_DIRECTORIES:
                raise ValueError(f"{_LABEL} names {path!r} as a file and as a directory")
            for directory in _directory_keys(path):
                if directory in _RESERVED:
                    raise ValueError(f"{_LABEL} names a path under the reserved file {directory!r}")
                if directory in keys:
                    raise ValueError(
                        f"{_LABEL} names {keys[directory]!r} as a file and as a directory"
                    )
        return FloorInventory(self._patched_blocks(changes), keys, directories)

    def _patched_blocks(self, changes: Mapping[str, _Row | None]) -> list[_Block]:
        """The blocks with ``changes`` applied: untouched blocks are shared as they are."""

        if not self._blocks:
            return _blocks([row for _path, row in sorted(changes.items()) if row is not None])
        by_block: dict[int, list[str]] = {}
        for path in changes:
            by_block.setdefault(max(bisect_right(self._lows, path) - 1, 0), []).append(path)
        result: list[_Block] = []
        # Rows of an underfull rebuilt block, waiting to join the next block.
        pending: list[_Row] = []

        def place(rows: list[_Row]) -> None:
            nonlocal pending
            if pending:
                rows = [*pending, *rows]
                pending = []
            if len(rows) < _BLOCK // 4:
                pending = rows
                return
            if len(rows) > 2 * _BLOCK:
                result.extend(_blocks(rows))
            else:
                result.append(_block(rows))

        for index, block in enumerate(self._blocks):
            paths = by_block.get(index)
            if paths is None:
                if pending:
                    place(list(block.rows))
                else:
                    result.append(block)
                continue
            merged = dict(zip(block.paths, block.rows, strict=True))
            for path in paths:
                row = changes[path]
                if row is None:
                    merged.pop(path, None)
                else:
                    merged[path] = row
            place([merged[path] for path in sorted(merged)])
        if pending:
            if result:
                last = result.pop()
                rows = [*last.rows, *pending]
                result.extend(_blocks(rows) if len(rows) > 2 * _BLOCK else [_block(rows)])
            else:
                result.append(_block(pending))
        return result


@dataclass(frozen=True, eq=False)
class FloorManifestState:
    """One floor: its inventory and header, and each form of its manifest, computed once."""

    # The manifest's header: every field but ``files`` (empty here), with the
    # inventory's ``floor_digest``.
    header: FloorManifest
    inventory: FloorInventory

    @cached_property
    def manifest(self) -> FloorManifest:
        """The ``FloorManifest`` (its entries shared with the inventory, never rebuilt)."""

        return self.header.model_copy(update={"files": self.inventory.entries()})

    @cached_property
    def content(self) -> bytes:
        """``render_floor_manifest(self.manifest)``, from the rows kept per block."""

        header = render_floor_manifest(self.header)
        if not len(self.inventory):
            return header
        if header.count(_FILES_MARKER) != 1:  # pragma: no cover - the format names files once
            raise ValueError("a floor manifest header names its files once")
        return header.replace(
            _FILES_MARKER, b'"files": [\n' + self.inventory.manifest_rows() + b"\n  ]"
        )

    @cached_property
    def digest(self) -> str:
        """``floor_manifest_digest(self.manifest)``: the digest a delta names this floor by."""

        digest = content_digest(self.content)
        _remember(digest, self)
        return digest

    @property
    def generation(self) -> int:
        return self.header.generation

    @property
    def renderer(self) -> str:
        return self.header.renderer

    @property
    def floor_digest(self) -> str:
        return self.header.floor_digest


def floor_manifest_state(
    inventory: FloorInventory,
    *,
    renderer: str,
    coordinate: BaseModel,
    generation: int,
    notes_digest: str,
) -> FloorManifestState:
    """``build_floor_manifest`` over ``inventory``: the header validated, no entry rebuilt."""

    header = FloorManifest(
        renderer=renderer,
        coordinate=AcceptedCoordinate.model_validate(coordinate.model_dump(mode="json")),
        generation=generation,
        notes_digest=notes_digest,
        files=(),
        floor_digest=_EMPTY_FLOOR_DIGEST,
    )
    if inventory.latest > generation:
        raise ValueError("a floor file cannot change after the floor's generation")
    return FloorManifestState(
        header.model_copy(update={"floor_digest": inventory.floor_digest}), inventory
    )


def floor_manifest_state_of(manifest: FloorManifest) -> FloorManifestState:
    """The state of a validated manifest, adopting its entries as they are."""

    state = FloorManifestState(
        manifest.model_copy(update={"files": ()}), FloorInventory.from_entries(manifest.files)
    )
    state.__dict__["manifest"] = manifest
    return state


# -- the floors last named --------------------------------------------------------

_KNOWN: OrderedDict[str, FloorManifestState] = OrderedDict()
_KNOWN_CAPACITY = 8
_KNOWN_LOCK = threading.Lock()


def _remember(digest: str, state: FloorManifestState) -> None:
    # Only a floor whose rendered manifest reads back as itself: a non-NFC path
    # renders NFC, and its bytes then parse to another inventory.
    if not state.inventory.canonical:
        return
    with _KNOWN_LOCK:
        _KNOWN[digest] = state
        _KNOWN.move_to_end(digest)
        while len(_KNOWN) > _KNOWN_CAPACITY:
            _KNOWN.popitem(last=False)


def known_floor_manifest(content: bytes) -> FloorManifestState | None:
    """The floor whose ``manifest.json`` is exactly ``content``, if one was named lately.

    Those bytes parse to that floor's manifest (it is their rendering, and
    every path is NFC), so the floor stands for them already validated.
    """

    digest = content_digest(content)
    with _KNOWN_LOCK:
        found = _KNOWN.get(digest)
        if found is not None:
            _KNOWN.move_to_end(digest)
    if found is None or found.content != content:
        return None
    return found


__all__ = [
    "FloorInventory",
    "FloorManifestState",
    "floor_manifest_state",
    "floor_manifest_state_of",
    "known_floor_manifest",
]
