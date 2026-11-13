"""next's subject-end gate is one probe, and its upkeep tracks the change.

``next``'s dependency fold skips the retired build unless an end of a pair
holding a live fact has lost its last observation
(``_any_unobserved_pair_end``). The answer is kept in the typed projection
(``pair_end_unobserved``), so asking costs one probe whatever the instance
holds. Three worlds, at 20 and at 2,000, all answering "no item":

- ``observed_ends``: 20 vs 2,000 distinct ends, each observed, each with a
  retired observation, each ending a pair that holds a live fact;
- ``hub``: one observed end keyed by 20 vs 2,000 pairs holding live facts;
- ``history``: one end with 20 vs 2,000 retired observations and one live one.

The gate's SQLite VM steps are flat (rows visited, not statements), and so is
the upkeep of one more change in each world, by the scale guard's rule.
An end's observation is any non-key fact about it (``pt.exposure.note`` here).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from cruxible_core.service.discovery import next as playbill_next
from tests.core_support._pair_support import (
    COMPONENT,
    EXPOSURE,
    NOTE,
    SCOPE,
    VULN,
    claim,
    definitions,
    end,
    exposure,
    literal,
    path_of,
    projection,
    publish,
    put,
    retired_claim,
)
from tests.test_scale.test_scale_guard import RATIO, SLACK

SIZES = (20, 2_000)
World = tuple[dict[str, bytes], tuple[Any, ...]]


def _observed_ends(size: int) -> World:
    tree = definitions()
    put(tree, *end(VULN, "v0", 1))
    for index in range(size):
        number = 100_000 + 10 * index
        component = path_of(COMPONENT, f"c{index}")
        put(tree, *end(COMPONENT, f"c{index}", number), *exposure(f"c{index}", "v0", number + 1))
        put(
            tree,
            claim(
                number + 3, SCOPE, literal("required"), subject=path_of(EXPOSURE, f"c{index}--v0")
            ),
        )
        put(tree, claim(number + 4, NOTE, literal("seen"), subject=component))
        put(tree, retired_claim(claim(number + 5, NOTE, literal("old"), subject=component)))
    first = path_of(COMPONENT, "c0")
    # Its observation revised: retired, and a new one written.
    change = (
        retired_claim(claim(100_004, NOTE, literal("seen"), subject=first)),
        claim(60, NOTE, literal("seen again"), subject=first),
    )
    return tree, change


def _hub(size: int) -> World:
    tree = definitions()
    hub = path_of(COMPONENT, "hub")
    put(tree, *end(COMPONENT, "hub", 1))
    put(tree, claim(2, NOTE, literal("seen"), subject=hub))
    put(tree, retired_claim(claim(3, NOTE, literal("old"), subject=hub)))
    for index in range(size):
        number = 100_000 + 10 * index
        put(tree, *end(VULN, f"v{index}", number), *exposure("hub", f"v{index}", number + 1))
        put(
            tree,
            claim(
                number + 3, SCOPE, literal("required"), subject=path_of(EXPOSURE, f"hub--v{index}")
            ),
        )
    # One pair's fact retired: its units leave the hub, which stays observed.
    fact = claim(100_003, SCOPE, literal("required"), subject=path_of(EXPOSURE, "hub--v0"))
    return tree, (retired_claim(fact),)


def _history(size: int) -> World:
    tree = definitions()
    component = path_of(COMPONENT, "c")
    put(tree, *end(COMPONENT, "c", 1), *end(VULN, "v0", 2), *exposure("c", "v0", 3))
    put(tree, claim(5, SCOPE, literal("required"), subject=path_of(EXPOSURE, "c--v0")))
    for index in range(size):
        put(
            tree,
            retired_claim(claim(100_000 + index, NOTE, literal(f"r{index}"), subject=component)),
        )
    # The live observation sorts after every retired one.
    put(tree, claim(900_000, NOTE, literal("seen"), subject=component))
    # One more retired observation; the live one still stands.
    return tree, (retired_claim(claim(60, NOTE, literal("older"), subject=component)),)


WORLDS: dict[str, Callable[[int], World]] = {
    "observed_ends": _observed_ends,
    "hub": _hub,
    "history": _history,
}


class _Bound:
    """An instance whose accepted projection is this connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    @contextmanager
    def bind_accepted_projection(self, _coordinate: object) -> Iterator[Any]:
        yield SimpleNamespace(typed=SimpleNamespace(connection=self.connection))


def _steps(connection: sqlite3.Connection, run: Callable[[], Any]) -> tuple[Any, int]:
    """``run()`` and the SQLite VM steps it took on ``connection``."""

    steps = [0]

    def tick() -> int:
        steps[0] += 1
        return 0

    connection.set_progress_handler(tick, 1)
    try:
        return run(), steps[0]
    finally:
        connection.set_progress_handler(None, 0)


def _gate(connection: sqlite3.Connection) -> tuple[bool, int]:
    return _steps(
        connection,
        lambda: playbill_next._any_unobserved_pair_end(_Bound(connection), None),  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("name", sorted(WORLDS))
def test_the_gate_and_one_changes_upkeep_are_flat_when_no_end_is_unobserved(name: str) -> None:
    gate: list[int] = []
    upkeep: list[int] = []
    for size in SIZES:
        tree, change = WORLDS[name](size)
        connection = projection(tree)
        try:
            answer, steps = _gate(connection)
            assert answer is False
            gate.append(steps)
            changes = put({}, *change)
            _published, steps = _steps(
                connection, lambda: publish(connection, changes, changed=changes)
            )
            upkeep.append(steps)
            assert _gate(connection)[0] is False
        finally:
            connection.close()
    small, large = gate
    # B-tree depth may add a few steps at 100x the rows; never a step per end or row.
    assert large <= small + 40, (name, gate)
    small, large = upkeep
    assert large <= small * RATIO + SLACK["sql_vm_steps"], (name, upkeep)


def test_an_end_losing_its_last_observation_turns_the_gate_on_and_back_off() -> None:
    tree, _change = _history(20)
    connection = projection(tree)
    component = path_of(COMPONENT, "c")
    try:
        observation = claim(900_000, NOTE, literal("seen"), subject=component)
        changes = put({}, retired_claim(observation))
        publish(connection, changes, changed=changes)
        assert _gate(connection)[0] is True
        assert [row[0] for row in connection.execute("SELECT * FROM pair_end_unobserved")] == [
            component
        ]
        # Its pair's last fact retired: the end leaves the pairs holding live facts.
        fact = claim(5, SCOPE, literal("required"), subject=path_of(EXPOSURE, "c--v0"))
        changes = put({}, retired_claim(fact))
        publish(connection, changes, changed=changes)
        assert _gate(connection)[0] is False
    finally:
        connection.close()
