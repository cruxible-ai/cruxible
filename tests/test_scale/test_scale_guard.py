"""CI's scale guard: a route's work grows with the change it handles, not with the instance.

AGENTS.md "Cost model" made enforceable. Two worlds of one shape are built per
worker (``_worlds.py``): SMALL and LARGE, LARGE about ten times SMALL in work
items, Claims and bulk generations. Every route below applies the same small
change (or read, or consumer tick) to SMALL and then to LARGE, in-process
through the daemon's runtime facade and service layer, with background work run
explicitly once (the workspace advertiser's refresh, each consumer part's tick)
and never on a thread, and counts its work (``_counters.py``): Claim parses,
canonical encodes and digests, file opens under the instance's managed root,
git and other subprocess spawns, SQLite statements and the VM instructions they
ran (measured by a progress handler, never inferred from plans), projection
binds, CAS body reads and ledger blob reads. Counts, never time. Every case's
outcome is the same run to run, and most counts are too. A route that builds
new items (writes, dry runs, settles, attestations, the Line tick's run), and a
fold that reads them, varies by a few units between runs (in the runs observed,
at most 16 file opens, 16 canonical encodes or digests, 3 SQL statements and 374
VM steps, or about 2,000 for the new_capture attestation in LARGE, which fails
the rule by far more; parses, binds, CAS and blob reads never varied):
Claim ids and Capture digests are fresh each run, and the ledger's loose
objects, the CAS and the Merkle trie shard by hash, so ten new items land in a
varying number of directories. That is bounded by the change, and RATIO's
headroom absorbs it.

The rule, for every route and counter::

    LARGE <= SMALL * 1.25 + SLACK[counter]

``SLACK`` absorbs differences that are not growth: a key, generation number
or coordinate that renders one byte longer, a SQLite B-tree one level deeper
on the bigger file. Its per-counter constants:

========================  =====  ==============================================
counter                   slack  why
========================  =====  ==============================================
claim_parses              2      none expected; room for one revision chain step
canonical_encodes         16     a longer id or path re-encoded on a few paths
canonical_digests         8      as above, for digests
file_opens                4      a sharded directory or segment one level deeper
git_spawns                1      none expected
other_spawns              0      none expected
sql_statements            16     an index catch-up step per touched partition
sql_vm_steps              1024   those 16 statements at 64 steps each, the
                                 dearest bounded statement in the counter's
                                 self-tests (a five-row non-unique equality,
                                 54 steps; a unique seek is 15 at any size)
projection_binds          1      none expected
cas_body_reads            2      none expected
git_blob_reads            4      a tree listing one level deeper re-read
========================  =====  ==============================================

O(log n) work is allowed only through a named index lookup, which none of these
counters sees (a SQLite seek is one statement and one VM step whatever the
depth). A failing ``sql_vm_steps`` case names the statements behind it: the
heaviest ones in LARGE, with their steps in SMALL, the Python line that ran
them, their tables and how each cursor moved.

A route/counter that fails today carries ``xfail(strict=True)`` naming what
grows and where (file:line), so the package lands green and a fix turns the
case into an XPASS failure that forces removing the mark.

A route must also have done what it stands for: a window holding SQL that ran
on a connection without a step counter fails the route (never zero), and each
consumer tick
checks what it found due (``Route.verify``), so a tick that silently skipped
(an unbuilt index, nothing matched) cannot pass as cheap. The refusal index is
built and caught up when the worlds are, so the settled-refusals part schedules
from it: after a generation it has a generation to index, after a settle it has
the settle terminal's record to count, and idle it reads a current index.
Neither world holds a prediction contract, so the prediction part's ticks only
match; that part's run path is not measured here.

Two whole-population scans are deliberate cadence work and stay out: the daily
evidence sweep and the hourly prediction anchor retry. Both worlds retire those
two Triggers, so no tick here ever fires them.

Run with ``--dist loadfile`` (as CI does) when running in parallel: the worlds
are a module fixture, built once per worker that runs this module, so plain
``-n`` would spread the cases and build them on every worker. Run alone,
``CRUXIBLE_SCALE_REPORT=<path>`` writes every measurement as JSON.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.authoring.inputs import ChangeSetInput, TriggerInput
from cruxible_client.contracts.authoring.models import (
    AttestationAuthoringPayload,
    ChangeSetAuthoringPayload,
    authoring_member_identity,
)
from cruxible_client.contracts.compact_query import QueryRequest
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.triggers import GenerationAcceptedSchedule
from cruxible_core.coverage.contracts import CoverageAccessProfile
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import reset_registry
from cruxible_core.service.discovery.audit import PlaybillAuditRequestV1
from cruxible_core.service.discovery.next import NextRequest, NextWorkspaceObservation
from cruxible_core.service.procedures.procedure_runs import LineRunRequest
from tests.core_support._write_support import KIND
from tests.test_scale import _worlds
from tests.test_scale._counters import COUNTERS, counting, trace_sqlite
from tests.test_scale._worlds import World, settle_advertisement

SMALL = _worlds.Shape("small", generations=3)
LARGE = _worlds.Shape("large", generations=30)

RATIO = 1.25
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
}
assert tuple(SLACK) == COUNTERS

_ACCESS = CoverageAccessProfile(profile_id="scale", permitted_access_classes=("instance", "public"))


@dataclass
class Session:
    small: World
    large: World
    #: route -> world name -> counter -> count (and the statements behind sql_vm_steps).
    measured: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    #: route -> why it could not be measured.
    failed: dict[str, BaseException] = field(default_factory=dict)


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Session]:
    """Build both worlds, then measure every route in order, once per worker.

    Routes change the worlds, so they always run in the same order, whatever
    cases a run selects; each case only reads its measurement.
    """

    root = tmp_path_factory.mktemp("scale")
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
        # The advertiser's refresh runs where a window can count it, never on a thread.
        patch.setattr(PlaybillInstance, "_start_advertiser_locked", lambda self: None)
        reset_permissions()
        reset_registry()
        reset_runtime_credential_store()
        get_playbill_manager().clear()
        try:
            with trace_sqlite(state):
                built = Session(
                    small=_worlds.build_world(root / "small", SMALL),
                    large=_worlds.build_world(root / "large", LARGE),
                )
                for route in ROUTES.values():
                    try:
                        built.measured[route.name] = _measure(built, route)
                    except Exception as exc:  # noqa: BLE001 - reported by the route's cases
                        built.failed[route.name] = exc
                yield built
                report = os.environ.get("CRUXIBLE_SCALE_REPORT")
                if report:
                    Path(report).write_text(json.dumps(built.measured, indent=1, sort_keys=True))
        finally:
            get_playbill_manager().clear()
            reset_runtime_credential_store()
            reset_registry()
            reset_permissions()


# -- the routes -----------------------------------------------------------------------


@dataclass(frozen=True)
class Route:
    name: str
    run: Callable[[World], object]
    #: Work done before the window opens (moving the head, reopening the instance).
    prepare: Callable[[World], object] | None = None
    #: Checked after the window closes, with what ``run`` returned: the route did
    #: the work it stands for (a tick that was due ran), so a silent zero fails.
    verify: Callable[[World, Any], None] | None = None
    #: counter -> why it grows today (file:line); each becomes a strict xfail.
    grows: dict[str, str] = field(default_factory=dict)


def _now() -> datetime:
    return datetime.now(UTC)


def _creates(world: World, tag: str) -> list[dict[str, Any]]:
    return [
        {"op": "set", "subject": f"{KIND}/{tag}-{index:02d}", "field": "title", "value": tag}
        for index in range(10)
    ]


def _revisions(world: World, tag: str) -> list[dict[str, Any]]:
    """Revise the title of the first ten work items (no bulk write touches a title twice)."""

    return [
        {
            "op": "set",
            "subject": world.subject(index),
            "field": "title",
            "value": f"{tag} {world.subject(index)}",
        }
        for index in range(10)
    ]


def _write(
    changes: Callable[[World], list[dict[str, Any]]], **options: Any
) -> Callable[[World], object]:
    def run(world: World) -> object:
        outcome = _worlds.write(world, changes(world), **options)
        expected = "would_accept" if options.get("dry_run") else "accepted"
        assert outcome.status == expected, outcome
        return outcome

    return run


def _write_doc_sections(world: World) -> object:
    """Count one identical section/Claim write in SMALL and ten-times LARGE."""
    from cruxible_client.authoring.doc_writes import write_workspace
    from cruxible_client.contracts.write import WriteRequest

    request = WriteRequest.model_validate(
        {
            "because": "The edited section records the basis.",
            "docs": [
                {
                    "op": "append",
                    "id": "basis",
                    "file": "doc-write.md",
                    "text": "# Decision\nThe checks passed.\n",
                }
            ],
            "changes": [
                {
                    "op": "set",
                    "subject": f"{KIND}/doc-scale",
                    "field": "title",
                    "value": "Document write",
                    "evidence": {
                        "kind": "section",
                        "ref": "basis",
                    },
                }
            ],
        }
    )
    result = write_workspace(
        request,
        workspace=world.workspace,
        submit=lambda observed: playbill_api.playbill_write(world.instance_id, request=observed),
    )
    assert result.status == "accepted" and result.docs[0].written, result
    return result


def _typed_creates(world: World) -> list[dict[str, Any]]:
    """Two Subjects by ``create`` changes, one naming the other by its label."""

    return [
        {"op": "create", "label": "a", "subject_kind": KIND, "key": "typed-a"},
        {"op": "create", "label": "b", "subject_kind": KIND, "key": "typed-b"},
        {"op": "set", "subject": {"created": "a"}, "field": "title", "value": "typed a"},
        {"op": "add", "subject": {"created": "b"}, "field": "governs", "value": {"created": "a"}},
    ]


_HISTORY_SUBJECT = f"{KIND}/history-subject"


def _revise(world: World, subject: str, count: int) -> None:
    for revision in range(count):
        _worlds.write(
            world,
            [{"op": "set", "subject": subject, "field": "title", "value": f"revision {revision}"}],
        )
    settle_advertisement(world.instance)


def _subject_history(subject: str, *, limit: int | None = None) -> Callable[[World], object]:
    def run(world: World) -> object:
        result = playbill_api.playbill_get(
            world.instance_id, request=GetRequest(ref=subject, detail="history", limit=limit)
        )
        assert result.history is not None and result.history.kind == "subject", result
        assert len(result.history.events) >= min(limit or 6, 6), result.history
        return result

    return run


def _tick(part: str) -> Callable[[World], object]:
    def run(world: World) -> object:
        return _worlds.tick(world, part)

    return run


#: Whether a tick of each part has work after a generation: the triggers fire
#: floor.refresh and curation.detect, the armed Line's GenerationAccepted Trigger
#: is due, the new Claims' Captures wait for the evidence check, next's facts and the
#: floor have a new target, curation a new generation, and the refusal index a
#: generation to catch up. Neither world holds a prediction contract, so the
#: prediction part only ever matches. Idle, nothing is due anywhere.
_DUE_AFTER_GENERATION = {
    "triggers": True,
    "line": True,
    "floor": True,
    "facts": True,
    "evidence": True,
    "prediction": False,
    "curation": True,
    "settled_refusals": True,
    "line_upgrades": False,
}


def _ticked(part: str, *, due: bool) -> Callable[[World, Any], None]:
    def verify(world: World, units: Any) -> None:
        assert bool(units) == due, f"{part} tick on {world.shape.name}: due {units!r}"
        if part == "settled_refusals":
            # The current-index path, never the unbuilt-index skip.
            assert _worlds.refusals_behind(world) is False, world.shape.name

    return verify


def _settle_pending(world: World) -> None:
    """A settle terminal's record waits past the refusal index's counting cursor."""

    assert _worlds.refusals_behind(world) is True, world.shape.name
    world.prepared = _worlds.refusal_cursor(world)


def _settle_counted(world: World, units: Any) -> None:
    assert units, f"settled_refusals tick on {world.shape.name} found nothing due"
    assert _worlds.refusal_cursor(world) > world.prepared, world.shape.name
    assert _worlds.refusals_behind(world) is False, world.shape.name


def _orient(world: World) -> object:
    return playbill_api.playbill_orient(world.instance_id)


def _orient_generations(world: World) -> object:
    result = playbill_api.playbill_orient(world.instance_id, section="generations", limit=5)
    assert result.generations is not None and len(result.generations) == 5, result
    assert result.truncated, result
    return result


def _orient_graph(world: World) -> object:
    """The graph's first page: five of the world's live Subjects, never the rest."""

    result = playbill_api.playbill_orient(world.instance_id, section="graph", limit=5)
    assert result.graph is not None and len(result.graph.nodes) == 5, result
    assert result.graph.totals.subjects >= len(world.items) and result.truncated, result
    return result


def _orient_pins(world: World) -> object:
    """The definition pins' first page: three rows, never a Claim's or a Subject's pins."""

    result = playbill_api.playbill_orient(world.instance_id, section="pins", limit=3)
    assert result.pins is not None and len(result.pins) == 3 and result.truncated, result
    return result


def _since_first_page(world: World) -> object:
    """The first page after genesis: only its generations are read, never the rest."""

    result = playbill_api.playbill_since(
        world.instance_id,
        request={
            "generation": 0,
            "access_profile": _ACCESS.model_dump(mode="json"),
            "max_rows": 10,
        },
    )
    assert len(result.rows) == 10 and result.truncated, result
    return result


def _line_runs(world: World) -> object:
    result = playbill_api.playbill_get(
        world.instance_id,
        request=GetRequest(ref=f"Line:{_worlds.SETTLE}-line", detail="runs", limit=5),
    )
    assert result.runs is not None and result.runs.runs, result
    return result


def _get(ref: Callable[[World], str]) -> Callable[[World], object]:
    def run(world: World) -> object:
        return playbill_api.playbill_get(world.instance_id, request=GetRequest(ref=ref(world)))

    return run


def _query_where(world: World) -> object:
    ids = [world.subject(index).split("/", 1)[1] for index in range(10, 20)]
    return playbill_api.playbill_query(
        world.instance_id,
        request=QueryRequest.model_validate(
            {"kind": KIND, "where": [{"field": "subject_id", "in": ids}], "select": ["status"]}
        ),
    )


def _query_named(world: World) -> object:
    return playbill_api.playbill_query(
        world.instance_id, request=QueryRequest(name=_worlds.QUERY_NAME)
    )


def _query_definitions(kind: str) -> Callable[[World], object]:
    """One definition kind's listing: the world's definitions, never its Claims or Subjects."""

    def run(world: World) -> object:
        result = playbill_api.playbill_query(world.instance_id, request=QueryRequest(kind=kind))
        assert result.rows, (kind, result)
        return result

    return run


def _next(*, sources: bool = False) -> Callable[[World], object]:
    def run(world: World) -> object:
        return playbill_api.playbill_next(
            world.instance_id,
            request=NextRequest(
                evaluation_time=_now(),
                access_profile=_ACCESS,
                workspace_observation=(
                    NextWorkspaceObservation(source_observations=()) if sources else None
                ),
            ),
        )

    return run


def _observe_reports(world: World) -> None:
    """The workspace's ``reports.md``, observed client-side through its file catalog."""

    from cruxible_client.authoring.selectors import WorkspaceFiles
    from cruxible_core.coverage.workspace import bindings_from_mapping, observe_workspace

    bindings = bindings_from_mapping(WorkspaceFiles(world.workspace).coverage_bindings())
    world.prepared = observe_workspace(bindings, root=world.workspace, files=("reports.md",))


def _coverage(world: World) -> object:
    return playbill_api.playbill_resolve_coverage(world.instance_id, observations=world.prepared)


def _audit(world: World) -> object:
    return playbill_api.playbill_audit(
        world.instance_id,
        request=PlaybillAuditRequestV1(evaluation_time=_now(), access_profile=_ACCESS),
    )


def _authoring_claims(world: World) -> object:
    members = tuple(
        {
            "kind": "claim",
            "subject": world.subject(index),
            "predicate": f"{KIND}.ruling",
            "object": {"kind": "exact_content", "text": f"ruling {index}"},
            "role": "normative",
            "rationale": "The scale guard records a ruling.",
            "source": {"kind": "self_source", "body": f"ruling {index}"},
        }
        for index in range(10)
    )
    return _submit_and_activate(
        world, ChangeSetInput.model_validate({"kind": "change_set", "members": members})
    )


def _submit_and_activate(world: World, value: Any) -> object:
    submitted = playbill_api.playbill_authoring_submit_input(world.instance_id, input=value)
    assert submitted.status.state == "accepted", submitted.status
    assert submitted.outcome.status == "landed", submitted.outcome
    world.generation += 1
    return submitted


def _authoring_trigger(world: World) -> object:
    return _submit_and_activate(
        world,
        TriggerInput(
            kind="trigger",
            name="scale-extra-on-generation",
            schedule=GenerationAcceptedSchedule(),
            line_name=f"{_worlds.TICK}-line",
        ),
    )


def _run_line(name: str) -> Callable[[World], object]:
    def run(world: World) -> object:
        digest = _worlds.line_digest(name)
        state = playbill_api.playbill_line_run(
            world.instance_id, digest, request=LineRunRequest(line=digest)
        )
        assert state.status == "succeeded", state.terminal
        (egress,) = state.terminal_egress
        assert egress.verdict == "delivered", egress
        world.terminal_proposals[name] = egress.proposal_id
        if name == _worlds.SETTLE:
            assert egress.settle_outcome == "settled", egress
            world.generation += 1
        return state

    return run


def _activate_proposal(world: World) -> object:
    proposal_id = world.terminal_proposals[_worlds.PROPOSE]
    assert proposal_id is not None
    receipt = playbill_api.playbill_activate(world.instance_id, proposal_id)
    assert receipt.status == "accepted", receipt
    world.generation += 1
    return receipt


def _prepare_stale_activation(world: World) -> None:
    pending = _worlds.write(
        world,
        [{"op": "set", "subject": world.subject(0), "field": "title", "value": "stale title"}],
        accept="never",
    )
    assert pending.proposal is not None
    world.prepared = pending.proposal.proposal_id
    _worlds.write(
        world, [{"op": "set", "subject": world.subject(1), "field": "title", "value": "head moved"}]
    )
    settle_advertisement(world.instance)


def _activate_stale(world: World) -> object:
    result = playbill_api.playbill_activate(world.instance_id, world.prepared)
    assert result.status == "accepted", result
    assert result.proposal_id != world.prepared
    world.generation += 1
    return result


def _attestation(basis: str, subject: int) -> Callable[[World], object]:
    """Build and sign the request client-side, before the window."""

    def prepare(world: World) -> None:
        world.prepared = _worlds.attestation_request(world, world.subject(subject), basis=basis)

    return prepare


def _append_attestation(world: World) -> object:
    return _worlds.append_attestation(world, world.prepared)


def _attestation_member(world: World) -> object:
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.proposals.proposals import AuthenticatedActor
    from cruxible_core.service.authoring.documents import service_activate_playbill_proposal

    coordinator = AuthoringIntentCoordinator.for_instance(world.instance)
    actor = AuthenticatedActor(actor_id=_worlds.OPERATOR)
    payload = ChangeSetAuthoringPayload(
        members=tuple(
            sorted(
                (AttestationAuthoringPayload(attestation=world.prepared.attestation),),
                key=authoring_member_identity,
            )
        )
    )
    intent = coordinator.create(
        actor=actor, payload=payload, canonical_timestamp=_worlds.timestamp()
    ).intent
    submitted = coordinator.submit(intent.intent_id, actor=actor)
    assert submitted.status.state == "ready_to_activate", submitted.status
    assert submitted.status.proposal_id is not None
    receipt = service_activate_playbill_proposal(
        world.instance, proposal_id=submitted.status.proposal_id, activated_by=_worlds.OPERATOR
    )
    assert receipt.status == "accepted", receipt
    world.generation += 1
    return receipt


def _hold_coordinate(world: World) -> None:
    """Remember the head, then move it with an unrelated one-item write."""

    world.held_coordinate = world.instance.accepted_coordinate().git_oid
    _worlds.write(world, _creates(world, "moved")[:1])
    settle_advertisement(world.instance)


def _stale_base_write(world: World) -> object:
    outcome = _worlds.write(world, _creates(world, "stale"), at=world.held_coordinate)
    assert outcome.status == "accepted", outcome
    return outcome


def _store_body(world: World) -> None:
    """Bring next's facts current, then store one body (a client's ``store_body``)."""

    _worlds.tick(world, "facts")
    playbill_api.playbill_store_body(
        world.instance_id,
        content_base64=base64.b64encode(f"scale body {world.shape.name}\n".encode()).decode(),
    )


def _reopen(world: World) -> None:
    get_playbill_manager().clear()
    world.instance  # noqa: B018 - the open itself is not measured


# -- what grows today ------------------------------------------------------------------
#
# Each entry is a strict xfail: the route/counter fails the rule now, for the
# reason given (the audit row of ``rebuild-audit-1012`` where it has one). A fix
# makes the case pass, which a strict xfail reports as a failure until the entry
# is removed.

_CURATION = (
    "audit#2: curation detection rescans the instance every generation: unscoped facts "
    "(curation/curation_detectors.py:1279), every Claim revision ever accepted "
    "(curation_detectors.py:182 _curation_history_index) and every dependency artifact "
    "(claims/closure.py:417), via service/discovery/curation.py:738"
)
_COVERAGE = (
    "audit#10: coverage reads every cited Capture envelope and re-derives every citation "
    "id (indexes/evidence/citation_coverage.py:103-174 via "
    "service/discovery/coverage.py:143; coverage/contracts.py:518)"
)
_AUDIT = (
    "audit#4: audit page 1 reads and parses every current Claim at every generation "
    "(service/discovery/audit.py:315-319 _history_index), plus unscoped facts (:725) and "
    "a Capture per row (:427)"
)
_SETTLE = (
    "audit#9: each settle builds the condition's query facts over every Claim of its "
    "predicates and every Subject, three times at one coordinate "
    "(proposals/delegated_authority.py:460 -> runtime/instance.py:398 -> "
    "service/discovery/query.py:626; via procedures/proposal_delivery.py:2014 and "
    "proposals/proposals.py:4554)"
)
_COLD_DERIVE = (
    "audit#23: the first write or dry run after an open derives the indexed state cold, "
    "building every artifact's dependency edges (indexes/evaluated_state.py:747 "
    "derive_indexed_state -> claims/closure.py:496, via proposals/proposals.py:4998); "
    "the cost model allows a cold-start rebuild (needs a ruling)"
)
_ACQUIRE = (
    "the history index acquired afresh: a read-only open with BEGIN and a schema load "
    "(indexes/acquisition.py:96-101), a schema check (indexes/history/history_index.py:129 "
    "via :1290) and its generation resolved (:491, :1292)"
)
_BLOB_READS = (
    "; in SQL, every accepted blob read verifies its generation with "
    + _ACQUIRE
    + " (runtime/instance.py:2090 blobs_at -> :2002 coordinate_for_oid -> :1924)"
)
_LAW_HEAD = (
    "a law-location lookup per Claim (indexes/history/history_index.py:686 via "
    "service/evidence/evidence.py:926, from service/discovery/query.py:493)"
)
_DIGESTS = ("canonical_encodes", "canonical_digests")


def _grows(reason: str, *counters: str) -> dict[str, str]:
    return dict.fromkeys(counters, reason)


GROWS: dict[str, dict[str, str]] = {
    "tick.after_generation.floor": {
        # Its parses, encodes, digests, blob and CAS reads and statements no
        # longer grow once the world's floor has rendered before the measured
        # tick (a steady-state refresh reads only what the generation changed).
        "file_opens": (
            "audit#11: each floor refresh re-reads every cited Capture envelope for "
            "sources/LEDGER (service/floor/floor_sources.py:114 via :165)"
        ),
    },
    "tick.after_generation.curation": {
        **_grows(
            _CURATION,
            "claim_parses",
            *_DIGESTS,
            "file_opens",
            "sql_statements",
            "git_blob_reads",
        ),
        "sql_vm_steps": (
            "audit#2: each curation detection walks the whole history index from its start "
            "(SELECT * FROM artifact_versions WHERE occurrence_sequence<=? ORDER BY ..., "
            "indexes/history/history_index.py:893, from curation/curation_detectors.py:169 "
            "_curation_history_index), and looks up every Claim's law locations by a "
            "non-unique prefix (indexes/history/history_index.py:686 via "
            "service/evidence/evidence.py:926, from service/discovery/query.py:493)"
        ),
    },
    # Workspace-source next digests passed with section-copy commitments (strict XPASS).
    # Keep that case under the ordinary guard, without changing ratio or slack.
    # Its encodes no longer grow after the encode-once batch (measured XPASS).
    "read.coverage.workspace_sources": _grows(
        _COVERAGE, "canonical_digests", "file_opens", "cas_body_reads"
    ),
    "read.audit.first_page": {
        **_grows(
            _AUDIT,
            "claim_parses",
            *_DIGESTS,
            "file_opens",
            "sql_statements",
            "cas_body_reads",
            "git_blob_reads",
        ),
        "sql_vm_steps": (_AUDIT + "; in SQL, " + _LAW_HEAD + ", and repeatedly " + _ACQUIRE),
    },
    "procedure.settle": {
        # Its encodes and digests no longer grow after the encode-once batch
        # (measured XPASS).
        **_grows(_SETTLE, "claim_parses", "git_blob_reads"),
        **_grows(
            "settle's SQL work grows on the merged tree after perf B (statements 3,011 -> 4,523 at "
            "10x); attribution owed by dev.roadmap_item/settle-cost-after-batch-a",
            "sql_statements",
            "sql_vm_steps",
        ),
    },
    "cold.write": _grows(_COLD_DERIVE, *_DIGESTS),
    "cold.dry_run": _grows(_COLD_DERIVE, *_DIGESTS),
}


def _routes() -> list[Route]:
    ticks = _worlds.TICK_PARTS
    return [
        Route("write.create.dry_run", _write(lambda w: _creates(w, "dry"), dry_run=True)),
        Route("write.create", _write(lambda w: _creates(w, "new"))),
        Route("write.doc_sections", _write_doc_sections),
        *(
            Route(
                f"tick.after_generation.{part}",
                _tick(part),
                verify=_ticked(part, due=_DUE_AFTER_GENERATION[part]),
            )
            for part in ticks
        ),
        *(
            Route(f"tick.idle.{part}", _tick(part), verify=_ticked(part, due=False))
            for part in ticks
        ),
        Route("read.orient", _orient),
        Route("read.orient.generations", _orient_generations),
        Route("read.orient.graph", _orient_graph),
        Route("read.orient.pins", _orient_pins),
        Route("read.since.first_page", _since_first_page),
        Route("read.get.subject", _get(lambda w: w.subject(7))),
        Route("read.get.claim", _get(lambda w: w.claims[f"{w.subject(7)}#title"])),
        Route("read.query.where", _query_where),
        Route("read.query.named", _query_named),
        Route("read.query.subject_kinds", _query_definitions("SubjectKind")),
        Route("read.query.capture_contracts", _query_definitions("CaptureContract")),
        Route("read.query.mandates", _query_definitions("ProcedureMandate")),
        Route("read.query.query_definitions", _query_definitions("QueryDefinition")),
        Route("read.next.stored_queue", _next()),
        Route("read.next.workspace_sources", _next(sources=True)),
        Route("read.coverage.workspace_sources", _coverage, prepare=_observe_reports),
        Route("read.audit.first_page", _audit),
        Route("write.revise.dry_run", _write(lambda w: _revisions(w, "revised"), dry_run=True)),
        Route("write.revise", _write(lambda w: _revisions(w, "revised"))),
        Route("read.next.no_stored_queue", _next()),
        Route("read.orient.after_activation", _orient),
        Route("authoring.submit_and_activate", _authoring_claims),
        Route("proposal.stale.activate", _activate_stale, prepare=_prepare_stale_activation),
        Route("procedure.propose", _run_line(_worlds.PROPOSE)),
        Route("procedure.propose.activate", _activate_proposal),
        Route("procedure.settle", _run_line(_worlds.SETTLE)),
        Route(
            "tick.after_settle.settled_refusals",
            _tick("settled_refusals"),
            prepare=_settle_pending,
            verify=_settle_counted,
        ),
        Route("read.get.line_runs", _line_runs),
        Route(
            "attestation.examined_existing",
            _append_attestation,
            prepare=_attestation("examined_existing", 8),
        ),
        Route(
            "attestation.new_capture", _append_attestation, prepare=_attestation("new_capture", 9)
        ),
        Route(
            "authoring.attestation_member",
            _attestation_member,
            prepare=_attestation("examined_existing", 10),
        ),
        Route("authoring.trigger_member", _authoring_trigger),
        Route("write.stale_base", _stale_base_write, prepare=_hold_coordinate),
        Route("write.create.typed", _write(_typed_creates)),
        Route(
            "read.get.subject_history",
            _subject_history(_HISTORY_SUBJECT),
            prepare=lambda world: _revise(world, _HISTORY_SUBJECT, 6),
        ),
        Route("tick.after_body_store.facts", _tick("facts"), prepare=_store_body),
        Route("cold.write", _write(lambda w: _creates(w, "cold")), prepare=_reopen),
        Route(
            "cold.dry_run", _write(lambda w: _creates(w, "colddry"), dry_run=True), prepare=_reopen
        ),
    ]


ROUTES = {route.name: replace(route, grows=GROWS.get(route.name, {})) for route in _routes()}
assert set(GROWS) <= set(ROUTES), set(GROWS) - set(ROUTES)
assert all(set(counters) <= set(COUNTERS) for counters in GROWS.values())


def _measure(session: Session, route: Route) -> dict[str, dict[str, Any]]:
    measured = {}
    for world in (session.small, session.large):
        if route.prepare is not None:
            route.prepare(world)
        settle_advertisement(world.instance)
        root = world.root
        with counting(root) as window:
            result = route.run(world)
            settle_advertisement(world.instance)
        if window.unexplained:
            raise AssertionError(
                f"{route.name} on {world.shape.name}: {len(window.unexplained)} SQL statements "
                f"whose work the guard cannot count: {window.unexplained[:5]}"
            )
        if route.verify is not None:
            route.verify(world, result)
        measured[world.shape.name] = {
            **{name: window.counts[name] for name in COUNTERS},
            # Attribution for a failing case (and CRUXIBLE_SCALE_REPORT), outside the rule.
            "statements": dict(window.statements.most_common(40)),
            "attribution": window.attribution,
        }
    return measured


def _cases() -> list[Any]:
    return [
        pytest.param(
            route.name,
            counter,
            id=f"{route.name}-{counter}",
            marks=(
                pytest.mark.xfail(strict=True, reason=route.grows[counter])
                if counter in route.grows
                else ()
            ),
        )
        for route in ROUTES.values()
        for counter in COUNTERS
    ]


@pytest.mark.parametrize(("route", "counter"), _cases())
def test_route_work_does_not_grow_with_the_instance(
    session: Session, route: str, counter: str
) -> None:
    if route in session.failed:
        raise session.failed[route]
    measured = session.measured[route]
    small, large = measured["small"][counter], measured["large"][counter]
    assert large <= small * RATIO + SLACK[counter], (
        f"{route} {counter}: SMALL {small}, LARGE {large} "
        f"(allowed {small * RATIO + SLACK[counter]:g})"
        + (_top_statements(measured) if counter == "sql_vm_steps" else "")
    )


@pytest.mark.parametrize(
    "route",
    [
        "write.create",
        "write.doc_sections",
        "write.revise",
        "write.create.typed",
        "cold.write",
        "procedure.settle",
    ],
)
def test_write_routes_spawn_no_git(session: Session, route: str) -> None:
    if route in session.failed:
        raise session.failed[route]
    for size, measurement in session.measured[route].items():
        assert measurement["git_spawns"] == 0, (route, size, measurement["git_spawns"])


def test_a_subject_history_page_grows_only_in_the_rows_that_locate_it(session: Session) -> None:
    """6 vs 60 of the Subject's own revisions, one page of five: only locating grows.

    Every counter but the VM steps that walk the Subject's member rows stays
    at the page's cost: its blobs, parses, record facts and reasons. The
    revisions are adds and retirements of one value, each a lineage of its own:
    revising one Claim 60 times would accumulate its evidence, so the page's
    Claims themselves would grow, which is the data and not the read.
    """

    world = session.small
    six, sixty = f"{KIND}/history-six", f"{KIND}/history-sixty"
    target = world.subject(0)
    for subject, cycles in ((six, 3), (sixty, 30)):
        _revise(world, subject, 1)
        for _cycle in range(cycles):
            _worlds.write(
                world, [{"op": "add", "subject": subject, "field": "governs", "value": target}]
            )
            _worlds.write(
                world,
                [{"op": "retire", "target": {"subject": subject, "field": "governs"}}],
            )
    settle_advertisement(world.instance)
    # Each page read once first: both measured reads find their generations'
    # records, facts and admissions parsed, so neither pays a cold first parse.
    for subject in (six, sixty):
        _subject_history(subject, limit=5)(world)
    measured: dict[str, dict[str, Any]] = {}
    for subject in (six, sixty):
        with counting(world.root) as window:
            _subject_history(subject, limit=5)(world)
        assert not window.unexplained, window.unexplained[:5]
        measured[subject] = {name: window.counts[name] for name in COUNTERS}
    for counter in COUNTERS:
        if counter == "sql_vm_steps":
            continue  # the located rows: the Subject's own revisions
        small, large = measured[six][counter], measured[sixty][counter]
        assert large <= small * RATIO + SLACK[counter], (
            f"subject history {counter}: 6 revisions {small}, 60 revisions {large}"
        )


def test_an_incoming_page_reads_only_its_own_rows(session: Session) -> None:
    """6 vs 60 Claims pointing at a Subject, one page of five: no counter grows.

    The page is a keyset seek of the object index (``claims_by_object_subject``)
    after its last Claim, bounded by the page: unlike a history page, locating
    it costs nothing of the Subject's whole incoming set, so even the VM steps
    stay at the page's.
    """

    world = session.small
    six, sixty = world.subject(1), world.subject(2)
    for target, sources in ((six, 6), (sixty, 60)):
        _worlds.write(
            world,
            [
                {
                    "op": "add",
                    "subject": f"{KIND}/points-{sources}-{index:02d}",
                    "field": "governs",
                    "value": target,
                }
                for index in range(sources)
            ],
        )
    settle_advertisement(world.instance)

    def page(target: str) -> object:
        result = playbill_api.playbill_get(
            world.instance_id, request=GetRequest(ref=target, detail="incoming", limit=5)
        )
        assert result.incoming is not None and len(result.incoming.claims) == 5, result
        assert result.truncated, result
        return result

    for target in (six, sixty):
        page(target)
    measured: dict[str, dict[str, Any]] = {}
    for target in (six, sixty):
        with counting(world.root) as window:
            page(target)
        assert not window.unexplained, window.unexplained[:5]
        measured[target] = {name: window.counts[name] for name in COUNTERS}
    for counter in COUNTERS:
        small, large = measured[six][counter], measured[sixty][counter]
        assert large <= small * RATIO + SLACK[counter], (
            f"incoming page {counter}: 6 pointing {small}, 60 pointing {large}"
        )


def test_a_graph_edge_page_reads_only_its_own_rows(session: Session) -> None:
    """6 vs 60 more Subject-valued Claims, one edge page of five: no counter grows.

    The page is a keyset seek of the object index (``claims_by_object_subject``)
    from its start, bounded by the page, so the graph's whole edge set costs
    nothing to locate it.
    """

    from cruxible_core.service.discovery.orient import _keyset_cursor, service_playbill_head

    world = session.small
    target = world.subject(3)

    def add(count: int, tag: str) -> None:
        _worlds.write(
            world,
            [
                {
                    "op": "add",
                    "subject": f"{KIND}/graph-{tag}-{index:02d}",
                    "field": "governs",
                    "value": target,
                }
                for index in range(count)
            ],
        )
        settle_advertisement(world.instance)

    def page() -> object:
        head = service_playbill_head(world.instance)
        cursor = _keyset_cursor(view="graph", served=head.coordinate, last_key=["edge", "", ""])
        result = playbill_api.playbill_orient(
            world.instance_id, section="graph", limit=5, cursor=cursor
        )
        assert result.graph is not None and not result.graph.nodes, result
        assert len(result.graph.edges) == 5 and result.truncated, result
        return result

    measured: dict[str, dict[str, Any]] = {}
    for count, tag in ((6, "six"), (54, "sixty")):
        add(count, tag)
        page()
        with counting(world.root) as window:
            page()
        assert not window.unexplained, window.unexplained[:5]
        measured[tag] = {name: window.counts[name] for name in COUNTERS}
    for counter in COUNTERS:
        small, large = measured["six"][counter], measured["sixty"][counter]
        assert large <= small * RATIO + SLACK[counter], (
            f"graph edge page {counter}: 6 more edges {small}, 60 more edges {large}"
        )


#: The ontology reads a kind page makes, each answered from maintained counters.
_ONTOLOGY_READS: dict[str, Callable[[World], object]] = {
    "orient.claim_types": lambda world: playbill_api.playbill_orient(
        world.instance_id, section="claim_types", limit=5
    ),
    "get.subject_kind": lambda world: playbill_api.playbill_get(
        world.instance_id, request=GetRequest(ref=f"SubjectKind:{KIND}")
    ),
    "orient.kind": lambda world: playbill_api.playbill_orient(world.instance_id, kind=KIND),
    "get.claim_type": lambda world: playbill_api.playbill_get(
        world.instance_id, request=GetRequest(ref=f"ClaimType:{KIND}.title")
    ),
    "query.subject_kinds": _query_definitions("SubjectKind"),
    "query.capture_contracts": _query_definitions("CaptureContract"),
    "query.mandates": _query_definitions("ProcedureMandate"),
    "query.query_definitions": _query_definitions("QueryDefinition"),
}


def test_ontology_reads_never_count_their_population(session: Session) -> None:
    """10 vs 250 more Subjects (each with a title Claim): the ontology reads stay flat.

    Subject counts come from ``query_subject_counts``, which Subject writes
    maintain, and per-predicate Claim and Subject counts from ``predicate_counts``,
    which Claim writes maintain; nothing on these reads counts the kind's
    Subjects or Claims (``orient(kind=K)`` still lists five sample ids). The
    bound is absolute (``SLACK`` alone, no ratio): a recount of 240 more rows
    costs well over 1,024 VM steps, so it fails here however large the read's
    fixed work is.
    """

    world = session.small

    def add(count: int, tag: str) -> None:
        _worlds.write(
            world,
            [
                {
                    "op": "set",
                    "subject": f"{KIND}/onto-{tag}-{index:03d}",
                    "field": "title",
                    "value": tag,
                }
                for index in range(count)
            ],
        )
        settle_advertisement(world.instance)

    measured: dict[str, dict[str, dict[str, Any]]] = {name: {} for name in _ONTOLOGY_READS}
    for count, tag in ((10, "ten"), (240, "many")):
        add(count, tag)
        for name, read in _ONTOLOGY_READS.items():
            read(world)
            with counting(world.root) as window:
                read(world)
            assert not window.unexplained, window.unexplained[:5]
            measured[name][tag] = {counter: window.counts[counter] for counter in COUNTERS}
    for name, by_tag in measured.items():
        for counter in COUNTERS:
            small, large = by_tag["ten"][counter], by_tag["many"][counter]
            assert large <= small + SLACK[counter], (
                f"{name} {counter}: 10 more Subjects {small}, 250 more Subjects {large}"
            )


def _accept_type(world: World, field: str, **update: Any) -> None:
    """Accept a ClaimType of the work items no Claim states yet, or its successor."""

    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.claim_types import (
        claim_type_digest,
        claim_type_path,
        parse_claim_type,
        render_claim_type,
    )
    from tests.core_support._write_support import _claim_type

    path = claim_type_path(f"{KIND}.{field}")
    instance = world.instance
    held = instance.immutable_tree_at(instance.accepted_coordinate().git_oid).get(path)
    if held is None:
        claim_type = _claim_type(field, literal_schema={"type": "string"})
    else:
        current = parse_claim_type(held, path=path)
        claim_type = current.model_copy(
            update={
                **update,
                "lifecycle": ArtifactLifecycle(
                    predecessor_digest=claim_type_digest(current).tagged
                ),
            }
        )
    _worlds.accept_members(
        world, {path: render_claim_type(claim_type)}, name=f"onto-{field}-{world.generation}"
    )


def test_the_ontology_moves_only_with_its_definitions(session: Session) -> None:
    """The ontology files cost the definitions that changed, never the instance.

    In SMALL and in LARGE (ten times the work items and Claims): a ten-Claim
    write delivers no ``ontology/`` file and renders none, and a revision of
    one ClaimType that changes no view (a description edit; the v1 test
    vocabulary carries none, so only its lineage moves) delivers only the
    kind card it shows on and ``ontology/claim-types/INDEX`` (no ``current/``
    file), with the advance's work flat across the two worlds.
    """

    from cruxible_core.service.floor import floor_ontology
    from cruxible_core.service.floor.floor_delta import service_playbill_floor_delta
    from cruxible_core.service.floor.floor_index import FloorRender, advance_floor_index

    def delivered(world: World, base: FloorRender, head: FloorRender) -> list[str]:
        delta = service_playbill_floor_delta(
            world.instance,
            head=head.inputs.coordinate,
            base_generation=base.generation,
            base_renderer=base.renderer,
        )
        assert delta.kind == "delta", world.shape.name
        return sorted(item.path for item in delta.files)

    rendered: list[str] = []
    render = floor_ontology._Renderer.render

    def counted(self: Any, path: str, changed_at: int) -> Any:
        rendered.append(path)
        return render(self, path, changed_at)

    measured: dict[str, dict[str, int]] = {}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(floor_ontology._Renderer, "render", counted)
        for world in (session.small, session.large):
            _accept_type(world, "onto_note")
            base = advance_floor_index(world.instance)
            _worlds.write(world, _creates(world, f"onto-{world.shape.name}"))
            rendered.clear()
            written = advance_floor_index(world.instance)
            assert rendered == [], world.shape.name
            assert not [
                path for path in delivered(world, base, written) if path.startswith("ontology/")
            ], world.shape.name
            _accept_type(world, "onto_note", description=f"A note ({world.shape.name}).")
            rendered.clear()
            with counting(world.root) as window:
                head = advance_floor_index(world.instance)
            assert not window.unexplained, window.unexplained[:5]
            measured[world.shape.name] = {counter: window.counts[counter] for counter in COUNTERS}
            files = delivered(world, written, head)
            assert not [path for path in files if path.startswith("current/")], files
            assert [path for path in files if path.startswith("ontology/")] == [
                "ontology/claim-types/INDEX",
                f"ontology/kinds/{KIND}.md",
            ], files
            assert sorted(rendered) == [path for path in files if path.startswith("ontology/")]
    for counter in COUNTERS:
        small, large = measured["small"][counter], measured["large"][counter]
        assert large <= small * RATIO + SLACK[counter], (
            f"ontology description edit {counter}: SMALL {small}, LARGE {large}"
        )


def _top_statements(measured: dict[str, dict[str, Any]], shown: int = 5) -> str:
    """The heaviest statements in LARGE, with their steps in SMALL, where they ran from,
    their tables and how each cursor moved."""

    small = measured["small"]["statements"]
    lines = []
    for entry in measured["large"]["attribution"][:shown]:
        lines.append(
            f"\n  {entry['vm_steps']} steps (SMALL {small.get(entry['statement'], 0)}) "
            f"in {entry['runs']} runs: {entry['statement']}"
        )
        for path, steps, runs in entry["paths"]:
            lines.append(f"\n    {steps} steps in {runs} runs from {path}")
        if "unavailable" in entry:
            lines.append(f"\n    tables and cursors unavailable: {entry['unavailable']}")
        for cursor in entry.get("cursors", ()):
            lines.append(f"\n    {cursor}")
    return "".join(lines)
