"""Pair writes cost what the change holds: never the unrelated pairs, linear in its pairs.

Two worlds of the pair vocabulary (``tests.core_support._pair_world``), SMALL and
LARGE, differ only in how many unrelated exposures they hold (each a pair of its
own component and one shared vulnerability, with a justification). Each world
then takes the same writes, activated, and their work is counted (the scale
guard's counters, SQL on projection pieces included):

- ``write.pairs_10`` and ``write.pairs_40``: one write creating 10 (40) new
  components and an exposure of each with an existing vulnerability, the ends
  named ``{created: label}`` (pending until lowering names them);
- ``write.pair_judgment``: the package judgment as one write -- retire the
  ``affected_by`` Claim, add ``none of V``, create the exposure, set its
  justification;
- ``write.pair_update_1``: one fact of one pair revised (an occurrence's scope).

Rules: for every route and counter ``LARGE <= SMALL * RATIO + SLACK`` (the
per-change guard's), and, in each world, ``pairs_40 <= 4.75 * pairs_10 + SLACK``
(the 30 further pairs cost at most a quarter more each than the first 10): the
work grows with the pairs the write creates, never faster.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._pair_world import (
    COMPONENT,
    EXPOSURE,
    INVENTORY,
    OCCURRENCE,
    VULN,
    accepted,
    create,
    pair_world,
    write,
)
from tests.test_scale import _counters
from tests.test_scale._counters import COUNTERS, counting, trace_sqlite
from tests.test_scale.test_per_change_cost import RATIO, SLACK

SMALL, LARGE = 100, 1_000
#: ``pairs_40 <= LINEAR * pairs_10``: ten pairs, then 30 more at most 1.25 times as dear each.
LINEAR = 1 + 3 * 1.25
BATCH = 160
_DESCRIPTOR_ALIASES = ("/dev/fd", "/proc/self/fd")
#: Counters the routes are held to (spawns and opens are the ledger's, outside these).
COUNTED = tuple(
    name for name in COUNTERS if name not in {"git_spawns", "other_spawns", "file_opens"}
)


@dataclass
class _World:
    name: str
    root: Path
    instance: PlaybillInstance
    #: How many components the routes have created, so each run names new ones.
    made: int = 0


@dataclass
class _Session:
    worlds: dict[str, _World]
    measured: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    failed: dict[str, BaseException] = field(default_factory=dict)


def _build(root: Path, name: str, unrelated: int) -> _World:
    root.mkdir(parents=True)
    instance = pair_world(root)
    accepted(
        write(
            instance,
            create("v", VULN, "CVE-0"),
            create("jc", COMPONENT, "pkg:pypi/judged@1"),
            create("jv", VULN, "CVE-JUDGED"),
            create("i", INVENTORY, "web"),
            create(
                "o", OCCURRENCE, {"inventory": {"created": "i"}, "component": {"created": "jc"}}
            ),
            {"op": "set", "subject": {"created": "o"}, "field": "scope", "value": "required"},
        )
    )
    accepted(
        write(
            instance,
            {
                "op": "add",
                "subject": f"{COMPONENT}/pkg-pypi-judged-1",
                "field": "affected_by",
                "value": f"{VULN}/cve-judged",
            },
        )
    )
    for start in range(0, unrelated, BATCH):
        changes: list[dict[str, Any]] = []
        for index in range(start, min(unrelated, start + BATCH)):
            changes.append(create(f"c{index}", COMPONENT, f"pkg:npm/unrelated-{index}@1"))
            changes.append(
                create(
                    f"e{index}",
                    EXPOSURE,
                    {"component": {"created": f"c{index}"}, "vulnerability": f"{VULN}/cve-0"},
                )
            )
            changes.append(
                {
                    "op": "set",
                    "subject": {"created": f"e{index}"},
                    "field": "justification",
                    "value": "vulnerable_code_not_present",
                }
            )
        accepted(write(instance, *changes))
    return _World(name=name, root=root, instance=instance)


def _pairs(count: int) -> Callable[[_World], object]:
    def run(world: _World) -> object:
        changes: list[dict[str, Any]] = []
        for index in range(world.made, world.made + count):
            changes.append(create(f"c{index}", COMPONENT, f"pkg:pypi/new-{index}@1"))
            changes.append(
                create(
                    f"e{index}",
                    EXPOSURE,
                    {"component": {"created": f"c{index}"}, "vulnerability": f"{VULN}/cve-0"},
                )
            )
        world.made += count
        return accepted(write(world.instance, *changes))

    return run


def _judgment(world: _World) -> object:
    component = f"{COMPONENT}/pkg-pypi-judged-1"
    vuln = f"{VULN}/cve-judged"
    return accepted(
        write(
            world.instance,
            {
                "op": "retire",
                "target": {"subject": component, "field": "affected_by", "value": vuln},
            },
            {"op": "add", "subject": component, "field": "affected_by", "value": {"none": vuln}},
            create("e", EXPOSURE, {"component": component, "vulnerability": vuln}),
            {
                "op": "set",
                "subject": {"created": "e"},
                "field": "justification",
                "value": "vulnerable_code_not_present",
            },
        )
    )


def _update(world: _World) -> object:
    return accepted(
        write(
            world.instance,
            {
                "op": "set",
                "subject": f"{OCCURRENCE}/web--pkg-pypi-judged-1",
                "field": "scope",
                "value": "optional",
            },
        )
    )


ROUTES: dict[str, Callable[[_World], object]] = {
    "write.pairs_10": _pairs(10),
    "write.pairs_40": _pairs(40),
    "write.pair_judgment": _judgment,
    "write.pair_update_1": _update,
}


def _measure(session: _Session, run: Callable[[_World], object]) -> dict[str, dict[str, int]]:
    found: dict[str, dict[str, int]] = {}
    for name, world in session.worlds.items():
        print(f"measuring {name}", file=sys.stderr, flush=True)
        with counting(world.root) as window:
            run(world)
        assert window.unexplained == [], window.unexplained[:5]
        found[name] = {counter: window.counts[counter] for counter in COUNTED}
    return found


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Session]:
    root = tmp_path_factory.mktemp("pair-cost")
    with pytest.MonkeyPatch.context() as patch:
        # No advertiser thread: it would run SQL on traced connections while a
        # window installs its progress handlers (the per-change guard does the same).
        patch.setattr(PlaybillInstance, "_start_advertiser_locked", lambda self: None)
        # A projection piece is opened through its descriptor's alias: count it too.
        prefixes = _counters._prefixes  # noqa: SLF001
        patch.setattr(_counters, "_prefixes", lambda path: (*prefixes(path), *_DESCRIPTOR_ALIASES))
        with trace_sqlite(root):
            built = _Session(
                worlds={
                    "small": _build(root / "small", "small", SMALL),
                    "large": _build(root / "large", "large", LARGE),
                }
            )
            for route, run in ROUTES.items():
                print(f"route {route}", file=sys.stderr, flush=True)
                try:
                    built.measured[route] = _measure(built, run)
                except Exception as exc:  # noqa: BLE001 - reported by the route's cases
                    built.failed[route] = exc
            yield built


@pytest.mark.parametrize("counter", COUNTED)
@pytest.mark.parametrize("route", tuple(ROUTES))
def test_a_pair_write_costs_the_same_beside_ten_times_the_pairs(
    session: _Session, route: str, counter: str
) -> None:
    if route in session.failed:
        raise session.failed[route]
    small = session.measured[route]["small"][counter]
    large = session.measured[route]["large"][counter]
    assert large <= small * RATIO + SLACK[counter], (route, counter, small, large)


@pytest.mark.parametrize("counter", COUNTED)
@pytest.mark.parametrize("world", ["small", "large"])
def test_pair_creation_grows_linearly_with_the_pairs_a_write_creates(
    session: _Session, world: str, counter: str
) -> None:
    for route in ("write.pairs_10", "write.pairs_40"):
        if route in session.failed:
            raise session.failed[route]
    ten = session.measured["write.pairs_10"][world][counter]
    forty = session.measured["write.pairs_40"][world][counter]
    # Four times the pairs, each costing at most a quarter more than at ten: a
    # term in the square of the pairs (q * N^2) shows once q exceeds ~1/150 of
    # the per-pair cost.
    assert forty <= LINEAR * ten + SLACK[counter], (counter, ten, forty)


def test_the_counts_are_reported(session: _Session) -> None:
    """The measured counts, for the slice report (``-s`` prints them)."""

    for route, worlds in session.measured.items():
        print(
            route,
            {
                name: {k: worlds[name][k] for k in ("sql_statements", "sql_vm_steps")}
                for name in worlds
            },
        )
    assert set(session.measured) | set(session.failed) == set(ROUTES)
