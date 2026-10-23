"""The unit index and the polarity law (inverse Claims, slice I2).

A unit holds values or one none, never both. The index (``claim_units``) is
kept from each change's Claims, the fan-in of a key Claim's Subject and the
Claims of a ClaimType whose inverse key changed; incremental upkeep equals a
cold rebuild. The boundary judges a change set against its parent's index:
admission and settlement both evaluate there, at the head the change lands on.
"""

from __future__ import annotations

import random
import sqlite3
from typing import Any

import pytest

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.claim_types import (
    ClaimInverse,
    ClaimType,
    claim_type_digest,
    claim_type_path,
    render_claim_type,
)
from cruxible_client.contracts.claims import (
    ClaimArtifact,
    ClaimArtifactV2,
    ClaimRetirementAttribution,
    LiteralClaimObject,
    NoneClaimObject,
    SubjectClaimObject,
    claim_path,
    render_claim,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_core.indexes.claim_units import (
    FLIP_RETIREMENT_REASON,
    FLIP_UNIT_MOVED,
    INVERSE_DUPLICATE,
    POLARITY_CONFLICT,
    _claim_facts,
    candidate_polarity_issues,
    polarity_issues,
    refresh_claim_units,
)
from cruxible_core.indexes.typed_state import _claim_fields, owner_values, schema_sql
from tests.test_claims.test_claim_type_v7 import v7
from tests.test_claims.test_claim_type_v9 import many_subject, v9
from tests.test_claims.test_claims import _claim

KEY = "project.release.product_name"
RUNS = "project.work_item.blocks"
ASSET = "subjects/project.work_item/wi-42.json"
OTHER_ASSET = "subjects/project.work_item/wi-7.json"


def _inverse(key: dict[str, Any]) -> ClaimInverse:
    return ClaimInverse.model_validate({"key": key, "evidence_rules": ["source"]})


RUNS_TYPE = many_subject(_inverse({"kind": "object_key", "predicate": KEY}))
SLOT_TYPE = v9()  # project.work_item.status, one-valued, inverse keyed by slot
REFUSING_SLOT_TYPE = v9(
    resolution_policy=SLOT_TYPE.resolution_policy.model_copy(update={"conflict_result": "refuse"})
)
OBJECT_TYPE = v9(
    _inverse({"kind": "object"}),
    identity={"kind": "ClaimType", "name": "project.work_item.labels"},
    predicate="project.work_item.labels",
    cardinality="many",
    member_descriptions=(),
    resolution_policy=v7().resolution_policy.model_copy(
        update={"cardinality": "many", "selector": "all"}
    ),
)
KEY_TYPE = v7(
    identity={"kind": "ClaimType", "name": KEY},
    predicate=KEY,
    allowed_subject_kinds=("project.release",),
    member_descriptions=(),
    literal_schema={"type": "string"},
)


def release(name: str) -> str:
    return f"subjects/project.release/{name}.json"


def claim(
    number: int,
    claim_type: ClaimType,
    obj: Any,
    *,
    subject: str = ASSET,
    qualifier: str | None = None,
    live: bool = True,
    reason: str = "superseded",
) -> ClaimArtifactV2 | ClaimArtifact:
    """A Claim of ``claim_type``; retired (``live=False``) with an attributed ``reason``."""

    claim_id = f"CLM-{number:032x}"
    base = _claim(
        claim_id=claim_id,
        capture_digest="sha256:" + "ab" * 32,
        source_digest="sha256:" + "cd" * 32,
        source_length=12,
    )
    statement = base.statement.model_copy(
        update={
            "subject": SemanticAddress.whole_artifact(subject),
            "claim_type": claim_type.identity,
            "claim_type_digest": claim_type_digest(claim_type).tagged,
            "predicate": claim_type.predicate,
            "object": obj,
            "qualifier": qualifier,
        }
    )
    if not live:
        return ClaimArtifact(
            identity=base.identity,
            statement=statement,
            backing=base.backing,
            pins=base.pins,
            lifecycle=ArtifactLifecycle(state="retired", predecessor_digest="sha256:" + "0" * 64),
            retirement=ClaimRetirementAttribution.model_validate({"reason": reason}),
        )
    return ClaimArtifactV2.model_validate(
        {**base.model_dump(), "statement": statement.model_dump()}
    )


def runs(number: int, target: str, **kwargs: Any) -> ClaimArtifactV2 | ClaimArtifact:
    obj = SubjectClaimObject(address=SemanticAddress.whole_artifact(release(target)))
    return claim(number, RUNS_TYPE, obj, **kwargs)


def runs_none(number: int, key: str, **kwargs: Any) -> ClaimArtifactV2 | ClaimArtifact:
    return claim(number, RUNS_TYPE, NoneClaimObject(of=LiteralClaimObject(value=key)), **kwargs)


def product(number: int, target: str, name: str, **kwargs: Any) -> ClaimArtifactV2 | ClaimArtifact:
    return claim(
        number, KEY_TYPE, LiteralClaimObject(value=name), subject=release(target), **kwargs
    )


def tree(*members: ClaimArtifactV2 | ClaimArtifact | ClaimType) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    for member in members:
        if isinstance(member, ClaimType):
            found[claim_type_path(member.predicate)] = render_claim_type(member)
        else:
            found[claim_path(member.identity.name)] = render_claim(member)
    return found


def judge(
    parent: dict[str, bytes],
    *changed: ClaimArtifactV2 | ClaimArtifact | ClaimType,
    base: dict[str, bytes] | None = None,
    contend: tuple[ClaimArtifactV2 | ClaimArtifact, ...] = (),
    base_changes: frozenset[str] | None = None,
) -> list[str]:
    """The polarity codes of ``changed`` landing on ``parent``, made against ``base``.

    ``contend`` are the changed Claims whose write declared a contend.
    """

    candidate = {**parent, **tree(*changed)}
    scope = tuple(sorted(tree(*changed)))
    return [
        issue.code
        for issue in candidate_polarity_issues(
            current_tree=parent,
            candidate_tree=candidate,
            scope=scope,
            base_tree=base,
            base_changes=base_changes,
            contending=frozenset(item.identity.qualified for item in contend),
        )
    ]


TYPES = (RUNS_TYPE, KEY_TYPE)
#: The runs type as it stood before it declared its inverse.
PLAIN_RUNS_TYPE = ClaimType.model_validate({**RUNS_TYPE.model_dump(mode="python"), "inverse": None})


# -- the index ------------------------------------------------------------------


def _database() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(schema_sql())
    return connection


def _put_type(connection: sqlite3.Connection, claim_type: ClaimType) -> None:
    from cruxible_core.indexes.typed_state import OWNER_BY_KIND

    values = owner_values(OWNER_BY_KIND["claim-type"], claim_type)
    connection.execute("DELETE FROM claim_types WHERE identity=?", (claim_type.identity.qualified,))
    connection.execute(
        "INSERT INTO claim_types (identity,path,format_tag,artifact_digest,predecessor_digest,"
        "revision,lifecycle,predicate,object_kind,cardinality,referent_sensitivity,inverse_key,"
        "inverse_key_predicate) VALUES (?,?,?,?,NULL,1,'live',?,?,?,?,?,?)",
        (
            claim_type.identity.qualified,
            claim_type_path(claim_type.predicate),
            claim_type.artifact_format,
            claim_type_digest(claim_type).tagged,
            values["predicate"],
            values["object_kind"],
            values["cardinality"],
            values["referent_sensitivity"],
            values["inverse_key"],
            values["inverse_key_predicate"],
        ),
    )


def _put_claim(connection: sqlite3.Connection, artifact: ClaimArtifactV2 | ClaimArtifact) -> None:
    fields = _claim_fields(artifact)
    fields.update(
        identity=artifact.identity.qualified,
        path=claim_path(artifact.identity.name),
        format_tag="playbill-claim-v2",
        artifact_digest="sha256:" + "00" * 32,
        revision=1,
        lifecycle=artifact.lifecycle.state,
    )
    connection.execute("DELETE FROM claims WHERE identity=?", (fields["identity"],))
    connection.execute(
        f"INSERT INTO claims ({','.join(fields)}) VALUES ({','.join('?' * len(fields))})",
        tuple(fields.values()),
    )


def _units(connection: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return connection.execute("SELECT * FROM claim_units ORDER BY claim_identity").fetchall()


def test_a_none_is_indexed_without_a_value_any_read_could_project() -> None:
    connection = _database()
    _put_type(connection, RUNS_TYPE)
    _put_claim(connection, runs_none(1, "gh"))
    row = connection.execute(
        "SELECT object_kind, object_path, literal_type, literal_text, none_of FROM claims"
    ).fetchone()
    assert row == ("none", None, None, None, '["literal","string","gh"]')
    refresh_claim_units(connection)
    assert [unit[4:7] for unit in _units(connection)] == [
        ("object_key", '["literal","string","gh"]', "inverse")
    ]
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE claims SET literal_type='string', literal_text='gh' WHERE object_kind='none'"
        )


def test_positives_take_the_key_their_object_holds_and_move_with_it() -> None:
    connection = _database()
    for claim_type in TYPES:
        _put_type(connection, claim_type)
    for artifact in (product(10, "r1", "gh"), runs(1, "r1"), runs(2, "r1", subject=OTHER_ASSET)):
        _put_claim(connection, artifact)
    refresh_claim_units(connection)
    assert {unit[0]: unit[5] for unit in _units(connection)} == {
        runs(1, "r1").identity.qualified: '["literal","string","gh"]',
        runs(2, "r1").identity.qualified: '["literal","string","gh"]',
    }
    # The key Claim is revised: the change names it, and its Subject's fan-in moves.
    _put_claim(connection, product(10, "r1", "git"))
    refresh_claim_units(
        connection,
        claims=[product(10, "r1", "git").identity.qualified],
        key_subjects=[(release("r1"), KEY)],
    )
    assert {unit[5] for unit in _units(connection)} == {'["literal","string","git"]'}


def _random_world(seed: int, steps: int) -> sqlite3.Connection:
    """Random Claim, key and ClaimType changes, each applied as an accept applies it.

    After every change the incremental rows must equal a cold rebuild.
    """

    rng = random.Random(seed)
    connection = _database()
    for claim_type in (*TYPES, SLOT_TYPE, OBJECT_TYPE):
        _put_type(connection, claim_type)
    refresh_claim_units(connection)
    for _ in range(steps):
        choice = rng.randrange(6)
        number = rng.randrange(1, 9)
        subject = rng.choice((ASSET, OTHER_ASSET))
        live = rng.random() < 0.8
        changed_types: list[str] = []
        if choice == 0:
            artifact = runs(number, rng.choice(("r1", "r2", "r3")), subject=subject, live=live)
        elif choice == 1:
            artifact = runs_none(number, rng.choice(("gh", "git")), subject=subject, live=live)
        elif choice == 2:
            artifact = product(
                20 + number % 3, f"r{1 + number % 3}", rng.choice(("gh", "git")), live=live
            )
        elif choice == 3:
            artifact = claim(
                40 + number,
                SLOT_TYPE,
                rng.choice((LiteralClaimObject(value="ready"), NoneClaimObject())),
                subject=subject,
                live=live,
            )
        elif choice == 4:
            artifact = claim(
                60 + number,
                OBJECT_TYPE,
                rng.choice(
                    (
                        LiteralClaimObject(value="a"),
                        NoneClaimObject(of=LiteralClaimObject(value="a")),
                        LiteralClaimObject(value=["x", 1]),
                    )
                ),
                subject=subject,
                live=live,
            )
        else:
            # The runs type drops or regains its inverse.
            current = connection.execute(
                "SELECT inverse_key FROM claim_types WHERE identity=?",
                (RUNS_TYPE.identity.qualified,),
            ).fetchone()[0]
            _put_type(connection, RUNS_TYPE if current is None else PLAIN_RUNS_TYPE)
            changed_types.append(RUNS_TYPE.identity.qualified)
            refresh_claim_units(connection, claims=(), claim_types=changed_types)
            _assert_cold_equal(connection)
            continue
        identity = artifact.identity.qualified
        before = connection.execute(
            "SELECT subject_path, predicate FROM claims WHERE identity=?", (identity,)
        ).fetchall()
        _put_claim(connection, artifact)
        refresh_claim_units(
            connection,
            claims=[identity],
            key_subjects=[
                *(tuple(row) for row in before),
                (artifact.statement.subject.artifact_path, artifact.statement.predicate),
            ],
        )
        _assert_cold_equal(connection)
    return connection


def _assert_cold_equal(connection: sqlite3.Connection) -> None:
    incremental = _units(connection)
    refresh_claim_units(connection)
    assert incremental == _units(connection)


@pytest.mark.parametrize("seed", range(6))
def test_incremental_upkeep_equals_a_cold_rebuild(seed: int) -> None:
    connection = _random_world(seed, 120)
    assert {row[6] for row in _units(connection)} == {"positive", "inverse"}


# -- the polarity law at the boundary --------------------------------------------


def test_a_none_beside_a_value_of_its_key_is_refused() -> None:
    parent = tree(*TYPES, product(10, "r1", "gh"), runs(1, "r1"))
    assert judge(parent, runs_none(2, "gh")) == [POLARITY_CONFLICT]
    assert judge(parent, runs_none(2, "git")) == []
    # Another asset's unit is another unit.
    assert judge(parent, runs_none(2, "gh", subject=OTHER_ASSET)) == []


def test_a_flip_revises_the_value_into_the_none() -> None:
    parent = tree(*TYPES, product(10, "r1", "gh"), runs(1, "r1"))
    assert judge(parent, runs_none(1, "gh")) == []


def test_a_flip_over_several_values_retires_the_rest_in_the_same_set() -> None:
    parent = tree(
        *TYPES, product(10, "r1", "gh"), product(11, "r2", "gh"), runs(1, "r1"), runs(2, "r2")
    )
    assert judge(parent, runs_none(1, "gh")) == [POLARITY_CONFLICT]
    assert judge(parent, runs_none(1, "gh"), runs(2, "r2", live=False)) == []


def test_the_write_verbs_flip_shapes_are_judged_as_flips() -> None:
    """IV precondition: the shapes set/add/retire write are flips, both ways, in one change.

    A one-valued ``set`` revises the live Claim into the other polarity; a
    many-valued unit flips by retiring its values and adding the none (a new
    lineage) in the same change set, and back by retiring the none and adding
    the value. The writer names what it retires: a value left live refuses.
    """

    value = claim(1, SLOT_TYPE, LiteralClaimObject(value="ready"))
    landed = tree(SLOT_TYPE, value)
    # set FIELD none over the value, and set FIELD value over the none: revisions.
    assert judge(landed, claim(1, SLOT_TYPE, NoneClaimObject())) == []
    noned = tree(SLOT_TYPE, claim(1, SLOT_TYPE, NoneClaimObject()))
    assert judge(noned, claim(1, SLOT_TYPE, LiteralClaimObject(value="done"))) == []
    # A one-valued flip as retire-plus-new is a flip too.
    retired = claim(1, SLOT_TYPE, LiteralClaimObject(value="ready"), live=False)
    assert judge(landed, claim(2, SLOT_TYPE, NoneClaimObject()), retired) == []
    # Many values under an object_key inverse: retire them all and add the none.
    parent = tree(
        *TYPES, product(10, "r1", "gh"), product(11, "r2", "gh"), runs(1, "r1"), runs(2, "r2")
    )
    none = runs_none(3, "gh")
    assert judge(parent, none, runs(1, "r1", live=False)) == [POLARITY_CONFLICT]
    assert judge(parent, none, runs(1, "r1", live=False), runs(2, "r2", live=False)) == []
    # Not a revision flip: each retirement keeps the reason its writer gave.
    wrong = runs(2, "r2", live=False, reason="was-wrong")
    assert judge(parent, none, runs(1, "r1", live=False), wrong) == []
    # And back: retire the none, add a value of its key.
    held = tree(*TYPES, product(10, "r1", "gh"), runs_none(3, "gh"))
    assert judge(held, runs(1, "r1")) == [POLARITY_CONFLICT]
    assert judge(held, runs(1, "r1"), runs_none(3, "gh", live=False)) == []
    # An object unit (a literal many-valued field) flips the same way.
    labels = tree(OBJECT_TYPE, claim(1, OBJECT_TYPE, LiteralClaimObject(value="a")))
    none_a = claim(2, OBJECT_TYPE, NoneClaimObject(of=LiteralClaimObject(value="a")))
    retired_a = claim(1, OBJECT_TYPE, LiteralClaimObject(value="a"), live=False)
    assert judge(labels, none_a, retired_a) == []


def test_an_object_key_flip_stays_in_its_unit() -> None:
    parent = tree(*TYPES, product(10, "r1", "gh"), runs(1, "r1"))
    assert judge(parent, runs_none(1, "git")) == [FLIP_UNIT_MOVED]
    # Back to a value: the release it now runs carries the key the none named.
    flipped = tree(*TYPES, product(10, "r1", "gh"), product(11, "r2", "git"), runs_none(1, "gh"))
    assert judge(flipped, runs(1, "r1")) == []
    assert judge(flipped, runs(1, "r2")) == [FLIP_UNIT_MOVED]


def test_a_key_revision_moves_values_into_a_unit_that_holds_none() -> None:
    parent = tree(*TYPES, product(10, "r1", "gh"), runs(1, "r1"), runs_none(2, "git"))
    assert judge(parent, product(10, "r1", "git")) == [POLARITY_CONFLICT]
    # The same change may flip the none it would collide with.
    assert judge(parent, product(10, "r1", "git"), runs_none(2, "git", live=False)) == []
    issues = candidate_polarity_issues(
        current_tree=parent,
        candidate_tree={**parent, **tree(product(10, "r1", "git"))},
        scope=tuple(tree(product(10, "r1", "git"))),
    )
    assert issues[0].path == claim_path(product(10, "r1", "git").identity.name)


def test_two_changes_that_each_pass_alone_cannot_both_land() -> None:
    """Concurrent writers (or a Line settling behind a write) are judged where they land."""

    parent = tree(*TYPES, product(10, "r1", "gh"))
    first, second = runs(1, "r1"), runs_none(2, "gh")
    assert judge(parent, first) == [] and judge(parent, second) == []
    landed = {**parent, **tree(first)}
    assert judge(landed, second) == [POLARITY_CONFLICT]


def test_a_line_terminal_flip_rejudged_behind_a_write_that_added_a_value() -> None:
    """A Line's terminal flips the unit (one flips, the rest retire); a write lands first."""

    parent = tree(
        *TYPES, product(10, "r1", "gh"), product(11, "r2", "gh"), product(12, "r3", "gh"),
        runs(1, "r1"), runs(2, "r2"),
    )  # fmt: skip
    terminal = (runs_none(1, "gh"), runs(2, "r2", live=False))
    assert judge(parent, *terminal) == []
    written = {**parent, **tree(runs(3, "r3"))}
    assert judge(written, *terminal) == [POLARITY_CONFLICT]


def test_one_valued_rivals_contend_only_under_an_unresolved_policy() -> None:
    value = claim(1, SLOT_TYPE, LiteralClaimObject(value="ready"))
    none = claim(2, SLOT_TYPE, NoneClaimObject())
    assert judge(tree(SLOT_TYPE, value), none, contend=(none,)) == []
    refusing_value = claim(1, REFUSING_SLOT_TYPE, LiteralClaimObject(value="ready"))
    refusing_none = claim(2, REFUSING_SLOT_TYPE, NoneClaimObject())
    assert judge(
        tree(REFUSING_SLOT_TYPE, refusing_value), refusing_none, contend=(refusing_none,)
    ) == [POLARITY_CONFLICT]
    # A qualified slot is another slot.
    assert (
        judge(
            tree(REFUSING_SLOT_TYPE, refusing_value),
            claim(2, REFUSING_SLOT_TYPE, NoneClaimObject(), qualifier="primary"),
        )
        == []
    )


def test_a_one_valued_dispute_needs_a_declared_contend() -> None:
    """Seeing the opposite in the base is not contention: the write declares it (I2-001)."""

    value = claim(1, SLOT_TYPE, LiteralClaimObject(value="ready"))
    none = claim(2, SLOT_TYPE, NoneClaimObject())
    landed = tree(SLOT_TYPE, value)
    # A raw none against a head already holding the value, no contend: refused.
    assert judge(landed, none) == [POLARITY_CONFLICT]
    assert judge(landed, none, base=landed) == [POLARITY_CONFLICT]
    # The same none written with contend disputes the value it read.
    assert judge(landed, none, contend=(none,)) == []
    assert judge(landed, none, base=landed, contend=(none,)) == []
    # A contend declared for another Claim of the change is not this one's.
    other = claim(3, SLOT_TYPE, LiteralClaimObject(value="x"), subject=OTHER_ASSET)
    assert judge(landed, none, other, contend=(other,)) == [POLARITY_CONFLICT]
    # Either polarity: a value written beside a live none needs its own contend.
    noned = tree(SLOT_TYPE, none)
    rival = claim(3, SLOT_TYPE, LiteralClaimObject(value="blocked"))
    assert judge(noned, rival) == [POLARITY_CONFLICT]
    assert judge(noned, rival, contend=(rival,)) == []
    # Rejudged after a rebase: the contend stands against what its base held.
    empty = tree(SLOT_TYPE)
    assert judge(landed, none, base=empty, contend=(none,)) == [POLARITY_CONFLICT]
    later = claim(4, SLOT_TYPE, LiteralClaimObject(value="late"))
    moved = {**landed, **tree(later)}
    changed = frozenset(tree(later))
    assert judge(moved, none, base=landed, contend=(none,)) == [POLARITY_CONFLICT]
    assert judge(moved, none, base=landed, contend=(none,), base_changes=changed) == [
        POLARITY_CONFLICT
    ]
    assert judge(moved, none, base=moved, contend=(none,)) == []
    # Unrelated paths changed meanwhile: the dispute stands.
    assert (
        judge(
            {**landed, **tree(other)},
            none,
            base=landed,
            contend=(none,),
            base_changes=frozenset(tree(other)),
        )
        == []
    )


def test_a_one_valued_dispute_stands_only_against_a_value_its_writer_saw() -> None:
    """Policy alone is no dispute: a contend is written against the value it read (I2-001)."""

    empty = tree(SLOT_TYPE)
    value = claim(1, SLOT_TYPE, LiteralClaimObject(value="ready"))
    none = claim(2, SLOT_TYPE, NoneClaimObject())
    both = (value, none)
    # Two candidates prepared against the empty slot each pass alone.
    assert judge(empty, value) == [] and judge(empty, none) == []
    # Whichever lands second never read the other: it disputes nothing, so it refuses.
    assert judge({**empty, **tree(value)}, none, base=empty, contend=both) == [POLARITY_CONFLICT]
    assert judge({**empty, **tree(none)}, value, base=empty, contend=both) == [POLARITY_CONFLICT]
    # Written at (or rebased from) a head that holds the value: a deliberate contend.
    landed = {**empty, **tree(value)}
    assert judge(landed, none, contend=both) == []
    assert judge(landed, none, base=landed, contend=both) == []
    # One change writing both polarities into the slot contends with nothing it read.
    assert judge(empty, value, none, contend=both) == [POLARITY_CONFLICT]
    # The value it read was flipped to none meanwhile: the none it now meets is unseen.
    flipped = {**empty, **tree(claim(1, SLOT_TYPE, NoneClaimObject()))}
    rival = claim(3, SLOT_TYPE, LiteralClaimObject(value="blocked"))
    assert judge(flipped, rival, base=landed, contend=(rival,)) == [POLARITY_CONFLICT]
    # Rewriting a rival already in an accepted dispute starts no new one.
    disputed = tree(SLOT_TYPE, value, none)
    assert judge(disputed, claim(1, SLOT_TYPE, LiteralClaimObject(value="ready"))) == []
    assert judge(disputed, none, base=disputed) == []


def test_a_unit_holds_one_none() -> None:
    """Values or one none: a second inverse lineage refuses in every unit kind (I2-002)."""

    keyed = tree(*TYPES, product(10, "r1", "gh"))
    assert judge(keyed, runs_none(1, "gh"), runs_none(2, "gh")) == [INVERSE_DUPLICATE]
    landed = {**keyed, **tree(runs_none(1, "gh"))}
    # Concurrent, or rebased behind the first: the second is judged where it lands.
    assert judge(landed, runs_none(2, "gh")) == [INVERSE_DUPLICATE]
    assert judge(landed, runs_none(2, "gh"), base=keyed) == [INVERSE_DUPLICATE]
    # Restating the live none revises it; replacing it retires it in the same change.
    assert judge(landed, runs_none(1, "gh")) == []
    assert judge(landed, runs_none(2, "gh"), runs_none(1, "gh", live=False)) == []
    # Another key, another asset: other units.
    assert judge(landed, runs_none(2, "git")) == []
    assert judge(landed, runs_none(2, "gh", subject=OTHER_ASSET)) == []
    slot = tree(SLOT_TYPE, claim(1, SLOT_TYPE, NoneClaimObject()))
    assert judge(slot, claim(2, SLOT_TYPE, NoneClaimObject())) == [INVERSE_DUPLICATE]
    labels = tree(
        OBJECT_TYPE, claim(1, OBJECT_TYPE, NoneClaimObject(of=LiteralClaimObject(value="a")))
    )
    assert judge(
        labels, claim(2, OBJECT_TYPE, NoneClaimObject(of=LiteralClaimObject(value="a")))
    ) == [INVERSE_DUPLICATE]
    assert (
        judge(labels, claim(2, OBJECT_TYPE, NoneClaimObject(of=LiteralClaimObject(value="b"))))
        == []
    )


def test_values_a_flip_leaves_behind_retire_as_superseded() -> None:
    parent = tree(
        *TYPES, product(10, "r1", "gh"), product(11, "r2", "gh"), product(12, "r3", "git"),
        runs(1, "r1"), runs(2, "r2"), runs(3, "r3"),
    )  # fmt: skip
    assert judge(parent, runs_none(1, "gh"), runs(2, "r2", live=False)) == []
    wrong = runs(2, "r2", live=False, reason="was-wrong")
    assert judge(parent, runs_none(1, "gh"), wrong) == [FLIP_RETIREMENT_REASON]
    # A retirement in another unit, or with no flip at all, keeps its own reason.
    other = runs(3, "r3", live=False, reason="was-wrong")
    assert judge(parent, runs_none(1, "gh"), runs(2, "r2", live=False), other) == []
    assert judge(parent, wrong) == []


def test_an_object_unit_is_the_object_itself() -> None:
    parent = tree(OBJECT_TYPE, claim(1, OBJECT_TYPE, LiteralClaimObject(value="a")))
    none_a = claim(2, OBJECT_TYPE, NoneClaimObject(of=LiteralClaimObject(value="a")))
    none_b = claim(2, OBJECT_TYPE, NoneClaimObject(of=LiteralClaimObject(value="b")))
    assert judge(parent, none_a) == [POLARITY_CONFLICT]
    assert judge(parent, none_b) == []


def test_a_type_gaining_its_inverse_judges_the_values_it_already_holds() -> None:
    plain = PLAIN_RUNS_TYPE
    parent = tree(KEY_TYPE, plain, product(10, "r1", "gh"), runs(1, "r1"))
    assert judge(parent, RUNS_TYPE, runs_none(2, "gh")) == [POLARITY_CONFLICT]
    assert judge(parent, RUNS_TYPE, runs_none(2, "git")) == []


def test_a_change_without_inverse_types_reads_no_unit() -> None:
    plain = tree(KEY_TYPE, product(10, "r1", "gh"))
    assert judge(plain, product(10, "r1", "git")) == []


def test_the_projection_index_judges_as_the_cold_tree_does() -> None:
    """The boundary over an accepted projection's tables reads the same units as a cold tree."""

    members = (product(10, "r1", "gh"), runs(1, "r1"), runs_none(2, "git"))
    parent_tree = tree(*TYPES, *members)
    connection = _database()
    for claim_type in TYPES:
        _put_type(connection, claim_type)
    for artifact in members:
        _put_claim(connection, artifact)
    refresh_claim_units(connection)
    for change in (
        (runs_none(3, "gh"),),
        (product(10, "r1", "git"),),
        (runs_none(1, "git"),),
        (runs_none(1, "gh"),),
    ):
        changed = tree(*change)
        facts = {
            facts["identity"]: facts
            for facts in (_claim_facts(content, path) for path, content in changed.items())
        }
        indexed = polarity_issues(
            connection,
            claims=facts,
            claim_types={},
            claim_type_of=lambda identity: {item.identity.qualified: item for item in TYPES}.get(
                identity
            ),
        )
        cold = candidate_polarity_issues(
            current_tree=parent_tree,
            candidate_tree={**parent_tree, **changed},
            scope=tuple(sorted(changed)),
        )
        assert indexed == cold


def test_value_reads_never_project_a_none() -> None:
    """A none is no value: reads skip it by object kind, so a literal null stays (I2-003)."""

    from contextlib import contextmanager
    from types import SimpleNamespace

    from cruxible_core.service.discovery.query_values import (
        ValueIndex,
        claim_values_by_path,
        read_live_values,
    )

    connection = _database()
    for claim_type in (SLOT_TYPE, OBJECT_TYPE):
        _put_type(connection, claim_type)
    null = claim(1, SLOT_TYPE, LiteralClaimObject(value=None), subject=OTHER_ASSET)
    nones = (
        claim(2, SLOT_TYPE, NoneClaimObject()),
        claim(3, OBJECT_TYPE, NoneClaimObject(of=LiteralClaimObject(value="a"))),
    )
    for artifact in (null, *nones):
        _put_claim(connection, artifact)
    loaded: list[str] = []
    projection = SimpleNamespace(
        typed=SimpleNamespace(
            connection=connection, source=lambda identity: loaded.append(identity)
        )
    )

    @contextmanager
    def bind(_coordinate: object) -> Any:
        yield projection

    instance = SimpleNamespace(bind_accepted_projection=bind)
    values = read_live_values(instance, None, subject_paths=None, predicates=None)  # type: ignore[arg-type]
    assert [(item.identity, item.value) for item in values] == [(null.identity.qualified, None)]
    assert loaded == []
    index = ValueIndex(values)
    assert index.slot(ASSET, SLOT_TYPE.predicate) == [] and index.subject(ASSET) == []
    paths = [claim_path(item.identity.name) for item in (null, *nones)]
    assert set(claim_values_by_path(projection, paths)) == {paths[0]}
