"""A change set's retirements lower exactly as they did with one inventory per member.

``ChangeRetirementClosures`` answers every retirement member of one change set
from one overlay of the tree the first retirement saw. The oracle here is the
law it replaces: each member's closure from ``claim_retirement_inventory`` over
that member's own staged tree. Each seed lowers one randomized change set both
ways and requires identical proposed tree bytes, resolved authoring and
member-by-path, or the identical refusal (code, element, message, repairs).

The world: accepted status Claims and two accepted layers of derived Claims
over them (Claim-target input pins plus backing-input digests). Per seed: new
derived Claims over accepted ones (a retirement retires them as dependents in
the change that creates them), shared dependents, roots an earlier member
retires (``claim_terminal``), and dependents carried wrong on purpose. No
fixture artifact kind outside Claims pins a Claim, so a non-Claim dependent is
covered by the randomized walk test (tests/test_claims) only.
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import os
import random
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import cruxible_core.authoring.lowering as lowering
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.authoring.models import (
    ChangeSetAuthoringPayload,
    ClaimAuthoringPayload,
    ClaimDerivationBinding,
    ClaimRetirementMember,
    authoring_member_identity,
)
from cruxible_client.contracts.claims import (
    ClaimArtifact,
    ClaimRetireDependent,
    LiteralClaimObject,
    claim_artifact_digest,
    parse_claim,
)
from cruxible_core.authoring.lowering import AuthoringLoweringError, lower_authoring
from cruxible_core.claims.claim_retirement import (
    ChangeRetirementClosures,
    ClaimRetireInventoryItemV1,
    claim_retirement_inventory,
)
from cruxible_core.indexes.projection import AcceptedCoordinate
from tests.core_support._retirement_support import activate_submitted
from tests.test_authoring.test_authoring_preflight import _self_source_payload
from tests.test_authoring.test_derivation_admission import TIMESTAMP, derivation_world
from tests.test_claims.test_claim_retirement import _accept_historical_derivation_tree

pytestmark = pytest.mark.executor_only

SEEDS = int(os.environ.get("CRUXIBLE_RETIREMENT_ORACLE_SEEDS", "100"))
REASONS = ("was-rescinded", "was-wrong", "superseded")


class _PerMemberClosures:
    """Test-only oracle: today's per-member law over each member's staged tree."""

    def __init__(self, instance: Any, *, coordinate: AcceptedCoordinate) -> None:
        self._instance = instance
        self._coordinate = coordinate

    def inventory(
        self, *, tree: Any, claim: ClaimArtifact
    ) -> tuple[ClaimRetireInventoryItemV1, ...]:
        return claim_retirement_inventory(
            self._instance, tree=tree, coordinate=self._coordinate, claim=claim
        )

    def record(self, **_: object) -> None:
        return None

    def close(self) -> None:
        return None


class _Batched(ChangeRetirementClosures):
    """The production object, counting the members it answered from its one overlay."""

    walked = 0
    lowerings_walked = 0

    def inventory(
        self, *, tree: Any, claim: ClaimArtifact
    ) -> tuple[ClaimRetireInventoryItemV1, ...]:
        try:
            return super().inventory(tree=tree, claim=claim)
        finally:
            if self._walker is not None:
                type(self).walked += 1

    def close(self) -> None:
        if self._walker is not None:
            type(self).lowerings_walked += 1
        super().close()


def _pin(claim: ClaimArtifact) -> ArtifactPin:
    return ArtifactPin(
        role="input-claim",
        target=claim.identity,
        artifact_digest=claim_artifact_digest(claim).tagged,
    )


def _status(
    qualifier: str, value: str = "ready", revises: str | None = None
) -> ClaimAuthoringPayload:
    template = _self_source_payload()
    return template.model_copy(
        update={
            "statement": template.statement.model_copy(
                update={"qualifier": qualifier, "object": LiteralClaimObject(value=value)}
            ),
            "revises": revises,
        }
    )


def _derived(
    template: ClaimAuthoringPayload, qualifier: str, inputs: list[ClaimArtifact]
) -> ClaimAuthoringPayload:
    derivation = template.derivation
    assert derivation is not None
    return template.model_copy(
        update={
            "statement": template.statement.model_copy(update={"qualifier": qualifier}),
            "rationale": f"Derived {qualifier} from its inputs.",
            "derivation": ClaimDerivationBinding(
                procedure=derivation.procedure,
                inputs=tuple(
                    sorted(
                        (_pin(claim) for claim in inputs),
                        key=lambda pin: pin.target.qualified.encode("utf-8"),
                    )
                ),
            ),
        }
    )


def _change_set(*members: object) -> ChangeSetAuthoringPayload:
    return ChangeSetAuthoringPayload(
        members=tuple(  # type: ignore[arg-type]
            sorted(
                members,  # type: ignore[type-var]
                key=lambda member: authoring_member_identity(member).encode("utf-8"),  # type: ignore[arg-type]
            )
        )
    )


def _claims(instance: Any) -> dict[str, ClaimArtifact]:
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    return {
        path: parse_claim(content, path=path)
        for path, content in tree.items()
        if path.startswith("claims/")
    }


class _World:
    def __init__(self, root: Path) -> None:
        world = derivation_world(root)
        self.instance = world.instance
        self.owner = world.owner
        self.actor = world.actor
        # Deterministic Claim IDs: the world, and so every seed, is the same each run.
        minted = (
            "CLM-" + hashlib.sha256(f"oracle-{index}".encode()).hexdigest()[:32]
            for index in itertools.count()
        )
        self.coordinator = dataclasses.replace(
            world.coordinator, claim_id_factory=lambda: next(minted)
        )
        self.template: ClaimAuthoringPayload = world.payload
        assert self.template.derivation is not None
        self.reducer = self.template.derivation.procedure
        rng = random.Random(20261107)
        # Accepted status Claims, the live way.
        statuses = [_status(f"s{index}") for index in range(6)]
        created = self.coordinator.create(
            actor=self.actor, payload=_change_set(*statuses), canonical_timestamp=TIMESTAMP
        )
        submitted = self.coordinator.submit(created.intent.intent_id, actor=self.actor)
        activate_submitted(self.instance, self.owner, submitted)
        # Two layers of derived Claims, accepted as historical records.
        for layer, count in ((1, 5), (2, 4)):
            accepted = sorted(_claims(self.instance).values(), key=lambda c: c.identity.name)
            members = [
                _derived(
                    self.template,
                    f"d{layer}-{index}",
                    rng.sample(accepted, rng.randint(1, min(3, len(accepted)))),
                )
                for index in range(count)
            ]
            lowered = self._lower(_change_set(*members))
            _accept_historical_derivation_tree(self.instance, lowered.proposed_tree)
        self.claims = sorted(_claims(self.instance).values(), key=lambda c: c.identity.name)
        assert sum(bool(c.backing.input_claim_digests) for c in self.claims) == 9

    def intent(self, payload: ChangeSetAuthoringPayload) -> Any:
        return self.coordinator.create(
            actor=self.actor, payload=payload, canonical_timestamp=TIMESTAMP
        ).intent

    def _lower(self, payload: ChangeSetAuthoringPayload) -> Any:
        return lower_authoring(
            self.instance,
            intent=self.intent(payload),
            actor_id="owner",
            derivation_procedure=self.reducer,
        )


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_World]:
    from tests.support.procedures import executor_only_steps

    # The module fixture is built before the per-test executor_only fixture runs.
    with executor_only_steps():
        built = _World(tmp_path_factory.mktemp("retirement-oracle"))
    yield built


def _outcome(world: _World, intent: Any, closures: type, monkeypatch: pytest.MonkeyPatch) -> Any:
    with monkeypatch.context() as patch:
        patch.setattr(lowering, "ChangeRetirementClosures", closures)
        try:
            lowered = lower_authoring(
                world.instance,
                intent=intent,
                actor_id="owner",
                derivation_procedure=world.reducer,
            )
        except AuthoringLoweringError as error:
            return (
                "refused",
                error.code,
                error.offending_element,
                error.message,
                [repair.model_dump(mode="json") for repair in error.repairs],
            )
    return (
        "lowered",
        dict(lowered.proposed_tree.items()),
        lowered.resolved_authoring,
        lowered.member_by_path,
        lowered.changed_members,
    )


def _retirement(
    claim: ClaimArtifact, reason: str, dependents: tuple[ClaimRetireDependent, ...]
) -> ClaimRetirementMember:
    return ClaimRetirementMember(retires=claim.identity.name, reason=reason, dependents=dependents)  # type: ignore[arg-type]


def _dependents(required: list[dict[str, Any]], reason: str) -> tuple[ClaimRetireDependent, ...]:
    return tuple(
        sorted(
            (
                ClaimRetireDependent(
                    artifact_identity=ArtifactIdentity.model_validate(item["artifact_identity"]),
                    predecessor_digest=item["predecessor_digest"],
                    reason=reason,  # type: ignore[arg-type]
                )
                for item in required
            ),
            key=lambda item: item.artifact_identity.qualified.encode("utf-8"),
        )
    )


@pytest.mark.parametrize("seed", range(SEEDS))
def test_batched_retirement_closures_lower_as_per_member_inventories(
    world: _World, seed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _exercise_seed(world, seed, monkeypatch, _Batched)


def _exercise_seed(
    world: _World,
    seed: int,
    monkeypatch: pytest.MonkeyPatch,
    closures: type[_Batched],
) -> tuple[str, int | None]:
    rng = random.Random(seed)
    creates = [
        _derived(world.template, f"n{seed}-{index}", rng.sample(world.claims, rng.randint(1, 3)))
        for index in range(rng.randint(0, 3))
    ]
    if rng.random() < 0.3:
        creates.append(_status(f"n{seed}-status"))
    roots = rng.sample(world.claims, rng.randint(1, 5))
    reasons = {root.identity.name: rng.choice(REASONS) for root in roots}
    dependents: dict[str, tuple[ClaimRetireDependent, ...]] = {
        root.identity.name: () for root in roots
    }

    def payload() -> ChangeSetAuthoringPayload:
        return _change_set(
            *creates,
            *(
                _retirement(root, reasons[root.identity.name], dependents[root.identity.name])
                for root in roots
            ),
        )

    # One intent per seed (its minted Claim IDs fixed); repairs only rewrite dependents.
    intent = world.intent(payload())
    names = [
        member.retires for member in payload().members if isinstance(member, ClaimRetirementMember)
    ]
    # Carry each member's closure as the oracle demands it, member by member.
    for _attempt in range(len(roots) + 1):
        outcome = _outcome(
            world, intent.model_copy(update={"payload": payload()}), _PerMemberClosures, monkeypatch
        )
        if outcome[1] != "cruxible.authoring.claim_retirement_closure_incomplete":
            break
        index = int(outcome[2].split("[", 1)[1].split("]", 1)[0])
        member = payload().members[index]
        assert isinstance(member, ClaimRetirementMember)
        replacement = outcome[4][0]["replacement"]
        dependents[member.retires] = _dependents(
            replacement["required_dependents"], reasons[member.retires]
        )
    # Then, sometimes, carry one member's closure wrong.
    if rng.random() < 0.25:
        name = rng.choice(names)
        carried = list(dependents[name])
        if carried and rng.random() < 0.5:
            carried.pop(rng.randrange(len(carried)))
        else:
            extra = rng.choice(world.claims)
            carried.append(
                ClaimRetireDependent(
                    artifact_identity=extra.identity,
                    predecessor_digest=claim_artifact_digest(extra).tagged,
                    reason=reasons[name],  # type: ignore[arg-type]
                )
            )
        dependents[name] = tuple(
            sorted(set(carried), key=lambda item: item.artifact_identity.qualified.encode("utf-8"))
        )
    final = intent.model_copy(update={"payload": payload()})
    expected = _outcome(world, final, _PerMemberClosures, monkeypatch)
    batched = _outcome(world, final, closures, monkeypatch)
    assert batched == expected, (seed, expected[:2], batched[:2])
    if expected[0] == "lowered":
        retired = [
            path
            for path, content in expected[4]
            if path.startswith("claims/") and parse_claim(content, path=path).retirement
        ]
        return "lowered", len(retired)
    return expected[1], None


def test_the_seeds_exercised_the_batched_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Own world, seeds and counters: works alone or on any xdist worker."""

    class Coverage(_Batched):
        walked = 0
        lowerings_walked = 0

    world = _World(tmp_path)
    outcomes: Counter[str] = Counter()
    retired: list[int] = []
    coverage_seeds = 32
    for seed in range(coverage_seeds):
        outcome, count = _exercise_seed(world, seed, monkeypatch, Coverage)
        outcomes[outcome] += 1
        if count is not None:
            retired.append(count)
    print(dict(outcomes), sorted(retired))
    assert {
        "lowered",
        "cruxible.authoring.claim_terminal",
        "cruxible.authoring.claim_retirement_closure_incomplete",
    } <= set(outcomes), outcomes
    assert max(retired) >= 8, retired
    assert Coverage.lowerings_walked >= coverage_seeds // 2, Coverage.lowerings_walked
    assert Coverage.walked >= 2 * coverage_seeds, Coverage.walked
