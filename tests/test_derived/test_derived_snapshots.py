"""Independent ownership, delta scope, and accepted-history snapshot checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client._persistent import PersistentMap
from cruxible_client.contracts.authoring.models import SubjectAuthoringPayloadV1
from cruxible_client.contracts.claims import claim_path, render_claim
from cruxible_client.contracts.subjects import render_subject
from cruxible_core.authoring.lowering import _same_predicate_claims
from cruxible_core.derived.derived_state import SnapshotTree
from cruxible_core.derived.evaluation_state_cache import EvaluationStateCache
from cruxible_core.proposals.proposals import (
    AuthenticatedActor,
    advance_tree_members,
    build_tree_state,
)
from tests.core_support._support import initialize_local
from tests.test_authoring.test_authoring_change_set_intents import _accept, _coordinator
from tests.test_authoring.test_authoring_disposition_slots import _claim_in_slot
from tests.test_authoring.test_authoring_preflight import TIMESTAMP, _seed_claim_surface
from tests.test_claims.test_incremental_closure import _path, _subject
from tests.test_proposals.test_evaluation_state_cache import _tree


def test_forks_seal_each_revision_and_report_complete_edits_after_rollback() -> None:
    rows = {_path("anchor"): b"original", _path("removed"): b"present"}
    base = SnapshotTree(rows)
    left, right = base.fork(), base.fork()
    left[_path("anchor")] = b"first"
    del left[_path("removed")]
    left[_path("new")] = b"new"
    first = left.snapshot()
    continuation = first.fork()
    continuation[_path("anchor")] = b"original"
    continuation[_path("removed")] = b"present"
    del continuation[_path("new")]
    continuation[_path("final")] = b"final"
    final = continuation.snapshot()
    left[_path("anchor")] = b"later mutation"
    right[_path("right")] = b"right only"

    assert dict(base) == rows
    assert first[_path("anchor")] == b"first"
    assert _path("removed") not in first
    assert dict(first.edits_from(base)) == {
        _path("anchor"): b"first",
        _path("removed"): None,
        _path("new"): b"new",
    }
    assert dict(final.edits_from(base)) == {_path("final"): b"final"}
    assert _path("right") not in first and _path("right") not in final
    assert dict(right.snapshot().edits_from(base)) == {_path("right"): b"right only"}
    assert final.edits_from(SnapshotTree(rows)) is None
    assert final.edits_from(final) == {}


@pytest.mark.parametrize("operation", ["replace", "delete"])
def test_cold_candidate_contenders_read_final_bytes_before_invalid_parent(operation) -> None:
    claim = _claim_in_slot(claim_id="CLM-" + "1" * 32, qualifier=None)
    path = claim_path(claim.identity.name)
    malformed = SnapshotTree({path: b"not a Claim"})
    candidate = malformed.fork()
    if operation == "replace":
        candidate[path] = render_claim(claim)
    else:
        del candidate[path]
    sealed = candidate.snapshot()
    assert sealed.claims_for(claim.statement) == _same_predicate_claims(sealed, claim.statement)
    # Repairing one candidate never repairs its accepted parent or a sibling.
    with pytest.raises(Exception) as oracle:
        _same_predicate_claims(malformed, claim.statement)
    with pytest.raises(type(oracle.value)) as retained:
        malformed.claims_for(claim.statement)
    assert str(retained.value) == str(oracle.value)


@pytest.mark.parametrize("operation", ["replace", "delete"])
def test_cold_candidate_evaluation_reads_final_bytes_before_invalid_parent(operation) -> None:
    path = _path("anchor")
    base = SnapshotTree({path: b"not a Subject"})
    candidate = base.fork()
    if operation == "replace":
        candidate[path] = render_subject(_subject("anchor"))
    else:
        del candidate[path]
    sealed = candidate.snapshot()
    cache = EvaluationStateCache()
    assert cache.derive(sealed) == build_tree_state(dict(sealed))
    with pytest.raises(Exception) as oracle:
        build_tree_state(dict(base))
    with pytest.raises(type(oracle.value)) as retained:
        cache.derive(base)
    assert str(retained.value) == str(oracle.value)


def test_returned_evaluation_wrappers_models_and_merkle_nodes_cannot_poison_root() -> None:
    tree = SnapshotTree(_tree())
    cache = EvaluationStateCache()
    expected = build_tree_state(dict(tree))
    view = cache.derive(tree)
    view.dependencies.states[_path("anchor")].identity.__dict__["name"] = "forged"
    node = view.merkle.nodes[_path("anchor")]
    node.__dict__["member_digest"] = "forged"
    object.__setattr__(node.digest, "value", "0" * 64)
    object.__setattr__(view.merkle.root, "value", "1" * 64)
    object.__setattr__(view.dependencies.edge_tree.root, "value", "2" * 64)
    view.dependencies.__dict__["states"] = {}
    view.__dict__["members"] = {}
    assert cache.derive(tree) == expected
    sibling = tree.fork()
    sibling[_path("new")] = render_subject(_subject("new"))
    assert cache.derive(sibling.snapshot()) == build_tree_state(dict(sibling))
    assert cache.derive(tree) == expected


def test_warm_manifest_and_evaluation_updates_do_not_iterate_unrelated_roots(monkeypatch) -> None:
    rows = {
        _path(f"item-{number:03d}"): render_subject(_subject(f"item-{number:03d}"))
        for number in range(80)
    }
    base = SnapshotTree(rows)
    cache = EvaluationStateCache()
    state = cache.derive(base)
    path = _path("item-040")
    candidate = base.fork()
    candidate[path] = render_subject(_subject("item-040", revision=1))
    sealed = candidate.snapshot()
    expected = build_tree_state(dict(sealed))
    original_map_iter = PersistentMap.__iter__

    def no_tree_iteration(self):
        pytest.fail("a one-member warm update iterated a whole SnapshotTree")

    def bounded_map_iteration(self):
        if len(self) >= 80:
            pytest.fail("a one-member warm update iterated an unrelated PersistentMap root")
        return original_map_iter(self)

    with monkeypatch.context() as patch:
        patch.setattr(SnapshotTree, "__iter__", no_tree_iteration)
        patch.setattr(PersistentMap, "__iter__", bounded_map_iteration)
        advanced = advance_tree_members(state, previous_tree=base, tree=sealed)
        result = cache.derive(sealed)
    assert advanced.scope == (path,)
    assert advanced.members == expected.members
    assert advanced.merkle == expected.merkle
    assert result == expected
    assert cache.derive(base) == state


def test_accepted_advancement_reads_only_changed_blobs_and_retains_old_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    before = instance.accepted_coordinate()
    old = instance.immutable_tree_at(before.git_oid)
    previous = dict(old)
    changed_path = _path("delta")
    proposed = {**previous, changed_path: render_subject(_subject("delta"))}
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    intent = coordinator.create(
        actor=actor,
        payload=SubjectAuthoringPayloadV1(subject=_subject("delta")),
        canonical_timestamp=TIMESTAMP,
    ).intent
    _accept(instance, owner, coordinator, intent.intent_id, actor)
    after = instance.accepted_coordinate()
    expected = instance.tree_at(after.git_oid)
    changed = {
        path
        for path in previous.keys() | expected.keys()
        if previous.get(path) != expected.get(path)
    }
    reads = []
    original_blobs = instance._ledger.blobs_at

    def read_blobs(oid, paths):
        paths = tuple(paths)
        reads.append((oid, paths))
        return original_blobs(oid, paths)

    def no_whole_read(*args, **kwargs):
        pytest.fail("accepted advancement loaded the complete tree")

    with monkeypatch.context() as patch:
        patch.setattr(instance._ledger, "blobs_at", read_blobs)
        patch.setattr(instance._ledger, "read_tree", no_whole_read)
        patch.setattr(instance, "tree_at", no_whole_read)
        current = instance.immutable_tree_at(after.git_oid)
        assert instance.immutable_tree_at(before.git_oid) is old
        assert instance.immutable_tree_at(after.git_oid) is current
    assert len(reads) == 1 and reads[0][0] == after.git_oid
    assert set(reads[0][1]) == changed
    assert dict(current) == expected
    assert dict(old) == previous
    assert changed_path not in old and current[changed_path] == proposed[changed_path]


def test_history_does_not_disable_semantic_evaluation_retention(monkeypatch):
    from cruxible_core.derived import evaluation_state_cache as module

    base = _tree()
    physical = {**base, **{f"changesets/cs-{i:020d}.json": b"history" * 100 for i in range(30)}}
    snapshot = SnapshotTree(physical)
    cache = EvaluationStateCache(max_members=len(base), max_input_bytes=10_000)
    expected = cache.derive(snapshot)
    assert snapshot._evaluation is not None
    monkeypatch.setattr(
        module, "build_tree_state", lambda *_: pytest.fail("history disabled retention")
    )
    assert cache.derive(snapshot) == expected
    candidate = snapshot.fork()
    candidate["cards/unrelated.md"] = b"card"
    candidate["changesets/new.json"] = b"history"
    assert candidate.snapshot()._semantic_members == len(base)
    assert candidate.snapshot()._semantic_bytes == snapshot._semantic_bytes


@pytest.mark.parametrize("limit", [{"max_members": 0}, {"max_members": 1}, {"max_input_bytes": 1}])
def test_snapshot_evaluation_respects_retention_limits(limit):
    snapshot = SnapshotTree(_tree())
    cache = EvaluationStateCache(**limit)
    assert cache.derive(snapshot) == build_tree_state(snapshot)
    assert snapshot._evaluation is None


def test_fast_path_collision_retains_cold_refusal_bytes():
    from cruxible_core.proposals.proposals import advance_tree_members

    base = SnapshotTree({"presentation/a.json": b"{}"})
    state = build_tree_state(base)
    draft = base.fork()
    draft["presentation/A.json"] = b"{}"
    candidate = draft.snapshot()
    with pytest.raises(Exception) as cold:
        advance_tree_members(state, previous_tree=dict(base), tree=dict(candidate))
    with pytest.raises(type(cold.value)) as scoped:
        advance_tree_members(state, previous_tree=base, tree=candidate)
    assert str(scoped.value) == str(cold.value)


def test_external_nfc_input_keeps_cold_normalization_boundary():
    from cruxible_core.derived.derived_state import snapshot_against
    from cruxible_core.proposals.proposals import advance_tree_members

    base = SnapshotTree({})
    state = build_tree_state(base)
    external = {"presentation/cafe\u0301.json": b"{}"}
    candidate = snapshot_against(external, base)
    assert candidate.edits_from(base) is None
    assert advance_tree_members(state, previous_tree=base, tree=candidate) == (
        advance_tree_members(state, previous_tree={}, tree=external)
    )


def test_fork_and_seal_do_not_enumerate_for_resource_accounting(monkeypatch):
    from cruxible_client._persistent import PersistentMap

    base = SnapshotTree(_tree())

    def refuse(*_):
        pytest.fail("warm resource accounting enumerated the map")

    with monkeypatch.context() as guard:
        guard.setattr(PersistentMap, "__iter__", refuse)
        guard.setattr(PersistentMap, "items", refuse)
        draft = base.fork()
        draft[_path("new")] = b"new"
        del draft[_path("anchor")]
        sealed = draft.snapshot()
    expected = dict(base)
    expected[_path("new")] = b"new"
    del expected[_path("anchor")]
    assert sealed._input_bytes == sum(len(p.encode()) + len(b) for p, b in expected.items())
    assert sealed._semantic_members == len(expected)
