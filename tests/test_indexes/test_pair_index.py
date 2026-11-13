"""The pair index (R12): ``pair_ends``, ``pair_slot_live`` and ``pair_units``.

Each pair Subject's ends are written once, at its creation; each pair slot
holding a live non-key Claim is counted; each such slot is listed under every
end. Upkeep applies the difference of live counts between each touched
Claim's row before and after the accept, reading ends only from ``pair_ends``
[F8, F9]. A full build of the same tree yields the same rows, after any
sequence of creations, revisions, retirements and nones, and an obsolete
projection (storage v31) rebuilds from authority to these rows.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.claims import ClaimArtifact, LiteralClaimObject, NoneClaimObject
from cruxible_client.contracts.subject_kinds import render_subject_kind, subject_kind_digest
from cruxible_client.contracts.subject_kinds import subject_kind_path as kind_path
from tests.core_support._pair_support import (
    COMPONENT,
    E_COMPONENT,
    E_VULN,
    EXPOSURE,
    KINDS,
    NOTE,
    O_FACT,
    O_OWNER,
    O_SERIAL,
    OBSERVATION_TABLES,
    OWNED,
    SCOPE,
    VULN,
    claim,
    definitions,
    end,
    exposure,
    literal,
    pair_rows,
    path_of,
    projection,
    publish,
    put,
    retired_claim,
    retired_shell,
    shell,
    successor_kind,
    to,
)

C1 = path_of(COMPONENT, "c1")
V1 = path_of(VULN, "v1")
E11 = path_of(EXPOSURE, "c1--v1")
ENDS = [
    (E11, E_COMPONENT, C1),
    (E11, E_VULN, V1),
]


class World:
    """A tree and its incrementally maintained projection, compared to a full build."""

    def __init__(self, tree: dict[str, bytes]) -> None:
        self.tree = dict(tree)
        self.connection = projection(self.tree)

    def accept(self, *items: Any) -> dict[str, list[tuple[object, ...]]]:
        changes = put({}, *items)
        publish(self.connection, changes, changed=changes)
        self.tree.update(changes)
        rows = pair_rows(self.connection)
        cold = projection(self.tree)
        try:
            assert rows == pair_rows(cold), "incremental pair index differs from a full build"
            assert pair_rows(self.connection, OBSERVATION_TABLES) == pair_rows(
                cold, OBSERVATION_TABLES
            ), "incremental unobserved-end tables differ from a full build"
        finally:
            cold.close()
        return rows


def _ends() -> dict[str, bytes]:
    tree = definitions()
    put(tree, *end(COMPONENT, "c1", 1), *end(VULN, "v1", 3))
    return tree


def _units(predicate: str, count: int | None) -> dict[str, list[tuple[object, ...]]]:
    return {
        "pair_ends": sorted(ENDS, key=repr),
        "pair_slot_live": [] if count is None else [(E11, predicate, count)],
        "pair_units": []
        if count is None
        else sorted(((C1, E_COMPONENT, predicate, E11), (V1, E_VULN, predicate, E11)), key=repr),
    }


def test_every_maintenance_case_follows_the_live_count() -> None:
    world = World(_ends())
    # A pair alone: its ends, no slot.
    assert world.accept(*exposure("c1", "v1", 5)) == _units(SCOPE, None)
    # create: +1 (0 -> 1 inserts the units).
    first = claim(10, SCOPE, literal("required"), subject=E11)
    assert world.accept(first) == _units(SCOPE, 1)
    # revise (a new Claim, the old retired, one change): 0.
    second = claim(11, SCOPE, literal("optional"), subject=E11)
    assert world.accept(second, retired_claim(first)) == _units(SCOPE, 1)
    # retire: -1 (1 -> 0 deletes the slot and its units by full key).
    assert world.accept(retired_claim(second)) == _units(SCOPE, None)
    # inverse/none: an ordinary Claim of the pair, counted like a positive.
    none = claim(12, SCOPE, NoneClaimObject(), subject=E11)
    assert world.accept(none) == _units(SCOPE, 1)
    assert world.accept(retired_claim(none)) == _units(SCOPE, None)
    # several live holders: the count stays > 0 until the last one retires.
    notes = [claim(20 + index, NOTE, literal(f"n{index}"), subject=E11) for index in range(3)]
    assert world.accept(*notes) == _units(NOTE, 3)
    assert world.accept(retired_claim(notes[0])) == _units(NOTE, 2)
    assert world.accept(retired_claim(notes[1])) == _units(NOTE, 1)
    assert world.accept(retired_claim(notes[2])) == _units(NOTE, None)


def test_a_retired_pair_with_its_claims_leaves_no_unit_and_keeps_its_ends() -> None:
    """[F9] (a): the 1 -> 0 cleanup reads the ends from ``pair_ends``, not ``subject_keys``."""

    world = World(_ends())
    pair, component, vuln = exposure("c1", "v1", 5)
    world.accept(pair, component, vuln)
    fact = claim(10, SCOPE, literal("required"), subject=E11)
    world.accept(fact)
    rows = world.accept(
        retired_shell(pair), retired_claim(component), retired_claim(vuln), retired_claim(fact)
    )
    assert rows == _units(SCOPE, None)
    assert world.connection.execute(
        "SELECT count(*) FROM subject_keys WHERE subject_path=?", (E11,)
    ).fetchone() == (0,)


def test_a_grandfathered_pair_indexes_a_new_fact_from_its_ends() -> None:
    """[F9] (b): the pair kind retired, a still-live pair's 0 -> 1 reads ``pair_ends``."""

    world = World(_ends())
    world.accept(*exposure("c1", "v1", 5))
    retiring = successor_kind(
        KINDS[EXPOSURE],
        lifecycle=ArtifactLifecycle(
            state="retired", predecessor_digest=subject_kind_digest(KINDS[EXPOSURE]).tagged
        ),
    )
    changes = {kind_path(EXPOSURE): render_subject_kind(retiring)}
    publish(world.connection, changes, changed=changes)
    world.tree.update(changes)
    assert world.connection.execute(
        "SELECT count(*) FROM subject_keys WHERE subject_path=?", (E11,)
    ).fetchone() == (0,)
    assert world.accept(claim(10, SCOPE, literal("required"), subject=E11)) == _units(SCOPE, 1)


def test_a_pair_and_its_first_fact_land_in_one_change() -> None:
    """[F9] (c): ``pair_ends`` is written first in the pass."""

    world = World(_ends())
    rows = world.accept(
        *exposure("c1", "v1", 5), claim(10, SCOPE, literal("required"), subject=E11)
    )
    assert rows == _units(SCOPE, 1)


def test_a_mixed_key_indexes_only_its_subject_part() -> None:
    """[F11] (e): a literal part is in ``pair_ends`` (end NULL), never a unit or a fact."""

    world = World(_ends())
    owned = path_of(OWNED, "c1--s1")
    owner = claim(60, O_OWNER, to(C1), subject=owned)
    serial = claim(61, O_SERIAL, literal("s1"), subject=owned)
    rows = world.accept(shell(OWNED, "c1--s1"), owner, serial)
    owned_ends = sorted([(owned, O_OWNER, C1), (owned, O_SERIAL, None)], key=repr)
    assert rows == {"pair_ends": owned_ends, "pair_slot_live": [], "pair_units": []}
    fact = claim(62, O_FACT, literal("on"), subject=owned)
    rows = world.accept(fact)
    assert rows == {
        "pair_ends": owned_ends,
        "pair_slot_live": [(owned, O_FACT, 1)],
        "pair_units": [(C1, O_OWNER, O_FACT, owned)],
    }
    # A literal re-key of serial changes no fact rows.
    after_rekey = world.accept(
        retired_claim(serial), claim(63, O_SERIAL, literal("s2"), subject=owned)
    )
    assert after_rekey == rows
    assert world.accept(retired_claim(fact)) == {
        "pair_ends": owned_ends,
        "pair_slot_live": [],
        "pair_units": [],
    }


def test_a_kind_made_a_pair_kind_while_it_had_no_subject_builds_alike() -> None:
    """[F12] (f): the fresh kind adds its first Subject part, then pairs are created."""

    from tests.core_support._pair_support import keyed

    tree = _ends()
    literal_owned = keyed(OWNED, O_SERIAL)
    tree[kind_path(OWNED)] = render_subject_kind(literal_owned)
    world = World(tree)
    paired = successor_kind(literal_owned, identity_rule=KINDS[OWNED].identity_rule)
    changes = {kind_path(OWNED): render_subject_kind(paired)}
    publish(world.connection, changes, changed=changes)
    world.tree.update(changes)
    owned = path_of(OWNED, "c1--s1")
    rows = world.accept(
        shell(OWNED, "c1--s1"),
        claim(60, O_OWNER, to(C1), subject=owned),
        claim(61, O_SERIAL, literal("s1"), subject=owned),
        claim(62, O_FACT, literal("on"), subject=owned),
    )
    assert rows["pair_units"] == [(C1, O_OWNER, O_FACT, owned)]


def test_the_permitted_first_part_transitions_build_alike() -> None:
    """Review F1: a retiring successor adding a part, and a ClaimType-only conversion, of a
    kind with no Subject: the incremental index equals a full build (both empty of it)."""

    from cruxible_client.contracts.claim_types import (
        claim_type_digest,
        claim_type_path,
        render_claim_type,
    )
    from cruxible_client.contracts.shapes import coerce_field_type
    from tests.core_support._pair_support import TYPES, keyed

    tree = _ends()
    literal_owned = keyed(OWNED, O_SERIAL)
    tree[kind_path(OWNED)] = render_subject_kind(literal_owned)
    world = World(tree)
    retiring = successor_kind(
        literal_owned,
        identity_rule=KINDS[OWNED].identity_rule,
        lifecycle=ArtifactLifecycle(
            state="retired", predecessor_digest=subject_kind_digest(literal_owned).tagged
        ),
    )
    changes = {kind_path(OWNED): render_subject_kind(retiring)}
    publish(world.connection, changes, changed=changes)
    world.tree.update(changes)
    previous = TYPES[O_SERIAL]
    converted = previous.model_copy(
        update={
            "value": coerce_field_type({"kind": "subject", "subject_kinds": (VULN,)}),
            "lifecycle": ArtifactLifecycle(predecessor_digest=claim_type_digest(previous).tagged),
        }
    )
    changes = {claim_type_path(O_SERIAL): render_claim_type(converted)}
    publish(world.connection, changes, changed=changes)
    world.tree.update(changes)
    rows = pair_rows(world.connection)
    cold = projection(world.tree)
    try:
        assert rows == pair_rows(cold)
    finally:
        cold.close()
    assert rows == {"pair_ends": [], "pair_slot_live": [], "pair_units": []}


def test_full_build_equals_incremental_after_randomized_sequences() -> None:
    for seed in range(6):
        _randomized(random.Random(seed))


def _randomized(rng: random.Random) -> None:
    tree = definitions()
    components, vulns = ("c0", "c1", "c2"), ("v0", "v1", "v2")
    number = 1
    for subject_id in components:
        put(tree, *end(COMPONENT, subject_id, number))
        number += 1
    for subject_id in vulns:
        put(tree, *end(VULN, subject_id, number))
        number += 1
    world = World(tree)
    pairs: dict[str, tuple[Any, ...]] = {}  # live pair path -> (shell, endpoints...)
    live: dict[str, list[ClaimArtifact]] = {}  # pair path -> its live facts

    def fact(pair: str) -> ClaimArtifact:
        nonlocal number
        number += 1
        predicate = rng.choice((SCOPE, NOTE, NOTE))
        obj: Any = (
            NoneClaimObject()
            if predicate == SCOPE and rng.random() < 0.2
            else LiteralClaimObject(value=f"x{number}")
        )
        return claim(10_000 + number, predicate, obj, subject=pair)

    for _step in range(30):
        op = rng.choice(("create", "create_with", "fact", "retire", "revise", "drop"))
        free = [
            (c, v)
            for c in components
            for v in vulns
            if path_of(EXPOSURE, f"{c}--{v}") not in world.tree
        ]
        if op.startswith("create") and free:
            c, v = rng.choice(free)
            made = exposure(c, v, 10_000 + number + 1)
            number += 2
            pair = path_of(EXPOSURE, f"{c}--{v}")
            items: list[Any] = list(made)
            facts = [fact(pair)] if op == "create_with" else []
            pairs[pair] = made
            live[pair] = facts
            world.accept(*items, *facts)
        elif not pairs:
            continue
        elif op == "fact":
            pair = rng.choice(sorted(pairs))
            new = [fact(pair) for _ in range(rng.randint(1, 3))]
            live[pair].extend(new)
            world.accept(*new)
        elif op == "retire" and any(live.values()):
            pair = rng.choice(sorted(item for item in live if live[item]))
            gone = live[pair].pop(rng.randrange(len(live[pair])))
            world.accept(retired_claim(gone))
        elif op == "revise" and any(live.values()):
            pair = rng.choice(sorted(item for item in live if live[item]))
            gone = live[pair].pop(rng.randrange(len(live[pair])))
            new = fact(pair)
            live[pair].append(new)
            world.accept(retired_claim(gone), new)
        elif op == "drop":
            pair = rng.choice(sorted(pairs))
            made = pairs.pop(pair)
            facts = live.pop(pair)
            world.accept(
                retired_shell(made[0]),
                *(retired_claim(item) for item in made[1:]),
                *(retired_claim(item) for item in facts),
            )
    rows = pair_rows(world.connection)
    expected_slots = sorted(
        (
            (pair, predicate, count)
            for pair, facts in live.items()
            for predicate, count in _counts(facts).items()
        ),
        key=repr,
    )
    assert rows["pair_slot_live"] == expected_slots


def _counts(facts: list[ClaimArtifact]) -> dict[str, int]:
    found: dict[str, int] = {}
    for item in facts:
        found[item.statement.predicate] = found.get(item.statement.predicate, 0) + 1
    return found


# -- unobserved ends: next's subject-end gate (``pair_end_unobserved``) -------------------


def _unobserved(connection: Any) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute("SELECT end_path FROM pair_end_unobserved ORDER BY 1")
    ]


def test_unobserved_ends_follow_observations_and_pair_facts_after_randomized_sequences() -> None:
    """An end is listed exactly while it ends a pair holding a live fact, has no live
    observation (a non-key fact about it, ``NOTE`` here) and has a retired one; the
    incremental tables equal a full build at every step."""

    for seed in range(6):
        _randomized_observations(random.Random(seed))


def _randomized_observations(rng: random.Random) -> None:
    tree = definitions()
    number = 1
    ends: dict[str, list[ClaimArtifact]] = {}  # end path -> its live observations
    for kind, subject_id in (
        (COMPONENT, "c0"),
        (COMPONENT, "c1"),
        (COMPONENT, "c2"),
        (VULN, "v0"),
        (VULN, "v1"),
    ):
        put(tree, *end(kind, subject_id, number))
        ends[path_of(kind, subject_id)] = []
        number += 1
    pairs: dict[str, tuple[str, str]] = {}  # pair path -> its two ends
    for component in ("c0", "c1", "c2"):
        for vuln in ("v0", "v1"):
            put(tree, *exposure(component, vuln, number))
            number += 2
            pairs[path_of(EXPOSURE, f"{component}--{vuln}")] = (
                path_of(COMPONENT, component),
                path_of(VULN, vuln),
            )
    world = World(tree)
    facts: dict[str, list[ClaimArtifact]] = {pair: [] for pair in pairs}
    retired: dict[str, int] = dict.fromkeys(ends, 0)

    def fresh(subject: str) -> ClaimArtifact:
        nonlocal number
        number += 1
        return claim(10_000 + number, NOTE, literal(f"x{number}"), subject=subject)

    for _step in range(30):
        op = rng.choice(("fact", "unfact", "observe", "unobserve", "revise"))
        held = sorted(item for item in ends if ends[item])
        if op == "fact":
            pair = rng.choice(sorted(pairs))
            facts[pair].append(fresh(pair))
            world.accept(facts[pair][-1])
        elif op == "unfact" and any(facts.values()):
            pair = rng.choice(sorted(item for item in facts if facts[item]))
            world.accept(retired_claim(facts[pair].pop(rng.randrange(len(facts[pair])))))
        elif op == "observe":
            subject = rng.choice(sorted(ends))
            ends[subject].append(fresh(subject))
            world.accept(ends[subject][-1])
        elif op in ("unobserve", "revise") and held:
            subject = rng.choice(held)
            gone = retired_claim(ends[subject].pop(rng.randrange(len(ends[subject]))))
            retired[subject] += 1
            if op == "revise":
                ends[subject].append(fresh(subject))
                world.accept(gone, ends[subject][-1])
            else:
                world.accept(gone)
        expected = sorted(
            item
            for item in ends
            if not ends[item]
            and retired[item]
            and any(facts[pair] for pair, both in pairs.items() if item in both)
        )
        assert _unobserved(world.connection) == expected


def test_a_kind_change_recounts_its_subjects_observations_alike() -> None:
    """A SubjectKind the batch writes is recounted under its current key parts."""

    tree = _ends()
    put(tree, *exposure("c1", "v1", 10))
    world = World(tree)
    world.accept(claim(20, SCOPE, literal("required"), subject=E11))
    observation = claim(21, NOTE, literal("seen"), subject=C1)
    world.accept(observation)
    world.accept(retired_claim(observation))
    assert _unobserved(world.connection) == [C1]
    retiring = successor_kind(
        KINDS[COMPONENT],
        lifecycle=ArtifactLifecycle(
            state="retired", predecessor_digest=subject_kind_digest(KINDS[COMPONENT]).tagged
        ),
    )
    changes = {kind_path(COMPONENT): render_subject_kind(retiring)}
    publish(world.connection, changes, changed=changes)
    world.tree.update(changes)
    cold = projection(world.tree)
    try:
        assert pair_rows(world.connection, OBSERVATION_TABLES) == pair_rows(
            cold, OBSERVATION_TABLES
        )
    finally:
        cold.close()


# -- storage v31 -> v32: the obsolete projection rebuilds from authority -----------------


def _pair_tables(connection: Any) -> dict[str, list[tuple[object, ...]]]:
    return {
        table: sorted(
            (tuple(row) for row in connection.execute(f"SELECT * FROM {table}")), key=repr
        )
        for table in (
            "pair_ends",
            "pair_slot_live",
            "pair_units",
            "subject_keys",
            *OBSERVATION_TABLES,
        )
    }


def test_a_v31_projection_recovers_to_v32_rows_equal_to_a_fresh_build(tmp_path: Path) -> None:
    import sqlite3

    from cruxible_core.indexes.projection import PROJECTION_STORAGE_SCHEMA_VERSION
    from cruxible_core.indexes.sqlite import ProjectionIntegrityError, bind_projection
    from cruxible_core.runtime.instance import PlaybillInstance
    from tests.core_support._pair_real import pair_instance

    assert PROJECTION_STORAGE_SCHEMA_VERSION == 39
    instance, facts = pair_instance(tmp_path, pairs=2)
    head = instance.accepted_coordinate()
    with instance.bind_accepted_projection(head) as published:
        maintained = _pair_tables(published.typed.connection)
    assert len(maintained["pair_units"]) == 4 and len(maintained["pair_slot_live"]) == 2
    directory = instance.root / "projections"
    manifest = next(
        path
        for path in directory.glob("projection-*.json")
        if json.loads(path.read_bytes())["git_oid"] == head.git_oid
    )
    data = json.loads(manifest.read_bytes())
    piece = directory / data["pieces"][0]["name"]
    # The projection an older build left: the same rows without the pair index,
    # at storage version 31 (its own manifest beside it).
    old_piece = directory / ("v31-" + piece.name)
    old_piece.write_bytes(piece.read_bytes())
    old_piece.chmod(0o600)
    with sqlite3.connect(old_piece) as connection:
        for table in ("pair_units", "pair_slot_live", "pair_ends"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=31")
    data["storage_schema_version"] = 31
    data["pieces"][0]["name"] = old_piece.name
    old_manifest = directory / "projection-v31.json"
    old_manifest.write_text(json.dumps(data))
    with pytest.raises(ProjectionIntegrityError):
        bind_projection(old_manifest, expected=head)
    # A v32 build finds no v32 projection of the head (the old build wrote none).
    manifest.unlink()
    piece.unlink()
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.accepted_coordinate() == head
    with reopened.bind_accepted_projection(head) as rebuilt:
        assert rebuilt.manifest.storage_schema_version == 39
        assert _pair_tables(rebuilt.typed.connection) == maintained
    assert set(facts) == {row[0] for row in maintained["pair_slot_live"]}
