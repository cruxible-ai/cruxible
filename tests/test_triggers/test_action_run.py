"""Manual on-demand fire of a registered internal action.

``fire_manual`` appends the fire to the same journal a Trigger's own
schedule writes to; the facade (``playbill_api.playbill_action_run``) names
the registry, checks the GOVERNED_WRITE tier, and -- with ``wait`` -- runs
the owning consumer inline.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client import contracts
from cruxible_client.contracts.triggers import INTERNAL_ACTIONS
from cruxible_core.errors import ConfigError, PermissionDeniedError
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.permissions import PermissionMode, request_permission_scope
from cruxible_core.triggers.journal import fire_manual, trigger_events

NOW = datetime(2026, 10, 13, tzinfo=UTC)


def _bare_instance(root: Path):  # type: ignore[no-untyped-def]
    """Enough of an instance for the journal alone: ``root`` and ``descriptor``."""

    return SimpleNamespace(
        root=root, descriptor=SimpleNamespace(storage=SimpleNamespace(exhaust="exhaust"))
    )


def _manager(instance):  # type: ignore[no-untyped-def]
    return SimpleNamespace(get=lambda _instance_id: instance)


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


def test_run_action_wait_on_floor_refresh_does_not_force_delivery(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    """floor.refresh's own render admission is only safe from the daemon's HTTP path."""

    instance = _bare_instance(tmp_path)
    monkeypatch.setattr(playbill_api, "get_playbill_manager", lambda: _manager(instance))
    with request_permission_scope(PermissionMode.GOVERNED_WRITE):
        result = playbill_api.playbill_action_run(
            "inst_test",
            request=contracts.ActionRunRequest(action="floor.refresh", wait=True),
        )
    assert result.outcome.status == "not_waited"
    assert "floor deliver-now" in result.outcome.detail["reason"]
    # The fire is still recorded for the daemon's own tick.
    assert len(trigger_events(instance, action="floor.refresh")) == 1


# ---------------------------------------------------------------------------
# playbill_action_run(wait=True): the owning "next" consumer actually runs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", [n for n, spec in INTERNAL_ACTIONS.items() if spec.consumer == "next"]
)
def test_run_action_wait_runs_the_owning_next_part_inline(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    name: str,
) -> None:
    from tests.test_query.test_query_execution_service import _instance_with_query

    instance, _owner = _instance_with_query(tmp_path)
    monkeypatch.setattr(playbill_api, "get_playbill_manager", lambda: _manager(instance))
    with request_permission_scope(PermissionMode.GOVERNED_WRITE):
        result = playbill_api.playbill_action_run(
            "inst_test", request=contracts.ActionRunRequest(action=name, wait=True)
        )
    assert result.outcome.status == "completed"
    # Nothing else makes a freshly built instance's "next" parts due; the
    # inline run only happened because our fire made it so.
    assert result.outcome.detail["units_run"] >= 1
