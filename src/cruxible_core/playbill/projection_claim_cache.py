"""Disposable in-process compilation snapshots for exact, validated Claim bytes.

This cache is not a parser or an authority boundary. Callers validate each miss
with the compiler selected by the key before storing its immutable result. No
serialized cache input is accepted and no parsed Pydantic model is retained.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from dataclasses import dataclass

from cruxible_client.contracts.canonical import ArtifactCodec, canonical_bytes
from cruxible_client.contracts.projection_extensions import ProjectionFact


@dataclass(frozen=True, slots=True)
class FrozenClaimFact:
    """Private snapshot of a fact already validated by the running compiler."""

    schema_id: str
    schema_version: int
    subject_identity: str
    fact_key: str
    value_json: bytes

    @classmethod
    def from_fact(cls, fact: ProjectionFact) -> FrozenClaimFact:
        return cls(
            schema_id=fact.schema_id,
            schema_version=fact.schema_version,
            subject_identity=fact.subject_identity,
            fact_key=fact.fact_key,
            value_json=canonical_bytes(fact.value),
        )

    def materialize(self) -> ProjectionFact:
        # Only our private, already-validated in-process snapshots reach here.
        # This deliberately skips repeated normalization, not validation of any
        # persisted/wire input. JSON decoding detaches every nested mutable value.
        return ProjectionFact.model_construct(
            schema_id=self.schema_id,
            schema_version=self.schema_version,
            subject_identity=self.subject_identity,
            fact_key=self.fact_key,
            value=json.loads(self.value_json),
        )


@dataclass(frozen=True, slots=True)
class CachedClaim:
    """Immutable per-artifact result; cross-artifact checks remain with callers."""

    identity: str
    format_tag: str
    input_digest: str
    artifact_digest: str
    statement_digest: str
    predecessor_digest: str | None
    retired: bool
    pins: tuple[tuple[str, str], ...]
    facts: tuple[FrozenClaimFact, ...]

    def materialize_facts(self) -> tuple[ProjectionFact, ...]:
        return tuple(fact.materialize() for fact in self.facts)


_Key = tuple[str, ArtifactCodec, str, bytes]


def _text_bytes(value: str) -> int:
    # Include escaping and string delimiters, not just unescaped character bytes.
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _weight(key: _Key, entry: CachedClaim) -> int:
    # Conservative serialized-payload accounting, including field names/framing.
    # It is deliberately not an estimate or guarantee of Python process RSS.
    weight = len(key[3]) + 512
    weight += sum(_text_bytes(value) for value in (key[0], key[1].value, key[2]))
    weight += sum(
        _text_bytes(value)
        for value in (
            entry.identity,
            entry.format_tag,
            entry.input_digest,
            entry.artifact_digest,
            entry.statement_digest,
        )
    )
    if entry.predecessor_digest is not None:
        weight += _text_bytes(entry.predecessor_digest)
    for identity, digest in entry.pins:
        weight += _text_bytes(identity) + _text_bytes(digest) + 16
    for fact in entry.facts:
        weight += len(fact.value_json) + len(str(fact.schema_version)) + 128
        weight += sum(
            _text_bytes(value) for value in (fact.schema_id, fact.subject_identity, fact.fact_key)
        )
    return weight


class ClaimCompilationCache:
    """Thread-safe LRU bounded by entries and accounted encoded payload bytes.

    Compilation, freezing and materialization happen outside this lock. Oversize
    entries and zero limits disable retention without refusing valid compilation.
    Keys retain full input bytes: a digest collision cannot authorize a hit.
    """

    def __init__(self, max_entries: int = 4096, max_bytes: int = 32 * 1024 * 1024):
        if max_entries < 0 or max_bytes < 0:
            raise ValueError("claim cache limits must be nonnegative")
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._entries: OrderedDict[_Key, tuple[CachedClaim, int]] = OrderedDict()
        self._retained_bytes = 0
        self._lock = threading.Lock()

    def get(
        self, *, compiler_digest: str, codec: ArtifactCodec, path: str, content: bytes
    ) -> CachedClaim | None:
        key = (compiler_digest, codec, path, content)
        with self._lock:
            found = self._entries.get(key)
            if found is None:
                return None
            self._entries.move_to_end(key)
            return found[0]

    def put(
        self,
        *,
        compiler_digest: str,
        codec: ArtifactCodec,
        path: str,
        content: bytes,
        entry: CachedClaim,
    ) -> None:
        key = (compiler_digest, codec, path, content)
        weight = _weight(key, entry)
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._retained_bytes -= previous[1]
            if not self._max_entries or weight > self._max_bytes:
                return
            self._entries[key] = (entry, weight)
            self._retained_bytes += weight
            while len(self._entries) > self._max_entries or self._retained_bytes > self._max_bytes:
                _, (_, evicted_weight) = self._entries.popitem(last=False)
                self._retained_bytes -= evicted_weight

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._retained_bytes = 0

    @property
    def entry_count(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def retained_bytes(self) -> int:
        with self._lock:
            return self._retained_bytes
