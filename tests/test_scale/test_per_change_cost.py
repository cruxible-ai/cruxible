"""Per-change cost at adoption size: one change costs the same at ten times the instance.

``test_scale_guard`` holds every route to its change between worlds of about 20
and 200 work items. Several costs only show at adoption size: a case-fold check
that walked a flat directory per added path, a slot lookup the planner turned
into a walk of a predicate, lowering that re-indexed every staged Claim per
member, a catch-up that re-bound the projection per Claim, ledger reads that
resolved the repository path and re-read a commit per call. This guard builds
two worlds of one shape, SMALL and LARGE (ten times SMALL), each grown by bulk
writes of up to 500 creates (a Subject, its key Claim and a title Claim each),
then applies the same change to both and counts its work, never its time.

Sizes: by default 100 and 1,000 work items, so the ordinary run (CI included)
stays near the scale guard's cost. The adoption-size run, 500 and 5,000 (about
five minutes of world building), is opt-in like the repository's other heavy
suites: ``CRUXIBLE_RUN_ADOPTION_SCALE_TESTS=1``. ``CRUXIBLE_PER_CHANGE_SIZES=a,b``
picks any other pair (``a`` at least 100: the Lines name 100 existing items).

Routes:

- ``write.trivial``: one title revision;
- ``write.create_100``: one write creating 100 work items, activated (100 new
  names in the flat Subject directory);
- ``line.propose_100``: one Line run proposing a Claim on each of 100 items;
- ``proposal.activate_100``: activating that proposal.

Counters are the scale guard's (``_counters.py``), with SQL counted on projection
pieces too (opened through their descriptor's ``/dev/fd`` alias, outside the
state root, which the scale guard's tracing does not see), plus four of this
file's, each counted on the measuring thread inside the window:

- ``realpaths``: calls of ``os.path.realpath``;
- ``piece_bytes_hashed``: bytes of whole projection pieces hashed
  (``physical_file_digest`` at build, the descriptor digest at a cold bind);
- ``git_listed_names``: names ledger directory listings returned
  (``GitLedger.tree_child_names``);
- ``persistent_sets``: ``PersistentMap.set`` calls.

The rule, for every route and counter: ``LARGE <= SMALL * 1.5 + SLACK``. A
counter that still grows for a reason outside this guard's fixes is a strict
xfail naming it, so a fix turns it into an XPASS failure that forces the mark off.
Work that grows with the change but not with the instance (per-read path
resolution, per-member re-indexing, per-Claim projection binds) is invisible to
that ratio, so a few counters also carry an absolute ceiling (``CEILINGS``).
Also ``tick.facts``: the next facts catching up over the routes' generations.

Run alone with ``CRUXIBLE_PER_CHANGE_REPORT=<path>`` to write every measurement.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.persistent import PersistentMap
from cruxible_client.contracts.procedure_mandates import (
    ProcedureMandate,
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.artifacts import render_procedure
from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec
from cruxible_client.contracts.procedures.models import ProposeChangeSetNode
from cruxible_core.compiler import assembler
from cruxible_core.governance.keys import generate_client_principal_key
from cruxible_core.indexes import sqlite as projection_sqlite
from cruxible_core.ledger.git import GitLedger
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import get_registry, reset_registry
from cruxible_core.service.procedures.procedure_runs import LineRunRequest
from tests.core_support._write_support import KIND
from tests.core_support._write_support import seed_write_vocabulary as _seed_vocabulary
from tests.test_scale import _counters, _worlds
from tests.test_scale._counters import COUNTERS, counting, trace_sqlite
from tests.test_scale._worlds import World, settle_advertisement

_ADOPTION_SCALE_ENV = "CRUXIBLE_RUN_ADOPTION_SCALE_TESTS"
SMALL, LARGE = (
    tuple(int(size) for size in os.environ["CRUXIBLE_PER_CHANGE_SIZES"].split(","))
    if os.environ.get("CRUXIBLE_PER_CHANGE_SIZES")
    else (500, 5_000)
    if os.environ.get(_ADOPTION_SCALE_ENV, "").lower() in {"1", "true", "yes", "on"}
    else (100, 1_000)
)
BATCH = 500
ITEMS = 100
LINE = "cost-propose"
CREATE = "cost-create"

_DESCRIPTOR_ALIASES = ("/dev/fd", "/proc/self/fd")
EXTRA = ("realpaths", "piece_bytes_hashed", "git_listed_names", "persistent_sets")
ALL = (*COUNTERS, *EXTRA)
RATIO = 1.5
SLACK = {
    "claim_parses": 2,
    "canonical_encodes": 16,
    "canonical_digests": 8,
    "file_opens": 4,
    "git_spawns": 1,
    "other_spawns": 0,
    "sql_statements": 16,
    "sql_vm_steps": 1024,
    "projection_binds": 1,
    "cas_body_reads": 2,
    "git_blob_reads": 4,
    "realpaths": 4,
    "piece_bytes_hashed": 0,
    "git_listed_names": 64,
    "persistent_sets": 256,
}
assert tuple(SLACK) == ALL


# -- the worlds ---------------------------------------------------------------------


def _created(index: int) -> dict[str, object]:
    """A Subject created by its key, as an import Line creates one per exported row."""

    return {
        "tag": "playbill-procedure-subject-proposal-item-v1",
        "subject_kind": KIND,
        "key": {"slug": f"made-{index:03d}"},
        "rationale": "Listed in the export.",
        "source": {
            "tag": "playbill-self-source-body-v1",
            "content_base64": base64.b64encode(f"made {index}\n".encode()).decode(),
        },
    }


def _propose_line(
    name: str, templates: tuple[dict[str, object], ...], namespace: tuple[str, ...]
) -> dict[str, bytes]:
    procedure = _worlds._procedure(  # noqa: SLF001
        name,
        terminal=ProposeChangeSetNode(
            node_id="propose", candidate_templates=templates, result="$steps.result"
        ),
        capability=2,
    )
    line = _worlds._line(f"{name}-line", procedure, authority="propose")  # noqa: SLF001
    mandate = ProcedureMandate(
        identity=ArtifactIdentity(kind="ProcedureMandate", name=f"{name}-mandate"),
        procedure=_worlds._pin(procedure),  # noqa: SLF001
        grants="propose",
        resource_ceiling=procedure.procedure.definition.hard_caps,
        namespace=namespace,
        valid_from=_worlds._FAR_PAST,  # noqa: SLF001
        expires_at=_worlds._FAR_FUTURE,  # noqa: SLF001
    )
    return {
        procedure.path: render_procedure(procedure.procedure),
        line_spec_path(line.identity.name): render_line_spec(line),
        procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
    }


def _definitions(world: World) -> dict[str, bytes]:
    """Two propose Lines: a priority Claim on each of 100 items; 100 new items by key."""

    return {
        **_propose_line(
            LINE,
            tuple(
                _worlds._item(world.subject(index), "high")  # noqa: SLF001
                for index in range(ITEMS)
            ),
            ("claims",),
        ),
        **_propose_line(
            CREATE,
            tuple(_created(index) for index in range(ITEMS)),
            ("claims", f"subjects/{KIND}"),
        ),
    }


def _build(directory: Path, name: str, items: int) -> World:
    """A world of ``items`` work items, grown by bulk writes straight to the ledger."""

    instance_id = f"inst_cost_{name}"
    registered = get_registry().create_governed_instance_with_id(instance_id)
    managed = Path(registered.record.location)
    owner = generate_client_principal_key(
        directory / "operator-custody",
        principal_id=_worlds.OPERATOR,
        kind="ordinary",
        forbidden_roots=(managed,),
    )
    playbill_api.playbill_init(instance_id, principals=(owner.principal,))
    world = World(
        shape=_worlds.Shape(name, generations=0),
        instance_id=instance_id,
        owner=owner,
        workspace=_worlds._git_workspace(directory / "workspace"),  # noqa: SLF001
    )
    _seed_vocabulary(world.instance, actor_id=_worlds.OPERATOR)
    for start in range(0, items, BATCH):
        changes = []
        for index in range(start, min(items, start + BATCH)):
            item = f"{KIND}/b{index:05d}"
            world.items.append(item)
            changes.append({"op": "set", "subject": item, "field": "title", "value": item})
        outcome = _worlds.write(world, changes)
        assert outcome.status == "accepted", outcome
        settle_advertisement(world.instance)
    _worlds.accept_members(world, _definitions(world), name=f"cost-definitions-{name}")
    # The next facts are built once here, so the measured tick is a catch-up.
    _worlds.tick(world, "facts")
    return world


# -- this file's counters -------------------------------------------------------------


_CURRENT: list[Counter[str]] = []


def _caller(counter: str) -> None:
    """Note the innermost product frames of a counted call, for a failing case's message."""

    if not _CURRENT:
        return
    frames = []
    frame = sys._getframe(2)  # noqa: SLF001
    while frame is not None and len(frames) < 4:
        name = frame.f_code.co_filename
        if "/src/cruxible_core/" in name:
            frames.append(f"{name.split('/src/')[-1]}:{frame.f_lineno}")
        frame = frame.f_back
    _CURRENT[-1][f"{counter} <- " + " <- ".join(frames)] += 1


def _measuring() -> _counters.Window | None:
    window = _counters._WINDOW  # noqa: SLF001
    return window if window is not None and window.measuring() else None


@contextmanager
def _extra_counters() -> Iterator[None]:
    """Count this file's four counters into the open window (none outside one)."""

    with pytest.MonkeyPatch.context() as patch:
        realpath = os.path.realpath

        def counted_realpath(path: Any, *args: Any, **kwargs: Any) -> str:
            window = _measuring()
            if window is not None:
                window.counts["realpaths"] += 1
                _caller("realpaths")
            return realpath(path, *args, **kwargs)

        patch.setattr(os.path, "realpath", counted_realpath)

        physical = projection_sqlite.physical_file_digest

        def counted_physical(path: Path) -> Any:
            window = _measuring()
            if window is not None:
                window.counts["piece_bytes_hashed"] += path.stat().st_size
                _caller("piece_bytes_hashed")
            return physical(path)

        patch.setattr(projection_sqlite, "physical_file_digest", counted_physical)
        patch.setattr(assembler, "physical_file_digest", counted_physical)

        descriptor_digest = projection_sqlite._descriptor_digest  # noqa: SLF001

        def counted_descriptor(descriptor: int) -> str:
            window = _measuring()
            if window is not None:
                window.counts["piece_bytes_hashed"] += os.fstat(descriptor).st_size
                _caller("piece_bytes_hashed")
            return descriptor_digest(descriptor)

        patch.setattr(projection_sqlite, "_descriptor_digest", counted_descriptor)

        child_names = GitLedger.tree_child_names

        def counted_names(self: GitLedger, oid: str, directory: str) -> tuple[str, ...]:
            names = child_names(self, oid, directory)
            window = _measuring()
            if window is not None:
                window.counts["git_listed_names"] += len(names)
                _caller("git_listed_names")
            return names

        patch.setattr(GitLedger, "tree_child_names", counted_names)

        persistent_set = PersistentMap.set

        def counted_set(self: Any, *args: Any, **kwargs: Any) -> Any:
            window = _measuring()
            if window is not None:
                window.counts["persistent_sets"] += 1
            return persistent_set(self, *args, **kwargs)

        patch.setattr(PersistentMap, "set", counted_set)
        yield


# -- the routes -----------------------------------------------------------------------


def _trivial(world: World) -> object:
    outcome = _worlds.write(
        world, [{"op": "set", "subject": world.subject(7), "field": "title", "value": "trivial"}]
    )
    assert outcome.status == "accepted", outcome
    return outcome


def _create_100(world: World) -> object:
    outcome = _worlds.write(
        world,
        [
            {"op": "set", "subject": f"{KIND}/new-{index:03d}", "field": "title", "value": "new"}
            for index in range(ITEMS)
        ],
    )
    assert outcome.status == "accepted", outcome
    return outcome


def _run(name: str) -> Callable[[World], object]:
    def run(world: World) -> object:
        digest = _worlds.line_digest(name)
        state = playbill_api.playbill_line_run(
            world.instance_id, digest, request=LineRunRequest(line=digest)
        )
        assert state.status == "succeeded", state.terminal
        (egress,) = state.terminal_egress
        assert egress.verdict == "delivered", egress
        world.terminal_proposals[name] = egress.proposal_id
        return state

    return run


def _activate(name: str) -> Callable[[World], object]:
    def run(world: World) -> object:
        proposal_id = world.terminal_proposals[name]
        assert proposal_id is not None
        receipt = playbill_api.playbill_activate(world.instance_id, proposal_id)
        assert receipt.status == "accepted", receipt
        world.generation += 1
        return receipt

    return run


ROUTES: dict[str, Callable[[World], object]] = {
    "write.trivial": _trivial,
    "write.create_100": _create_100,
    "line.propose_100": _run(LINE),
    "proposal.activate_100": _activate(LINE),
    "line.create_100": _run(CREATE),
    "proposal.activate_create_100": _activate(CREATE),
    # The next facts catching up over every generation the routes above accepted.
    "tick.facts": lambda world: _worlds.tick(world, "facts"),
}

_PIECE_DIGEST = (
    "every generation hashes its whole projection piece for the manifest's physical "
    "digest (compiler/assembler.py:306 -> indexes/sqlite.py physical_file_digest); taking "
    "it off the build path changes when manifests are published and is held for a ruling "
    "(coordinator-scratch/dispatch/report-perf-per-change.md, fix 4)"
)

#: route -> counter -> why it still grows (file:line): each is a strict xfail.
GROWS: dict[str, dict[str, str]] = {
    route: {"piece_bytes_hashed": _PIECE_DIGEST}
    for route in (
        "write.trivial",
        "write.create_100",
        "proposal.activate_100",
        "proposal.activate_create_100",
    )
}


@dataclass
class Session:
    small: World
    large: World
    measured: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    failed: dict[str, BaseException] = field(default_factory=dict)


def _measure(session: Session, run: Callable[[World], object]) -> dict[str, dict[str, Any]]:
    measured = {}
    for world in (session.small, session.large):
        settle_advertisement(world.instance)
        _CURRENT.append(Counter())
        try:
            with counting(world.root) as window:
                run(world)
                settle_advertisement(world.instance)
        finally:
            callers = _CURRENT.pop()
        if window.unexplained:
            raise AssertionError(f"uncounted SQL on {world.shape.name}: {window.unexplained[:5]}")
        measured[world.shape.name] = {
            **{name: window.counts[name] for name in ALL},
            "statements": dict(window.statements.most_common(20)),
            "callers": dict(callers.most_common(20)),
        }
    return measured


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Session]:
    root = tmp_path_factory.mktemp("cost")
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
        patch.setattr(PlaybillInstance, "_start_advertiser_locked", lambda self: None)
        # A projection piece is opened through its verified descriptor's alias
        # (``/dev/fd/N``), which is not under the state root: count those
        # connections' SQL too, or every projection read would go unseen.
        prefixes = _counters._prefixes  # noqa: SLF001
        patch.setattr(_counters, "_prefixes", lambda path: (*prefixes(path), *_DESCRIPTOR_ALIASES))
        reset_permissions()
        reset_registry()
        reset_runtime_credential_store()
        get_playbill_manager().clear()
        try:
            with trace_sqlite(state):
                built = Session(
                    small=_build(root / "small", "small", SMALL),
                    large=_build(root / "large", "large", LARGE),
                )
                with _extra_counters():
                    for name, run in ROUTES.items():
                        try:
                            built.measured[name] = _measure(built, run)
                        except Exception as exc:  # noqa: BLE001 - reported by the route's cases
                            built.failed[name] = exc
                yield built
                report = os.environ.get("CRUXIBLE_PER_CHANGE_REPORT")
                if report:
                    Path(report).write_text(json.dumps(built.measured, indent=1, sort_keys=True))
        finally:
            get_playbill_manager().clear()
            reset_runtime_credential_store()
            reset_registry()
            reset_permissions()


def _cases() -> list[Any]:
    return [
        pytest.param(
            route,
            counter,
            id=f"{route}-{counter}",
            marks=(
                pytest.mark.xfail(strict=True, reason=GROWS[route][counter])
                if counter in GROWS.get(route, {})
                else ()
            ),
        )
        for route in ROUTES
        for counter in ALL
    ]


@pytest.mark.parametrize(("route", "counter"), _cases())
def test_one_change_costs_the_same_at_ten_times_the_instance(
    session: Session, route: str, counter: str
) -> None:
    if route in session.failed:
        raise session.failed[route]
    measured = session.measured[route]
    small, large = measured["small"][counter], measured["large"][counter]
    assert large <= small * RATIO + SLACK[counter], (
        f"{route} {counter}: SMALL {small}, LARGE {large} "
        f"(allowed {small * RATIO + SLACK[counter]:g}); LARGE callers and heaviest statements: "
        + json.dumps(measured["large"]["callers"], indent=1)[:1500]
        + json.dumps(measured["large"]["statements"], indent=1)[:1500]
    )


#: Absolute ceilings in LARGE for work that grows with the change rather than the
#: instance, so the ratio above cannot see it regress. Each is what this build
#: measured at 5,000 work items with headroom for run-to-run variation (a
#: quarter where the count is steady, twice where it is not), and well under what
#: the unfixed build did (the comment): a regression that brings back even part
#: of the growth fails. Smaller worlds measure no more. Raise one deliberately,
#: with the reason, never to make a run pass.
CEILINGS: dict[str, dict[str, int]] = {
    # The repository key resolved per ledger read (ledger/git.py _repository_key).
    "write.create_100": {
        "realpaths": 2 * 1_089,  # unfixed 7,375
        # Lowering re-indexed every staged Claim per member (derived/derived_state.py).
        # Measured 10,260-10,616 over five runs; one O(n^2) re-index of the
        # change's 200 Claims alone adds ~15k.
        "persistent_sets": 12_500,  # unfixed 25,774
    },
    "line.create_100": {"persistent_sets": 7_500},  # measured 6,139-6,169; unfixed 20,802
    "proposal.activate_100": {"realpaths": 2 * 222},  # unfixed 2,454
    "proposal.activate_create_100": {"realpaths": 2 * 222},  # unfixed 4,354
    "tick.facts": {
        "realpaths": 2 * 2_071,  # unfixed 20,059
        # The catch-up bound the projection per derived Claim and slot (next/facts.py).
        "sql_vm_steps": 2 * 1_115_454,  # unfixed 7,362,786
    },
}
assert all(set(counters) <= set(ALL) for counters in CEILINGS.values())


@pytest.mark.parametrize(
    ("route", "counter"),
    [(route, counter) for route, counters in CEILINGS.items() for counter in counters],
)
def test_one_change_stays_under_its_measured_ceiling(
    session: Session, route: str, counter: str
) -> None:
    if route in session.failed:
        raise session.failed[route]
    large = session.measured[route]["large"][counter]
    assert large <= CEILINGS[route][counter], (
        f"{route} {counter}: LARGE {large} over its ceiling {CEILINGS[route][counter]}; "
        + json.dumps(session.measured[route]["large"]["callers"], indent=1)[:1500]
    )
