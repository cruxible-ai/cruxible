"""Floor refresh follows fires and confines its optional single writer to one workspace."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
from types import SimpleNamespace

import pytest

from cruxible_client.authoring.floor_apply import read_floor_manifest
from cruxible_client.authoring.workspace import WorkspaceError
from cruxible_core.consumers import floor
from cruxible_core.consumers.floor import FLOOR, floor_outcomes, refresh_floor
from cruxible_core.runtime.admission import FLOOR_ADMISSION
from cruxible_core.server.registry import InstanceRegistry
from cruxible_core.triggers.journal import evaluate_triggers, schedule_deadline
from tests.core_support._write_support import seed_write_surface
from tests.test_floor.test_floor_index import WI1, _set, _write

NOW = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
def world(tmp_path, monkeypatch):
    (tmp_path / "host").mkdir()
    instance, _ = seed_write_surface(tmp_path / "host")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = InstanceRegistry(tmp_path / "state")
    registry.create_governed_instance_with_id(
        instance.descriptor.instance_id, workspace_root=workspace
    )
    monkeypatch.setattr(floor, "get_registry", lambda: registry)
    return instance, workspace, registry


def signal(instance):
    # A worker signal uses the same journal and cursor as a Trigger fire.
    schedule_deadline(instance, "test.floor", NOW)
    # The internal action name is reserved for governed Triggers; use the
    # journal's record seam to supply a deterministic action event in this fixture.
    with floor_journal(instance) as connection:
        from cruxible_core.triggers.journal import _Fire, _record

        _record(connection, [_Fire("floor.refresh", "Trigger:floor", NOW)], now=NOW)
    evaluate_triggers(instance, now=NOW, listening_since=NOW, triggers=())
    FLOOR.match(instance, now=NOW, daemon_id="daemon")


def floor_journal(instance):
    from cruxible_core.triggers.journal import _open

    return _open(instance, create=True)


def test_off_renders_without_writing_and_on_delivers_the_head(world):
    instance, workspace, registry = world
    registry.set_floor_delivery(instance.descriptor.instance_id, False)
    assert refresh_floor(instance, instance.descriptor.instance_id) is None
    assert instance.floor_current_memo
    assert not (workspace / ".cruxible").exists()
    registry.set_floor_delivery(instance.descriptor.instance_id, True)
    result = refresh_floor(instance, instance.descriptor.instance_id)
    assert result.written.status == "written"
    manifest = read_floor_manifest(workspace / ".cruxible" / "floor")
    assert manifest.generation == result.delta.head.generation
    assert refresh_floor(instance, instance.descriptor.instance_id).written.status == "unchanged"
    assert [outcome.status for outcome in floor_outcomes(instance)] == ["unchanged"]


def test_outcomes_keep_only_the_latest_even_after_an_existing_history(world):
    instance, _, registry = world
    registry.set_floor_delivery(instance.descriptor.instance_id, False)
    refresh_floor(instance, instance.descriptor.instance_id)
    (outcome,) = floor_outcomes(instance)
    with floor._STATE.open(instance) as connection:
        connection.executemany(
            "INSERT INTO outcomes(payload) VALUES (?)", [(outcome.model_dump_json(),)] * 40
        )
    # Readers are bounded even before the next run prunes an older database.
    assert floor_outcomes(instance) == (outcome,)
    for _ in range(40):
        refresh_floor(instance, instance.descriptor.instance_id)
    with floor._STATE.open(instance, create=False) as connection:
        assert connection.execute("SELECT count(*) FROM outcomes").fetchone() == (1,)
    assert floor_outcomes(instance) == (outcome,)
    assert FLOOR.health(instance, now=NOW)[0].detail["outcome"] == outcome.model_dump(mode="json")


def test_three_pending_accepts_coalesce_at_the_current_head(world):
    instance, _, registry = world
    registry.set_floor_delivery(instance.descriptor.instance_id, True)
    signal(instance)
    (work,) = FLOOR.due(instance, now=NOW)
    manager = SimpleNamespace(get=lambda _: instance)
    busy = Event()

    def run():
        busy.set()
        FLOOR.run(manager, instance.descriptor.instance_id, work, now=NOW)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with FLOOR_ADMISSION.hold(instance.descriptor.instance_id):
            pending = pool.submit(run)
            assert busy.wait(10)
            for value in ("ready", "blocked", "done"):
                _write(instance, _set(WI1, "status", value))
                signal(instance)
        pending.result(timeout=10)
    assert len(floor_outcomes(instance)) == 1
    with instance.accepted_history_reader() as history:
        assert floor_outcomes(instance)[0].generation == history.sequence
    assert tuple(FLOOR.due(instance, now=NOW)) == ()


def test_hand_edited_base_gets_exactly_one_full_retry(world, monkeypatch):
    instance, workspace, registry = world
    registry.set_floor_delivery(instance.descriptor.instance_id, True)
    result = refresh_floor(instance, instance.descriptor.instance_id)
    path = workspace / ".cruxible" / "floor" / result.delta.files[0].path
    path.write_text("changed by hand")
    calls = []
    fetch = floor.service_playbill_floor_delta

    def observed(*args, **kwargs):
        calls.append((kwargs["base_generation"], kwargs["base_renderer"]))
        return fetch(*args, **kwargs)

    monkeypatch.setattr(floor, "service_playbill_floor_delta", observed)
    repaired = refresh_floor(instance, instance.descriptor.instance_id)
    assert repaired.written.status == "written"
    assert len(calls) == 2 and calls[-1] == (None, None)


def test_failed_target_stalls_until_head_or_registration_changes(world, monkeypatch):
    from cruxible_core.service.discovery.next import _consumer_stalled_items

    instance, _, registry = world
    registry.set_floor_delivery(instance.descriptor.instance_id, True)
    signal(instance)
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(True)
        raise WorkspaceError("persistent apply failure")

    monkeypatch.setattr(floor, "sync_floor_directory", fail)
    with pytest.raises(WorkspaceError, match="persistent"):
        refresh_floor(instance, instance.descriptor.instance_id)
    health = FLOOR.health(instance, now=NOW)
    assert health[0].state == "stalled"
    assert health[0].repair.operation == "cruxible.floor.export"
    assert _consumer_stalled_items(health)[0].reason == "consumer_stalled"
    for _ in range(3):
        FLOOR.match(instance, now=NOW, daemon_id="daemon")
        assert tuple(FLOOR.due(instance, now=NOW)) == ()
    assert len(attempts) == 1
    _write(instance, _set(WI1, "status", "ready"))
    FLOOR.match(instance, now=NOW, daemon_id="daemon")
    assert tuple(FLOOR.due(instance, now=NOW))
    registry.set_floor_delivery(instance.descriptor.instance_id, False)
    FLOOR.match(instance, now=NOW, daemon_id="daemon")
    refresh_floor(instance, instance.descriptor.instance_id)
    assert FLOOR.health(instance, now=NOW)[0].state == "running"


@pytest.mark.parametrize("component", [".cruxible", "floor", ".CRUXIBLE"])
def test_symlinked_delivery_subtree_is_refused(world, tmp_path, component):
    instance, workspace, registry = world
    registry.set_floor_delivery(instance.descriptor.instance_id, True)
    outside = tmp_path / "outside"
    outside.mkdir()
    if component == "floor":
        (workspace / ".cruxible").mkdir()
        link = workspace / ".cruxible" / "floor"
    else:
        link = workspace / component
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceError, match="symlink"):
        refresh_floor(instance, instance.descriptor.instance_id)
    assert list(outside.iterdir()) == []
    assert floor_outcomes(instance)[-1].status == "failed"


def test_registry_migration_is_idempotent_and_detach_clears_delivery(tmp_path):
    import sqlite3

    from cruxible_core.errors import ConfigError

    database = tmp_path / "daemon" / "registry.db"
    database.parent.mkdir()
    # An operator migration may have already added a column before this step.
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE instances (instance_id TEXT PRIMARY KEY, backend TEXT NOT NULL, "
            "location TEXT NOT NULL, workspace_root TEXT, created_at TEXT NOT NULL, "
            "operator_column TEXT, UNIQUE(backend,location))"
        )
        connection.execute(
            "INSERT INTO instances VALUES ('inst_old','governed_daemon','old',NULL,'old','kept')"
        )
        connection.execute(
            "INSERT INTO instances VALUES "
            "('inst_attached','governed_daemon','attached','/attached','old','kept')"
        )
        connection.execute(
            "CREATE TABLE registry_migrations (step TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO registry_migrations VALUES ('operator-step','kept')")
    registry = InstanceRegistry(tmp_path)
    assert registry.get("inst_attached").floor_delivery
    registry.set_floor_delivery("inst_attached", False)
    assert not InstanceRegistry(tmp_path).get("inst_attached").floor_delivery
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry.create_governed_instance_with_id("inst_floor", workspace_root=workspace)
    assert registry.get("inst_floor").floor_delivery
    registry.set_floor_delivery("inst_floor", False)
    assert not registry.get("inst_floor").floor_delivery
    registry.set_floor_delivery("inst_floor", True)
    registry = InstanceRegistry(registry.state_root)
    assert registry.get("inst_floor").floor_delivery
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT operator_column,floor_delivery FROM instances WHERE instance_id='inst_old'"
        ).fetchone() == ("kept", 0)
        # The operator's own step is kept; the chain records each of its own
        # steps once, by id, beside it.
        steps = [
            row[0]
            for row in connection.execute("SELECT step FROM registry_migrations ORDER BY rowid")
        ]
        assert steps == [
            "operator-step",
            "2026-10-01-relative-locations",
            "2026-10-01-floor-delivery-column",
        ]
        columns = [row[1] for row in connection.execute("PRAGMA table_info(instances)")]
        assert columns.count("floor_delivery") == 1
    registry = InstanceRegistry(tmp_path)
    assert registry.get("inst_floor").floor_delivery
    assert not registry.detach_governed_workspace(
        "inst_floor", expected_workspace_root=workspace
    ).floor_delivery
    with pytest.raises(ConfigError, match="bound local workspace"):
        registry.set_floor_delivery("inst_floor", True)
    assert registry.attach_governed_workspace("inst_floor", workspace).floor_delivery


def test_delivery_and_deliver_now_share_admission_but_instances_proceed(
    world, monkeypatch, tmp_path
):
    import asyncio

    from cruxible_core.runtime import host_api
    from cruxible_core.server.playbill_request_models import FloorDeltaRequest
    from cruxible_core.server.routes import playbill as routes

    instance, _, registry = world
    registry.set_floor_delivery(instance.descriptor.instance_id, True)
    (tmp_path / "other-host").mkdir()
    other_instance, _ = seed_write_surface(tmp_path / "other-host")
    other_workspace = tmp_path / "other-workspace"
    other_workspace.mkdir()
    registry.create_governed_instance_with_id("inst_other", workspace_root=other_workspace)
    registry.set_floor_delivery("inst_other", True)
    entered, release, second = Event(), Event(), Event()
    render = floor.advance_floor_index
    calls = []

    def blocked(*args):
        if args[0] is other_instance:
            return render(*args)
        calls.append(True)
        if len(calls) == 1:
            entered.set()
            assert release.wait(10)
        else:
            second.set()
        return render(*args)

    monkeypatch.setattr(floor, "advance_floor_index", blocked)
    monkeypatch.setattr(
        host_api, "get_playbill_manager", lambda: SimpleNamespace(get=lambda _: instance)
    )
    # The HTTP delta route is the third writer of the same admission.
    delta_rendered = Event()

    def delta(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        delta_rendered.set()
        return "delta"

    monkeypatch.setattr(routes, "resolve_server_instance_id", lambda instance_id: instance_id)
    monkeypatch.setattr(routes.playbill_api, "playbill_floor_delta", delta)

    def delta_route():  # type: ignore[no-untyped-def]
        return asyncio.run(routes.floor_delta(instance.descriptor.instance_id, FloorDeltaRequest()))

    with ThreadPoolExecutor(max_workers=4) as pool:
        first = pool.submit(refresh_floor, instance, instance.descriptor.instance_id)
        assert entered.wait(10)
        queued = pool.submit(
            host_api.deliver_playbill_floor_now,
            instance.descriptor.instance_id,
            workspace_attachment_authorized=True,
        )
        routed = pool.submit(delta_route)
        other = pool.submit(refresh_floor, other_instance, "inst_other")
        assert other.result(timeout=10).written.status == "written"
        assert read_floor_manifest(other_workspace / ".cruxible" / "floor") is not None
        assert not second.is_set()
        # The route is admitted only once the consumer's render lets go.
        assert not delta_rendered.wait(0.2)
        release.set()
        assert first.result(timeout=10).written.status == "written"
        assert queued.result(timeout=10).written.status == "unchanged"
        assert routed.result(timeout=10) == "delta"
        assert second.is_set() and delta_rendered.is_set()
    assert FLOOR_ADMISSION.active_keys() == 0


def test_persistent_base_mismatch_retries_once_and_stalls(world, monkeypatch):
    from cruxible_client.authoring import workspace as adapter
    from cruxible_client.contracts.floor import FloorApplyResult

    instance, _, registry = world
    registry.set_floor_delivery(instance.descriptor.instance_id, True)
    calls = []

    def mismatch(_, delta):
        calls.append(True)
        return FloorApplyResult(
            status="base_mismatch", kind=delta.kind, generation=delta.head.generation
        )

    monkeypatch.setattr(adapter, "apply_floor_delta", mismatch)
    with pytest.raises(WorkspaceError, match="full floor"):
        refresh_floor(instance, instance.descriptor.instance_id)
    assert len(calls) == 2
    assert FLOOR.health(instance, now=NOW)[0].state == "stalled"


def test_delivery_authority_requires_the_local_attachment_gate(world, monkeypatch):
    from cruxible_core.errors import ConfigError
    from cruxible_core.runtime import host_api

    instance, _, registry = world
    monkeypatch.setattr(host_api, "get_registry", lambda: registry)
    with pytest.raises(ConfigError, match="Unix socket"):
        host_api.set_playbill_floor_delivery(instance.descriptor.instance_id, enabled=True)
    with pytest.raises(ConfigError, match="Unix socket"):
        host_api.deliver_playbill_floor_now(instance.descriptor.instance_id)
    assert registry.get(instance.descriptor.instance_id).floor_delivery
    result = host_api.set_playbill_floor_delivery(
        instance.descriptor.instance_id, enabled=True, workspace_attachment_authorized=True
    )
    assert result.floor_delivery


def test_deliver_now_keeps_opt_in_cards_under_the_same_writer(world):
    from cruxible_client.authoring.workspace import record_floor_output

    instance, workspace, registry = world
    registry.set_floor_delivery(instance.descriptor.instance_id, True)
    result = refresh_floor(instance, instance.descriptor.instance_id, include=("discovery",))
    assert result.export is not None
    assert any(item.path.startswith("subjects/") for item in result.export.files)
    record_floor_output(
        workspace,
        instance_id=instance.descriptor.instance_id,
        server_socket=str(workspace.parent / "socket"),
        include=("discovery",),
    )
    # A scheduled refresh retains the chosen profile instead of becoming a
    # second writer that silently strips the full export's cards.
    result = refresh_floor(instance, instance.descriptor.instance_id)
    assert result.export is not None
    assert result.written.status == "unchanged"


def test_new_routes_deliver_synchronously_and_refuse_tcp_callers(world, monkeypatch):
    import asyncio

    from fastapi import Request

    from cruxible_client import contracts
    from cruxible_core.errors import ConfigError
    from cruxible_core.runtime import host_api
    from cruxible_core.server.routes import hosted_instances as routes

    instance, _, registry = world
    instance_id = instance.descriptor.instance_id
    monkeypatch.setattr(host_api, "get_registry", lambda: registry)
    monkeypatch.setattr(
        host_api, "get_playbill_manager", lambda: SimpleNamespace(get=lambda _: instance)
    )
    monkeypatch.setattr(routes, "resolve_server_instance_id", lambda value: value)
    monkeypatch.setattr(
        routes, "resolve_server_settings", lambda: SimpleNamespace(server_socket="socket")
    )
    local = Request({"type": "http", "client": None})
    tcp = Request({"type": "http", "client": ("127.0.0.1", 1234)})
    request = contracts.FloorDeliveryRequest(enabled=True)
    with pytest.raises(ConfigError, match="Unix socket"):
        asyncio.run(routes.set_playbill_floor_delivery(instance_id, request, tcp))
    assert asyncio.run(
        routes.set_playbill_floor_delivery(instance_id, request, local)
    ).floor_delivery
    deliver = contracts.FloorDeliverNowRequest()
    result = asyncio.run(routes.deliver_playbill_floor_now(instance_id, local, deliver))
    assert result.written.status == "written"
    with pytest.raises(ConfigError, match="Unix socket"):
        asyncio.run(routes.deliver_playbill_floor_now(instance_id, tcp, deliver))


def test_floor_schedule_advisory_requires_delivery_and_no_live_floor_trigger(world, monkeypatch):
    from cruxible_core.coverage.contracts import CoverageAccessProfile
    from cruxible_core.server import registry as registry_module
    from cruxible_core.service.discovery.next import _triggers_health

    instance, _, registry = world
    monkeypatch.setattr(registry_module, "get_registry", lambda: registry)

    def health():
        return _triggers_health(
            instance,
            coordinate=instance.accepted_coordinate(),
            access_profile=CoverageAccessProfile(profile_id="floor-test"),
        )

    registry.set_floor_delivery(instance.descriptor.instance_id, False)
    assert health().state == "scheduled"
    registry.set_floor_delivery(instance.descriptor.instance_id, True)
    assert health().state == "scheduled"
    from cruxible_client.contracts.triggers import parse_trigger, render_trigger, trigger_path
    from tests.support.lines import successor
    from tests.test_floor.test_floor_index import accept_edit

    path = trigger_path("floor-refresh")
    live = parse_trigger(instance.tree_at(instance.accepted_coordinate().git_oid)[path], path=path)
    accept_edit(
        instance, "retire-floor-trigger", {path: render_trigger(successor(live, state="retired"))}
    )
    assert health().state == "unscheduled"
    assert health().detail["unscheduled"] == ["floor.refresh"]
    registry.set_floor_delivery(instance.descriptor.instance_id, False)
    assert health().state == "scheduled"


def test_retired_floor_trigger_is_active_only_until_outstanding_work_finishes(world, monkeypatch):
    instance, _, _ = world
    live = [SimpleNamespace(action="floor.refresh")]
    monkeypatch.setattr(floor, "internal_triggers", lambda _: tuple(live))
    assert FLOOR.active(instance)
    refresh_floor(instance, instance.descriptor.instance_id)
    live.clear()  # The accepted Trigger set after the last floor Trigger retires.
    assert floor._STATE.path(instance).exists()
    assert not FLOOR.active(instance)
    _write(instance, _set(WI1, "status", "ready"))
    signal(instance)
    assert FLOOR.active(instance)
    refresh_floor(instance, instance.descriptor.instance_id)
    assert not FLOOR.active(instance)

    def fail(*_):
        raise WorkspaceError("render failed")

    monkeypatch.setattr(floor, "advance_floor_index", fail)
    with pytest.raises(WorkspaceError, match="render failed"):
        refresh_floor(instance, instance.descriptor.instance_id)
    assert not FLOOR.active(instance)  # No runnable work remains for a retired Trigger.
    live.append(SimpleNamespace(action="floor.refresh"))
    assert FLOOR.active(instance)
    assert FLOOR.health(instance, now=NOW)[0].state == "stalled"


def test_deliver_now_refuses_a_pinned_coordinate_with_a_runnable_repair(world, monkeypatch):
    from cruxible_client import contracts
    from cruxible_client.errors import response_to_error
    from cruxible_core.errors import RequestRefusedError
    from cruxible_core.indexes.projection import AcceptedCoordinate
    from cruxible_core.runtime import host_api
    from cruxible_core.server.errors import error_to_response

    instance, workspace, registry = world
    instance_id = instance.descriptor.instance_id
    pinned = contracts.AcceptedCoordinate.model_validate(
        AcceptedCoordinate.from_internal(instance.accepted_coordinate()).model_dump(mode="json")
    )
    registry.set_floor_delivery(instance_id, True)
    _write(instance, _set(WI1, "status", "ready"))
    monkeypatch.setattr(
        host_api, "get_playbill_manager", lambda: SimpleNamespace(get=lambda _: instance)
    )
    with pytest.raises(RequestRefusedError, match="current accepted head") as refused:
        host_api.deliver_playbill_floor_now(
            instance_id, at=pinned, workspace_attachment_authorized=True
        )
    assert "use get or query with at" in str(refused.value)
    assert "floor-delivery off" in str(refused.value)
    status, envelope = error_to_response(refused.value)
    assert status == 400
    assert envelope.error_code == "cruxible.floor.delivery_head_only"
    assert envelope.repair.operation == "cruxible.workspace.floor-delivery"
    assert envelope.repair.arguments == {"state": "off", "instance_id": instance_id}
    client_error = response_to_error(status, envelope)
    assert client_error.error_code == envelope.error_code
    assert client_error.repair == envelope.repair
    assert floor_outcomes(instance) == ()
    assert not (workspace / ".cruxible").exists()


def test_daemon_delivery_writes_the_local_indexes_the_client_writes(tmp_path, monkeypatch):
    from cruxible_client.authoring.workspace import materialize_floor
    from cruxible_core.service.floor.floor import service_export_playbill_floor
    from tests.core_support._write_support import report_evidence
    from tests.test_floor.test_floor_current import NOTE, _add_document, _export_envelope

    (tmp_path / "host").mkdir()
    instance, _ = seed_write_surface(tmp_path / "host")
    workspace = tmp_path / "workspace"
    _write(instance, _set(WI1, "measured", 3, evidence=report_evidence(workspace, "Count: 3")))
    _add_document(instance, "reports", NOTE.encode())
    registry = InstanceRegistry(tmp_path / "state")
    registry.create_governed_instance_with_id(
        instance.descriptor.instance_id, workspace_root=workspace
    )
    registry.set_floor_delivery(instance.descriptor.instance_id, True)
    monkeypatch.setattr(floor, "get_registry", lambda: registry)
    local = ("sources/INDEX", "projections/INDEX", ".gitignore")

    assert refresh_floor(instance, instance.descriptor.instance_id).written.status == "written"
    delivered = {path: (workspace / ".cruxible/floor" / path).read_bytes() for path in local}
    assert b"reports.md" in delivered["sources/INDEX"]
    assert delivered[".gitignore"] == b"*\n"
    import subprocess

    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", ".cruxible/floor"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == ""

    client = tmp_path / "client"
    report_evidence(client, "Count: 3")
    materialize_floor(client, export=_export_envelope(service_export_playbill_floor(instance)))
    assert delivered == {path: (client / ".cruxible/floor" / path).read_bytes() for path in local}


def test_seeded_floor_trigger_delivers_after_accept_and_retirement_stops_it(tmp_path, monkeypatch):
    from datetime import timedelta

    from cruxible_client.contracts.triggers import parse_trigger, trigger_path
    from cruxible_core.consumers.runner import ConsumerRunner
    from cruxible_core.triggers.journal import trigger_events
    from tests.core_support._support import initialize_local
    from tests.support.lines import action_trigger, successor, trigger_members
    from tests.test_indexes.test_resolution_contracts import _accept_tree

    instance, owner = initialize_local(tmp_path)
    workspace = tmp_path / "workspace"
    registry = InstanceRegistry(tmp_path / "state")
    registry.create_governed_instance_with_id(instance.descriptor.instance_id, workspace)
    monkeypatch.setattr(floor, "get_registry", lambda: registry)
    manager = SimpleNamespace(get=lambda _: instance)
    runner = ConsumerRunner(manager, kinds=(FLOOR,))
    instance_id = instance.descriptor.instance_id
    runner.match_once(instance_id, instance, now=NOW)
    assert not (workspace / ".cruxible/floor").exists()

    def accept(name, at, trigger):
        tree = instance.tree_at(instance.accepted_coordinate().git_oid)
        tree.update(trigger_members(trigger))
        _accept_tree(
            instance,
            owner,
            tree,
            timestamp=at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            proposal_name=name,
        )
        runner.match_once(instance_id, instance, now=at)
        for work in FLOOR.due(instance, now=at):
            FLOOR.run(manager, instance_id, work, now=at)

    at = NOW + timedelta(seconds=1)
    extra = action_trigger("extra-sweep", action="evidence.sweep", interval_seconds=60)
    accept("first-accept", at, extra)
    floor_root = workspace / ".cruxible/floor"
    delivered = read_floor_manifest(floor_root)
    assert delivered.coordinate.git_oid == instance.accepted_coordinate().git_oid
    assert [
        event.trigger for event in trigger_events(instance) if event.action == "floor.refresh"
    ] == ["Trigger:floor-refresh"]
    path = trigger_path("floor-refresh")
    seeded = parse_trigger(
        instance.tree_at(instance.accepted_coordinate().git_oid)[path], path=path
    )
    accept("retire-floor", at + timedelta(seconds=1), successor(seeded, state="retired"))
    accept("next-accept", at + timedelta(seconds=2), successor(extra))
    assert read_floor_manifest(floor_root) == delivered
    assert not FLOOR.active(instance)
    assert (
        len([event for event in trigger_events(instance) if event.action == "floor.refresh"]) == 1
    )


@pytest.mark.parametrize("relative", ["floor/.gitignore", "coverage.json"])
def test_fifo_local_input_stalls_delivery_promptly_and_releases_admission(world, relative):
    import os

    from tests.support.fifos import call_with_fifo_timeout

    instance, workspace, _ = world
    instance_id = instance.descriptor.instance_id
    refresh_floor(instance, instance_id)
    fifo = workspace / ".cruxible" / relative
    fifo.unlink(missing_ok=True)
    os.mkfifo(fifo)
    with pytest.raises(WorkspaceError, match="not a regular file"):
        call_with_fifo_timeout(fifo, lambda: refresh_floor(instance, instance_id))
    assert FLOOR_ADMISSION.active_keys() == 0
    with FLOOR_ADMISSION.hold(instance_id):
        pass
    assert floor_outcomes(instance)[0].status == "failed"
    health = FLOOR.health(instance, now=NOW)
    assert health[0].state == "stalled"
    assert health[0].repair.operation == "cruxible.floor.export"
