"""Edited-tree Claim selection equals the cold full-tree oracle, without a per-lookup overlay."""

from __future__ import annotations

import random
from dataclasses import replace
from pathlib import Path

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
