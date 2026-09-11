"""Scoped owner/group updates preserve cold citation rows and old readers."""

from __future__ import annotations

import copy
import random

import pytest

from cruxible_client.contracts.captures import (
    FOREIGN_SOURCE_COORDINATE_TYPE,
    FOREIGN_SOURCE_SELECTOR_TYPE,
)
from cruxible_client.contracts.projection_extensions import ProjectionFact
from cruxible_core.evidence import citation_relations as relations
from cruxible_core.indexes.evidence import citation_index as index_module
from cruxible_core.indexes.evidence.citation_index import CitationIndex


def use(claim: int, capture: int, *, retired=False, source=0, start=0, end=10, citation=None):
    return {
        "capture_contract_digest": {"$digest": "sha256:" + "a" * 64},
        "capture_digest": {"$digest": f"sha256:{capture:064x}"},
        "citation_id": f"sha256:{(claim * 1000 + capture if citation is None else citation):064x}",
        "claim_artifact_digest": {"$digest": f"sha256:{claim + int(retired):064x}"},
        "claim_identity": f"Claim:CLM-{claim:032x}",
        "claim_lifecycle": "retired" if retired else "live",
        "claim_path": f"claims/CLM-{claim:032x}.json",
        "commitment": {},
        "origin": "authored",
        "role": "support",
        "source": {
            "tag": "playbill-external-source-reference-v1",
            "kind": "external",
            "source_identity": f"source-{source}",
            "producer_binding_digest": "sha256:" + "c" * 64,
            "coordinate_type": FOREIGN_SOURCE_COORDINATE_TYPE,
            "coordinate": {"version": 1},
            "selector_type": FOREIGN_SOURCE_SELECTOR_TYPE,
            "selector": {"start_byte": start, "end_byte": end},
            "replayability": "exact",
        },
    }


def key(fact):
    return fact.schema_id, fact.schema_version, fact.subject_identity, fact.fact_key


def cold(uses, contracts=()):
    """Full public compiler remains the oracle; replace only its CAS read seam."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            relations, "_claim_uses", lambda *_a, **_kw: sorted(uses, key=index_module._use_key)
        )
        patch.setattr(relations, "_contract_facts", lambda *_a, **_kw: list(contracts))
        return relations.build_citation_relation_facts({}, bodies=None)


def advance(root, uses, paths, contracts=()):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(index_module, "_claim_uses", lambda *_a, **_kw: uses)
        patch.setattr(index_module, "_contract_facts", lambda *_a, **_kw: list(contracts))
        return root.advance({}, changed_paths=frozenset(paths), bodies=None)


def assert_delta(before, after, delta):
    rows = {key(f): f for f in before.facts()}
    for k in delta.deletes:
        assert k in rows
        del rows[k]
    for frozen in delta.inserts:
        f = frozen.materialize()
        assert key(f) not in rows
        rows[key(f)] = f
    assert rows == {key(f): f for f in after.facts()}


def test_capture_precedence_removal_restores_untouched_weaker_groups():
    values = [
        use(1, 10, source=1),
        use(1, 20, source=2),
        use(2, 10, retired=True, source=1),
        use(3, 21, retired=True, source=2),
    ]
    root = CitationIndex.rebuild(cold(values))
    initial = root.facts()
    assert {
        f.value["relation_kind"]
        for f in initial
        if f.schema_id == relations.RELATION_RETIRED_CONFLICT_SCHEMA
    } == {"capture"}
    next_root, delta = advance(root, [], [values[2]["claim_path"]])
    assert sorted(next_root.facts(), key=key) == sorted(
        cold([values[0], values[1], values[3]]), key=key
    )
    assert {
        f.value["relation_kind"]
        for f in next_root.facts()
        if f.schema_id == relations.RELATION_RETIRED_CONFLICT_SCHEMA
    } == {"exact_external", "same_version_span"}
    assert_delta(root, next_root, delta)
    assert root.facts() == initial
    assert CitationIndex.rebuild(next_root.facts()).estimated_bytes == next_root.estimated_bytes


def test_moves_removals_retirement_and_witness_order_match_cold_randomized():
    rng = random.Random(8097)
    world = {n: [use(n, n % 9, retired=n % 5 == 0, source=n % 3)] for n in range(1, 31)}
    root = CitationIndex.rebuild(cold([u for rows in world.values() for u in rows]))
    for _ in range(100):
        claim = rng.randrange(1, 36)
        path = use(claim, 0)["claim_path"]
        incoming = (
            []
            if rng.randrange(5) == 0
            else [
                use(
                    claim,
                    cap,
                    retired=bool(rng.randrange(2)),
                    source=cap % 3,
                    start=cap % 7,
                    end=cap % 7 + 3,
                    citation=claim * 1000 + cap,
                )
                for cap in rng.sample(range(12), rng.randrange(1, 4))
            ]
        )
        world[claim] = incoming
        previous = root
        root, delta = advance(root, incoming, [path])
        expected = cold([u for rows in world.values() for u in rows])
        assert sorted(root.facts(), key=key) == sorted(expected, key=key)
        assert_delta(previous, root, delta)
        assert root.estimated_bytes == CitationIndex.rebuild(expected).estimated_bytes


def test_unrelated_groups_are_not_visited_and_models_are_detached(monkeypatch):
    values = [use(n, n, source=n) for n in range(1, 1001)]
    root = CitationIndex.rebuild(cold(values))
    untouched = root.groups[index_module._groups(values[-1])[0]]
    calls = []
    original = index_module._conflict_facts

    def counted(uses, groups=None):
        calls.append(len(uses))
        assert all(u["claim_identity"] == values[0]["claim_identity"] for u in uses)
        return original(uses, groups)

    monkeypatch.setattr(index_module, "_conflict_facts", counted)
    updated = copy.deepcopy(values[0])
    updated["claim_lifecycle"] = "retired"
    successor, delta = advance(root, [updated], [updated["claim_path"]])
    assert calls == [1, 1, 1]
    assert successor.groups[index_module._groups(values[-1])[0]] is untouched
    assert len(delta.deletes) == len(delta.inserts) == 3
    updated["source"]["source_identity"] = "poisoned"
    delta.inserts[0].materialize().value["source"]["source_identity"] = "poisoned"
    assert "poisoned" not in str(successor.facts())
    assert root.facts()[0].value["claim_lifecycle"] == "live"


def test_contract_replacement_and_removal_do_not_visit_claim_groups(monkeypatch):
    def contract(digit):
        return ProjectionFact(
            schema_id=relations.RELATION_CONTRACT_SCHEMA,
            schema_version=1,
            subject_identity=f"capture-contract-{digit * 64}",
            fact_key="accepted-contract",
            value={
                "path": {"$path": "capture-contracts/example.json"},
                "artifact_digest": {"$digest": f"sha256:{digit * 64}"},
            },
        )

    values = [use(1, 1)]
    root = CitationIndex.rebuild(cold(values, [contract("a")]))
    monkeypatch.setattr(
        index_module,
        "_conflict_facts",
        lambda *_a, **_kw: pytest.fail("contract has no use-group effects"),
    )
    after, delta = advance(root, [], ["capture-contracts/example.json"], [contract("b")])
    assert len(delta.deletes) == len(delta.inserts) == 1
    assert after.groups._root is root.groups._root
    assert_delta(root, after, delta)
    removed, delta = advance(after, [], ["capture-contracts/example.json"])
    assert sorted(removed.facts(), key=key) == sorted(cold(values), key=key)
    assert_delta(after, removed, delta)


def test_noop_has_empty_delta():
    values = [use(1, 1), use(2, 1, retired=True)]
    root = CitationIndex.rebuild(cold(values))
    after, delta = advance(root, [values[0]], [values[0]["claim_path"]])
    assert delta.deletes == delta.inserts == ()
    assert after.facts() == root.facts()


def test_cache_coordinates_eviction_clear_and_lossy_parent_fallback(tmp_path):
    from types import SimpleNamespace

    from cruxible_core.derived.derived_state import DerivedState
    from cruxible_core.indexes.evidence.citation_index import CitationIndexCache
    from cruxible_core.indexes.projection import AssemblerRequest
    from tests.core_support._support import initialize_local

    instance, _ = initialize_local(tmp_path)
    coordinate = instance.accepted_coordinate()
    owner = DerivedState()
    cache = CitationIndexCache(owner)
    values = [use(1, 1), use(2, 1, retired=True)]
    rows = cold(values)
    reads = []

    def facts(schema):
        reads.append(schema)
        return tuple(f for f in rows if f.schema_id == schema)

    handle = SimpleNamespace(accepted=coordinate, semantic_facts=facts)
    root = cache.parent(handle)
    assert root is not None
    assert len(reads) == 3
    assert cache.parent(handle) is root
    assert len(reads) == 3
    # Equal semantic state at another generation is not a hit.
    later = coordinate.model_copy(update={"git_oid": "f" * len(coordinate.git_oid)})
    handle.accepted = later
    assert cache.parent(handle) is not root
    assert len(reads) == 6
    request = AssemblerRequest(
        **{
            k: getattr(later, k)
            for k in (
                "instance_id",
                "repository_path",
                "git_object_format",
                "git_oid",
                "semantic_root",
                "generation_root",
            )
        },
        compiler_digest=later.compiler.rule_digest,
        output_staging_directory=str(tmp_path / "stage"),
    )
    epoch = cache.cache.generation
    owner.clear()
    cache.remember(request, root, epoch=epoch)
    assert cache.peek(later) is None
    cache.cache.configure(max_entries=0, max_bytes=0)
    assert cache.parent(handle) is not None
    assert cache.peek(later) is None
    # Missing weaker/conflict rows from a lossy historical incremental build
    # cannot be the physical parent of a delta calculated from cold raw groups.
    rows = tuple(f for f in rows if f.schema_id != relations.RELATION_RETIRED_CONFLICT_SCHEMA)
    assert cache.parent(handle) is None


def test_witness_cap_half_open_spans_and_complete_removal():
    retired = [use(n, 1, retired=True, source=1) for n in range(10, 22)]
    values = [use(1, 1, source=1), use(2, 2, source=1, start=10, end=20), *retired]
    root = CitationIndex.rebuild(cold(values))
    capture = next(
        f for f in root.facts() if f.schema_id == relations.RELATION_RETIRED_CONFLICT_SCHEMA
    )
    assert capture.value["retired_claim_count"] == 12
    assert len(capture.value["retired_claim_witnesses"]) == 8
    assert all(
        f.value["live_claim_identity"] != values[1]["claim_identity"]
        for f in root.facts()
        if f.schema_id == relations.RELATION_RETIRED_CONFLICT_SCHEMA
    )
    successor, delta = advance(root, [], [retired[0]["claim_path"]])
    assert sorted(successor.facts(), key=key) == sorted(cold([*values[:2], *retired[1:]]), key=key)
    assert_delta(root, successor, delta)
    empty, delta = advance(successor, [], [u["claim_path"] for u in values])
    assert empty.facts() == ()
    assert empty.estimated_bytes == 0
    assert not empty.groups and not empty.raw_by_group and not empty.raw_by_claim
    assert_delta(successor, empty, delta)
