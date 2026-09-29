"""The bootstrap secret lives in an owner-only state-root file and is never printed."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from cruxible_core.cli.main import cli
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server import app as server_app
from cruxible_core.server import restart as restart_module
from cruxible_core.server.bootstrap_secret import (
    bootstrap_secret_path,
    read_local_bootstrap_secret,
)
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import reset_registry
from cruxible_core.server.state_lock import StateRootLock


class _Socket:
    def fileno(self) -> int:
        return 7

    def close(self) -> None:
        pass


def _start_socket_daemon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **kwargs: Any) -> Path:
    state_root = tmp_path / "state"
    socket = tmp_path / "run" / "d.sock"
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(state_root))
    # Recorded so the teardown removes what run_server writes into os.environ.
    monkeypatch.setenv("CRUXIBLE_SERVER_SOCKET", "placeholder")
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "false")
    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", "")
    monkeypatch.setattr(server_app, "prepare_socket_directory", lambda _directory: None)
    monkeypatch.setattr(server_app, "bind_private_unix_socket", lambda _path: _Socket())
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=lambda *_a, **_k: None))
    reset_registry()
    reset_runtime_credential_store()
    try:
        server_app.run_server(socket_path=str(socket), **kwargs)
    finally:
        reset_registry()
        reset_runtime_credential_store()
    return state_root


def test_the_generated_secret_is_written_owner_only_and_never_printed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    extra = tmp_path / "copy" / "bootstrap"

    state_root = _start_socket_daemon(
        monkeypatch, tmp_path, auth=True, bootstrap_secret_file=str(extra)
    )

    secret = os.environ["CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET"]
    assert secret
    captured = capfd.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    held = bootstrap_secret_path(state_root)
    for path in (held, extra):
        assert path.read_text().strip() == secret
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert f"bootstrap secret written to {held}" in captured.err
    for log in (state_root / "daemon" / "logs").glob("*"):
        assert secret not in log.read_text()


def test_an_auth_off_start_removes_a_stale_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stale = bootstrap_secret_path(tmp_path / "state")
    stale.parent.mkdir(parents=True)
    stale.write_text("old\n")

    _start_socket_daemon(monkeypatch, tmp_path)

    assert not stale.exists()


def _held_secret(tmp_path: Path, transport: str, *, mode: int = 0o600) -> Path:
    state_root = tmp_path / "state"
    lock = StateRootLock(state_root, transport=transport).acquire()
    lock.release()
    path = bootstrap_secret_path(state_root)
    path.write_text("the-secret\n")
    path.chmod(mode)
    return state_root


def test_the_local_secret_goes_only_to_the_daemon_that_owns_the_state_root(
    tmp_path: Path,
) -> None:
    socket = tmp_path / "run" / "d.sock"
    by_socket = _held_secret(tmp_path, f"unix socket {socket}")

    assert (
        read_local_bootstrap_secret(by_socket, server_url=None, server_socket=str(socket))
        == "the-secret"
    )
    assert (
        read_local_bootstrap_secret(
            by_socket, server_url=None, server_socket=str(tmp_path / "other.sock")
        )
        is None
    )
    assert (
        read_local_bootstrap_secret(
            by_socket, server_url="http://127.0.0.1:8100", server_socket=None
        )
        is None
    )


def test_a_tcp_local_secret_matches_loopback_spellings_and_needs_mode_0600(
    tmp_path: Path,
) -> None:
    state_root = _held_secret(tmp_path, "127.0.0.1:8100")

    assert (
        read_local_bootstrap_secret(
            state_root, server_url="http://localhost:8100", server_socket=None
        )
        == "the-secret"
    )
    assert (
        read_local_bootstrap_secret(
            state_root, server_url="http://example.com:8100", server_socket=None
        )
        is None
    )
    bootstrap_secret_path(state_root).chmod(0o644)
    assert (
        read_local_bootstrap_secret(
            state_root, server_url="http://127.0.0.1:8100", server_socket=None
        )
        is None
    )


def test_restart_and_status_use_the_local_secret_with_no_env_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    socket = tmp_path / "run" / "d.sock"
    state_root = _held_secret(tmp_path, f"unix socket {socket}")
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(state_root))
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", "the-secret")
    monkeypatch.delenv("CRUXIBLE_SERVER_BEARER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    get_playbill_manager().clear()
    restarted: list[bool] = []
    # Never re-exec the test runner: the scheduled restart is replaced outright.
    monkeypatch.setattr(restart_module, "schedule_server_restart", lambda: restarted.append(True))
    app = server_app.create_app()
    from cruxible_client.transport.lifecycle import DaemonLifecycleClient

    real_init = DaemonLifecycleClient.__init__

    def in_process(self: DaemonLifecycleClient, **kwargs: Any) -> None:
        real_init(self, **kwargs)
        headers = (
            {} if kwargs.get("token") is None else {"Authorization": f"Bearer {kwargs['token']}"}
        )
        self._transport._client = TestClient(app, headers=headers)  # type: ignore[assignment]

    monkeypatch.setattr(DaemonLifecycleClient, "__init__", in_process)
    try:
        restart = CliRunner().invoke(
            cli, ["--server-socket", str(socket), "server", "restart", "--no-wait"]
        )
        status = CliRunner().invoke(cli, ["--server-socket", str(socket), "server", "status"])
    finally:
        get_playbill_manager().clear()
        reset_runtime_credential_store()
        reset_registry()
        reset_permissions()

    assert restart.exit_code == 0, restart.output
    assert "Restart scheduled" in restart.output
    assert restarted == [True]
    assert status.exit_code == 0, status.output
    assert "Auth enabled: yes" in status.output
    assert "the-secret" not in restart.output + status.output
