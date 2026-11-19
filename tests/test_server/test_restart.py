"""Unit tests for the in-place daemon re-exec helper."""

from __future__ import annotations

import threading

import pytest

from cruxible_core.server import restart as restart_module


def test_schedule_server_restart_invokes_exec_via_background_timer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fired = threading.Event()
    restart_module.set_exec_self(fired.set)
    monkeypatch.setattr(restart_module, "_RESTART_DELAY_SECONDS", 0.0)
    try:
        restart_module.schedule_server_restart()
        assert fired.wait(timeout=2.0)
    finally:
        restart_module.reset_exec_self()


def test_reset_exec_self_restores_default() -> None:
    restart_module.set_exec_self(lambda: None)
    restart_module.reset_exec_self()
    assert restart_module._exec_self is restart_module._default_exec_self


def test_the_version_probe_and_restart_ack_carry_the_process_boot_id(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from cruxible_core.runtime.permissions import reset_permissions
    from cruxible_core.server.app import create_app
    from cruxible_core.server.credentials import reset_runtime_credential_store
    from cruxible_core.server.registry import reset_registry

    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    calls: list[bool] = []
    restart_module.set_exec_self(lambda: calls.append(True))
    try:
        client = TestClient(create_app())
        probe = client.get("/version").json()
        assert probe["boot_id"] == restart_module.PROCESS_BOOT_ID
        ack = client.post("/api/v1/server/restart")
        assert ack.status_code == 200, ack.text
        assert ack.json()["boot_id"] == restart_module.PROCESS_BOOT_ID
    finally:
        restart_module.reset_exec_self()


@pytest.mark.parametrize("operation", ["restart", "stop"])
def test_detached_server_timer_drops_the_launching_request_context(monkeypatch, operation):
    from contextvars import copy_context

    from cruxible_core.runtime.admission import FLOOR_ADMISSION, HTTP_REQUEST_CONTEXT
    from cruxible_core.server import shutdown

    original_timer = threading.Timer
    fired = threading.Event()
    observed = []

    def inheriting_timer(interval, function, args=(), kwargs=None):
        return original_timer(interval, copy_context().run, args=(function, *args), kwargs=kwargs)

    def callback():
        try:
            observed.append(HTTP_REQUEST_CONTEXT.get())
            with FLOOR_ADMISSION.hold("inst_detached_timer"):
                observed.append("held")
        finally:
            fired.set()

    monkeypatch.setattr(threading, "Timer", inheriting_timer)
    module = restart_module if operation == "restart" else shutdown
    monkeypatch.setattr(
        module, "_exec_self" if operation == "restart" else "_signal_self", callback
    )
    monkeypatch.setattr(
        module, "_RESTART_DELAY_SECONDS" if operation == "restart" else "_STOP_DELAY_SECONDS", 0
    )
    token = HTTP_REQUEST_CONTEXT.set(True)
    try:
        (
            module.schedule_server_restart
            if operation == "restart"
            else module.schedule_server_stop
        )()
        assert fired.wait(5)
        assert HTTP_REQUEST_CONTEXT.get()
    finally:
        HTTP_REQUEST_CONTEXT.reset(token)
    assert observed == [False, "held"]
    assert FLOOR_ADMISSION.active_keys() == 0
