"""Incremental dependency-index maintenance against the from-scratch oracle.

Closure judging cannot tell whether the indexes it reads were built by parsing a
whole tree or carried forward from the parent generation and updated for the
members that moved. That is the whole safety argument for the incremental path,
and it is only worth anything if it is checked: every case below walks a seeded
sequence of change sets and asserts that the carried index equals the index a
from-scratch build produces, and that the judgement over carried indexes is
identical to the judgement over cold ones -- verdict, both refusal shapes, the
per-member proofs, and the committed edge root.
"""

from __future__ import annotations

import random

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.subjects import SubjectShell, render_subject, subject_digest
from cruxible_core.playbill.closure import (
    DependencyIndexV1,
    build_dependency_index,
    closure_evaluation_v2,
    closure_evaluation_v3,
    judge_dependency_closure,
    update_dependency_index,
)

_SEED = "playbill-incremental-closure-v1"


def _subject(
    name: str,
    *,
    revision: int = 0,
    retired: bool = False,
    pins: tuple[ArtifactPin, ...] = (),
) -> SubjectShell:
    return SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name=f"project.work_item/{name}"),
        subject_kind="project.work_item",
        subject_id=name,
        lifecycle=ArtifactLifecycle(
            state="retired" if retired else "live",
            predecessor_digest=(f"sha256:{revision:064x}" if revision else None),
        ),
        pins=pins,
    )


def _path(name: str) -> str:
    return f"subjects/project.work_item/{name}.yaml"


def _pin_to(shell: SubjectShell, *, role: str = "example-subject") -> ArtifactPin:
    return ArtifactPin(
        role=role,
        target=shell.identity,
        artifact_digest=subject_digest(shell).tagged,
    )


def _assert_same_index(carried: DependencyIndexV1, cold: DependencyIndexV1) -> None:
    assert dict(carried.states) == dict(cold.states)
    assert dict(carried.paths_by_identity) == dict(cold.paths_by_identity)
    assert dict(carried.sources_by_pinned_identity) == dict(cold.sources_by_pinned_identity)
    assert dict(carried.edges_by_source) == dict(cold.edges_by_source)
    assert dict(carried.edges_by_target) == dict(cold.edges_by_target)
    assert carried.edge_root == cold.edge_root
    assert carried.edge_tree.nodes == cold.edge_tree.nodes
    assert carried.edges() == cold.edges()


def _assert_same_judgement(
    *,
    carried_parent: DependencyIndexV1,
    carried_candidate: DependencyIndexV1,
    cold_parent: DependencyIndexV1,
    cold_candidate: DependencyIndexV1,
    scope: tuple[str, ...],
) -> None:
    incremental = judge_dependency_closure(
        parent=carried_parent,
        candidate=carried_candidate,
        scope=scope,
    )
    oracle = judge_dependency_closure(
        parent=cold_parent,
        candidate=cold_candidate,
        scope=scope,
    )
    assert incremental.verdict == oracle.verdict
    assert incremental.missing_dependents == oracle.missing_dependents
    assert incremental.unresolved_pins == oracle.unresolved_pins
    assert incremental.member_dependency_proofs == oracle.member_dependency_proofs
    assert closure_evaluation_v3(incremental) == closure_evaluation_v3(oracle)
    assert closure_evaluation_v2(incremental) == closure_evaluation_v2(oracle)


def _changed(previous: dict[str, bytes], current: dict[str, bytes]) -> tuple[str, ...]:
    return tuple(
        sorted({path for path in {*previous, *current} if previous.get(path) != current.get(path)})
    )


def _walk(steps: list[dict[str, bytes]]) -> None:
    """Replay one generation walk twice: carried forward, and from scratch."""

    carried = build_dependency_index(steps[0])
    for previous, current in zip(steps, steps[1:]):
        scope = _changed(previous, current)
        parent_carried = carried
        parent_cold = build_dependency_index(previous)
        carried = update_dependency_index(carried, tree=current, changed=scope)
        cold = build_dependency_index(current)
        _assert_same_index(carried, cold)
        if scope:
            _assert_same_judgement(
                carried_parent=parent_carried,
                carried_candidate=carried,
                cold_parent=parent_cold,
                cold_candidate=cold,
                scope=scope,
            )


def test_a_carried_index_matches_a_cold_one_through_every_member_transition() -> None:
    anchor = _subject("anchor")
    anchor_next = _subject("anchor", revision=1)
    dependent = _subject("dependent", pins=(_pin_to(anchor),))
    stale = _subject("dependent", pins=(_pin_to(anchor),), revision=1)
    repinned = _subject("dependent", pins=(_pin_to(anchor_next),), revision=1)
    retired_anchor = _subject("anchor", revision=1, retired=True)
    unrelated = _subject("unrelated")

    base = {
        _path("anchor"): render_subject(anchor),
        _path("dependent"): render_subject(dependent),
    }
    _walk(
        [
            base,
            # An unrelated member arrives: no edge anywhere moves.
            {**base, _path("unrelated"): render_subject(unrelated)},
            # The anchor's digest moves, which breaks an edge from a member that
            # did not itself change -- the case a source-only recomputation misses.
            {
                **base,
                _path("unrelated"): render_subject(unrelated),
                _path("anchor"): render_subject(anchor_next),
            },
            # The dependent re-pins the new digest and the edge comes back.
            {
                **base,
                _path("unrelated"): render_subject(unrelated),
                _path("anchor"): render_subject(anchor_next),
                _path("dependent"): render_subject(repinned),
            },
            # The anchor retires under a live dependent.
            {
                **base,
                _path("unrelated"): render_subject(unrelated),
                _path("anchor"): render_subject(retired_anchor),
                _path("dependent"): render_subject(repinned),
            },
            # The anchor leaves the tree entirely, taking its leaf with it. The
            # anchor is a pure target, so only its incoming edges are at stake.
            {
                _path("unrelated"): render_subject(unrelated),
                _path("dependent"): render_subject(stale),
            },
            # And comes back.
            {
                _path("unrelated"): render_subject(unrelated),
                _path("dependent"): render_subject(stale),
                _path("anchor"): render_subject(anchor),
            },
            # The dependent re-pins so there is a live edge to lose.
            {
                _path("unrelated"): render_subject(unrelated),
                _path("dependent"): render_subject(dependent),
                _path("anchor"): render_subject(anchor),
            },
            # Now remove the *source*. Its outgoing edges leave with it, and
            # nothing else in the change set mentions them -- so an update that
            # only re-resolves surviving members would carry a stale edge, and a
            # stale edge is a wrong committed graph root.
            {
                _path("unrelated"): render_subject(unrelated),
                _path("anchor"): render_subject(anchor),
            },
            # A member that is both a source and a target leaves at once.
            {
                _path("unrelated"): render_subject(unrelated),
                _path("anchor"): render_subject(anchor),
                _path("dependent"): render_subject(dependent),
                _path("middle"): render_subject(_subject("middle", pins=(_pin_to(dependent),))),
            },
            {
                _path("unrelated"): render_subject(unrelated),
                _path("anchor"): render_subject(anchor),
                _path("middle"): render_subject(_subject("middle", pins=(_pin_to(dependent),))),
            },
        ]
    )


def test_a_seeded_multi_generation_walk_never_diverges_from_the_oracle() -> None:
    rng = random.Random(_SEED)
    names = [f"m{index:02d}" for index in range(12)]
    revisions = dict.fromkeys(names, 0)
    live: set[str] = set(names[:6])
    shells: dict[str, SubjectShell] = {}

    def rebuild() -> dict[str, bytes]:
        shells.clear()
        # Unpinned members are materialized first so a pin always names a shell
        # whose exact bytes are already fixed for this generation.
        for name in sorted(live):
            shells[name] = _subject(name, revision=revisions[name])
        tree: dict[str, bytes] = {}
        for name in sorted(live):
            target = pinned.get(name)
            shell = shells[name]
            if target is not None and target in shells and target != name:
                shell = _subject(
                    name,
                    revision=revisions[name],
                    retired=name in retired,
                    pins=(_pin_to(shells[target]),),
                )
            elif name in retired:
                shell = _subject(name, revision=revisions[name], retired=True)
            shells[name] = shell
            tree[_path(name)] = render_subject(shell)
        return tree

    pinned: dict[str, str] = {}
    retired: set[str] = set()
    steps = [rebuild()]
    for _generation in range(40):
        # Every action is drawn against a member it can actually move, so the
        # walk is a sequence of real change sets rather than a run of no-ops.
        choices = ["revise", "pin", "retire"]
        if set(names) - live:
            choices.append("add")
        if len(live) > 2:
            choices.append("drop")
        if pinned:
            choices.append("unpin")
        if retired:
            choices.append("revive")
        action = rng.choice(choices)
        if action == "add":
            live.add(rng.choice(sorted(set(names) - live)))
        elif action == "drop":
            name = rng.choice(sorted(live))
            live.discard(name)
            pinned.pop(name, None)
        elif action == "revise":
            name = rng.choice(sorted(live))
            revisions[name] = revisions[name] + 1
        elif action == "pin":
            name = rng.choice(sorted(live))
            pinned[name] = rng.choice(sorted(live - {name})) if live - {name} else name
        elif action == "unpin":
            pinned.pop(rng.choice(sorted(pinned)), None)
        elif action == "retire":
            retired.add(rng.choice(sorted(live)))
        elif action == "revive":
            retired.discard(rng.choice(sorted(retired)))
        steps.append(rebuild())

    # The walk must actually exercise change, not a run of no-ops, and it must
    # reach states where an edge exists to break.
    assert sum(1 for a, b in zip(steps, steps[1:]) if a != b) >= 30
    assert any(build_dependency_index(step).edges() for step in steps)
    _walk(steps)
