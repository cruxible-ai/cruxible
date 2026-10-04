"""The environment the queue is read in is status beside the work, not work rows.

Each facet names its condition and its repair while it needs attention, and
returns to a healthy state once the repair is made.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cruxible_client.contracts import ProviderLaneStatus
from cruxible_client.contracts.declared_blocks import (
    PresentationPolicy,
    ProjectionAdvisoryPolicy,
)
from cruxible_client.contracts.errors import InstanceDecommissioned
from cruxible_client.contracts.projection import AcceptedCoordinate as ClientAcceptedCoordinate
from cruxible_core.cli.commands.playbill import _NEXT_STATUS_ATTENTION
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.runtime.instance import DESCRIPTOR_FILE
from cruxible_core.service.discovery.next import (
    NextRequestV1,
    NextWorkspaceObservation,
    service_playbill_next,
)
from tests.core_support._support import initialize_local
from tests.test_integration.test_graph_v4_provider_closure import _accepted_procedure
from tests.test_integration.test_next_closed_loop import (
    EVALUATION_TIME,
    _access,
    _request,
)

_ENVIRONMENT_REASONS = {
    "floor_missing",
    "floor_stale",
    "procedure_projection_missing",
    "instance_decommissioned",
    "provider_lane_unavailable",
    "ledger_mirror_behind",
}


def _attention(status):  # type: ignore[no-untyped-def]
    """The facets the CLI prints as needing attention, in its fixed order."""
    return tuple(
        (facet, getattr(status, facet))
        for facet, states in _NEXT_STATUS_ATTENTION.items()
        if getattr(status, facet).state in states
    )


def _status(instance, request, **kwargs):  # type: ignore[no-untyped-def]
    result = service_playbill_next(instance, request=request, **kwargs)
    # The environment never reappears as work rows.
    assert not {item.reason for item in result.items} & _ENVIRONMENT_REASONS
    return result.status


@pytest.mark.parametrize("reported", ["missing", "stale"])
def test_a_missing_or_stale_floor_names_the_export_until_it_is_current(
    tmp_path: Path, reported: str
) -> None:
    instance, _owner = initialize_local(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())

    status = _status(
        instance,
        _request(instance, workspace=NextWorkspaceObservation(floor_status=reported)),
    )
    assert status.floor.state == reported
    assert status.floor.repair is not None
    assert status.floor.repair.operation == "cruxible.floor.export"
    assert _attention(status) == (("floor", status.floor),)

    current = _status(
        instance,
        _request(
            instance,
            workspace=NextWorkspaceObservation(
                floor_status="current", installed_coordinate=coordinate
            ),
        ),
    )
    assert current.floor.state == "current" and _attention(current) == ()


def test_a_workspace_that_never_configured_a_floor_says_nothing_about_it(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)

    status = _status(
        instance,
        _request(instance, workspace=NextWorkspaceObservation(floor_status="not_configured")),
    )

    assert status.floor.state == "not_configured" and _attention(status) == ()


def test_an_unavailable_provider_lane_is_status_until_it_recovers(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    request = _request(instance)

    degraded = _status(
        instance,
        request,
        provider_lane=ProviderLaneStatus(
            state="unavailable",
            code="provider_runtime_recovery_failed",
            detail="operator recovery failed",
        ),
    )
    assert degraded.provider_lane.state == "unavailable"
    assert degraded.provider_lane.repair is not None
    assert degraded.provider_lane.repair.operation == "hand_edit"

    repaired = _status(
        instance,
        request,
        provider_lane=ProviderLaneStatus(state="available", code=None, detail=None),
    )
    assert repaired.provider_lane.state == "available" and _attention(repaired) == ()


def _bare_mirror(instance, remote: Path) -> None:  # type: ignore[no-untyped-def]
    subprocess.run(
        [
            "git",
            "init",
            "--bare",
            "-q",
            f"--object-format={instance.descriptor.git_object_format}",
            str(remote),
        ],
        check=True,
    )


def test_a_mirror_whose_push_failed_is_behind_until_the_remote_is_restored(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    request = _request(instance)
    assert _status(instance, request).ledger_mirror.state == "not_configured"
    remote = tmp_path / "mirror.git"
    _bare_mirror(instance, remote)
    subprocess.run(["rm", "-rf", str(remote)], check=True)
    assert instance.set_ledger_mirror(str(remote)).status == "behind"  # type: ignore[union-attr]

    behind = _status(instance, request)
    assert behind.ledger_mirror.state == "behind"
    assert behind.ledger_mirror.repair is not None
    assert behind.ledger_mirror.repair.target == str(remote)

    _bare_mirror(instance, remote)
    assert instance.publish_ledger_mirror().status == "current"  # type: ignore[union-attr]
    restored = _status(instance, request)
    assert restored.ledger_mirror.state == "current" and _attention(restored) == ()


def test_a_current_mirror_stays_current_for_an_earlier_requested_coordinate(
    tmp_path: Path,
) -> None:
    from tests.test_authoring.test_authoring_preflight import _seed_claim_surface

    instance, owner = initialize_local(tmp_path)
    earlier = _request(instance)
    remote = tmp_path / "mirror.git"
    _bare_mirror(instance, remote)
    assert instance.set_ledger_mirror(str(remote)).status == "current"  # type: ignore[union-attr]
    _seed_claim_surface(instance, owner)
    assert instance.publish_ledger_mirror().status == "current"  # type: ignore[union-attr]
    assert earlier.at is not None and earlier.at.git_oid != instance.accepted_coordinate().git_oid

    # Mirror health is measured at the head, not at the coordinate read.
    assert _status(instance, earlier).ledger_mirror.state == "current"


def _catalog_observation(instance, *, advisory: bool) -> NextWorkspaceObservation:  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.declared_blocks import (
        ProjectionCoverageObservation,
    )

    public = ClientAcceptedCoordinate.model_validate(
        AcceptedCoordinate.from_internal(instance.accepted_coordinate()).model_dump(mode="json")
    )
    return NextWorkspaceObservation(
        presentation_policy=PresentationPolicy(
            projection_advisories=ProjectionAdvisoryPolicy(procedure=advisory)
        ),
        projection_coverage=ProjectionCoverageObservation(
            coordinate=public, complete_kinds=("Procedure",), bindings=()
        ),
    )


def test_a_procedure_catalog_is_checked_only_where_the_workspace_asks_for_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.indexes.typed_state import ProcedureInventoryRow, TypedStateReader

    instance, _owner = initialize_local(tmp_path)
    procedure = _accepted_procedure()
    monkeypatch.setattr(
        TypedStateReader,
        "procedure_inventory",
        lambda self: (
            ProcedureInventoryRow(
                procedure.procedure.identity.qualified, procedure.path, "live", False
            ),
        ),
    )

    def status(observation):  # type: ignore[no-untyped-def]
        return _status(
            instance,
            NextRequestV1(
                evaluation_time=EVALUATION_TIME,
                access_profile=_access(),
                workspace_observation=observation,
            ),
        )

    # Off by default: an uncatalogued Procedure is not a finding.
    assert status(_catalog_observation(instance, advisory=False)).procedure_catalog.state == (
        "not_required"
    )
    asked = status(_catalog_observation(instance, advisory=True)).procedure_catalog
    assert asked.state == "missing"
    assert asked.repair is not None
    assert asked.repair.target == ".playbill/sources.yaml"
    assert asked.repair.required_change == "add_procedure_projection_catalog_entries"


def test_a_decommissioned_instance_blocks_in_the_status_header(tmp_path: Path) -> None:
    root = tmp_path / "decommissioned"
    root.mkdir()
    instance, _owner = initialize_local(root)
    request = _request(instance)
    active = _status(instance, request)
    assert not active.blocking and active.instance.state == "active"

    instance.decommission(reason="superseded by a fresh host", decommissioned_by="owner")

    status = _status(instance, request)
    assert status.blocking and status.instance.state == "decommissioned"
    assert status.instance.repair is not None
    assert status.instance.repair.target == DESCRIPTOR_FILE
    assert status.instance.repair.required_change == (
        "allocate_a_new_instance_with_playbill_host_create_or_archive_this_directory_yourself"
    )
    # Terminal: nothing inside the instance clears it.
    with pytest.raises(InstanceDecommissioned):
        instance.decommission(reason="a second reason", decommissioned_by="owner")


def test_a_compiler_behind_the_running_one_names_the_upgrade_until_it_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.compiler.compiler import GOVERNED_TRIGGERS_COMPILER, TRIGGER_CAPTURE_COMPILER
    from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
    from tests.test_ledger.test_compiler_upgrade import approve, old_instance, propose

    instance, _owner, reviewer = old_instance(tmp_path, monkeypatch, TRIGGER_CAPTURE_COMPILER)
    behind = _status(instance, _request(instance))
    assert behind.compiler.state == "upgrade_available"
    assert behind.compiler.repair is not None
    assert behind.compiler.repair.command == (
        f"cruxible playbill compiler upgrade --to {GOVERNED_TRIGGERS_COMPILER.rule_digest} "
        "--name upgrade-to-governed-triggers-v1"
    )
    # A compiler before Trigger artifacts schedules no internal action at all.
    assert behind.triggers.state == "unscheduled"
    assert behind.triggers.repair is not None
    assert behind.triggers.repair.required_change.startswith("upgrade_the_compiler")
    assert _attention(behind) == (
        ("compiler", behind.compiler),
        ("triggers", behind.triggers),
    )

    proposal = propose(instance, GOVERNED_TRIGGERS_COMPILER)
    approve(instance, proposal, reviewer)
    assert (
        service_activate_playbill_proposal(
            instance, proposal_id=proposal.admission.proposal_id, activated_by="owner"
        ).status
        == "accepted"
    )
    upgraded = _status(instance, _request(instance))
    # The upgrade seeds nothing: its internal actions wait for authored Triggers.
    assert upgraded.compiler.state == "current"
    assert _attention(upgraded) == (("triggers", upgraded.triggers),)
    assert upgraded.triggers.repair is not None
    assert upgraded.triggers.repair.required_change == (
        "author_a_trigger_aimed_at_the_unscheduled_action"
    )


def test_a_status_repair_the_caller_cannot_perform_keeps_the_facet_but_not_the_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R07 for the status header: a compiler upgrade is an ADMIN act."""

    from cruxible_core.compiler.compiler import TRIGGER_CAPTURE_COMPILER
    from tests.test_ledger.test_compiler_upgrade import old_instance

    instance, _owner, _reviewer = old_instance(tmp_path, monkeypatch, TRIGGER_CAPTURE_COMPILER)
    request = _request(instance)

    admin = _status(instance, request, caller_rung=3)
    assert admin.compiler.repair is not None
    assert "repair_hidden" not in admin.compiler.model_dump(mode="json")

    for rung in (0, 1, 2):  # READ_ONLY, GOVERNED_WRITE, GRAPH_WRITE
        lower = _status(instance, request, caller_rung=rung)
        assert lower.compiler.state == "upgrade_available"
        assert lower.compiler.detail == admin.compiler.detail
        assert lower.compiler.repair is None
        assert lower.compiler.repair_hidden is True
        assert lower.compiler.repair_requires is not None
        assert lower.compiler.repair_requires.tier == "admin"
        assert lower.compiler.repair_requires.because == ("tier",)
        # Facets are not rows: the status counts no left-out rows.
        assert "hidden" not in lower.model_dump(mode="json")

    mcp = request.model_copy(
        update={"caller_surface": "mcp", "caller_tools": ("cruxible_playbill_next",)}
    )
    profiled = _status(instance, mcp, caller_rung=3)
    assert profiled.compiler.repair is None and profiled.compiler.repair_hidden is True
    assert profiled.compiler.repair_requires.because == ("profile",)


def test_a_compiler_with_no_forward_edge_is_reported_without_a_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.compiler.compiler import RESOLUTION_COMPILER

    instance, _owner = initialize_local(tmp_path)
    # A daemon older than the state it serves has nothing to upgrade to.
    monkeypatch.setattr(
        "cruxible_core.service.discovery.next.current_compiler_coordinate",
        lambda: RESOLUTION_COMPILER,
    )
    status = _status(instance, _request(instance))
    assert status.compiler.state == "no_upgrade_path"
    assert status.compiler.repair is None and _attention(status) == ()


def test_due_line_occurrences_name_their_dispatch_until_it_admits_them(tmp_path: Path) -> None:
    from datetime import timedelta

    from cruxible_client.contracts.line_dispatch import LineDispatchRequest
    from cruxible_client.contracts.procedures.line_specs import line_identity_digest
    from cruxible_core.service.procedures.line_dispatch import service_dispatch_line
    from tests.test_procedures.test_line_dispatch import queued_world
    from tests.test_procedures.test_procedure_run_surface import _actor

    instance, line, _procedure, occurrence, now = queued_world(tmp_path)
    identity = line_identity_digest(line.identity)

    waiting = _status(
        instance,
        _request(instance, evaluation_time=occurrence.eligible_at - timedelta(microseconds=1)),
    ).line_dispatch
    assert waiting.state == "waiting" and waiting.repair is None
    assert waiting.detail == {
        "due": 0,
        "waiting": 1,
        "lines": [
            {
                "line_identity_digest": identity,
                "due": 0,
                "waiting": 1,
                "oldest_eligible_at": occurrence.eligible_at.isoformat(),
            }
        ],
    }

    due = _status(instance, _request(instance, evaluation_time=now))
    assert due.line_dispatch.state == "due"
    assert due.line_dispatch.repair is not None
    assert due.line_dispatch.repair.command == f"cruxible playbill line dispatch {identity}"
    assert _attention(due) == (("line_dispatch", due.line_dispatch),)
    hidden = NextRequestV1(
        evaluation_time=now,
        access_profile=_access().model_copy(update={"permitted_access_classes": ("public",)}),
    )
    assert _status(instance, hidden).line_dispatch.state == "not_observed"

    dispatched = service_dispatch_line(
        instance,
        identity,
        LineDispatchRequest(),
        actor=_actor(instance),
        now=now,
        caller_rung=3,
    )
    assert dispatched.items[0].status == "admitted", dispatched
    drained = _status(instance, _request(instance, evaluation_time=now))
    assert drained.line_dispatch.state == "idle" and _attention(drained) == ()


def test_an_instance_that_never_evaluated_a_line_keeps_no_dispatch_state(tmp_path: Path) -> None:
    from cruxible_core.exhaust.line_dispatch import dispatch_root

    instance, _owner = initialize_local(tmp_path)
    assert _status(instance, _request(instance)).line_dispatch.state == "idle"
    assert not dispatch_root(instance).exists()


def test_a_retired_lines_pending_work_is_neither_due_nor_a_repair(tmp_path: Path) -> None:
    from datetime import timedelta

    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.line_dispatch import LineEvaluateRequest
    from cruxible_client.contracts.procedures.line_specs import (
        line_spec_digest,
        line_spec_path,
        render_line_spec,
    )
    from cruxible_client.contracts.triggers import CaptureLandingSchedule
    from cruxible_core.service.procedures.line_dispatch import service_evaluate_line
    from tests.support.lines import line_trigger, successor, trigger_members
    from tests.test_indexes.test_resolution_contracts import _accept_tree
    from tests.test_procedures.test_line_triggers import SELECTOR, TRIGGER, capture, line_world
    from tests.test_procedures.test_procedure_run_surface import READ_TIME, _actor

    schedule = CaptureLandingSchedule(event=SELECTOR)
    instance, line, procedure, owner = line_world(tmp_path, schedule, with_owner=True)
    capture(instance, procedure)
    now = READ_TIME + timedelta(seconds=2)
    service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequest(since=READ_TIME, until=now),
        actor=_actor(instance),
        now=now,
    )
    assert _status(instance, _request(instance, evaluation_time=now)).line_dispatch.state == "due"

    retired = line.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=line_spec_digest(line).tagged
            )
        }
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[line_spec_path(line.identity.name)] = render_line_spec(retired)
    # A Line retires with the Triggers aimed at it.
    tree.update(
        trigger_members(
            successor(
                line_trigger(TRIGGER, line=line.identity.name, schedule=schedule), state="retired"
            )
        )
    )
    _accept_tree(
        instance, owner, tree, timestamp="2026-08-28T15:02:00.000000Z", proposal_name="retire-line"
    )

    status = _status(instance, _request(instance, evaluation_time=now))
    assert status.line_dispatch.state == "idle" and _attention(status) == ()


def test_worker_findings_report_how_current_they_are(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    from tests.support.internal_triggers import fire_internal_triggers
    from tests.test_consumers.test_evidence_availability import _drain, _world

    instance, _capture = _world(tmp_path)
    swept = datetime(2026, 9, 1, tzinfo=UTC)
    _drain(instance, now=swept)

    def consumers(at, **kwargs):  # type: ignore[no-untyped-def]
        return _status(instance, _request(instance, evaluation_time=at), **kwargs).consumers

    # No consumer loop: findings stand as of the last pass, and that is not work.
    idle = consumers(swept)
    assert idle.state == "not_running" and idle.repair is None
    assert [worker["kind"] for worker in idle.detail["workers"]] == ["next"]

    assert consumers(swept, consumers_running=True).state == "current"
    first_fire = swept + timedelta(days=1)
    fire_internal_triggers(instance, now=first_fire)
    assert consumers(first_fire, consumers_running=True).state == "current"
    late = swept + timedelta(days=2)
    fire_internal_triggers(instance, now=late)
    lagging = _status(instance, _request(instance, evaluation_time=late), consumers_running=True)
    assert lagging.consumers.state == "lagging"
    assert _attention(lagging) == (("consumers", lagging.consumers),)

    monkeypatch.setenv("CRUXIBLE_DISABLED_CONSUMERS", "next")
    off = consumers(late, consumers_running=True)
    assert off.state == "current" and off.detail["workers"] == [
        {"kind": "next", "state": "disabled"}
    ]

    hidden = NextRequestV1(
        evaluation_time=late,
        access_profile=_access().model_copy(update={"permitted_access_classes": ("public",)}),
    )
    assert _status(instance, hidden, consumers_running=True).consumers.state == "not_observed"


def test_one_next_request_reads_each_workers_health_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from cruxible_core.consumers.next import NEXT_QUEUE
    from tests.test_consumers.test_evidence_availability import _drain, _world

    instance, _capture = _world(tmp_path)
    swept = datetime(2026, 9, 1, tzinfo=UTC)
    _drain(instance, now=swept)
    health = NEXT_QUEUE.health
    calls: list[datetime] = []

    def counted(target, *, now):  # type: ignore[no-untyped-def]
        calls.append(now)
        return health(target, now=now)

    monkeypatch.setattr(NEXT_QUEUE, "health", counted)
    _status(instance, _request(instance, evaluation_time=swept), consumers_running=True)

    assert calls == [swept]


def test_an_internal_action_no_trigger_schedules_is_status_with_an_authoring_repair(
    tmp_path: Path,
) -> None:

    from cruxible_client.contracts.triggers import (
        CadenceSchedule,
        GenerationAcceptedSchedule,
    )
    from cruxible_core.triggers.journal import internal_triggers
    from tests.support.lines import action_trigger, successor, trigger_members
    from tests.test_indexes.test_resolution_contracts import _accept_tree

    instance, owner = initialize_local(tmp_path)
    seeded = _status(instance, _request(instance))
    assert seeded.triggers.state == "scheduled" and _attention(seeded) == ()
    assert seeded.triggers.detail["scheduled"] == {
        "evidence.sweep": ["Trigger:evidence-sweep"],
        "floor.refresh": ["Trigger:floor-refresh"],
        "prediction.anchor_retry": ["Trigger:prediction-anchor-retry"],
    }

    sweep = action_trigger("evidence-sweep", action="evidence.sweep", interval_seconds=86400)
    retry = action_trigger(
        "prediction-anchor-retry", action="prediction.anchor_retry", interval_seconds=3600
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree.update(
        trigger_members(
            successor(sweep, schedule=CadenceSchedule(interval_seconds=600)),
            successor(retry, state="retired"),
        )
    )
    _accept_tree(
        instance, owner, tree, timestamp="2026-09-30T12:00:00.000000Z", proposal_name="retime"
    )
    # The daemon's cadences are whatever the accepted Triggers say, per generation.
    assert [(item.action, item.schedule) for item in internal_triggers(instance)] == [
        ("evidence.sweep", CadenceSchedule(interval_seconds=600)),
        ("floor.refresh", GenerationAcceptedSchedule()),
    ]
    unscheduled = _status(instance, _request(instance))
    facet = unscheduled.triggers
    assert facet.state == "unscheduled" and not unscheduled.blocking
    assert facet.detail["scheduled"] == {
        "evidence.sweep": ["Trigger:evidence-sweep"],
        "floor.refresh": ["Trigger:floor-refresh"],
    }
    assert facet.detail["unscheduled"] == ["prediction.anchor_retry"]
    assert facet.detail["message"] == "no trigger schedules prediction.anchor_retry"
    assert facet.repair is not None
    assert facet.repair.target == "prediction.anchor_retry"
    assert facet.repair.required_change == "author_a_trigger_aimed_at_the_unscheduled_action"
    assert (facet.repair.operation, facet.repair.arguments) == (
        "cruxible.authoring.create",
        {"example": "trigger"},
    )
    assert _attention(unscheduled) == (("triggers", facet),)

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree.update(
        trigger_members(
            action_trigger(
                "anchor-retry-often", action="prediction.anchor_retry", interval_seconds=900
            )
        )
    )
    _accept_tree(
        instance, owner, tree, timestamp="2026-09-30T12:01:00.000000Z", proposal_name="restore"
    )
    restored = _status(instance, _request(instance))
    assert restored.triggers.state == "scheduled" and _attention(restored) == ()
    assert restored.triggers.detail["scheduled"] == {
        "evidence.sweep": ["Trigger:evidence-sweep"],
        "floor.refresh": ["Trigger:floor-refresh"],
        "prediction.anchor_retry": ["Trigger:anchor-retry-often"],
    }
