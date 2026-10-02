"""Floor refresh follows fires and confines its optional single writer to one workspace."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
from types import SimpleNamespace

import pytest

from cruxible_client.authoring.floor_apply import read_floor_manifest
from cruxible_client.authoring.workspace import PlaybillWorkspaceError
from cruxible_core.consumers import floor
from cruxible_core.consumers.floor import FLOOR, floor_outcomes, refresh_floor
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
    registry = InstanceRegistry(tmp_path / "state" / "daemon" / "registry.db")
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
    assert refresh_floor(instance, instance.descriptor.instance_id) is None
    assert instance.floor_current_memo
    assert not (workspace / ".playbill").exists()
    registry.set_floor_delivery(instance.descriptor.instance_id, True)
    result = refresh_floor(instance, instance.descriptor.instance_id)
    assert result.written.status == "written"
    manifest = read_floor_manifest(workspace / ".playbill" / "floor")
    assert manifest.generation == result.delta.head.generation
    assert refresh_floor(instance, instance.descriptor.instance_id).written.status == "unchanged"
    assert [outcome.status for outcome in floor_outcomes(instance)] == ["unchanged"]


def test_outcomes_keep_only_the_latest_even_after_an_existing_history(world):
    instance, _, _ = world
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
        with floor.floor_admission(instance.descriptor.instance_id):
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
    path = workspace / ".playbill" / "floor" / result.delta.files[0].path
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
        raise PlaybillWorkspaceError("persistent apply failure")

    monkeypatch.setattr(floor, "sync_floor_directory", fail)
    with pytest.raises(PlaybillWorkspaceError, match="persistent"):
        refresh_floor(instance, instance.descriptor.instance_id)
    health = FLOOR.health(instance, now=NOW)
    assert health[0].state == "stalled"
    assert health[0].repair.operation == "playbill.floor.export"
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


@pytest.mark.parametrize("component", [".playbill", "floor", ".PLAYBILL"])
def test_symlinked_delivery_subtree_is_refused(world, tmp_path, component):
    instance, workspace, registry = world
    registry.set_floor_delivery(instance.descriptor.instance_id, True)
    outside = tmp_path / "outside"
    outside.mkdir()
    if component == "floor":
        (workspace / ".playbill").mkdir()
        link = workspace / ".playbill" / "floor"
    else:
        link = workspace / component
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PlaybillWorkspaceError, match="symlink"):
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
            "CREATE TABLE registry_migrations (step TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO registry_migrations VALUES ('operator-step','kept')")
    registry = InstanceRegistry(database)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry.create_governed_instance_with_id("inst_floor", workspace_root=workspace)
    assert not registry.get("inst_floor").floor_delivery
    registry.set_floor_delivery("inst_floor", True)
    registry = InstanceRegistry(registry.db_path)
    assert registry.get("inst_floor").floor_delivery
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT operator_column,floor_delivery FROM instances WHERE instance_id='inst_old'"
        ).fetchone() == ("kept", 0)
        assert connection.execute("SELECT * FROM registry_migrations").fetchall() == [
            ("operator-step", "kept")
        ]
        columns = [row[1] for row in connection.execute("PRAGMA table_info(instances)")]
        assert columns.count("floor_delivery") == 1
    registry = InstanceRegistry(database)
    assert registry.get("inst_floor").floor_delivery
    assert not registry.detach_governed_workspace(
        "inst_floor", expected_workspace_root=workspace
    ).floor_delivery
    with pytest.raises(ConfigError, match="bound local workspace"):
        registry.set_floor_delivery("inst_floor", True)


def test_delivery_and_deliver_now_share_admission_but_instances_proceed(
    world, monkeypatch, tmp_path
):
    from cruxible_core.runtime import host_api

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
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(refresh_floor, instance, instance.descriptor.instance_id)
        assert entered.wait(10)
        queued = pool.submit(
            host_api.deliver_playbill_floor_now,
            instance.descriptor.instance_id,
            workspace_attachment_authorized=True,
        )
        other = pool.submit(refresh_floor, other_instance, "inst_other")
        assert other.result(timeout=10).written.status == "written"
        assert read_floor_manifest(other_workspace / ".playbill" / "floor") is not None
        assert not second.is_set()
        release.set()
        assert first.result(timeout=10).written.status == "written"
        assert queued.result(timeout=10).written.status == "unchanged"
        assert second.is_set()


def test_persistent_base_mismatch_retries_once_and_stalls(world, monkeypatch):
    from cruxible_client.authoring import workspace as adapter
    from cruxible_client.contracts.floor import PlaybillFloorApplyResultV1

    instance, _, registry = world
    registry.set_floor_delivery(instance.descriptor.instance_id, True)
    calls = []

    def mismatch(_, delta):
        calls.append(True)
        return PlaybillFloorApplyResultV1(
            status="base_mismatch", kind=delta.kind, generation=delta.head.generation
        )

    monkeypatch.setattr(adapter, "apply_floor_delta", mismatch)
    with pytest.raises(PlaybillWorkspaceError, match="full floor"):
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
    assert not registry.get(instance.descriptor.instance_id).floor_delivery
    result = host_api.set_playbill_floor_delivery(
        instance.descriptor.instance_id, enabled=True, workspace_attachment_authorized=True
    )
    assert result.floor_delivery


def test_deliver_now_keeps_opt_in_cards_under_the_same_writer(world):
    from cruxible_client.authoring.workspace import record_playbill_floor_output

    instance, workspace, registry = world
    registry.set_floor_delivery(instance.descriptor.instance_id, True)
    result = refresh_floor(instance, instance.descriptor.instance_id, include=("discovery",))
    assert result.export is not None
    assert any(item.path.startswith("subjects/") for item in result.export.files)
    record_playbill_floor_output(
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
    request = contracts.PlaybillFloorDeliveryRequestV1(enabled=True)
    with pytest.raises(ConfigError, match="Unix socket"):
        routes.set_playbill_floor_delivery(instance_id, request, tcp)
    assert routes.set_playbill_floor_delivery(instance_id, request, local).floor_delivery
    deliver = contracts.PlaybillFloorDeliverNowRequestV1()
    result = routes.deliver_playbill_floor_now(instance_id, local, deliver)
    assert result.written.status == "written"
    with pytest.raises(ConfigError, match="Unix socket"):
        routes.deliver_playbill_floor_now(instance_id, tcp, deliver)


def test_floor_schedule_advisory_is_opt_in(world, monkeypatch):
    from cruxible_core.coverage.contracts import CoverageAccessProfileV1
    from cruxible_core.server import registry as registry_module
    from cruxible_core.service.discovery.next import _triggers_health

    instance, _, registry = world
    monkeypatch.setattr(registry_module, "get_registry", lambda: registry)

    def health():
        return _triggers_health(
            instance,
            coordinate=instance.accepted_coordinate(),
            access_profile=CoverageAccessProfileV1(profile_id="floor-test"),
        )

    assert health().state == "scheduled"
    registry.set_floor_delivery(instance.descriptor.instance_id, True)
    assert health().state == "unscheduled"
    assert health().detail["unscheduled"] == ["floor.refresh"]


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
        raise PlaybillWorkspaceError("render failed")

    monkeypatch.setattr(floor, "advance_floor_index", fail)
    with pytest.raises(PlaybillWorkspaceError, match="render failed"):
        refresh_floor(instance, instance.descriptor.instance_id)
    assert not FLOOR.active(instance)  # No runnable work remains for a retired Trigger.
    live.append(SimpleNamespace(action="floor.refresh"))
    assert FLOOR.active(instance)
    assert FLOOR.health(instance, now=NOW)[0].state == "stalled"
