"""Exact derivation parity, ownership and bounded retained evaluation state."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from cruxible_client.contracts.subjects import render_subject
from cruxible_core.playbill import closure
from cruxible_core.playbill import evaluation_state_cache as cache_module
from cruxible_core.playbill.evaluation_state_cache import EvaluationStateCache
from cruxible_core.playbill.proposals import build_tree_state
from tests.test_playbill.test_incremental_closure import _path, _pin_to, _subject


def _tree():
    anchor = _subject("anchor")
    return {
        _path("anchor"): render_subject(anchor),
        _path("dependent"): render_subject(_subject("dependent", pins=(_pin_to(anchor),))),
        _path("unrelated"): render_subject(_subject("unrelated")),
    }


def test_cache_matches_cold_through_add_revision_retirement_removal_and_rewind():
    cache = EvaluationStateCache()
    base = _tree()
    anchor = _subject("anchor", revision=1)
    revised = {**base, _path("anchor"): render_subject(anchor)}
    repinned = {
        **revised,
        _path("dependent"): render_subject(_subject("dependent", pins=(_pin_to(anchor),))),
    }
    retired = {**repinned, _path("anchor"): render_subject(_subject("anchor", retired=True))}
    removed = {p: b for p, b in retired.items() if p != _path("anchor")}
    for tree in [base, revised, repinned, retired, removed, {}, base]:
        assert cache.derive(tree) == build_tree_state(tree)
        assert cache.derive(tree) == build_tree_state(tree)


def test_warm_state_parses_only_changed_member_and_reresolves_its_dependents(monkeypatch):
    cache = EvaluationStateCache()
    base = _tree()
    cache.derive(base)
    changed = {**base, _path("anchor"): render_subject(_subject("anchor", revision=1))}
    expected = build_tree_state(changed)
    original = closure.parse_dependency_artifact
    calls = []

    def parse(path, content):
        calls.append(path)
        return original(path, content)

    def unexpected(*args, **kwargs):
        pytest.fail("warm evaluation rebuilt the whole parent state")

    monkeypatch.setattr(cache_module, "build_tree_state", unexpected)
    monkeypatch.setattr(closure, "parse_dependency_artifact", parse)
    assert cache.derive(base) == build_tree_state(base)
    calls.clear()  # The independent oracle above parses its own inputs.
    assert cache.derive(changed) == expected
    assert calls == [_path("anchor")]
    assert cache.derive(changed) == expected
    assert calls == [_path("anchor")]


def test_inputs_and_returned_nested_state_cannot_mutate_cached_derivations():
    cache = EvaluationStateCache()
    tree = _tree()
    expected = build_tree_state(tree)
    result = cache.derive(tree)
    tree.clear()
    result.members.clear()
    result.dependencies.states[_path("anchor")].identity.__dict__["name"] = "forged"
    result.dependencies.paths_by_identity.clear()
    result.merkle.nodes.clear()
    assert cache.derive(_tree()) == expected


def test_irrelevant_history_bytes_do_not_invalidate_semantic_derivations(monkeypatch):
    cache = EvaluationStateCache()
    tree = _tree()
    expected = cache.derive(tree)

    def unexpected(*args, **kwargs):
        pytest.fail("history-only change rebuilt a semantic index")

    monkeypatch.setattr(cache_module, "build_tree_state", unexpected)
    monkeypatch.setattr(cache_module, "advance_tree_members", unexpected)
    assert cache.derive({**tree, "changesets/cs-00000000000000000001.json": b"history"}) == expected


@pytest.mark.parametrize("limits", [{"max_members": 0}, {"max_members": 1}, {"max_input_bytes": 1}])
def test_budget_bypass_retains_no_state_and_preserves_cold_output(limits):
    cache = EvaluationStateCache(**limits)
    tree = _tree()
    assert cache.derive(tree) == build_tree_state(tree)
    assert cache.derive(tree) == build_tree_state(tree)
    assert cache._tree is cache._state is None


@pytest.mark.parametrize("limits", [{"max_members": -1}, {"max_input_bytes": -1}])
def test_negative_limits_refused(limits):
    with pytest.raises(ValueError):
        EvaluationStateCache(**limits)


def test_clear_forces_a_new_cold_derivation(monkeypatch):
    cache = EvaluationStateCache()
    tree = _tree()
    cache.derive(tree)
    cache.clear()
    calls = []
    original = cache_module.build_tree_state

    def build(tree):
        calls.append(1)
        return original(tree)

    monkeypatch.setattr(cache_module, "build_tree_state", build)
    assert cache.derive(tree) == build_tree_state(tree)
    assert calls == [1]


def test_concurrent_different_trees_and_clear_keep_exact_results():
    cache = EvaluationStateCache()
    trees = [{_path(f"item-{i}"): render_subject(_subject(f"item-{i}"))} for i in range(8)]
    expected = [build_tree_state(tree) for tree in trees]

    def run(i):
        if i % 3 == 0:
            cache.clear()
        assert cache.derive(trees[i % len(trees)]) == expected[i % len(trees)]

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(run, range(24)))


def test_failed_incremental_derivation_uses_cold_refusal_and_keeps_previous_state(monkeypatch):
    cache = EvaluationStateCache()
    base = _tree()
    expected = cache.derive(base)
    malformed = {**base, _path("z-invalid"): b"{}"}
    with pytest.raises(Exception) as cold:
        build_tree_state(malformed)

    def earlier_error(*args, **kwargs):
        raise ValueError("candidate tree contains a duplicate semantic artifact identity")

    with monkeypatch.context() as guarded:
        guarded.setattr(cache_module, "advance_tree_state", earlier_error)
        with pytest.raises(type(cold.value)) as warm:
            cache.derive(malformed)
        assert str(warm.value) == str(cold.value)
    assert cache.derive(base) == expected
