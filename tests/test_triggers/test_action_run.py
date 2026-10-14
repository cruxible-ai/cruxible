"""Manual on-demand fire of a registered internal action.

``fire_manual`` appends the fire to the same journal a Trigger's own
schedule writes to; the facade (``playbill_api.playbill_action_run``) names
the registry, checks the GOVERNED_WRITE tier, and -- with ``wait`` -- only
observes the owning part's own recorded progress until it covers the fire.
It starts, admits and runs no work of its own: the daemon's normal consumer
runner is what actually executes a fire, on its own tick, exactly as it
would a scheduled one.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client import contracts
from cruxible_client.contracts.triggers import INTERNAL_ACTIONS
from cruxible_core.consumers.next import NEXT_QUEUE, curation, evidence, predictions
from cruxible_core.errors import ConfigError, PermissionDeniedError
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.permissions import PermissionMode, request_permission_scope
from cruxible_core.triggers.journal import fire_manual, trigger_events

NOW = datetime(2026, 10, 13, tzinfo=UTC)

#: Every "next" action's own completion-cursor reader, keyed by action name.
_NEXT_COVERED_SEQUENCE = {
    "evidence.sweep": evidence.sweep_covered_sequence,
    "prediction.anchor_retry": predictions.retry_covered_sequence,
    "curation.detect": curation.covered_sequence,
}


def _bare_instance(root: Path):  # type: ignore[no-untyped-def]
    """Enough of an instance for the journal alone: ``root`` and ``descriptor``."""

    return SimpleNamespace(
        root=root, descriptor=SimpleNamespace(storage=SimpleNamespace(exhaust="exhaust"))
    )


def _manager(instance):  # type: ignore[no-untyped-def]
    return SimpleNamespace(get=lambda _instance_id: instance)


def _run_next_tick(instance, manager, *, now: datetime) -> None:  # type: ignore[no-untyped-def]
    """Exactly what the daemon's own consumer runner does on one tick."""

    NEXT_QUEUE.match(instance, now=now, daemon_id="daemon-tick")
    for work in NEXT_QUEUE.due(instance, now=now):
        NEXT_QUEUE.run(manager, "inst_test", work, now=now)


# ---------------------------------------------------------------------------
# fire_manual: pure journal mechanics
# ---------------------------------------------------------------------------


def test_fire_manual_refuses_an_unregistered_action(tmp_path: Path) -> None:
    world = _bare_instance(tmp_path)
    with pytest.raises(ValueError):
        fire_manual(world, action="not.a.real.action", principal="owner", now=NOW)


def test_fire_manual_records_the_principal_and_is_never_coalesced(tmp_path: Path) -> None:
    world = _bare_instance(tmp_path)
    first = fire_manual(world, action="evidence.sweep", principal="owner", now=NOW)
    second = fire_manual(world, action="evidence.sweep", principal="owner", now=NOW)
    assert first.trigger is None and second.trigger is None
    assert second.sequence == first.sequence + 1
    events = trigger_events(world, action="evidence.sweep")
    assert [event.sequence for event in events] == [first.sequence, second.sequence]


def test_fire_manual_covers_every_registered_action(tmp_path: Path) -> None:
    world = _bare_instance(tmp_path)
    for name in INTERNAL_ACTIONS:
        event = fire_manual(world, action=name, principal="owner", now=NOW)
        assert event.action == name


# ---------------------------------------------------------------------------
# playbill_action_run: registry, tier, and the fire it appends
# ---------------------------------------------------------------------------


def test_run_action_refuses_an_unregistered_name(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    instance = _bare_instance(tmp_path)
    monkeypatch.setattr(playbill_api, "get_playbill_manager", lambda: _manager(instance))
    with request_permission_scope(PermissionMode.GOVERNED_WRITE):
        with pytest.raises(ConfigError):
            playbill_api.playbill_action_run(
                "inst_test", request=contracts.ActionRunRequest(action="not.registered")
            )
    assert trigger_events(instance) == ()


def test_run_action_requires_governed_write_tier(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    def forbidden():  # type: ignore[no-untyped-def]
        pytest.fail("the instance was loaded before the permission check")

    monkeypatch.setattr(playbill_api, "get_playbill_manager", forbidden)
    with request_permission_scope(PermissionMode.READ_ONLY):
        with pytest.raises(PermissionDeniedError):
            playbill_api.playbill_action_run(
                "inst_test", request=contracts.ActionRunRequest(action="evidence.sweep")
            )


@pytest.mark.parametrize("name", sorted(INTERNAL_ACTIONS))
def test_run_action_appends_the_fire_for_every_registered_action(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    name: str,
) -> None:
    instance = _bare_instance(tmp_path)
    monkeypatch.setattr(playbill_api, "get_playbill_manager", lambda: _manager(instance))
    with request_permission_scope(PermissionMode.GOVERNED_WRITE):
        result = playbill_api.playbill_action_run(
            "inst_test", request=contracts.ActionRunRequest(action=name)
        )
    assert result.action == name
    assert result.outcome.status == "not_waited"
    (event,) = trigger_events(instance, action=name)
    assert event.sequence == result.event_id
    assert event.trigger is None


# ---------------------------------------------------------------------------
# A manual fire is picked up on the owning part's own next tick -- never by
# anything `playbill_action_run` itself admits or runs.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(_NEXT_COVERED_SEQUENCE))
def test_manual_fire_is_processed_by_the_owning_part_on_its_next_tick(
    tmp_path: Path, name: str
) -> None:
    from tests.test_query.test_query_execution_service import _instance_with_query

    instance, _owner = _instance_with_query(tmp_path)
    manager = _manager(instance)
    event = fire_manual(instance, action=name, principal="owner", now=NOW)
    covered = _NEXT_COVERED_SEQUENCE[name]
    assert covered(instance) < event.sequence

    # A part's very first tick ever on an instance may only bootstrap its
    # cursor (prediction.anchor_retry's match() does, reading no journal yet
    # on that first pass); a couple more ticks is still "the daemon's own
    # tick", never anything this action-run request admits or runs itself.
    for _tick in range(3):
        _run_next_tick(instance, manager, now=NOW)
        if covered(instance) >= event.sequence:
            break

    assert covered(instance) >= event.sequence


# ---------------------------------------------------------------------------
# playbill_action_run(wait=True): observes, never runs
# ---------------------------------------------------------------------------


def test_run_action_wait_completes_once_the_owning_part_processes_the_fire(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    """A concurrent tick -- never the waiting request itself -- does the work."""

    from tests.test_query.test_query_execution_service import _instance_with_query

    instance, _owner = _instance_with_query(tmp_path)
    manager = _manager(instance)
    monkeypatch.setattr(playbill_api, "get_playbill_manager", lambda: manager)
    name = "evidence.sweep"

    def _background_daemon() -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if trigger_events(instance, action=name):
                _run_next_tick(instance, manager, now=NOW)
                return
            time.sleep(0.01)

    thread = threading.Thread(target=_background_daemon)
    thread.start()
    try:
        started = time.monotonic()
        with request_permission_scope(PermissionMode.GOVERNED_WRITE):
            result = playbill_api.playbill_action_run(
                "inst_test",
                request=contracts.ActionRunRequest(
                    action=name, wait=True, wait_timeout_seconds=5.0
                ),
            )
        elapsed = time.monotonic() - started
    finally:
        thread.join(timeout=5.0)

    assert result.outcome.status == "completed"
    assert result.outcome.detail["covered_through"] >= result.event_id
    # Returned once the background tick finished, nowhere near the 5s bound.
    assert elapsed < 2.0


def test_run_action_wait_times_out_without_blocking_past_the_timeout(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    """Nothing ever processes the fire; the wait still returns promptly."""

    instance = _bare_instance(tmp_path)
    monkeypatch.setattr(playbill_api, "get_playbill_manager", lambda: _manager(instance))
    with request_permission_scope(PermissionMode.GOVERNED_WRITE):
        started = time.monotonic()
        result = playbill_api.playbill_action_run(
            "inst_test",
            request=contracts.ActionRunRequest(
                action="evidence.sweep", wait=True, wait_timeout_seconds=0.2
            ),
        )
        elapsed = time.monotonic() - started
    assert result.outcome.status == "timed_out"
    assert elapsed < 1.0
    # The fire stays queued for the daemon's own tick; nothing ran it here.
    assert len(trigger_events(instance, action="evidence.sweep")) == 1
    assert evidence.sweep_covered_sequence(instance) < result.event_id


def test_run_action_wait_times_out_despite_a_held_state_lock(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    """A worker transaction holding the cursor store cannot extend the wait past its budget."""

    from tests.test_query.test_query_execution_service import _instance_with_query

    instance, _owner = _instance_with_query(tmp_path)
    manager = _manager(instance)
    monkeypatch.setattr(playbill_api, "get_playbill_manager", lambda: manager)
    name = "evidence.sweep"
    fire_manual(instance, action=name, principal="owner", now=NOW)

    entered = threading.Event()
    released = threading.Event()

    def _hold_the_state_lock() -> None:
        with evidence._STATE.open(instance) as connection:
            assert connection is not None
            entered.set()
            released.wait(timeout=5.0)

    holder = threading.Thread(target=_hold_the_state_lock)
    holder.start()
    try:
        assert entered.wait(timeout=5.0)
        with request_permission_scope(PermissionMode.GOVERNED_WRITE):
            started = time.monotonic()
            result = playbill_api.playbill_action_run(
                "inst_test",
                request=contracts.ActionRunRequest(
                    action=name, wait=True, wait_timeout_seconds=0.2
                ),
            )
            elapsed = time.monotonic() - started
    finally:
        released.set()
        holder.join(timeout=5.0)

    assert result.outcome.status == "timed_out"
    # Bounded by the wait budget, not by however long the lock stayed held.
    assert elapsed < 1.0


def test_run_action_wait_on_floor_refresh_also_only_observes(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    """floor.refresh follows the same observe-only wait rule as every other action."""

    from tests.core_support._write_support import seed_write_surface

    from cruxible_core.consumers import floor as floor_module
    from cruxible_core.server.registry import InstanceRegistry

    (tmp_path / "host").mkdir()
    instance, _owner = seed_write_surface(tmp_path / "host")
    instance_id = instance.descriptor.instance_id
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = InstanceRegistry(tmp_path / "state")
    registry.create_governed_instance_with_id(instance_id, workspace_root=workspace)
    monkeypatch.setattr(floor_module, "get_registry", lambda: registry)
    manager = _manager(instance)
    monkeypatch.setattr(playbill_api, "get_playbill_manager", lambda: manager)

    def _background_daemon() -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if trigger_events(instance, action="floor.refresh"):
                floor_module.refresh_floor(instance, instance_id, follow_fire=True)
                return
            time.sleep(0.01)

    thread = threading.Thread(target=_background_daemon)
    thread.start()
    try:
        with request_permission_scope(PermissionMode.GOVERNED_WRITE):
            result = playbill_api.playbill_action_run(
                instance_id,
                request=contracts.ActionRunRequest(
                    action="floor.refresh", wait=True, wait_timeout_seconds=5.0
                ),
            )
    finally:
        thread.join(timeout=5.0)

    assert result.outcome.status == "completed"
    assert floor_module.refresh_covered_sequence(instance) >= result.event_id
