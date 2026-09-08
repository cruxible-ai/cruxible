"""Disposable citation owner/group state and exact semantic-row deltas.

Only the verified projection adapter supplies parents and complete member edits.
Roots contain immutable bytes/scalars and persistent maps, never caller-owned
Pydantic values. Raw conflicts remain indexed even when capture precedence hides
one from the served rows, so removal can restore a weaker conflict without a
world scan. No new ledger, fact schema, or SQLite format is introduced.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from cruxible_client._persistent import MapMutation, PersistentMap
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.cas_contracts import BodyProjectionProtocol
from cruxible_client.contracts.projection_extensions import ProjectionFact
from cruxible_client.contracts.source_references import ExternalSourceReferenceV1
from cruxible_core.playbill.citation_relations import (
    RELATION_CONTRACT_SCHEMA,
    RELATION_RETIRED_CONFLICT_SCHEMA,
    RELATION_USE_SCHEMA,
    _claim_uses,
    _conflict_facts,
    _contract_facts,
    _relation_group_key,
    _same_version_span_key,
    _use_facts,
    external_source_relation_subject,
)
from cruxible_core.playbill.derived_state import DerivedState, IndexDefinition
from cruxible_core.playbill.projection import AcceptedProjectionCoordinate, AssemblerRequest
from cruxible_core.playbill.projection_claim_cache import FrozenClaimFact

if TYPE_CHECKING:
    from cruxible_core.storage.playbill_projection import ProjectionHandle

FactKey = tuple[str, int, str, str]


def _key(fact: FrozenClaimFact) -> FactKey:
    return fact.schema_id, fact.schema_version, fact.subject_identity, fact.fact_key


def _use_key(use: Mapping[str, object]) -> str:
    # Matches cold builder's path order then claim_citation_references' ID order.
    return f"{use['claim_path']}\0{use['citation_id']}"


def _groups(use: Mapping[str, object]) -> tuple[str, ...]:
    capture = use["capture_digest"]
    assert isinstance(capture, dict)
    keys = [_relation_group_key("capture", str(capture["$digest"]))]
    source = use["source"]
    if isinstance(source, dict) and source.get("kind") == "external":
        external = ExternalSourceReferenceV1.model_validate(source)
        keys.append(
            _relation_group_key("exact_external", external_source_relation_subject(external))
        )
        span = _same_version_span_key(use)
        if span is not None:
            keys.append(_relation_group_key("same_version_span", span[0]))
    return tuple(keys)


def _freeze(facts: Iterable[ProjectionFact]) -> tuple[FrozenClaimFact, ...]:
    return tuple(FrozenClaimFact.from_fact(fact) for fact in facts)


def _fact_weight(facts: Iterable[FrozenClaimFact]) -> int:
    return sum(
        len(f.value_json)
        + sum(len(s.encode("utf-8")) for s in (f.schema_id, f.subject_identity, f.fact_key))
        + 256
        for f in facts
    )


@dataclass(frozen=True, slots=True)
class CitationDelta:
    """Delete exact prior keys and insert only new/changed normalized rows."""

    deletes: tuple[FactKey, ...]
    inserts: tuple[FrozenClaimFact, ...]


@dataclass(frozen=True, slots=True)
class CitationIndex:
    owners: PersistentMap[tuple[FrozenClaimFact, ...]]
    uses: PersistentMap[bytes]
    owner_uses: PersistentMap[tuple[str, ...]]
    groups: PersistentMap[PersistentMap[bool]]
    raw_by_group: PersistentMap[tuple[FrozenClaimFact, ...]]
    raw_by_claim: PersistentMap[PersistentMap[FrozenClaimFact]]
    estimated_bytes: int = 0

    @classmethod
    def empty(cls) -> CitationIndex:
        return cls(
            PersistentMap(),
            PersistentMap(),
            PersistentMap(),
            PersistentMap(),
            PersistentMap(),
            PersistentMap(),
        )

    @classmethod
    def rebuild(cls, facts: Iterable[ProjectionFact]) -> CitationIndex:
        uses: list[dict[str, object]] = []
        contracts: list[ProjectionFact] = []
        for fact in facts:
            if fact.schema_id == RELATION_USE_SCHEMA:
                assert isinstance(fact.value, dict)
                uses.append(fact.value)
            elif fact.schema_id == RELATION_CONTRACT_SCHEMA:
                contracts.append(fact)
        paths = frozenset(
            [str(use["claim_path"]) for use in uses]
            + [str(f.value["path"]["$path"]) for f in contracts]  # type: ignore[index]
        )
        return cls.empty()._advance(uses, contracts, paths, rebuilding=True)[0]

    def advance(
        self,
        inputs: Mapping[str, bytes],
        *,
        changed_paths: frozenset[str],
        bodies: BodyProjectionProtocol,
    ) -> tuple[CitationIndex, CitationDelta]:
        selected = {p: inputs[p] for p in changed_paths if p in inputs}
        return self._advance(
            _claim_uses(selected, bodies=bodies), _contract_facts(selected), changed_paths
        )

    def visible(self, claim: str) -> tuple[FrozenClaimFact, ...]:
        raw = self.raw_by_claim.get(claim, PersistentMap())
        capture = any(json.loads(f.value_json)["relation_kind"] == "capture" for f in raw.values())
        return tuple(
            f
            for f in raw.values()
            if not capture or json.loads(f.value_json)["relation_kind"] == "capture"
        )

    def facts(self) -> tuple[ProjectionFact, ...]:
        """Explicit full export, used by cold parity checks, never scoped apply."""
        return tuple(f.materialize() for fs in self.owners.values() for f in fs) + tuple(
            f.materialize() for claim in self.raw_by_claim for f in self.visible(claim)
        )

    def _advance(
        self,
        changed_uses: list[dict[str, object]],
        contracts: list[ProjectionFact],
        changed_paths: frozenset[str],
        *,
        rebuilding: bool = False,
    ) -> tuple[CitationIndex, CitationDelta]:
        owners, uses, owner_uses = (
            MapMutation(self.owners),
            MapMutation(self.uses),
            MapMutation(self.owner_uses),
        )
        groups, by_group, by_claim = (
            MapMutation(self.groups),
            MapMutation(self.raw_by_group),
            MapMutation(self.raw_by_claim),
        )
        touched: set[str] = set()
        affected_claims: set[str] = set()
        old_rows: dict[FactKey, FrozenClaimFact] = {}
        new_rows: dict[FactKey, FrozenClaimFact] = {}
        weight = self.estimated_bytes

        def membership(key: str, use: dict[str, object], *, remove: bool) -> None:
            nonlocal weight
            for group in _groups(use):
                touched.add(group)
                members = groups.get(group, PersistentMap())
                if remove:
                    members = members.delete(key)
                else:
                    members = members.set(key, True)
                if members:
                    groups[group] = members
                else:
                    groups.pop(group, None)
                weight += (-1 if remove else 1) * (
                    len(group.encode("utf-8")) + len(key.encode("utf-8")) + 128
                )

        for path in changed_paths:
            previous = owners.pop(path, ())
            old_rows.update((_key(f), f) for f in previous)
            weight -= _fact_weight(previous)
            for key in owner_uses.pop(path, ()):
                raw = uses.pop(key)
                membership(key, json.loads(raw), remove=True)
                weight -= len(raw) + 2 * len(key.encode("utf-8")) + 128
        incoming: dict[str, list[dict[str, object]]] = defaultdict(list)
        for use in changed_uses:
            key = _use_key(use)
            raw = canonical_bytes(use)
            uses[key] = raw
            membership(key, use, remove=False)
            incoming[str(use["claim_path"])].append(use)
            weight += len(raw) + 2 * len(key.encode("utf-8")) + 128
        for path, path_uses in incoming.items():
            owner_uses[path] = tuple(_use_key(use) for use in path_uses)
            owners[path] = _freeze(_use_facts(path_uses))
        for contract in contracts:
            path = str(contract.value["path"]["$path"])  # type: ignore[index]
            owners[path] = _freeze((contract,))
        for path in changed_paths:
            current = owners.get(path, ())
            new_rows.update((_key(f), f) for f in current)
            weight += _fact_weight(current)

        # Every touched group is recomputed from its final membership. A changed
        # Claim can move between groups or lose all citations; both ends matter.
        rebuilt: dict[str, list[ProjectionFact]] = defaultdict(list)
        if rebuilding:
            for conflict in _conflict_facts(sorted(changed_uses, key=_use_key)):
                rebuilt[str(conflict.value["relation_key"])].append(conflict)  # type: ignore[index]
        for group in sorted(touched):
            previous = by_group.pop(group, ())
            weight -= 2 * _fact_weight(previous)
            for fact in previous:
                claim = str(json.loads(fact.value_json)["live_claim_identity"])
                affected_claims.add(claim)
                remaining = by_claim[claim].delete(fact.fact_key)
                if remaining:
                    by_claim[claim] = remaining
                else:
                    by_claim.pop(claim)
            facts = (
                rebuilt.get(group, [])
                if rebuilding
                else _conflict_facts(
                    [json.loads(uses[key]) for key in groups.get(group, PersistentMap())], {group}
                )
            )
            frozen = _freeze(facts)
            if frozen:
                by_group[group] = frozen
            weight += 2 * _fact_weight(frozen)
            for fact in frozen:
                claim = str(json.loads(fact.value_json)["live_claim_identity"])
                affected_claims.add(claim)
                by_claim[claim] = by_claim.get(claim, PersistentMap()).set(fact.fact_key, fact)

        successor = CitationIndex(
            owners.finish(),
            uses.finish(),
            owner_uses.finish(),
            groups.finish(),
            by_group.finish(),
            by_claim.finish(),
            weight,
        )
        for claim in affected_claims:
            old_rows.update((_key(f), f) for f in self.visible(claim))
            new_rows.update((_key(f), f) for f in successor.visible(claim))
        delta = CitationDelta(
            tuple(sorted(k for k, f in old_rows.items() if new_rows.get(k) != f)),
            tuple(new_rows[k] for k in sorted(new_rows) if old_rows.get(k) != new_rows[k]),
        )
        return successor, delta


def rebuild_citation_index(projection: ProjectionHandle) -> CitationIndex | None:
    """Bootstrap once; an old, lossy conflict slice selects true cold assembly.

    Historical incremental compilers carried only exposed conflicts, so they
    could lose weaker rows after precedence changes. Never assume such a parent's
    physical rows equal our reconstructed raw-group state when applying deletes.
    """
    root = CitationIndex.rebuild(
        (
            *projection.semantic_facts(RELATION_USE_SCHEMA),
            *projection.semantic_facts(RELATION_CONTRACT_SCHEMA),
        )
    )
    actual = _freeze(projection.semantic_facts(RELATION_RETIRED_CONFLICT_SCHEMA))
    expected = tuple(f for claim in root.raw_by_claim for f in root.visible(claim))
    if sorted(actual, key=_key) != sorted(expected, key=_key):
        return None
    return root


def _binding(coordinate: AcceptedProjectionCoordinate | AssemblerRequest) -> tuple[object, ...]:
    compiler, schema = (
        (coordinate.compiler.rule_digest, coordinate.compiler.schema_version)
        if isinstance(coordinate, AcceptedProjectionCoordinate)
        else (coordinate.compiler_digest, coordinate.schema_version)
    )
    return (
        "citation-relations-v1",
        coordinate.instance_id,
        coordinate.git_object_format,
        coordinate.git_oid,
        coordinate.semantic_root,
        coordinate.generation_root,
        compiler,
        schema,
    )


class CitationIndexCache:
    """Instance-owned, coordinate-bound roots. A verified parent is always required.

    Candidate derivations are retained under their exact future coordinate only
    after SQLite apply succeeds. A cache hit never establishes acceptance: the
    next consumer first binds/verifies the authoritative parent's projection.
    Clear/eviction selects reconstruction from those verified use rows.
    """

    def __init__(
        self, owner: DerivedState, *, max_entries: int = 4, max_bytes: int = 128 * 1024 * 1024
    ) -> None:
        self.owner = owner
        self.cache = owner.memo("citation-relations", max_entries=max_entries, max_bytes=max_bytes)
        owner.register(
            IndexDefinition(
                "citation-relations", "accepted/candidate", "1", "verified-citation-uses-v1"
            ),
            self,
        )

    def clear(self) -> None:
        self.cache.clear()

    def parent(self, projection: ProjectionHandle) -> CitationIndex | None:
        key = _binding(projection.accepted)
        with self.owner.build(key):
            existing = self.cache.get(key)
            if existing is not None:
                return cast(CitationIndex, existing)
            epoch = self.cache.generation
            root = rebuild_citation_index(projection)
            if root is None:
                return None
            self.cache.put(key, root, weight=root.estimated_bytes, expected_generation=epoch)
            return root

    def peek(self, coordinate: AcceptedProjectionCoordinate) -> CitationIndex | None:
        return self.cache.get(_binding(coordinate))

    def remember(self, coordinate: AssemblerRequest, root: CitationIndex, *, epoch: int) -> None:
        self.cache.put(
            _binding(coordinate), root, weight=root.estimated_bytes, expected_generation=epoch
        )
