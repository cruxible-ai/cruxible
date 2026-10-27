"""Edited-tree Claim selection equals the cold full-tree oracle, without a per-lookup overlay."""

from __future__ import annotations

import random
from dataclasses import replace
from pathlib import Path

import pytest

from cruxible_client.contracts.claims import parse_claim
from cruxible_core.derived.derived_state import SnapshotTree
from cruxible_core.indexes import evaluated_state
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._adoption_fixture import AdoptionFixtureProfile, build_fixture
from tests.core_support._subject_kind_support import fixture_claim_paths

PROFILE = AdoptionFixtureProfile(
    name="edited-claims",
    subjects=4,
    claim_types=2,
    documents=1,
    query_definitions=1,
    seed_claims=6,
    generations=6,
    seed="edited-claims",
)


def test_edited_claim_items_match_the_cold_oracle(tmp_path: Path, monkeypatch) -> None:
    fixture = build_fixture(tmp_path, PROFILE)
    instance = PlaybillInstance.open(fixture.managed_root, trust_root=fixture.instance.trust_root)
    history = instance.accepted_history()
    head = instance.immutable_tree_at(history[-1].oid)
    # Claims from a second seed: the same Subjects and ClaimTypes, other paths.
    other = build_fixture(tmp_path / "other", replace(PROFILE, seed="edited-claims-other"))
    other_instance = PlaybillInstance.open(other.managed_root, trust_root=other.instance.trust_root)
    other_tree = other_instance.tree_at(other_instance.accepted_history()[-1].oid)
    additions = {
        path: content
        for path, content in other_tree.items()
        if path.startswith("claims/") and path not in head
    }
    claim_paths = fixture_claim_paths(head)
    assert claim_paths and additions

    overlays: list[int] = []
    original = evaluated_state.EvaluationRows.overlay
    monkeypatch.setattr(
        evaluated_state.EvaluationRows,
        "overlay",
        lambda self, edits: overlays.append(1) or original(self, edits),
    )
    rng = random.Random(3)
    added_seen = 0
    for _ in range(8):
        candidate = head.fork()
        for path in rng.sample(claim_paths, k=min(3, len(claim_paths))):
            if rng.random() < 0.5:
                del candidate[path]
            else:
                candidate[path] = head[path]  # rewritten with the same bytes
        for path in rng.sample(sorted(additions), k=min(4, len(additions))):
            candidate[path] = additions[path]
        sealed = candidate.snapshot()
        cold = SnapshotTree(dict(sealed.items()))
        statements = {
            parse_claim(content, path=path).statement
            for path, content in (*((p, head[p]) for p in claim_paths), *additions.items())
        }
        for statement in statements:
            items = sealed.claim_items(statement)
            assert items == cold.claim_items(statement)
            added_seen += sum(1 for path, _content in items if path in additions)
    assert added_seen > 0  # added Claims were selected alongside accepted ones
    assert overlays == []  # no overlay is built for a Claim lookup


def test_claims_staged_one_by_one_through_forks_match_the_cold_oracle_at_every_step(
    tmp_path: Path,
) -> None:
    """The slot index a fork carries stays exact as Claims are staged, revised and deleted.

    Each step forks the previous step's snapshot (as lowering does per member),
    so every lookup reads an index carried forward plus what was staged since.
    """

    fixture = build_fixture(tmp_path, PROFILE)
    instance = PlaybillInstance.open(fixture.managed_root, trust_root=fixture.instance.trust_root)
    head = instance.immutable_tree_at(instance.accepted_history()[-1].oid)
    other = build_fixture(tmp_path / "other", replace(PROFILE, seed="edited-claims-other"))
    other_instance = PlaybillInstance.open(other.managed_root, trust_root=other.instance.trust_root)
    other_tree = other_instance.tree_at(other_instance.accepted_history()[-1].oid)
    additions = sorted(
        (path, content)
        for path, content in other_tree.items()
        if path.startswith("claims/") and path not in head
    )
    claim_paths = fixture_claim_paths(head)
    statements = {
        parse_claim(content, path=path).statement
        for path, content in (*((p, head[p]) for p in claim_paths), *additions)
    }
    rng = random.Random(5)
    tree = head.fork()
    for step, (path, content) in enumerate(additions):
        tree[path] = content
        if step % 3 == 1 and claim_paths:
            del tree[rng.choice([p for p in claim_paths if p in tree] or [path])]
        if step % 4 == 2:
            tree[path] = head.get(path, content)  # restaged with the same bytes
        sealed = tree.snapshot()
        cold = SnapshotTree(dict(sealed.items()))
        for statement in statements:
            assert sealed.claim_items(statement) == cold.claim_items(statement), (step, path)
        tree = sealed.fork()


def test_the_claim_match_memo_is_bounded_by_the_bytes_it_keeps(monkeypatch) -> None:
    from cruxible_core.derived import derived_state

    monkeypatch.setattr(derived_state, "_CLAIM_MATCH_MAX_BYTES", 64 * 1024)
    monkeypatch.setattr(derived_state, "_claim_match_bytes", 0)
    monkeypatch.setattr(derived_state, "_CLAIM_MATCH_FACTS", derived_state.OrderedDict())
    monkeypatch.setattr(
        derived_state,
        "parse_claim",
        lambda content, path: type(
            "C",
            (),
            {
                "statement": type("S", (), {"subject": "s", "predicate": "p"})(),
                "lifecycle": type("L", (), {"state": "live"})(),
            },
        )(),
    )
    for index in range(200):
        derived_state._claim_facts(f"claims/{index}.json", bytes(2048) + bytes([index]))
    kept = sum(len(p) + len(c) for p, c in derived_state._CLAIM_MATCH_FACTS)
    assert kept <= 64 * 1024
    assert kept == derived_state._claim_match_bytes


def test_lowering_indexes_each_staged_claim_once(tmp_path: Path, monkeypatch) -> None:
    """A write's slot index work grows with its Claims, not with their square.

    Lowering looks up slots through forks of the tree it stages into; an index
    brought up to date only at lookup re-indexed every Claim staged since the
    forked tree's last lookup, so a 1,000-Claim change did ~25x the index work
    of a 200-Claim one. Creating a work item by key stages its key and title
    Claims: 100 and 500 changes stage 200 and 1,000 Claims.
    """

    from cruxible_client.contracts import persistent
    from cruxible_client.contracts.write import WriteRequest
    from cruxible_core.service.authoring.write_verbs import service_playbill_write
    from tests.core_support._write_support import KIND, caller, seed_write_surface

    instance, _owner = seed_write_surface(tmp_path)
    sets = [0]
    original = persistent.PersistentMap.set

    def counted(self, *args, **kwargs):
        sets[0] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(persistent.PersistentMap, "set", counted)
    measured = {}
    for changes in (100, 500):
        sets[0] = 0
        request = WriteRequest.model_validate(
            {
                "because": "lowering index work",
                "dry_run": True,
                "changes": [
                    {"op": "set", "subject": f"{KIND}/n{changes}-{i:04d}", "field": "title"}
                    | {"value": f"item {i}"}
                    for i in range(changes)
                ],
            }
        )
        outcome = service_playbill_write(instance, request=request, caller=caller())
        assert outcome.status == "would_accept", outcome.refusal
        measured[changes] = sets[0]
    # Linear is 5x (with a log factor from the persistent maps' depth); the
    # per-lookup re-index measured 4.3k vs 19.5k fixed and ~25x unfixed.
    assert measured[500] <= 6.5 * measured[100], measured


def test_a_long_unlooked_staging_chain_stays_exact_and_bounded(tmp_path: Path) -> None:
    """Sealing past the chain limit resolves the base: lookups stay exact, the chain short."""

    from cruxible_core.derived import derived_state

    fixture = build_fixture(tmp_path, PROFILE)
    instance = PlaybillInstance.open(fixture.managed_root, trust_root=fixture.instance.trust_root)
    head = instance.immutable_tree_at(instance.accepted_history()[-1].oid)
    other = build_fixture(tmp_path / "other", replace(PROFILE, seed="edited-claims-other"))
    other_instance = PlaybillInstance.open(other.managed_root, trust_root=other.instance.trust_root)
    other_tree = other_instance.tree_at(other_instance.accepted_history()[-1].oid)
    additions = sorted(
        (path, content)
        for path, content in other_tree.items()
        if path.startswith("claims/") and path not in head
    )
    assert additions
    tree = head.fork()
    depths = []
    for step in range(3 * derived_state._CLAIM_CHAIN_LIMIT):  # noqa: SLF001
        path, content = additions[step % len(additions)]
        if path in tree:
            del tree[path]
        else:
            tree[path] = content
        sealed = tree.snapshot()
        depths.append(sealed._claim_depth)  # noqa: SLF001
        tree = sealed.fork()
    assert max(depths) <= derived_state._CLAIM_CHAIN_LIMIT + 1  # noqa: SLF001
    cold = SnapshotTree(dict(sealed.items()))
    for path, content in additions:
        statement = parse_claim(content, path=path).statement
        assert sealed.claim_items(statement) == cold.claim_items(statement)


def _fixture_trees(tmp_path: Path):  # type: ignore[no-untyped-def]
    fixture = build_fixture(tmp_path, PROFILE)
    instance = PlaybillInstance.open(fixture.managed_root, trust_root=fixture.instance.trust_root)
    head = instance.immutable_tree_at(instance.accepted_history()[-1].oid)
    other = build_fixture(tmp_path / "other", replace(PROFILE, seed="edited-claims-other"))
    other_instance = PlaybillInstance.open(other.managed_root, trust_root=other.instance.trust_root)
    other_tree = other_instance.tree_at(other_instance.accepted_history()[-1].oid)
    additions = sorted(
        (path, content)
        for path, content in other_tree.items()
        if path.startswith("claims/") and path not in head
    )
    assert len(additions) >= 2
    return head, additions


def test_only_the_final_bytes_of_a_repaired_claim_decide_a_lookup(tmp_path: Path) -> None:
    """Bytes staged and sealed, then replaced: a lookup reads only the final bytes."""

    head, additions = _fixture_trees(tmp_path)
    (path, content), (other_path, other_content) = additions[0], additions[1]
    tree = head.fork()
    tree[path] = b"not a claim"
    tree = tree.snapshot().fork()
    tree[other_path] = other_content
    tree = tree.snapshot().fork()
    tree[path] = content  # repaired
    sealed = tree.snapshot()
    cold = SnapshotTree(dict(sealed.items()))
    for item_path, item in (additions[0], additions[1]):
        statement = parse_claim(item, path=item_path).statement
        assert sealed.claim_items(statement) == cold.claim_items(statement)
    # A tree that still holds the malformed bytes refuses them, as it always did.
    broken = head.fork()
    broken[path] = b"not a claim"
    with pytest.raises(Exception, match="Claim"):
        broken.snapshot().claim_items(parse_claim(content, path=path).statement)


def test_sealing_past_the_chain_limit_parses_nothing_and_keeps_final_semantics(
    tmp_path: Path, monkeypatch
) -> None:
    from cruxible_core.derived import derived_state

    head, additions = _fixture_trees(tmp_path)
    path, content = additions[0]
    tree = head.fork()
    tree[path] = b"not a claim"
    tree = tree.snapshot().fork()
    tree[path] = content  # repaired before the chain is compacted

    def refuse(*args, **kwargs):  # type: ignore[no-untyped-def]
        pytest.fail("sealing parsed a Claim")

    with monkeypatch.context() as guard:
        guard.setattr(derived_state, "parse_claim", refuse)
        for step in range(3 * derived_state._CLAIM_CHAIN_LIMIT):  # noqa: SLF001
            other_path, other_content = additions[1 + step % (len(additions) - 1)]
            if other_path in tree:
                del tree[other_path]
            else:
                tree[other_path] = other_content
            sealed = tree.snapshot()
            assert sealed._claim_depth <= derived_state._CLAIM_CHAIN_LIMIT + 1  # noqa: SLF001
            tree = sealed.fork()
    cold = SnapshotTree(dict(sealed.items()))
    for item_path, item in additions:
        statement = parse_claim(item, path=item_path).statement
        assert sealed.claim_items(statement) == cold.claim_items(statement)


def test_staging_distinct_claims_across_compactions_visits_each_path_a_bounded_number_of_times():
    """Seal and fork after each distinct Claim, no lookups: path work stays near n log n.

    Each compaction once carried every earlier owed path into the new bottom
    and the next one copied and re-sorted them all, so work grew with the
    cumulative change: ~20x for 5x the Claims. Paths here count every hash and
    comparison made on them, wherever the staging does it.
    """

    from cruxible_core.derived import derived_state
    from cruxible_core.derived.derived_state import fork_tree

    visits = [0]

    class CountedPath(str):
        __slots__ = ()

        def __hash__(self) -> int:
            visits[0] += 1
            return str.__hash__(self)

        def __eq__(self, other: object) -> bool:
            visits[0] += 1
            return str.__eq__(self, other)

        def __lt__(self, other: str) -> bool:
            visits[0] += 1
            return str.__lt__(self, other)

        def __gt__(self, other: str) -> bool:
            visits[0] += 1
            return str.__gt__(self, other)

    def staged(count: int) -> int:
        tree = fork_tree({"claims/00/CLM-" + "0" * 32 + ".json": b"{}"})
        visits[0] = 0
        for index in range(count):
            tree[CountedPath(f"claims/{index % 256:02x}/CLM-{index:032x}.json")] = b"{}"
            tree = tree.snapshot().fork()
        return visits[0]

    limit = derived_state._CLAIM_CHAIN_LIMIT  # noqa: SLF001
    small, large = staged(4 * limit), staged(20 * limit)  # 3 and 19 compactions
    # Measured 14,199 vs 93,787 (6.6x: every staged row is also a persistent-map
    # set, a log factor deeper at 5x); the per-compaction copy made it 14,221 vs
    # 186,574 (13x). Counts are deterministic.
    assert large <= 7 * small, (small, large)
