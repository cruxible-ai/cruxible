# ruff: noqa: E501  (the oracle below is the pre-fix loop, kept verbatim)
"""The dependent closure settles in one dependency-ordered pass, with the same result.

The successor loop used to restart a sorted scan of the whole remainder after
every member and rebuild a merged dict per member, which is quadratic in the
closure. It is now one topological pass with the same tie-break (smallest
identity first among the settleable), so its output must be byte-identical to
the old loop's. The old loop is kept below verbatim as the oracle.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.claim_types import (
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
    render_claim_type,
)
from cruxible_client.contracts.query.definitions import (
    query_definition_path,
    render_query_definition,
)
from cruxible_core.claims import claim_type_migrations as migrations
from tests.core_support._adoption_fixture import _query_definition
from tests.core_support._knowledge_loop_support import PREDICATE, seed_claims


def _quadratic_closure_candidate(
    *,
    tree: Mapping[str, bytes],
    changed: Mapping[str, bytes],
    inventory: tuple[migrations.ClaimTypeMigrationInventoryItemV1, ...],
    dispositions: tuple[migrations.ClaimTypeDependentDisposition, ...],
) -> tuple[dict[str, bytes], tuple[migrations.ClaimTypeMigrationDispositionV3, ...]]:
    """The pre-fix loop, verbatim: the oracle the one-pass order must match.

    Settle the closure of several changed definitions as one generation.

    The multi-root form of `build_claim_type_migration_candidate`, with the
    same per-dependent law: every dependent is read at its ACCEPTED bytes, so
    each takes exactly one successor naming its accepted digest, however many
    changed definitions it pins; its pins move to the final bytes in
    ``changed`` and to the successors of dependents settled before it.
    """

    by_identity = {item.identity.qualified: item for item in inventory}
    supplied = {item.identity.qualified: item for item in dispositions}
    if set(by_identity) != set(supplied):
        missing = sorted(set(by_identity) - set(supplied), key=lambda item: item.encode("utf-8"))
        extra = sorted(set(supplied) - set(by_identity), key=lambda item: item.encode("utf-8"))
        raise migrations.ClaimTypeMigrationDependentSetMismatch(
            f"{migrations.ClaimTypeMigrationDependentSetMismatch.code}: missing={missing!r}; "
            f"extra_or_stale={extra!r}"
        )
    replacements: dict[str, str] = {}
    for path, content in changed.items():
        before = tree.get(path)
        after = migrations.parse_dependency_artifact(path, content)
        if before is None or after is None:
            continue
        current = migrations.parse_dependency_artifact(path, before)
        if current is not None and current.artifact_digest != after.artifact_digest:
            replacements[current.artifact_digest] = after.artifact_digest
    writes: dict[str, bytes] = {}
    normalized: dict[str, migrations.ClaimTypeMigrationDispositionV3] = {}
    remaining = set(by_identity)
    while remaining:
        progressed = False
        for identity in sorted(remaining, key=lambda item: item.encode("utf-8")):
            row = by_identity[identity]
            current = migrations.parse_dependency_artifact(row.path, tree[row.path])
            if current is None:
                raise migrations.ClaimTypeMigrationIncomplete(
                    f"{migrations.ClaimTypeMigrationIncomplete.code}: inventory member disappeared"
                )
            if {pin.target.qualified for pin in current.pins}.intersection(remaining):
                continue
            entry = supplied[identity]
            disposition: migrations.MigrationResultDisposition = (
                "retire" if entry.disposition == "invalidation" else entry.disposition
            )
            if disposition not in row.permitted_dispositions:
                raise migrations.ClaimTypeMigrationDependentInvalid(
                    f"{migrations.ClaimTypeMigrationDependentInvalid.code}: {identity} does not permit "
                    f"{disposition}"
                )
            if migrations.reference_fields(row.path) is None:
                raise migrations.ClaimTypeMigrationDependentInvalid(
                    f"{migrations.ClaimTypeMigrationDependentInvalid.code}: {identity} cannot be re-pinned "
                    "automatically; settle it through its own change first"
                )
            successor_type = migrations._final_claim_type(
                tree, {**changed, **writes}, row.path, current
            )
            content = migrations._canonical_successor_bytes(
                current=current,
                content=tree[row.path],
                disposition=disposition,
                replacements=replacements,
                supplied=entry.successor,
                successor_type=successor_type,
                claim_retirement_reason=entry.claim_retirement_reason,
                claim_effective_until=entry.claim_effective_until,
                typed_references=True,
            )
            writes[row.path] = content
            successor_state = migrations.parse_dependency_artifact(row.path, content)
            if successor_state is None:
                raise migrations.ClaimTypeMigrationIncomplete(
                    f"{migrations.ClaimTypeMigrationIncomplete.code}: successor did not parse"
                )
            replacements[current.artifact_digest] = successor_state.artifact_digest
            normalized[identity] = migrations.ClaimTypeMigrationDispositionV3(
                identity=current.identity,
                disposition=disposition,
                claim_retirement_reason=entry.claim_retirement_reason,
                claim_effective_until=entry.claim_effective_until,
            )
            remaining.remove(identity)
            progressed = True
            break
        if not progressed:
            raise migrations.ClaimTypeMigrationIncomplete(
                f"{migrations.ClaimTypeMigrationIncomplete.code}: dependent closure contains a cycle"
            )
    return writes, tuple(
        normalized[identity]
        for identity in sorted(normalized, key=lambda item: item.encode("utf-8"))
    )


def _old_order(pins: Mapping[str, set[str]]) -> list[str]:
    remaining = set(pins)
    order: list[str] = []
    while remaining:
        for identity in sorted(remaining, key=lambda item: item.encode("utf-8")):
            if pins[identity] & remaining:
                continue
            order.append(identity)
            remaining.remove(identity)
            break
        else:
            raise migrations.ClaimTypeMigrationIncomplete("cycle")
    return order


def _synthetic(monkeypatch: pytest.MonkeyPatch, pins: Mapping[str, set[str]]) -> list[str]:
    by_identity = {
        identity: SimpleNamespace(path=f"claims/{index}.json")
        for index, identity in enumerate(pins)
    }
    states = {
        row.path: SimpleNamespace(
            pins=tuple(
                SimpleNamespace(target=SimpleNamespace(qualified=target))
                for target in sorted(pins[identity])
            )
        )
        for identity, row in by_identity.items()
    }
    monkeypatch.setattr(
        migrations, "parse_dependency_artifact", lambda path, _content: states[path]
    )
    tree = {row.path: b"" for row in by_identity.values()}
    return [
        identity
        for identity, _state in migrations._dependency_order(tree, by_identity)  # type: ignore[arg-type]
    ]


@pytest.mark.parametrize("seed", range(40))
def test_the_one_pass_order_is_the_old_scan_order(
    monkeypatch: pytest.MonkeyPatch, seed: int
) -> None:
    rng = random.Random(seed)
    names = [f"Claim:CLM-{rng.getrandbits(64):032x}" for _ in range(rng.randint(2, 30))]
    # A DAG with many shared dependents and ties: each member pins some earlier ones,
    # plus targets outside the closure, which never hold a member back.
    pins = {
        name: set(rng.sample(names[:index], rng.randint(0, min(index, 4))))
        | {"ClaimType:outside.closure"}
        for index, name in enumerate(names)
    }

    assert _synthetic(monkeypatch, pins) == _old_order(pins)


def test_a_cycle_is_refused_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    pins = {"Claim:a": {"Claim:b"}, "Claim:b": {"Claim:a"}, "Claim:c": set()}

    with pytest.raises(migrations.ClaimTypeMigrationIncomplete, match="contains a cycle"):
        _synthetic(monkeypatch, pins)


def test_a_multi_dependent_closure_settles_byte_identically(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = claim_type_path(PREDICATE)
    current = parse_claim_type(tree[path], path=path)
    for index in (1, 2, 3):
        query = _query_definition(index, current)
        tree[query_definition_path(query.identity.name)] = render_query_definition(query)
    successor = current.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(predecessor_digest=claim_type_digest(current).tagged)
        }
    )
    changed = {path: render_claim_type(successor)}
    inventory = migrations.dependent_closure_inventory(
        tree, roots=(current.identity,), fixed_paths=frozenset(changed)
    )
    assert len(inventory) >= 4  # two Claims and three QueryDefinitions
    dispositions = tuple(
        migrations.ClaimTypeDependentDisposition(identity=item.identity, disposition="successor")
        for item in inventory
    )

    fixed = migrations.build_dependent_closure_candidate(
        tree=tree, changed=changed, inventory=inventory, dispositions=dispositions
    )
    oracle = _quadratic_closure_candidate(
        tree=tree, changed=changed, inventory=inventory, dispositions=dispositions
    )

    assert fixed == oracle
    assert set(fixed[0]) == {item.path for item in inventory}
