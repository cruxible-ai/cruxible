"""Instance-owned immutable source snapshots and isolated prospective edits.

These are acceleration handles, never verification capabilities. Only the
instance's verified-history adapter installs accepted roots. Candidate roots
cannot enter that namespace. Canonical bytes are the retained row boundary.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, MutableMapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from cruxible_client._persistent import PersistentMap
from cruxible_client.contracts.canonical import normalize_ledger_path
from cruxible_client.contracts.claims import ClaimArtifactAny, ClaimStatement, parse_claim
from cruxible_client.contracts.errors import PlaybillError
from cruxible_core.derived.derived_runtime import BoundedCache, Lease, Registry


class SnapshotTree(Mapping[str, bytes]):
    """Immutable physical tree. Lazy derived values cannot alias caller models."""

    def __init__(
        self,
        rows: Mapping[str, bytes],
        *,
        parent: SnapshotTree | None = None,
        edits: PersistentMap[bytes | None] | None = None,
        input_bytes: int | None = None,
        semantic_bytes: int | None = None,
        semantic_members: int | None = None,
    ) -> None:
        self._rows = PersistentMap(rows)
        self._input_bytes = (
            sum(len(p.encode("utf-8")) + len(b) for p, b in rows.items())
            if input_bytes is None
            else input_bytes
        )
        self._semantic_bytes = (
            sum(len(p.encode("utf-8")) + len(b) for p, b in rows.items() if semantic_path(p))
            if semantic_bytes is None
            else semantic_bytes
        )
        self._semantic_members = (
            sum(1 for p in rows if semantic_path(p))
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
        return self._rows[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

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
        self._semantic_bytes = parent._semantic_bytes
        self._semantic_members = parent._semantic_members
        self._edits = parent._edits if parent._parent is not None else PersistentMap()
        self._cached: SnapshotTree | None = parent

    def __getitem__(self, key: str) -> bytes:
        return self._rows[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def __setitem__(self, key: str, value: bytes) -> None:
        if normalize_ledger_path(key) != key or not isinstance(value, bytes):
            raise ValueError("candidate edits require canonical paths and immutable bytes")
        previous = self._rows.get(key)
        weight_delta = (
            len(value) - len(previous)
            if previous is not None
            else len(key.encode("utf-8")) + len(value)
        )
        self._input_bytes += weight_delta
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
        weight = len(key.encode("utf-8")) + len(self._rows[key])
        self._input_bytes -= weight
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

    def __init__(self, *, max_roots: int = 4, max_input_bytes: int = 32 * 1024 * 1024) -> None:
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
            weight = tree._input_bytes
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


def advance_accepted_tree(parent: SnapshotTree, edits: Mapping[str, bytes | None]) -> SnapshotTree:
    """Carry immutable data through a verified physical delta; no ancestry chain."""
    rows = parent._rows
    weight = parent._input_bytes
    semantic_bytes = parent._semantic_bytes
    semantic_members = parent._semantic_members
    for path, content in edits.items():
        previous = rows.get(path)
        if previous is not None:
            previous_weight = len(path.encode("utf-8")) + len(previous)
            weight -= previous_weight
            if semantic_path(path):
                semantic_bytes -= previous_weight
                semantic_members -= 1
        if content is not None:
            content_weight = len(path.encode("utf-8")) + len(content)
            weight += content_weight
            if semantic_path(path):
                semantic_bytes += content_weight
                semantic_members += 1
        rows = rows.delete(path) if content is None else rows.set(path, content)
    result = SnapshotTree(
        rows, input_bytes=weight, semantic_bytes=semantic_bytes, semantic_members=semantic_members
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
