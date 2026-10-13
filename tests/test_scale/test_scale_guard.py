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


def _tick(part: str) -> Callable[[World], object]:
    def run(world: World) -> object:
        return _worlds.tick(world, part)

    return run


#: Whether a tick of each part has work after a generation: the triggers fire
#: floor.refresh and curation.detect, the armed Line's GenerationAccepted Trigger
#: is due, the new Claims' Captures wait for the evidence check, the queue and the
#: floor have a new target, curation a new generation, and the refusal index a
#: generation to catch up. Neither world holds a prediction contract, so the
#: prediction part only ever matches. Idle, nothing is due anywhere.
_DUE_AFTER_GENERATION = {
    "triggers": True,
    "line": True,
    "floor": True,
    "queue": True,
    "evidence": True,
    "prediction": False,
    "curation": True,
    "settled_refusals": True,
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
    """The workspace's ``reports.md``, observed client-side through its source catalog."""

    from cruxible_client.authoring.selectors import WorkspaceSources
    from cruxible_core.coverage.workspace import bindings_from_mapping, observe_workspace

    bindings = bindings_from_mapping(WorkspaceSources(world.workspace).coverage_bindings())
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
    assert submitted.status.state == "ready_to_activate", submitted.status
    assert submitted.status.proposal_id is not None
    receipt = playbill_api.playbill_activate(world.instance_id, submitted.status.proposal_id)
    assert receipt.status == "accepted", receipt
    world.generation += 1
    return receipt


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
    """Bring the stored queue current, then store one body (a client's ``store_body``)."""

    _worlds.tick(world, "queue")
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

_FLOOR_PARSE = (
    "audit#11: each floor refresh parses and digests every current Claim "
    "(service/floor/floor_index.py:188 _parse_into, via build_floor_inputs :277)"
)
_QUEUE_FOLD = (
    "K1/audit#1: each next-queue fold reads, parses and re-resolves every Claim, once for "
    "V1 and once for V2 (service/evidence/evidence.py:334 via "
    "service/discovery/next.py:2086; consumers/next/queue.py:213,219; "
    "service/discovery/claim_status.py:293)"
)
_CURATION = (
    "audit#2: curation detection rescans the instance every generation: unscoped facts "
    "(curation/curation_detectors.py:1279), every Claim revision ever accepted "
    "(curation_detectors.py:182 _curation_history_index) and every dependency artifact "
    "(claims/closure.py:417), via service/discovery/curation.py:738"
)
_STORED_QUEUE_BODIES = (
    "audit#14: reading the stored next queue stats every body its last fold rested on "
    "(consumers/next/queue.py:106 -> :70 _bodies_hold -> service/evidence/evidence.py:727)"
)
_NAMED_FACTS = (
    "new: a named query builds facts over every Claim of its predicates and every Subject "
    "before its budget truncates (service/discovery/query.py:626 build_accepted_query_facts "
    "via :809 evaluate_accepted_query)"
)
_CITATIONS = (
    "audit#6: next with workspace sources builds unscoped facts and each Claim's law "
    "record and reads a Capture per citation (service/discovery/next.py:2870-2934 "
    "_citation_commitments via :3827 _workspace_items; service/claims/claims.py:512)"
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
_LIVE_FOLD = (
    "audit#3: with no stored queue, next folds every Claim live: all Claims and slots "
    "(service/discovery/next.py:5630-5639) and each Claim's law record "
    "(next.py:5674 -> :3227 -> service/claims/claims.py:529)"
)
_ORIENT_FOLD = (
    "audit#15: orient after an activation runs the live next fold for its attention "
    "section (service/discovery/orient.py:621 -> next.py:5819 -> :5630)"
)
_SETTLE = (
    "audit#9: each settle builds the condition's query facts over every Claim of its "
    "predicates and every Subject, three times at one coordinate "
    "(proposals/delegated_authority.py:460 -> runtime/instance.py:398 -> "
    "service/discovery/query.py:626; via procedures/proposal_delivery.py:2014 and "
    "proposals/proposals.py:4554)"
)
_BODY_STORE = (
    "audit#1: storing one body moves the instance-wide evidence token "
    "(storage/cas.py:426-450), so the next queue tick refolds every Claim "
    "(service/evidence/evidence.py:334 via service/discovery/next.py:2086)"
)
_COLD_DERIVE = (
    "audit#23: the first write or dry run after an open derives the indexed state cold, "
    "building every artifact's dependency edges (indexes/evaluated_state.py:747 "
    "derive_indexed_state -> claims/closure.py:496, via proposals/proposals.py:4998); "
    "the cost model allows a cold-start rebuild (needs a ruling)"
)
_FOLD_SQL = (
    "; in SQL, every Claim law record walked whole (indexes/history/history_index.py:686 "
    "claim_law_locations with no path, via service/claims/claims.py:528) and two "
    "generation records read per Claim (:483 via claims.py:529-530, from "
    "service/discovery/next.py:3227)"
)
_HEADS_SQL = (
    ", and every Claim's law head read in one IN list (indexes/history/history_index.py:701 "
    "claim_law_heads via service/evidence/evidence.py:534, from "
    "service/discovery/claim_status.py:247)"
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
        **_grows(_FLOOR_PARSE, "claim_parses", "canonical_encodes"),
        "git_blob_reads": (
            "audit#11: each floor refresh reads every Claim blob "
            "(service/floor/floor_index.py:269 -> indexes/typed_state.py:959 prefetch_members)"
        ),
        **_grows(
            "audit#11: each floor refresh re-reads every cited Capture envelope for "
            "sources/LEDGER (service/floor/floor_sources.py:114 via :165)",
            "file_opens",
            "cas_body_reads",
        ),
        "sql_vm_steps": (
            "audit#11: each floor refresh walks all of accepted_member_locations "
            "(indexes/history/history_index.py:599 latest_sequences(paths=None) GROUP BY "
            "member_path, from service/floor/floor_index.py:289), reads a record per "
            "generation (:483) and every member of every generation with a rationale (:643 "
            "member_paths_by_sequence via service/floor/floor_content.py:296)"
        ),
        "sql_statements": (
            "audit#11: each floor refresh looks up a record per generation "
            "(indexes/history/history_index.py:483 via service/floor/floor_content.py:295)"
        ),
    },
    "tick.after_generation.queue": {
        **_grows(_QUEUE_FOLD, "claim_parses", *_DIGESTS, "git_blob_reads"),
        "sql_vm_steps": _QUEUE_FOLD + _FOLD_SQL + _HEADS_SQL,
        "file_opens": (
            "K1/audit#1: each next-queue fold evaluates every Claim's verdict "
            "(service/evidence/evidence.py:1285) and stats every body it rests on "
            "(service/evidence/evidence.py:727)"
        ),
        "sql_statements": (
            "K1/audit#1: each next-queue fold reads every Claim's law record "
            "(service/claims/claims.py:529 via service/discovery/next.py:3227)"
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
    "tick.idle.queue": {
        "file_opens": (
            "audit#13: every next-queue match stats every body the last fold read "
            "(consumers/next/queue.py:138 -> :70 -> service/evidence/evidence.py:727)"
        )
    },
    "read.orient": {
        "file_opens": (
            "audit#14/#15: orient stats every stored-queue body (consumers/next/queue.py:70 "
            "via service/discovery/next.py:5819) and every remembered slot's bodies "
            "(service/discovery/claim_status.py:139 via service/discovery/orient.py:1064)"
        )
    },
    "read.get.subject": {"file_opens": _STORED_QUEUE_BODIES + " via service/discovery/get.py:1123"},
    "read.get.claim": {"file_opens": _STORED_QUEUE_BODIES + " via service/discovery/get.py:1036"},
    "read.query.where": {
        **_grows(
            "new: a kind query reads and digests every Subject of the kind before its where "
            "filters (service/discovery/query.py:362 _reachable_subject_paths, :542-549 via "
            "compact_query.py:1286 live_facts)",
            *_DIGESTS,
            "git_blob_reads",
        ),
        "file_opens": _STORED_QUEUE_BODIES + " via service/discovery/read_flags.py:171",
    },
    "read.query.named": {
        **_grows(
            _NAMED_FACTS,
            "claim_parses",
            *_DIGESTS,
            "file_opens",
            "sql_statements",
            "git_blob_reads",
        ),
        "sql_vm_steps": _NAMED_FACTS + _BLOB_READS + ", and " + _LAW_HEAD,
    },
    "read.next.stored_queue": {
        "file_opens": _STORED_QUEUE_BODIES + " via service/discovery/next.py:5604"
    },
    "read.next.workspace_sources": {
        **_grows(
            _CITATIONS,
            "claim_parses",
            *_DIGESTS,
            "file_opens",
            "sql_statements",
            "cas_body_reads",
            "git_blob_reads",
        ),
        "sql_vm_steps": (
            _CITATIONS
            + "; in SQL, per item "
            + _ACQUIRE
            + " (runtime/instance.py:1825 accepted_history_reader), each Claim's member "
            "history read twice (indexes/history/history_index.py:542 via "
            "indexes/typed_state.py:1253 and :1322) and " + _LAW_HEAD
        ),
    },
    "read.coverage.workspace_sources": _grows(_COVERAGE, *_DIGESTS, "file_opens", "cas_body_reads"),
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
    "read.next.no_stored_queue": {
        **_grows(_LIVE_FOLD, *_DIGESTS, "file_opens", "sql_statements"),
        "sql_vm_steps": _LIVE_FOLD + _FOLD_SQL + _HEADS_SQL,
    },
    "read.orient.after_activation": {
        **_grows(
            _ORIENT_FOLD,
            "claim_parses",
            *_DIGESTS,
            "file_opens",
            "sql_statements",
            "git_blob_reads",
        ),
        "sql_vm_steps": _ORIENT_FOLD + _FOLD_SQL,
    },
    "procedure.settle": {
        **_grows(_SETTLE, "claim_parses", *_DIGESTS, "file_opens", "git_blob_reads"),
        **_grows(
            "settle's SQL work grows on the merged tree after perf B (statements 3,011 -> 4,523 at "
            "10x); attribution owed by dev.roadmap_item/settle-cost-after-batch-a",
            "sql_statements",
            "sql_vm_steps",
        ),
    },
    "tick.after_body_store.queue": {
        **_grows(
            _BODY_STORE,
            "claim_parses",
            *_DIGESTS,
            "file_opens",
            "sql_statements",
            "git_blob_reads",
        ),
        "sql_vm_steps": _BODY_STORE + _FOLD_SQL + _HEADS_SQL,
    },
    "cold.write": _grows(_COLD_DERIVE, *_DIGESTS),
    "cold.dry_run": _grows(_COLD_DERIVE, *_DIGESTS),
}


def _routes() -> list[Route]:
    ticks = _worlds.TICK_PARTS
    return [
        Route("write.create.dry_run", _write(lambda w: _creates(w, "dry"), dry_run=True)),
        Route("write.create", _write(lambda w: _creates(w, "new"))),
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
        Route("read.get.subject", _get(lambda w: w.subject(7))),
        Route("read.get.claim", _get(lambda w: w.claims[f"{w.subject(7)}#title"])),
        Route("read.query.where", _query_where),
        Route("read.query.named", _query_named),
        Route("read.next.stored_queue", _next()),
        Route("read.next.workspace_sources", _next(sources=True)),
        Route("read.coverage.workspace_sources", _coverage, prepare=_observe_reports),
        Route("read.audit.first_page", _audit),
        Route("write.revise.dry_run", _write(lambda w: _revisions(w, "revised"), dry_run=True)),
        Route("write.revise", _write(lambda w: _revisions(w, "revised"))),
        Route("read.next.no_stored_queue", _next()),
        Route("read.orient.after_activation", _orient),
        Route("authoring.submit_and_activate", _authoring_claims),
        Route("procedure.propose", _run_line(_worlds.PROPOSE)),
        Route("procedure.propose.activate", _activate_proposal),
        Route("procedure.settle", _run_line(_worlds.SETTLE)),
        Route(
            "tick.after_settle.settled_refusals",
            _tick("settled_refusals"),
            prepare=_settle_pending,
            verify=_settle_counted,
        ),
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
        Route("tick.after_body_store.queue", _tick("queue"), prepare=_store_body),
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
