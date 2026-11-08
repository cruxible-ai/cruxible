"""One overlay per change answers every retirement member's closure, exactly.

A change set's retirement stage judges member k on T_k: the tree the first
retirement member saw (T0) with members 0..k-1's retired successors written.
``RetirementClosureWalker`` walks T0 instead, excluding every identity an
earlier member retired (R). These randomized worlds stage the same members both
ways -- the per-member walk over T_k (``_walk_reverse_pin_closure`` with the
live inclusion ``claim_retirement_inventory`` uses) and the batched walk over
T0 -- and require identical closures and the same first refusal.

The worlds hold what the equality argument has to survive: cycles, non-Claim
nodes, provenance and backing-input roles, nodes retired at the base, roots
repeated or retired by an earlier member, shared dependents, and retired
successors whose own edges differ from their predecessors' (a successor's
historical input digests resolve differently once its path is edited).
"""

from __future__ import annotations

import os
import random
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_core.claims.claim_retirement import RetirementClosureWalker
from cruxible_core.claims.closure import ArtifactDependencyStateV1, _walk_reverse_pin_closure

SEEDS = int(os.environ.get("CRUXIBLE_RETIREMENT_WALK_SEEDS", "4000"))

Neighbors = Callable[[str], tuple[tuple[ArtifactDependencyStateV1, tuple[str, ...]], ...]]

#: Pin roles the worlds draw from: (claim, procedure) and (claim, line) are
#: provenance and never dependencies; the others are.
ROLES = ("input-claim", "subject", "claim-type", "procedure", "line")


def _digest(seed: int, index: int, version: int) -> str:
    return "sha256:" + f"{seed:016x}{index:08x}{version:08x}".ljust(64, "0")


@dataclass(frozen=True)
class _Node:
    index: int
    identity: ArtifactIdentity
    kind: str
    path: str


@dataclass
class _World:
    seed: int
    nodes: list[_Node]
    #: (source index, target index, role); "backing-input" is a consumed edge.
    edges: list[tuple[int, int, str]]
    base_retired: set[int]
    #: Sources listed as pinning a target with no role toward it (index damage).
    phantoms: set[tuple[int, int]]


def _world(seed: int) -> _World:
    rng = random.Random(seed)
    count = rng.randint(2, 14)
    nodes = []
    for index in range(count):
        claim = rng.random() < 0.8
        if claim:
            identity = ArtifactIdentity(kind="Claim", name=f"CLM-{index:032x}")
            nodes.append(_Node(index, identity, "claim", f"claims/CLM-{index:032x}.json"))
        else:
            identity = ArtifactIdentity(kind="Procedure", name=f"proc-{index}")
            nodes.append(_Node(index, identity, "procedure", f"procedures/proc-{index}.json"))
    edges: list[tuple[int, int, str]] = []
    density = rng.random()
    for source in range(count):
        for target in range(count):
            if source == target and rng.random() < 0.9:
                continue
            if rng.random() < density * 0.35:
                role = rng.choice(ROLES)
                edges.append((source, target, role))
                if rng.random() < 0.2:
                    edges.append((source, target, rng.choice(ROLES)))
            if (
                nodes[source].kind == "claim"
                and nodes[target].kind == "claim"
                and rng.random() < density * 0.2
            ):
                edges.append((source, target, "backing-input"))
    base_retired = {index for index in range(count) if rng.random() < 0.12}
    phantoms = {(rng.randrange(count), rng.randrange(count))} if rng.random() < 0.03 else set()
    return _World(seed, nodes, sorted(set(edges)), base_retired, phantoms)


class _States:
    """Each node's state at its base version and as a retired successor."""

    def __init__(self, world: _World) -> None:
        self.world = world
        self._cache: dict[tuple[int, bool], ArtifactDependencyStateV1] = {}

    def state(self, index: int, *, retired: bool) -> ArtifactDependencyStateV1:
        key = (index, retired or index in self.world.base_retired)
        held = self._cache.get(key)
        if held is None:
            node = self.world.nodes[index]
            version = 1 if retired else 0
            pins = tuple(
                sorted(
                    {
                        ArtifactPin(
                            role=role,
                            target=self.world.nodes[target].identity,
                            artifact_digest=_digest(self.world.seed, target, 0),
                        )
                        for source, target, role in self.world.edges
                        if source == index and role != "backing-input"
                    },
                    key=lambda pin: (pin.role, pin.target.qualified),
                )
            )
            held = ArtifactDependencyStateV1(
                path=node.path,
                artifact_kind=node.kind,  # type: ignore[arg-type]
                artifact_tag="test-artifact-v1",
                identity=node.identity,
                artifact_digest=_digest(self.world.seed, index, version),
                pins=pins,
                lifecycle=ArtifactLifecycle(state="retired" if key[1] else "live"),
            )
            self._cache[key] = held
        return held


def _neighbors(
    world: _World,
    states: _States,
    retired: frozenset[int],
    *,
    dropped: frozenset[tuple[int, int]] = frozenset(),
) -> Neighbors:
    """Reverse neighbors of one tree: the base with ``retired`` written as successors.

    ``dropped`` are (source, target) backing-input edges a retired successor no
    longer resolves; only a written path's rows are re-derived, so only nodes in
    ``retired`` may drop them.
    """

    by_identity = {node.identity.qualified: node.index for node in world.nodes}

    def neighbors(identity: str) -> tuple[tuple[ArtifactDependencyStateV1, tuple[str, ...]], ...]:
        target = by_identity[identity]
        roles: dict[int, set[str]] = {}
        for source, edge_target, role in world.edges:
            if edge_target != target:
                continue
            if role == "backing-input" and (source, target) in dropped:
                continue
            roles.setdefault(source, set()).add(role)
        for source, phantom_target in world.phantoms:
            if phantom_target == target:
                roles.setdefault(source, set())
        return tuple(
            (
                states.state(source, retired=source in retired),
                tuple(sorted(roles[source])),
            )
            for source in sorted(roles, key=lambda index: world.nodes[index].path)
        )

    return neighbors


def _live(state: ArtifactDependencyStateV1) -> bool:
    return state.lifecycle.state == "live"


def _shape(closure: tuple[object, ...]) -> tuple[object, ...]:
    return tuple(
        (
            item.state,  # type: ignore[attr-defined]
            item.triggering_identity.qualified,  # type: ignore[attr-defined]
            item.dependency_edge_roles,  # type: ignore[attr-defined]
        )
        for item in closure
    )


def _members(world: _World, rng: random.Random) -> list[tuple[int, int]]:
    """(root, perturbation seed) per member: roots may repeat or be non-Claims' dependents."""

    claims = [node.index for node in world.nodes if node.kind == "claim"]
    if not claims:
        return []
    return [(rng.choice(claims), rng.randrange(1 << 30)) for _ in range(rng.randint(1, 8))]


def _supplied(closure_claims: tuple[str, ...], perturbation: int, world: _World) -> set[str]:
    """What the member carries: usually the exact closure, sometimes one off."""

    rng = random.Random(perturbation)
    supplied = set(closure_claims)
    if rng.random() < 0.12:
        if supplied and rng.random() < 0.5:
            supplied.discard(rng.choice(sorted(supplied)))
        else:
            supplied.add(rng.choice(world.nodes).identity.qualified)
    return supplied


def _claim_target_cycle(world: _World, requests: set[int]) -> bool:
    """Whether the requests' Claim-target pins (non-backing) contain a cycle."""

    pending = set(requests)
    while pending:
        ready = [
            index
            for index in pending
            if not any(
                source == index
                and role != "backing-input"
                and target in pending
                and world.nodes[target].kind == "claim"
                for source, target, role in world.edges
            )
        ]
        if not ready:
            return True
        pending.discard(min(ready))
    return False


def _stage(
    world: _World, *, batched: bool, staged: list[set[int]] | None = None
) -> tuple[list[tuple[object, ...]], object]:
    """Stage every member; return the closures walked and the first refusal (or None).

    ``staged`` collects each successful member's retired node indices.
    """

    states = _States(world)
    rng = random.Random(world.seed ^ 0x5EED)
    members = _members(world, rng)
    by_identity = {node.identity.qualified: node.index for node in world.nodes}
    retired: set[int] = set()
    dropped: set[tuple[int, int]] = set()
    walker = RetirementClosureWalker(_neighbors(world, states, frozenset())) if batched else None
    walked: list[tuple[object, ...]] = []
    for position, (root, perturbation) in enumerate(members):
        root_state = states.state(root, retired=root in retired)
        # The staged-tree check both paths keep: the root must be live in T_k.
        if not _live(root_state):
            return walked, (position, "claim_terminal")
        try:
            if walker is not None:
                closure = walker.closure(root_state.identity)
            else:
                closure = _walk_reverse_pin_closure(
                    root_state.identity,
                    _live,
                    _neighbors(world, states, frozenset(retired), dropped=frozenset(dropped)),
                )
        except ValueError:
            return walked, (position, "missing_edge")
        walked.append(_shape(closure))
        if any(item.state.artifact_kind != "claim" for item in closure):
            return walked, (position, "closure_unsupported")
        closure_claims = tuple(item.state.identity.qualified for item in closure)
        supplied = _supplied(closure_claims, perturbation, world)
        if supplied != set(closure_claims):
            return walked, (position, "closure_incomplete")
        requests = {root, *(by_identity[identity] for identity in supplied)}
        if _claim_target_cycle(world, requests):
            return walked, (position, "claim_target_cycle")
        retired.update(requests)
        if staged is not None:
            staged.append(requests)
        # A retired successor's path is re-derived: some of its historical
        # backing inputs may no longer resolve (a perturbation of R's rows only).
        drop_rng = random.Random(perturbation ^ 0xD0)
        for source, target, role in world.edges:
            if source in requests and role == "backing-input" and drop_rng.random() < 0.5:
                dropped.add((source, target))
        if walker is not None:
            walker.record(world.nodes[index].identity.qualified for index in requests)
    return walked, None


@pytest.mark.parametrize("block", range(8))
def test_the_batched_walk_matches_the_per_member_walk_on_random_worlds(block: int) -> None:
    refusals: dict[str, int] = {}
    walked = 0
    for seed in range(block * SEEDS // 8, (block + 1) * SEEDS // 8):
        world = _world(seed)
        sequential = _stage(world, batched=False)
        batched = _stage(world, batched=True)
        assert sequential == batched, seed
        walked += len(sequential[0])
        if sequential[1] is not None:
            code = sequential[1][1]  # type: ignore[index]
            refusals[code] = refusals.get(code, 0) + 1
    # The worlds reach every outcome, so the equality is not vacuous.
    assert walked > SEEDS // 8
    assert {"claim_terminal", "closure_unsupported", "closure_incomplete"} <= set(refusals)


def test_an_excluded_node_with_a_role_less_row_is_a_boundary_in_both_walks() -> None:
    """Seed 18692: member 0 retires node 3, whose re-derived successor lost its
    backing-input edge to node 2 while an index row still lists it as pinning 2.
    Exclusion is judged before roles, so neither walk inspects node 3 and both
    refuse member 1 the same way (it carries its closure wrong)."""

    world = _world(18692)
    assert world.phantoms == {(3, 2)} and (3, 2, "backing-input") in world.edges
    sequential = _stage(world, batched=False)
    assert sequential == _stage(world, batched=True)
    assert sequential[1] == (1, "closure_incomplete")


def test_the_worlds_exercise_the_cases_the_argument_rests_on() -> None:
    """Earlier members' retirements bound later walks, and retire later roots."""

    terminal_after_retire = 0
    excluded_dependents = 0
    backing = 0
    for seed in range(SEEDS):
        world = _world(seed)
        members = _members(world, random.Random(seed ^ 0x5EED))
        staged: list[set[int]] = []
        walked, refusal = _stage(world, batched=True, staged=staged)
        if refusal is not None and refusal[1] == "claim_terminal":  # type: ignore[index]
            root = members[refusal[0]][0]  # type: ignore[index]
            terminal_after_retire += int(root not in world.base_retired)
        by_identity = {node.identity.qualified: node.index for node in world.nodes}
        for position, closure in enumerate(walked):
            backing += sum("backing-input" in item[2] for item in closure)  # type: ignore[index, operator]
            earlier = set().union(*staged[:position]) if position else set()
            expanded = {members[position][0]} | {
                by_identity[item[0].identity.qualified]  # type: ignore[index]
                for item in closure
            }
            # A node an earlier member retired that depends on what this one walks:
            # only the exclusion keeps it out of this member's closure.
            excluded_dependents += int(
                any(
                    source in earlier
                    and target in expanded
                    and role not in {"procedure", "line"}
                    and source not in world.base_retired
                    for source, target, role in world.edges
                )
            )
    assert terminal_after_retire > 50, terminal_after_retire
    assert excluded_dependents > 50, excluded_dependents
    assert backing > 50, backing
