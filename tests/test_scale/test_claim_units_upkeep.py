"""Unit index upkeep and the polarity check grow with the change, never the instance.

Two projections hold the same few units under test, beside SMALL and LARGE
(ten times as many) unrelated Subjects with values, nones and slot Claims of
the same inverse ClaimTypes. Accepting one change (a new none, and a key
revision whose Subject three values point at) and judging the same change at
the boundary run the same SQL work at both sizes: O(changed Claims plus the
fan-in of a changed key Subject), one seek per unit.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from cruxible_client.contracts.claims import LiteralClaimObject, claim_path
from cruxible_core.indexes.claim_units import _claim_facts, polarity_issues, refresh_claim_units
from cruxible_core.indexes.typed_state import schema_sql
from tests.test_indexes.test_claim_units import (
    ASSET,
    KEY,
    RUNS_TYPE,
    SLOT_TYPE,
    TYPES,
    _put_claim,
    _put_type,
    claim,
    product,
    release,
    render_claim,
    runs,
    runs_none,
)
from tests.test_scale._counters import counting, trace_sqlite
from tests.test_scale.test_scale_guard import RATIO, SLACK

SMALL = 20
SIZES = (SMALL, 10 * SMALL)


@pytest.fixture
def root(tmp_path: Path) -> Iterator[Path]:
    with trace_sqlite(tmp_path):
        yield tmp_path


def _world(root: Path, others: int) -> sqlite3.Connection:
    connection = sqlite3.connect(root / f"units-{others}.sqlite3", isolation_level=None)
    connection.executescript(schema_sql())
    for claim_type in (*TYPES, SLOT_TYPE):
        _put_type(connection, claim_type)
    # The units under test: three values of the release keyed gh.
    _put_claim(connection, product(1, "target", "gh"))
    for number, subject in ((2, ASSET), (3, "subjects/project.work_item/wi-2.json")):
        _put_claim(connection, runs(number, "target", subject=subject))
    _put_claim(connection, runs(4, "target", subject="subjects/project.work_item/wi-3.json"))
    # Everything else: unrelated Subjects with values, nones and slot Claims.
    for index in range(others):
        number = 1000 + 4 * index
        subject = f"subjects/project.work_item/other-{index}.json"
        _put_claim(connection, product(number, f"other-{index}", f"p{index}"))
        _put_claim(connection, runs(number + 1, f"other-{index}", subject=subject))
        _put_claim(connection, runs_none(number + 2, f"q{index}", subject=subject))
        _put_claim(
            connection,
            claim(number + 3, SLOT_TYPE, LiteralClaimObject(value="ready"), subject=subject),
        )
    refresh_claim_units(connection)
    return connection


def _grows(small: int, large: int) -> bool:
    return large > small * RATIO + SLACK["sql_vm_steps"]


def _accept(connection: sqlite3.Connection) -> None:
    changed = (runs_none(5, "zz"), product(1, "target", "git"))
    for artifact in changed:
        _put_claim(connection, artifact)
    refresh_claim_units(
        connection,
        claims=[artifact.identity.qualified for artifact in changed],
        key_subjects=[(ASSET, RUNS_TYPE.predicate), (release("target"), KEY)],
    )


def _judge(connection: sqlite3.Connection) -> None:
    changed = (runs_none(6, "gh"), product(1, "target", "gh"))
    facts = {
        artifact.identity.qualified: _claim_facts(
            render_claim(artifact), claim_path(artifact.identity.name)
        )
        for artifact in changed
    }
    types = {item.identity.qualified: item for item in (*TYPES, SLOT_TYPE)}
    issues = polarity_issues(
        connection, claims=facts, claim_types={}, claim_type_of=lambda identity: types[identity]
    )
    assert [issue.code for issue in issues] == ["cruxible.claim.polarity_conflict"]


@pytest.mark.parametrize("work", [_accept, _judge], ids=["accept", "boundary"])
def test_unit_index_work_is_flat_in_unrelated_claims(root: Path, work) -> None:  # type: ignore[no-untyped-def]
    steps = []
    for others in SIZES:
        connection = _world(root, others)
        with counting(root) as window:
            work(connection)
        assert window.unexplained == [], window.unexplained
        steps.append(window.counts["sql_vm_steps"])
        connection.close()
    small, large = steps
    assert small > 0
    assert not _grows(small, large), (steps, window.attribution[:3])


def test_the_accepted_change_moved_every_value_of_the_rekeyed_release(root: Path) -> None:
    connection = _world(root, SMALL)
    _accept(connection)
    moved = connection.execute(
        "SELECT unit FROM claim_units WHERE object_path=? ORDER BY claim_identity",
        (release("target"),),
    ).fetchall()
    assert moved == [('["literal","string","git"]',)] * 3


def _crowded(root: Path, values: int) -> sqlite3.Connection:
    """One unit (ASSET runs, key gh) holding ``values`` values, each a release keyed gh."""

    connection = sqlite3.connect(root / f"crowded-{values}.sqlite3", isolation_level=None)
    connection.executescript(schema_sql())
    for claim_type in TYPES:
        _put_type(connection, claim_type)
    for index in range(values):
        _put_claim(connection, product(2000 + index, f"gh-{index}", "gh"))
        _put_claim(connection, runs(5000 + index, f"gh-{index}"))
    _put_claim(connection, product(1999, "gh-new", "gh"))
    refresh_claim_units(connection)
    return connection


def _judge_crowded(connection: sqlite3.Connection, *changed: object) -> list[str]:
    facts = {
        artifact.identity.qualified: _claim_facts(  # type: ignore[attr-defined]
            render_claim(artifact),  # type: ignore[arg-type]
            claim_path(artifact.identity.name),  # type: ignore[attr-defined]
        )
        for artifact in changed
    }
    types = {item.identity.qualified: item for item in TYPES}
    return [
        issue.code
        for issue in polarity_issues(
            connection, claims=facts, claim_types={}, claim_type_of=lambda identity: types[identity]
        )
    ]


@pytest.mark.parametrize(
    "change",
    [
        (lambda: (runs(4999, "gh-new"),), []),
        (lambda: (runs_none(4998, "gh"),), ["cruxible.claim.polarity_conflict"]),
    ],
    ids=["value", "none"],
)
def test_the_boundary_never_enumerates_a_crowded_unit(root: Path, change) -> None:  # type: ignore[no-untyped-def]
    """One more value (or one none) beside many values seeks, never scans the unit (I2-004)."""

    build, expected = change
    steps = []
    for values in SIZES:
        connection = _crowded(root, values)
        with counting(root) as window:
            assert _judge_crowded(connection, *build()) == expected
        assert window.unexplained == [], window.unexplained
        steps.append(window.counts["sql_vm_steps"])
        connection.close()
    small, large = steps
    assert small > 0
    assert not _grows(small, large), (steps, window.attribution[:3])


def _disputed(root: Path, rivals: int) -> sqlite3.Connection:
    """One one-valued unresolved slot (ASSET status) holding ``rivals`` positive rivals."""

    connection = sqlite3.connect(root / f"disputed-{rivals}.sqlite3", isolation_level=None)
    connection.executescript(schema_sql())
    _put_type(connection, SLOT_TYPE)
    for index in range(rivals):
        _put_claim(connection, claim(7000 + index, SLOT_TYPE, LiteralClaimObject(value=f"v{index}")))
    refresh_claim_units(connection)
    return connection


@pytest.mark.parametrize(
    ("rebased", "contend", "expected"),
    [
        (False, True, []),
        (True, True, []),
        (False, False, ["cruxible.claim.polarity_conflict"]),
    ],
    ids=["current-base", "rebased", "undeclared"],
)
def test_a_one_valued_dispute_never_enumerates_its_rivals(
    root: Path, rebased: bool, contend: bool, expected: list[str]
) -> None:
    """One contending none beside many rivals is judged by seeks, not by its rivals (I2-004).

    Rebased, the paths that changed since the base (one unrelated Claim) bound
    the read of what landed meanwhile.
    """

    from cruxible_client.contracts.claims import NoneClaimObject

    none = claim(6999, SLOT_TYPE, NoneClaimObject())
    facts = {
        none.identity.qualified: _claim_facts(render_claim(none), claim_path(none.identity.name))
    }
    unrelated = claim(6998, SLOT_TYPE, LiteralClaimObject(value="x"), subject=release("other"))
    steps = []
    for rivals in SIZES:
        connection = _disputed(root, rivals)
        with counting(root) as window:
            codes = [
                issue.code
                for issue in polarity_issues(
                    connection,
                    claims=facts,
                    claim_types={},
                    claim_type_of=lambda _identity: SLOT_TYPE,
                    base_claim=(lambda identity: None) if rebased else None,
                    base_changes=(
                        frozenset({claim_path(unrelated.identity.name)}) if rebased else None
                    ),
                    contending=frozenset(facts) if contend else frozenset(),
                )
            ]
            assert codes == expected
        assert window.unexplained == [], window.unexplained
        steps.append(window.counts["sql_vm_steps"])
        connection.close()
    small, large = steps
    assert small > 0
    assert not _grows(small, large), (steps, window.attribution[:3])
