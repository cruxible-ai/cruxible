"""Procedure pair rows cost the change: flat in unrelated state and history, linear in the pairs.

Two worlds of the pair-row vocabulary (``tests.core_support._pair_procedure_support``),
SMALL and LARGE, differ only in history and unrelated state, ten times over:

- inventory ``web`` holds 10 current occurrences (a live ``scope``) and
  SMALL/LARGE historical shells (occurrences whose ``scope`` a complete import
  retired), all of one SBOM Line judging absence by inventory;
- component ``pkg:pypi/hub@1`` holds 10 current occurrences across inventories
  and SMALL/LARGE shells, of a Line judging absence by component;
- SMALL/LARGE unrelated inventories, one occurrence each, from a third Line.

Routes, each measured once per world (the run and its activation):

- ``line.absence_1_end.inventory`` / ``.component``: the complete source drops
  one of the end's 10 current units: one retirement, judged over the pair index
  (a shell holds no row, so it costs nothing);
- ``line.sbom_10`` / ``line.sbom_40``: a first import of an SBOM with 10 (40)
  new components into a new inventory, beside the unrelated inventories;
- ``line.sbom_repeat``: the same SBOM again: nothing changes.

Rules: for every route and counter ``LARGE <= SMALL * RATIO + SLACK`` (the
per-change guard's), and, in each world, ``sbom_40 <= LINEAR * sbom_10 + SLACK``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._pair_procedure_support import (
    activate,
    install,
    pair_procedure_world,
    pair_spec,
    rows,
    run,
    write_doc,
)
from tests.test_scale import _counters
from tests.test_scale._counters import COUNTERS, counting, trace_sqlite
from tests.test_scale.test_per_change_cost import RATIO, SLACK

#: The worlds are seeded through Line runs (about 15 per world): allow the build its time.
pytestmark = pytest.mark.timeout(3600)

SMALL, LARGE = 100, 1_000
LINEAR = 1 + 3 * 1.25
CURRENT = 10
BATCH = 200
HUB = "pkg:pypi/hub@1"
_DESCRIPTOR_ALIASES = ("/dev/fd", "/proc/self/fd")
COUNTED = tuple(
    name for name in COUNTERS if name not in {"git_spawns", "other_spawns", "file_opens"}
)
LINES = {
    "inventory": ("test.by-inventory", "inv"),
    "component": ("test.by-component", "comp"),
    "unrelated": ("test.unrelated", "unrelated"),
    "sbom": ("test.sbom", "sbom"),
}


@dataclass
class _World:
    name: str
    root: Path
    instance: PlaybillInstance
    workspace: Path
    minute: int = 0

    def run(self, line: str) -> Any:
        self.minute += 1
        state = run(self.instance, self.workspace, line=LINES[line][0], minute=self.minute)
        assert state.status == "succeeded", state.terminal.model_dump_json()
        return state

    def apply(self, line: str) -> Any:
        state = self.run(line)
        if state.terminal_egress[0].proposal_id is not None:
            activate(self.instance, state)
        return state


@dataclass
class _Session:
    worlds: dict[str, _World]
    measured: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    failed: dict[str, BaseException] = field(default_factory=dict)


def _batches(items: list[Any]) -> list[list[Any]]:
    """Seed in batches, each well under a proposal's changed-member limit."""

    return [items[start : start + BATCH] for start in range(0, len(items), BATCH)]


def _purls(prefix: str, count: int) -> list[str]:
    return [f"pkg:pypi/{prefix}-{index}@1" for index in range(count)]


def _build(root: Path, name: str, size: int) -> _World:
    root.mkdir(parents=True)
    instance, workspace = pair_procedure_world(root)
    for by, (line, doc) in LINES.items():
        spec = pair_spec(
            f"{line}-procedure",
            doc=doc,
            source=line,
            by=by if by in {"inventory", "component"} else "inventory" if by == "sbom" else None,
        )
        install(instance, spec, line=line)
    built = _World(name=name, root=root, instance=instance, workspace=workspace)
    current, shells = _purls("current", CURRENT), _purls("shell", size)
    # Inventory web: each import lists the 10 and one batch of shells, so it
    # retires the previous batch's scopes; the last lists the 10 alone.
    for batch in (*_batches(shells), []):
        write_doc(workspace, "inv", rows("web", current + batch), inventory="web")
        built.apply("inventory")
    # Component hub: occurrences in 10 current inventories and size others, the same way.
    others = [f"in-{index}" for index in range(CURRENT, CURRENT + size)]
    for batch in (*_batches(others), []):
        inventories = [f"in-{index}" for index in range(CURRENT)] + batch
        write_doc(
            workspace,
            "comp",
            [row for inv in inventories for row in rows(inv, [HUB])],
            component=HUB,
        )
        built.apply("component")
    # Unrelated inventories, one occurrence each.
    for batch in _batches(list(range(size))):
        write_doc(
            workspace,
            "unrelated",
            [row for index in batch for row in rows(f"u-{index}", [f"pkg:npm/u-{index}@1"])],
        )
        built.apply("unrelated")
    # The SBOM Line's first import, so its warm-up below reads a document.
    write_doc(workspace, "sbom", rows("warm", _purls("warm", 1)), inventory="warm")
    built.apply("sbom")
    return built


def _absence(by: str) -> Callable[[_World], object]:
    def route(world: _World) -> object:
        current = _purls("current", CURRENT)
        if by == "inventory":
            write_doc(world.workspace, "inv", rows("web", current[1:]), inventory="web")
        else:
            inventories = [f"in-{index}" for index in range(1, CURRENT)]
            write_doc(
                world.workspace,
                "comp",
                [row for inv in inventories for row in rows(inv, [HUB])],
                component=HUB,
            )
        state = world.apply(by)
        changes = sorted(c.change_kind for c in state.terminal_egress[0].children if c.change_kind)
        # One retirement; the nine still listed are re-observed from the new bytes.
        assert changes == ["reobservation"] * (CURRENT - 1) + ["retirement"], changes
        return state

    return route


def _sbom(count: int) -> Callable[[_World], object]:
    def route(world: _World) -> object:
        inventory = f"fresh-{count}"
        write_doc(
            world.workspace, "sbom", rows(inventory, _purls(inventory, count)), inventory=inventory
        )
        return world.apply("sbom")

    return route


def _repeat(world: _World) -> object:
    state = world.run("sbom")
    assert state.terminal_egress[0].proposal_id is None
    return state


ROUTES: dict[str, Callable[[_World], object]] = {
    "line.absence_1_end.inventory": _absence("inventory"),
    "line.absence_1_end.component": _absence("component"),
    "line.sbom_10": _sbom(10),
    "line.sbom_40": _sbom(40),
    "line.sbom_repeat": _repeat,
}


#: The Line each route runs. Before its window the Line runs once over its
#: unchanged source, so the amortized catch-ups since that Line last ran (the
#: refusal index, the retirement inventory's snapshot) are paid outside it: they
#: are O(the changes since), here the seeding, which differs between the worlds.
WARM = {
    "line.absence_1_end.inventory": "inventory",
    "line.absence_1_end.component": "component",
    "line.sbom_10": "sbom",
    "line.sbom_40": "sbom",
    "line.sbom_repeat": "sbom",
}


def _measure(
    session: _Session, route: Callable[[_World], object], line: str
) -> dict[str, dict[str, int]]:
    found: dict[str, dict[str, int]] = {}
    for name, world in session.worlds.items():
        print(f"measuring {name}", file=sys.stderr, flush=True)
        world.apply(line)
        # The projection catches up on the last activation before the window.
        with world.instance.bind_accepted_projection(world.instance.accepted_coordinate()):
            pass
        with counting(world.root) as window:
            route(world)
        assert window.unexplained == [], window.unexplained[:5]
        found[name] = {counter: window.counts[counter] for counter in COUNTED}
    return found


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Session]:
    root = tmp_path_factory.mktemp("pair-procedure-cost")
    with pytest.MonkeyPatch.context() as patch:
        # No advertiser thread: it would run SQL on traced connections while a
        # window installs its progress handlers (the per-change guard does the same).
        patch.setattr(PlaybillInstance, "_start_advertiser_locked", lambda self: None)
        prefixes = _counters._prefixes  # noqa: SLF001
        patch.setattr(_counters, "_prefixes", lambda path: (*prefixes(path), *_DESCRIPTOR_ALIASES))
        with trace_sqlite(root):
            built = _Session(
                worlds={
                    "small": _build(root / "small", "small", SMALL),
                    "large": _build(root / "large", "large", LARGE),
                }
            )
            for name, route in ROUTES.items():
                print(f"route {name}", file=sys.stderr, flush=True)
                try:
                    built.measured[name] = _measure(built, route, WARM[name])
                except Exception as exc:  # noqa: BLE001 - reported by the route's cases
                    built.failed[name] = exc
            yield built


@pytest.mark.parametrize("counter", COUNTED)
@pytest.mark.parametrize("route", tuple(ROUTES))
def test_a_pair_row_run_costs_the_same_beside_ten_times_the_history(
    session: _Session, route: str, counter: str
) -> None:
    if route in session.failed:
        raise session.failed[route]
    small = session.measured[route]["small"][counter]
    large = session.measured[route]["large"][counter]
    assert large <= small * RATIO + SLACK[counter], (route, counter, small, large)


@pytest.mark.parametrize("counter", COUNTED)
@pytest.mark.parametrize("world", ["small", "large"])
def test_an_sbom_import_grows_linearly_with_its_components(
    session: _Session, world: str, counter: str
) -> None:
    for route in ("line.sbom_10", "line.sbom_40"):
        if route in session.failed:
            raise session.failed[route]
    ten = session.measured["line.sbom_10"][world][counter]
    forty = session.measured["line.sbom_40"][world][counter]
    assert forty <= LINEAR * ten + SLACK[counter], (counter, ten, forty)


def test_the_measurements_report(session: _Session) -> None:
    """Every route measured (the numbers go to the slice report)."""

    import json
    import os

    assert not session.failed, {k: repr(v) for k, v in session.failed.items()}
    report = os.environ.get("PAIR_PROCEDURE_COUNTS")
    if report:
        with open(report, "w", encoding="utf-8") as handle:
            json.dump(session.measured, handle, indent=1, sort_keys=True)
