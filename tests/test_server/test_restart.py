"""Unit tests for the in-place daemon re-exec helper."""

from __future__ import annotations

import sys
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


def test_restart_command_reproduces_launch_argv() -> None:
    command = restart_module.restart_command()
    assert command[0] == sys.executable
    assert command[1:] == sys.argv


def test_reset_exec_self_restores_default() -> None:
    restart_module.set_exec_self(lambda: None)
    restart_module.reset_exec_self()
    assert restart_module._exec_self is restart_module._default_exec_self
