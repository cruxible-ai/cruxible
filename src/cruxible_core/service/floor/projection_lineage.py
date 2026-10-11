"""Rebuildable, bounded Claim lineage reads over verified accepted history.

No verdict, source observation, or operational journal is cached here. Entries
contain only immutable accepted Claim artifacts. Like the instance's tree memo,
reuse is conditional on the exact replay-verified generation coordinates. A
request fixes its history prefix before loading; concurrent acceptance cannot
silently add a successor halfway through that request.

A path's bytes change only in a generation whose verified change record names
it (replay reproduces each record's scope from the trees themselves), so a
lineage reads its path only at those generations, found through the history
index: the work is the path's revisions, not generations times paths.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from weakref import WeakKeyDictionary

from cruxible_client.contracts.claims import ClaimArtifactAny, claim_artifact_digest, parse_claim
from cruxible_client.contracts.errors import CruxibleError, FormatError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.runtime.instance import PlaybillInstance


@dataclass(frozen=True)
class ClaimLineageNode:
    claim: ClaimArtifactAny
    artifact_digest: str
    coordinate: AcceptedCoordinate
    generation: int


@dataclass(frozen=True)
class _Entry:
    prefix_length: int
    tip: tuple[str, str, str, int] | None
    nodes: dict[str, ClaimLineageNode]
    last_raw: bytes | None
    weight: int
    compiler: str


# Retained source-byte weight, not Python heap bytes, bounds the index; it has
# no path or node count cap, so a page's few thousand backings stay resident.
# An oversize request still works, but does not retain its index.
_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_LOCK = RLock()
_CACHE: WeakKeyDictionary[PlaybillInstance, OrderedDict[str, _Entry]] = WeakKeyDictionary()


def clear_lineage_index(instance: PlaybillInstance) -> None:
    """Discard derived state; the next read reconstructs it from the ledger."""
    with _LOCK:
        _CACHE.pop(instance, None)


def path_revisions(
    instance: PlaybillInstance, *, paths: tuple[str, ...], at: AcceptedCoordinate
) -> dict[str, tuple[tuple[int, str | None], ...]]:
    """Every generation, to ``at``, whose accepted bytes at each path may differ.

    One (sequence, member digest) per change-record occurrence, plus genesis as
    ``(0, None)`` where generation zero holds the path. Between two of these a
    path's bytes are the same accepted object.
    """
    with instance.accepted_history_reader(at=at) as history:
        found = history.member_versions(paths)
        wanted = set(paths)
        genesis = {row.path for row in history.versions_at(0) if row.path in wanted}
    return {path: (((0, None),) if path in genesis else ()) + found[path] for path in found}


def read_claim_lineages(
    instance: PlaybillInstance,
    *,
    paths: tuple[str, ...],
    at: AcceptedCoordinate,
    defer_errors: bool = False,
) -> dict[str, dict[str, ClaimLineageNode] | CruxibleError]:
    """Read each path at its own revisions only, extending cached paths over new ones."""
    coordinate = instance.resolve_accepted_coordinate(
        git_oid=at.git_oid,
        semantic_root=at.semantic_root,
        generation_root=at.generation_root,
        compiler_digest=at.compiler_digest,
    )
    recovered = instance.accepted_history()
    matches = [i for i, generation in enumerate(recovered) if generation.oid == at.git_oid]
    if len(matches) != 1:
        raise FormatError("lineage coordinate is outside accepted history")
    generations = recovered[: matches[0] + 1]
    if any(generation.sequence != i for i, generation in enumerate(generations)):
        raise FormatError("lineage history is not one contiguous accepted prefix")
    history = tuple(
        (g.oid, g.semantic_root.tagged, g.generation_root.tagged, g.sequence) for g in generations
    )
    compiler = coordinate.compiler.rule_digest
    unique_paths = tuple(dict.fromkeys(paths))
    with _LOCK:
        cache = _CACHE.get(instance, OrderedDict())
        existing = {path: cache.get(path) for path in unique_paths}
    failures: dict[str, CruxibleError] = {}
    # A verified generation root commits its entire ancestor chain. Matching
    # this retained tip inside the verified prefix proves prefix compatibility
    # without retaining/comparing one history tuple for every backing.
    entries: dict[str, _Entry] = {}
    starts: dict[str, int] = {}
    for path in unique_paths:
        old = existing[path]
        if (
            old is not None
            and old.compiler == compiler
            and old.prefix_length <= len(history)
            and (old.prefix_length == 0 or history[old.prefix_length - 1] == old.tip)
        ):
            entries[path] = _Entry(
                len(history),
                history[-1] if history else None,
                dict(old.nodes),
                old.last_raw,
                old.weight,
                compiler,
            )
            starts[path] = old.prefix_length
        else:
            entries[path] = _Entry(
                len(history), history[-1] if history else None, {}, None, 0, compiler
            )
            starts[path] = 0
    # Only the generations whose change records name a path can change its
    # bytes, so each path is read at those alone: one bounded multi-blob read
    # per such generation, never one per (backing, generation).
    pending = tuple(path for path in unique_paths if starts[path] < len(history))
    revisions = path_revisions(instance, paths=pending, at=at) if pending else {}
    by_generation: dict[int, list[str]] = {}
    for path in pending:
        for sequence in dict.fromkeys(sequence for sequence, _digest in revisions[path]):
            if sequence >= starts[path]:
                by_generation.setdefault(sequence, []).append(path)
    for i in sorted(by_generation):
        generation = generations[i]
        wanted = tuple(path for path in by_generation[i] if path not in failures)
        if not wanted:
            continue
        try:
            raws = instance.blobs_at(generation.oid, wanted)
        except CruxibleError:
            if not defer_errors:
                raise
            # A later unreadable blob must not preempt an earlier marker's
            # typed refusal. Only on failure, recover per-path attribution
            # using the original single-blob read semantics.
            raws = {}
            for path in wanted:
                try:
                    raw = instance.blob_at(generation.oid, path)
                    if raw is not None:
                        raws[path] = raw
                except CruxibleError as exc:
                    failures[path] = exc
        generation_coordinate = AcceptedCoordinate.from_internal(
            instance.coordinate_for_oid(generation.oid)
        )
        for path in wanted:
            if path in failures:
                continue
            entry = entries[path]
            raw = raws.get(path)
            if raw is None or raw == entry.last_raw:
                continue
            try:
                claim = parse_claim(raw, path=path)
            except CruxibleError as exc:
                if not defer_errors:
                    raise
                failures[path] = exc
                continue
            digest = claim_artifact_digest(claim).tagged
            weight = entry.weight
            if digest not in entry.nodes:
                entry.nodes[digest] = ClaimLineageNode(
                    claim, digest, generation_coordinate, generation.sequence
                )
                weight += len(raw)
            entries[path] = _Entry(
                len(history), history[-1] if history else None, entry.nodes, raw, weight, compiler
            )
    if not failures:
        with _LOCK:
            cache = _CACHE.setdefault(instance, OrderedDict())
            for path, entry in entries.items():
                cache.pop(path, None)
                if entry.weight <= _MAX_SOURCE_BYTES:
                    cache[path] = entry
            retained = sum(e.weight for e in cache.values())
            while cache and retained > _MAX_SOURCE_BYTES:
                retained -= cache.popitem(last=False)[1].weight
    # Do not expose cache-owned mutable Pydantic models to callers.
    return {
        path: failures[path]
        if path in failures
        else {
            digest: ClaimLineageNode(
                node.claim.model_copy(deep=True),
                node.artifact_digest,
                node.coordinate.model_copy(deep=True),
                node.generation,
            )
            for digest, node in entry.nodes.items()
        }
        for path, entry in entries.items()
    }
