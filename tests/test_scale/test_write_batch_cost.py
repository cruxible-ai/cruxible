"""A batch write costs its changes: planning and its dry run grow linearly with the batch.

The per-change guard (``test_per_change_cost``) holds a write to its change
across instance sizes; this holds a write's work to its batch size. One world of
``ITEMS`` work items (a title Claim each, the write vocabulary of
``_write_support``) takes the same mixed batch at two sizes, ``SMALL`` and
``LARGE`` (eight times ``SMALL``) changes, and the work of each is counted (the
scale guard's counters, SQL on projection pieces included):

- ``plan``: write planning and its lowering onto one change set
  (``_Planner.build`` and ``_lower``), the ``plan`` and first ``lower`` phases;
- ``dry_run``: the whole write as a dry run (planning, authoring lowering and
  the preflight evaluation), which accepts nothing, so both sizes run on the
  same head.

The batch cycles through every planning route, each change on its own work item
(``0..LARGE-1``); the values and based-on pins name items no change touches
(``LARGE..ITEMS-1``):

0. a ``create`` and a ``set`` on its label;
1. a ``set`` on a Subject that does not exist (create by key);
2. a ``set`` revising a live title;
3. an ``add`` of another work item to a many-valued field;
4. a ``retire`` of a live Claim by id;
5. a ``set`` pinned ``based_on`` another work item's title;
6. a ``retire`` of a whole Subject ``with_claims``;
7. a ``create`` and an ``add`` naming another create's label as its value.

The rule, for every route and counter: ``LARGE <= LINEAR * SMALL + SLACK``, with
``LINEAR`` eight times the batch, each further change at most a quarter dearer
than the first ``SMALL``. A term in the square of the batch fails it: the one
found here (the shared resolver re-read every Claim the write retired, two SQL
statements each, for every Subject reference it resolved) cost ``plan`` 39,198
SQL statements at ``LARGE`` against 1,468 at ``SMALL``; it now costs 7,514 against
1,004.

Work that grows with the instance on every change is a constant per change, so
the ratio above cannot see it: planning's slot reads walked every live Claim of
the instance (``read_live_values`` sought the lifecycle index, ``read_live_nones``
the predicate's). ``CEILINGS`` holds ``plan`` at ``LARGE`` under what this build
measured, per change, with headroom, and well under what the unfixed build did.

Run alone with ``CRUXIBLE_WRITE_BATCH_REPORT=<path>`` to write every measurement.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.write import WriteRequest
from cruxible_core.governance.keys import generate_client_principal_key
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import get_registry, reset_registry
from cruxible_core.service.authoring.write_verbs import _lower, _Planner, _with_default_subject
from tests.core_support._write_support import KIND
from tests.test_scale import _counters, _worlds
from tests.test_scale._counters import COUNTERS, counting, trace_sqlite
from tests.test_scale._worlds import World, settle_advertisement
from tests.test_scale.test_per_change_cost import SLACK

SMALL, LARGE = 50, 400
#: Items the values and pins name, never changed by a batch.
TARGETS = 100
ITEMS = LARGE + TARGETS
#: ``LARGE <= LINEAR * SMALL``: SMALL changes, then the rest at most 1.25 times as dear each.
LINEAR = 1 + (LARGE // SMALL - 1) * 1.25
_DESCRIPTOR_ALIASES = ("/dev/fd", "/proc/self/fd")
#: Counters the routes are held to (spawns and opens are the ledger's, outside these).
COUNTED = tuple(
    name for name in COUNTERS if name not in {"git_spawns", "other_spawns", "file_opens"}
)


@dataclass
class _Session:
    world: World
    #: route -> batch size -> counter -> count
    measured: dict[str, dict[int, dict[str, int]]] = field(default_factory=dict)
    failed: dict[str, BaseException] = field(default_factory=dict)


def _build(directory: Path) -> World:
    """``ITEMS`` work items, each with a live title, in one accepted write."""

    instance_id = "inst_write_batch"
    registered = get_registry().create_governed_instance_with_id(instance_id)
    owner = generate_client_principal_key(
        directory / "operator-custody",
        principal_id=_worlds.OPERATOR,
        kind="ordinary",
        forbidden_roots=(Path(registered.record.location),),
    )
    playbill_api.playbill_init(instance_id, principals=(owner.principal,))
    world = World(
        shape=_worlds.Shape("batch", generations=0),
        instance_id=instance_id,
        owner=owner,
        workspace=_worlds._git_workspace(directory / "workspace"),  # noqa: SLF001
    )
    _worlds.seed_native_vocabulary(world)
    changes = []
    for index in range(ITEMS):
        item = f"{KIND}/w{index:04d}"
        world.items.append(item)
        changes.append({"op": "set", "subject": item, "field": "title", "value": item})
    outcome = _worlds.write(world, changes)
    assert outcome.status == "accepted", outcome
    settle_advertisement(world.instance)
    return world


def _batch(world: World, size: int) -> list[dict[str, Any]]:
    """``size`` changes cycling through every planning route, each on its own item."""

    changes: list[dict[str, Any]] = []
    index = 0
    while len(changes) < size:
        item = world.subject(index)
        target = world.subject(LARGE + index % TARGETS)
        route = index % 8
        if route == 0:
            changes.append(
                {"op": "create", "label": f"c{index}", "subject_kind": KIND, "key": f"c-{index}"}
            )
            changes.append(
                {"op": "set", "subject": {"created": f"c{index}"}, "field": "title", "value": "c"}
            )
        elif route == 1:
            changes.append(
                {"op": "set", "subject": f"{KIND}/n-{index}", "field": "title", "value": "n"}
            )
        elif route == 2:
            changes.append({"op": "set", "subject": item, "field": "title", "value": "revised"})
        elif route == 3:
            changes.append({"op": "add", "subject": item, "field": "governs", "value": target})
        elif route == 4:
            changes.append({"op": "retire", "target": world.claims[f"{item}#title"]})
        elif route == 5:
            changes.append(
                {
                    "op": "set",
                    "subject": item,
                    "field": "priority",
                    "value": "high",
                    "role": "observation",
                    "based_on": [world.claims[f"{target}#title"]],
                }
            )
        elif route == 6:
            changes.append({"op": "retire", "target": item, "with_claims": True})
        else:
            changes.append(
                {"op": "create", "label": f"g{index}", "subject_kind": KIND, "key": f"g-{index}"}
            )
            # The previous graph create's label, or this one's own on the first.
            named = f"g{index - 8}" if index >= 8 else f"g{index}"
            changes.append(
                {
                    "op": "add",
                    "subject": {"created": f"g{index}"},
                    "field": "governs",
                    "value": {"created": named},
                }
            )
        index += 1
    return changes[:size]


def _request(world: World, size: int) -> WriteRequest:
    return WriteRequest.model_validate(
        {"because": f"batch of {size}", "changes": _batch(world, size), "dry_run": True}
    )


def _plan(world: World, request: WriteRequest) -> object:
    """Planning and lowering as ``service_playbill_write`` runs them: in one projection scope."""

    instance = world.instance
    with instance.projection_scope():
        head = instance.accepted_coordinate()
        request = _with_default_subject(request)
        plan = _Planner(instance, head=head, read_at=head, request=request).build()
        return _lower(plan, because=request.because, planned_at=head)


def _dry_run(world: World, request: WriteRequest) -> object:
    outcome = playbill_api.playbill_write(world.instance_id, request=request)
    assert outcome.refusal is None, outcome.refusal
    assert outcome.status == "would_accept", outcome
    return outcome


ROUTES: dict[str, Callable[[World, WriteRequest], object]] = {
    "plan": _plan,
    "dry_run": _dry_run,
}


def _measure(world: World, run: Callable[[World, WriteRequest], object]) -> dict[int, Any]:
    # Warm once at the larger size, so both measured runs find every cache as warm
    # and the counts compare the work each batch does, never a first read.
    run(world, _request(world, LARGE))
    found: dict[int, Any] = {}
    for size in (SMALL, LARGE):
        request = _request(world, size)
        print(f"measuring {size}", file=sys.stderr, flush=True)
        with counting(world.root) as window:
            run(world, request)
        assert window.unexplained == [], window.unexplained[:5]
        found[size] = {
            **{counter: window.counts[counter] for counter in COUNTED},
            "statements": dict(window.statements.most_common(10)),
        }
    return found


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Session]:
    root = tmp_path_factory.mktemp("write-batch")
    state = root / "state"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("CRUXIBLE_STATE_ROOT", str(state))
        for name in (
            "CRUXIBLE_SERVER_AUTH",
            "CRUXIBLE_SERVER_TOKEN",
            "CRUXIBLE_MODE",
            "CRUXIBLE_DISABLED_CONSUMERS",
            "CRUXIBLE_CONSUMPTION_RECEIPTS",
        ):
            patch.delenv(name, raising=False)
        # No advertiser thread: it would run SQL on traced connections while a
        # window installs its progress handlers (the per-change guard does the same).
        patch.setattr(PlaybillInstance, "_start_advertiser_locked", lambda self: None)
        # A projection piece is opened through its descriptor's alias: count it too.
        prefixes = _counters._prefixes  # noqa: SLF001
        patch.setattr(_counters, "_prefixes", lambda path: (*prefixes(path), *_DESCRIPTOR_ALIASES))
        reset_permissions()
        reset_registry()
        reset_runtime_credential_store()
        get_playbill_manager().clear()
        try:
            with trace_sqlite(state):
                built = _Session(world=_build(root / "world"))
                for route, run in ROUTES.items():
                    print(f"route {route}", file=sys.stderr, flush=True)
                    try:
                        built.measured[route] = _measure(built.world, run)
                    except Exception as exc:  # noqa: BLE001 - reported by the route's cases
                        built.failed[route] = exc
                yield built
                report = os.environ.get("CRUXIBLE_WRITE_BATCH_REPORT")
                if report:
                    Path(report).write_text(json.dumps(built.measured, indent=1, sort_keys=True))
        finally:
            get_playbill_manager().clear()
            reset_runtime_credential_store()
            reset_registry()
            reset_permissions()


@pytest.mark.parametrize("counter", COUNTED)
@pytest.mark.parametrize("route", tuple(ROUTES))
def test_a_batch_write_grows_linearly_with_its_changes(
    session: _Session, route: str, counter: str
) -> None:
    if route in session.failed:
        raise session.failed[route]
    small = session.measured[route][SMALL][counter]
    large = session.measured[route][LARGE][counter]
    # Eight times the changes, each costing at most a quarter more than at SMALL:
    # a term in the square of the batch (q * N^2) shows once q exceeds ~1/180 of
    # the per-change cost.
    assert large <= LINEAR * small + SLACK[counter], (
        f"{route} {counter}: {SMALL} changes {small}, {LARGE} changes {large} "
        f"(allowed {LINEAR * small + SLACK[counter]:g}); heaviest statements at {LARGE}: "
        + json.dumps(session.measured[route][LARGE]["statements"], indent=1)[:1500]
    )


#: Absolute ceilings for ``plan`` at LARGE changes, per change, for work that grows
#: with the instance on every change (invisible to the ratio above). Each is what
#: this build measured with headroom, and well under what the unfixed build did
#: (the comment). Raise one deliberately, with the reason, never to make a run pass.
CEILINGS: dict[str, int] = {
    # Slot reads walked every live Claim (service/discovery/query_values.py).
    "sql_vm_steps": 1_300,  # measured 881; unfixed 3,770
    # The shared resolver re-read every retired Claim per reference (write_verbs.py).
    "sql_statements": 25,  # measured 18.8; unfixed 98
}


@pytest.mark.parametrize("counter", tuple(CEILINGS))
def test_planning_a_change_stays_under_its_measured_ceiling(
    session: _Session, counter: str
) -> None:
    if "plan" in session.failed:
        raise session.failed["plan"]
    per_change = session.measured["plan"][LARGE][counter] / LARGE
    assert per_change <= CEILINGS[counter], (counter, per_change, CEILINGS[counter])


def test_the_counts_are_reported(session: _Session) -> None:
    """The measured counts, for the slice report (``-s`` prints them)."""

    for route, sizes in session.measured.items():
        shown = {
            size: {name: count for name, count in counts.items() if name != "statements"}
            for size, counts in sizes.items()
        }
        print(route, shown)
    assert set(session.measured) | set(session.failed) == set(ROUTES)
