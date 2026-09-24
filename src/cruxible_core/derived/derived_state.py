"""Instance-owned immutable source snapshots and isolated prospective edits.

These are acceleration handles, never verification capabilities. Only the
instance's verified-history adapter installs accepted roots. Candidate roots
cannot enter that namespace. Canonical bytes are the retained row boundary.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, MutableMapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from cruxible_client.contracts.canonical import normalize_ledger_path
from cruxible_client.contracts.claims import ClaimArtifactAny, ClaimStatement, parse_claim
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.persistent import PersistentMap
from cruxible_core.derived.derived_runtime import BoundedCache, Lease, Registry

BlobLoader = Callable[[Sequence[str]], Mapping[str, bytes]]


@dataclass(frozen=True, slots=True)
class BlobRef:
    """A committed blob named by object ID; its bytes stay in Git's object store."""

    oid: str
    size: int
    load: BlobLoader

    def __deepcopy__(self, memo: dict[int, Any]) -> BlobRef:
        return self  # immutable; never copy the loader's repository handle


# Recently read blob bytes, shared by every tree: object IDs are content
# addresses, so one entry serves every root that carries that blob.
_BLOB_CACHE_MAX_BYTES = 16 * 1024 * 1024
_BLOB_CACHE: OrderedDict[str, bytes] = OrderedDict()
_BLOB_CACHE_BYTES = 0
_BLOB_CACHE_LOCK = threading.Lock()
# Resident weight of one reference row: the path plus a small fixed overhead.
_REF_OVERHEAD = 160


def _size(value: bytes | BlobRef) -> int:
    return len(value) if isinstance(value, bytes) else value.size


def _resident(path: str, value: bytes | BlobRef) -> int:
    return len(path.encode("utf-8")) + (len(value) if isinstance(value, bytes) else _REF_OVERHEAD)


def _remember_blob(oid: str, content: bytes) -> None:
    global _BLOB_CACHE_BYTES
    if len(content) > _BLOB_CACHE_MAX_BYTES // 4:
        return
    with _BLOB_CACHE_LOCK:
        if oid in _BLOB_CACHE:
            _BLOB_CACHE.move_to_end(oid)
            return
        _BLOB_CACHE[oid] = content
        _BLOB_CACHE_BYTES += len(content)
        while _BLOB_CACHE_BYTES > _BLOB_CACHE_MAX_BYTES:
            _old, evicted = _BLOB_CACHE.popitem(last=False)
            _BLOB_CACHE_BYTES -= len(evicted)


def resolve_blobs(values: Sequence[bytes | BlobRef]) -> list[bytes]:
    """Each value's bytes, reading every uncached reference in one batch per loader."""

    found: dict[str, bytes] = {}
    missing: dict[int, dict[str, BlobRef]] = {}
    loaders: dict[int, BlobLoader] = {}
    with _BLOB_CACHE_LOCK:
        for value in values:
            if isinstance(value, bytes) or value.oid in found:
                continue
            cached = _BLOB_CACHE.get(value.oid)
            if cached is not None:
                _BLOB_CACHE.move_to_end(value.oid)
                found[value.oid] = cached
            else:
                missing.setdefault(id(value.load), {})[value.oid] = value
                loaders[id(value.load)] = value.load
    for key, refs in missing.items():
        loaded = loaders[key](tuple(refs))
        for oid, ref in refs.items():
            content = loaded[oid]
            if len(content) != ref.size:
                raise PlaybillError(f"ledger blob size changed while reading: {oid}")
            found[oid] = content
            _remember_blob(oid, content)
    return [value if isinstance(value, bytes) else found[value.oid] for value in values]


def row_values(tree: Mapping[str, bytes] | None) -> Mapping[str, bytes | BlobRef] | None:
    """A snapshot's raw rows (bytes or references), or None for any other mapping."""
    if isinstance(tree, (SnapshotTree, CandidateTree)):
        return tree._rows
    return None


def row_size(value: bytes | BlobRef) -> int:
    return _size(value)


def same_row(left: bytes | BlobRef, right: bytes | BlobRef) -> bool:
    """Whether two rows hold the same bytes, reading content only when forms differ."""
    if left is right:
        return True
    if isinstance(left, BlobRef) and isinstance(right, BlobRef):
        return left.oid == right.oid
    if isinstance(left, bytes) and isinstance(right, bytes):
        return left == right
    if _size(left) != _size(right):
        return False
    return _resolve(left) == _resolve(right)


def changed_paths(base: Mapping[str, bytes], candidate: Mapping[str, bytes]) -> tuple[str, ...]:
    """Paths whose bytes differ between two trees, reading content only where needed.

    Snapshot rows are compared by reference (the same object, or the same blob
    ID); a plain mapping on either side falls back to comparing bytes.
    """
    left, right = row_values(base), row_values(candidate)
    if left is None or right is None:
        return tuple(
            path for path in base.keys() | candidate.keys() if base.get(path) != candidate.get(path)
        )
    return tuple(
        path
        for path in left.keys() | right.keys()
        if path not in left or path not in right or not same_row(left[path], right[path])
    )


def _snapshot_equal(tree: Mapping[str, bytes], other: object) -> bool:
    if not isinstance(other, Mapping):
        return False
    mine, theirs = row_values(tree), row_values(other)
    if mine is None or theirs is None:
        return dict(tree.items()) == dict(other.items())
    if len(mine) != len(theirs):
        return False
    return all(path in theirs and same_row(value, theirs[path]) for path, value in mine.items())


def _resolve(value: bytes | BlobRef) -> bytes:
    return value if isinstance(value, bytes) else resolve_blobs((value,))[0]


class SnapshotTree(Mapping[str, bytes]):
    """Immutable physical tree. Lazy derived values cannot alias caller models."""

    def __init__(
        self,
        rows: Mapping[str, bytes | BlobRef],
        *,
        parent: SnapshotTree | None = None,
        edits: PersistentMap[bytes | None] | None = None,
        input_bytes: int | None = None,
        semantic_bytes: int | None = None,
        semantic_members: int | None = None,
        resident_bytes: int | None = None,
    ) -> None:
        # Rows hold bytes or a BlobRef whose bytes are read from Git on demand.
        # Sizes below are logical file bytes; ``_resident_bytes`` is what the
        # rows themselves hold in memory, which is what a cache budget weighs.
        raw_rows: Mapping[str, bytes | BlobRef] = (
            rows._rows if isinstance(rows, SnapshotTree) else rows
        )
        self._rows: PersistentMap[bytes | BlobRef] = PersistentMap(raw_rows)
        self._input_bytes = (
            sum(len(p.encode("utf-8")) + _size(b) for p, b in raw_rows.items())
            if input_bytes is None
            else input_bytes
        )
        self._resident_bytes = (
            sum(_resident(p, b) for p, b in raw_rows.items())
            if resident_bytes is None
            else resident_bytes
        )
        self._semantic_bytes = (
            sum(len(p.encode("utf-8")) + _size(b) for p, b in raw_rows.items() if semantic_path(p))
            if semantic_bytes is None
            else semantic_bytes
        )
        self._semantic_members = (
            sum(1 for p in raw_rows if semantic_path(p))
            if semantic_members is None
            else semantic_members
        )
        self._accepted = False
        self._parent = parent
        self._edits = edits if edits is not None else PersistentMap()
        self._lock = threading.RLock()
        self._accepted_reader: Callable[[], Any] | None = (
            None if parent is None else parent._accepted_reader
        )
        self._proofs: Any = None
        self._proof_seed: Any = None

    def __getitem__(self, key: str) -> bytes:
        return _resolve(self._rows[key])

    def __contains__(self, key: object) -> bool:
        return key in self._rows

    def __eq__(self, other: object) -> bool:
        return _snapshot_equal(self, other)

    __hash__ = None  # type: ignore[assignment]

    def __iter__(self) -> Iterator[str]:
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def items(self) -> Any:
        paths = tuple(self._rows)
        return list(zip(paths, resolve_blobs([self._rows[p] for p in paths]), strict=True))

    def values(self) -> Any:
        return resolve_blobs(list(self._rows.values()))

    def __deepcopy__(self, memo: dict[int, Any]) -> SnapshotTree:
        return self  # Only immutable bytes and private derived roots are retained.

    def snapshot(self) -> SnapshotTree:
        return self

    def fork(self) -> CandidateTree:
        return CandidateTree(self)

    def claim_items(self, statement: ClaimStatement) -> tuple[tuple[str, bytes], ...]:
        if self._accepted_reader is not None:
            from cruxible_core.indexes.evaluated_state import EvaluationRows

            base = EvaluationRows(self._accepted_reader())
            try:
                selected = base if self._parent is None else base.overlay(self._edits)
                return selected.claim_items(statement)
            finally:
                base.close()
        # Cold source-only ingress remains the exact full builder oracle.
        rows = []
        for path in self:
            if path.startswith("claims/"):
                claim = parse_claim(self[path], path=path)
                if (
                    claim.lifecycle.state == "live"
                    and claim.statement.subject == statement.subject
                    and claim.statement.predicate == statement.predicate
                ):
                    rows.append((path, self[path]))
        return tuple(sorted(rows))

    def claims_for(self, statement: ClaimStatement) -> tuple[ClaimArtifactAny, ...]:
        return tuple(
            parse_claim(content, path=path) for path, content in self.claim_items(statement)
        )

    def edits_from(self, parent: Mapping[str, bytes]) -> Mapping[str, bytes | None] | None:
        # Identity of an internal immutable root proves correspondence, not a
        # caller's coordinate string. External full-tree ingress has no shortcut.
        if self is parent:
            return PersistentMap()
        if isinstance(parent, SnapshotTree) and self._parent is parent:
            return self._edits
        return None


class CandidateTree(MutableMapping[str, bytes]):
    """Private mutable staging cursor over immutable roots; each read can seal a revision."""

    def __init__(self, parent: SnapshotTree) -> None:
        # Keep one base plus a bounded complete edit map, never an overlay chain.
        self._parent = parent._parent if parent._parent is not None else parent
        self._rows = parent._rows
        self._input_bytes = parent._input_bytes
        self._resident_bytes = parent._resident_bytes
        self._semantic_bytes = parent._semantic_bytes
        self._semantic_members = parent._semantic_members
        self._edits = parent._edits if parent._parent is not None else PersistentMap()
        self._cached: SnapshotTree | None = parent

    def __getitem__(self, key: str) -> bytes:
        return _resolve(self._rows[key])

    def __contains__(self, key: object) -> bool:
        return key in self._rows

    def __eq__(self, other: object) -> bool:
        return _snapshot_equal(self, other)

    __hash__ = None  # type: ignore[assignment]

    def __iter__(self) -> Iterator[str]:
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def __setitem__(self, key: str, value: bytes) -> None:
        if normalize_ledger_path(key) != key or not isinstance(value, bytes):
            raise ValueError("candidate edits require canonical paths and immutable bytes")
        previous = self._rows.get(key)
        weight_delta = (
            len(value) - _size(previous)
            if previous is not None
            else len(key.encode("utf-8")) + len(value)
        )
        self._input_bytes += weight_delta
        self._resident_bytes += _resident(key, value) - (
            _resident(key, previous) if previous is not None else 0
        )
        if semantic_path(key):
            self._semantic_bytes += weight_delta
            self._semantic_members += int(previous is None)
        self._rows = self._rows.set(key, value)
        self._edits = (
            self._edits.delete(key)
            if self._parent.get(key) == value
            else self._edits.set(key, value)
        )
        self._cached = None

    def __delitem__(self, key: str) -> None:
        if key not in self._rows:
            raise KeyError(key)
        weight = len(key.encode("utf-8")) + _size(self._rows[key])
        self._input_bytes -= weight
        self._resident_bytes -= _resident(key, self._rows[key])
        if semantic_path(key):
            self._semantic_bytes -= weight
            self._semantic_members -= 1
        self._rows = self._rows.delete(key)
        self._edits = self._edits.set(key, None) if key in self._parent else self._edits.delete(key)
        self._cached = None

    def snapshot(self) -> SnapshotTree:
        if self._cached is None:
            self._cached = SnapshotTree(
                self._rows,
                parent=self._parent,
                edits=self._edits,
                input_bytes=self._input_bytes,
                semantic_bytes=self._semantic_bytes,
                semantic_members=self._semantic_members,
                resident_bytes=self._resident_bytes,
            )
        return self._cached

    def fork(self) -> CandidateTree:
        return self.snapshot().fork()

    def claim_items(self, statement: ClaimStatement) -> tuple[tuple[str, bytes], ...]:
        return self.snapshot().claim_items(statement)

    def claims_for(self, statement: ClaimStatement) -> tuple[ClaimArtifactAny, ...]:
        return self.snapshot().claims_for(statement)


def fork_tree(tree: Mapping[str, bytes]) -> CandidateTree:
    if isinstance(tree, (SnapshotTree, CandidateTree)):
        return tree.fork()
    return SnapshotTree(tree).fork()


@dataclass(frozen=True)
class IndexDefinition:
    name: str
    namespace: str
    version: str
    source_contract: str


class DerivedState:
    """One local lifecycle owner. Root loading is single-flight outside its lock.

    Legacy adapters retain their existing fresh-source and budget rules during
    migration. Snapshot budgets are conservative input-byte upper bounds, not RSS.
    Leased roots remain readable after registry eviction; no source is deleted.
    """

    # Stopgap: an accepted tree over budget is never cached, so every write
    # would re-read the whole tree; 256 MiB keeps realistic instances cached
    # until the tree cache holds structure instead of bytes.
    def __init__(self, *, max_roots: int = 4, max_input_bytes: int = 256 * 1024 * 1024) -> None:
        if max_roots < 0 or max_input_bytes < 0:
            raise ValueError("derived-state budgets must be nonnegative")
        self._max_roots = max_roots
        self._max_input_bytes = max_input_bytes
        self._lock = threading.RLock()
        self._roots: OrderedDict[bytes, tuple[SnapshotTree, int]] = OrderedDict()
        self._runtime = Registry()
        self._epoch = 0
        self._adapters: dict[str, tuple[IndexDefinition, object]] = {}
        self._builds = 0
        self._hits = 0
        self._fallbacks = 0

    def register(self, definition: IndexDefinition, adapter: object) -> object:
        with self._lock:
            if definition.name in self._adapters:
                raise ValueError("derived index is already registered")
            self._adapters[definition.name] = (definition, adapter)
            clear = getattr(adapter, "clear", None) if adapter is not self else None
            self._runtime.register(
                definition.name, definition.namespace, definition.version, adapter, clear=clear
            )
        return adapter

    def accepted_tree(
        self,
        binding: bytes,
        load: Callable[[], Mapping[str, bytes]],
        advance: Callable[[bytes, SnapshotTree], SnapshotTree] | None = None,
    ) -> SnapshotTree:
        # The instance adapter validates the complete binding before this call.
        with self._lock:
            entry = self._roots.get(binding)
            if entry is not None:
                self._roots.move_to_end(binding)
                self._hits += 1
                return entry[0]
        with self._runtime.build(("accepted-tree", binding)):
            with self._lock:
                entry = self._roots.get(binding)
                if entry is not None:
                    self._hits += 1
                    return entry[0]
                epoch = self._epoch
                previous = next(reversed(self._roots.items()), None) if self._roots else None
            tree = (
                advance(previous[0], previous[1][0])
                if advance is not None and previous is not None
                else SnapshotTree(load())
            )
            weight = tree._resident_bytes
            tree._accepted = True
            with self._lock:
                self._builds += 1
                if epoch == self._epoch and self._max_roots and weight <= self._max_input_bytes:
                    self._roots[binding] = (tree, weight)
                    while (
                        len(self._roots) > self._max_roots
                        or sum(item[1] for item in self._roots.values()) > self._max_input_bytes
                    ):
                        self._roots.popitem(last=False)
                else:
                    self._fallbacks += 1
            return tree

    def build(self, key: object) -> AbstractContextManager[None]:
        return self._runtime.build(key)

    def memo(self, name: str, *, max_entries: int, max_bytes: int) -> BoundedCache[Any]:
        return self._runtime.memo(name, max_entries=max_entries, max_bytes=max_bytes)

    def lease(self, tree: SnapshotTree) -> Lease[SnapshotTree]:
        return self._runtime.lease(tree, estimated_bytes=tree._input_bytes)

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "definitions": tuple(d for d, _ in self._adapters.values()),
                "retained_roots": len(self._roots),
                "input_bytes_upper_bound": sum(w for _, w in self._roots.values()),
                "builds": self._builds,
                "hits": self._hits,
                "runtime": self._runtime.status(),
                "budget_fallbacks": self._fallbacks,
            }

    def clear(self) -> None:
        with self._lock:
            self._epoch += 1
            self._roots.clear()
        self._runtime.clear()


def advance_accepted_tree(
    parent: SnapshotTree, edits: Mapping[str, bytes | BlobRef | None]
) -> SnapshotTree:
    """Carry immutable data through a verified physical delta; no ancestry chain."""
    rows = parent._rows
    weight = parent._input_bytes
    resident = parent._resident_bytes
    semantic_bytes = parent._semantic_bytes
    semantic_members = parent._semantic_members
    for path, content in edits.items():
        previous = rows.get(path)
        if previous is not None:
            previous_weight = len(path.encode("utf-8")) + _size(previous)
            weight -= previous_weight
            resident -= _resident(path, previous)
            if semantic_path(path):
                semantic_bytes -= previous_weight
                semantic_members -= 1
        if content is not None:
            content_weight = len(path.encode("utf-8")) + _size(content)
            weight += content_weight
            resident += _resident(path, content)
            if semantic_path(path):
                semantic_bytes += content_weight
                semantic_members += 1
        rows = rows.delete(path) if content is None else rows.set(path, content)
    result = SnapshotTree(
        rows,
        input_bytes=weight,
        semantic_bytes=semantic_bytes,
        semantic_members=semantic_members,
        resident_bytes=resident,
    )
    with parent._lock:
        if parent._proofs is not None and parent._accepted_reader is not None:
            result._proof_seed = (parent._proofs, parent._accepted_reader, dict(edits))
    return result


def snapshot_against(tree: Mapping[str, bytes], parent: SnapshotTree) -> SnapshotTree:
    """Verify a full-tree ingress delta against its exact immutable parent.

    This compatibility boundary intentionally enumerates external full input.
    Once complete, downstream indexes need only the sealed changed paths.
    """
    if isinstance(tree, SnapshotTree) and tree.edits_from(parent) is not None:
        return tree
    builder = parent.fork()
    for path, content in tree.items():
        if parent.get(path) != content:
            try:
                builder[path] = content
            except (PlaybillError, ValueError):
                # External full trees preserve the original normalization and
                # refusal boundary; only internal canonical edits get a delta.
                return SnapshotTree(tree)
    for path in parent:
        if path not in tree:
            del builder[path]
    return builder.snapshot()


def semantic_path(path: str) -> bool:
    return not path.startswith(("changesets/", "cards/"))
