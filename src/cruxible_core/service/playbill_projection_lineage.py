"""Rebuildable, bounded Claim lineage reads over verified accepted history.

No verdict, source observation, or operational journal is cached here. Entries
contain only immutable accepted Claim artifacts. Like the instance's tree memo,
reuse is conditional on the exact replay-verified generation coordinates. A
request fixes its history prefix before loading; concurrent acceptance cannot
silently add a successor halfway through that request.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from weakref import WeakKeyDictionary

from cruxible_client.contracts.claims import ClaimArtifactAny, claim_artifact_digest, parse_claim
from cruxible_client.contracts.errors import PlaybillError, PlaybillFormatError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.playbill.instance import PlaybillInstance


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


# These bounds describe retained source-byte weight and node/path counts, not
# Python heap bytes. Oversize requests still work, but do not retain their index.
_MAX_PATHS = 512
_MAX_NODES = 4096
_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_LOCK = RLock()
_CACHE: WeakKeyDictionary[PlaybillInstance, OrderedDict[str, _Entry]] = WeakKeyDictionary()


def clear_lineage_index(instance: PlaybillInstance) -> None:
    """Discard derived state; the next read reconstructs it from the ledger."""
    with _LOCK:
        _CACHE.pop(instance, None)


def read_claim_lineages(
    instance: PlaybillInstance,
    *,
    paths: tuple[str, ...],
    at: AcceptedCoordinate,
    defer_errors: bool = False,
) -> dict[str, dict[str, ClaimLineageNode] | PlaybillError]:
    """Batch cold history reads, then extend cached paths only over new generations."""
    coordinate = instance.resolve_accepted_coordinate(
        git_oid=at.git_oid,
        semantic_root=at.semantic_root,
        generation_root=at.generation_root,
        compiler_digest=at.compiler_digest,
    )
    recovered = instance.accepted_history()
    matches = [i for i, generation in enumerate(recovered) if generation.oid == at.git_oid]
    if len(matches) != 1:
        raise PlaybillFormatError("lineage coordinate is outside accepted history")
    generations = recovered[: matches[0] + 1]
    history = tuple(
        (g.oid, g.semantic_root.tagged, g.generation_root.tagged, g.sequence) for g in generations
    )
    compiler = coordinate.compiler.rule_digest
    unique_paths = tuple(dict.fromkeys(paths))
    with _LOCK:
        cache = _CACHE.get(instance, OrderedDict())
        existing = {path: cache.get(path) for path in unique_paths}
    failures: dict[str, PlaybillError] = {}
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
    # One bounded multi-blob read per generation, rather than one subprocess
    # traversal per (backing, generation). An untouched Claim is parsed once.
    for i in range(min(starts.values(), default=len(generations)), len(generations)):
        generation = generations[i]
        wanted = tuple(path for path in unique_paths if starts[path] <= i and path not in failures)
        if not wanted:
            continue
        try:
            raws = instance.blobs_at(generation.oid, wanted)
        except PlaybillError:
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
                except PlaybillError as exc:
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
            except PlaybillError as exc:
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
                if entry.weight > _MAX_SOURCE_BYTES or len(entry.nodes) > _MAX_NODES:
                    cache.pop(path, None)
                    continue
                cache[path] = entry
                cache.move_to_end(path)
            while cache and (
                len(cache) > _MAX_PATHS
                or sum(e.weight for e in cache.values()) > _MAX_SOURCE_BYTES
                or sum(len(e.nodes) for e in cache.values()) > _MAX_NODES
            ):
                cache.popitem(last=False)
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
