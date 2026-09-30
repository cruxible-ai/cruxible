"""The bootstrap secret lives in an owner-only state-root file and is never printed."""

from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import click
import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from cruxible_client.contracts.operator_mac import (
    OPERATOR_MAC_HEADER,
    OPERATOR_NONCE_HEADER,
    OPERATOR_TIMESTAMP_HEADER,
    operator_request_mac,
)
from cruxible_core.cli.main import cli
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server import app as server_app
from cruxible_core.server import restart as restart_module
from cruxible_core.server.bootstrap_secret import (
    bootstrap_secret_path,
    read_local_bootstrap_secret,
    reset_operator_nonces,
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


def _held_secret(
    tmp_path: Path, transport: str, *, live: bool = True
) -> tuple[Path, StateRootLock | None]:
    """A state root whose lock records ``transport``; held by this process when live."""

    state_root = tmp_path / "state"
    lock = StateRootLock(state_root, transport=transport).acquire()
    if not live:
        lock.release()
    path = bootstrap_secret_path(state_root)
    path.write_text("the-secret\n")
    path.chmod(0o600)
    return state_root, lock if live else None


def test_a_stale_lock_never_releases_the_secret(tmp_path: Path) -> None:
    socket = tmp_path / "run" / "d.sock"
    # The daemon stopped or crashed: its record remains and nobody holds the lock.
    state_root, _lock = _held_secret(tmp_path, f"unix socket {socket}", live=False)

    assert (
        read_local_bootstrap_secret(state_root, server_url=None, server_socket=str(socket)) is None
    )


def test_the_secret_needs_a_live_lock_on_the_exact_transport_and_an_owner_only_file(
    tmp_path: Path,
) -> None:
    socket = tmp_path / "run" / "d.sock"
    state_root, lock = _held_secret(tmp_path, f"unix socket {socket}")
    try:
        held = read_local_bootstrap_secret(state_root, server_url=None, server_socket=str(socket))
        elsewhere = read_local_bootstrap_secret(
            state_root, server_url=None, server_socket=str(tmp_path / "other.sock")
        )
        bootstrap_secret_path(state_root).chmod(0o644)
        shared = read_local_bootstrap_secret(state_root, server_url=None, server_socket=str(socket))
    finally:
        assert lock is not None
        lock.release()

    assert held == "the-secret"
    assert elsewhere is None and shared is None


def _signed_headers(
    secret: str, method: str, path: str, *, nonce: str = "a" * 32, at: int | None = None
) -> dict[str, str]:
    timestamp = str(int(time.time()) if at is None else at)
    return {
        OPERATOR_NONCE_HEADER: nonce,
        OPERATOR_TIMESTAMP_HEADER: timestamp,
        OPERATOR_MAC_HEADER: operator_request_mac(
            secret,
            method=method,
            path=path,
            query="",
            body=b"",
            nonce=nonce,
            timestamp=timestamp,
        ),
    }


@pytest.fixture
def operator_daemon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", "the-secret")
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    reset_operator_nonces()
    get_playbill_manager().clear()
    yield TestClient(server_app.create_app())
    get_playbill_manager().clear()
    reset_runtime_credential_store()
    reset_registry()
    reset_permissions()


INFO = "/api/v1/server/info"


def test_a_request_mac_under_the_secret_authorizes_a_lifecycle_request(operator_daemon) -> None:
    answered = operator_daemon.get(INFO, headers=_signed_headers("the-secret", "GET", INFO))

    assert answered.status_code == 200, answered.text


def test_a_relay_or_replacement_without_the_secret_cannot_sign(operator_daemon) -> None:
    forged = operator_daemon.get(INFO, headers=_signed_headers("not-the-secret", "GET", INFO))
    # A valid MAC is bound to its method and path: it authorizes nothing else.
    moved = operator_daemon.post(
        "/api/v1/server/stop", headers=_signed_headers("the-secret", "GET", INFO)
    )

    for refused in (forged, moved):
        assert refused.status_code == 401, refused.text
        assert refused.json()["error_code"] == "runtime_bootstrap.operator_mac_invalid"


def test_a_replayed_or_stale_request_is_refused(operator_daemon) -> None:
    signed = _signed_headers("the-secret", "GET", INFO, nonce="b" * 32)
    first = operator_daemon.get(INFO, headers=signed)
    replayed = operator_daemon.get(INFO, headers=signed)
    stale = operator_daemon.get(
        INFO, headers=_signed_headers("the-secret", "GET", INFO, at=int(time.time()) - 3600)
    )

    assert first.status_code == 200, first.text
    assert replayed.status_code == 401
    assert replayed.json()["error_code"] == "runtime_bootstrap.operator_mac_replayed"
    assert stale.status_code == 401
    assert stale.json()["error_code"] == "runtime_bootstrap.operator_mac_stale"


def test_the_operator_proof_route_is_gone(operator_daemon) -> None:
    assert operator_daemon.post("/operator-proof", json={"challenge": "ab" * 32}).status_code in (
        401,
        404,
        405,
    )


def _fallback_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handler: Any) -> Any:
    from cruxible_core.cli.commands import _common

    socket = tmp_path / "run" / "d.sock"
    state_root, lock = _held_secret(tmp_path, f"unix socket {socket}")
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(state_root))
    monkeypatch.delenv("CRUXIBLE_SERVER_BEARER_TOKEN", raising=False)
    with click.Context(cli, obj={"server_socket": str(socket)}):
        client = _common._get_lifecycle_client()
    assert client is not None
    client._transport._client._client._transport = httpx.MockTransport(handler)
    return client, lock


def test_the_fallback_never_puts_the_secret_on_the_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[httpx.Request] = []

    def replaced_daemon(request: httpx.Request) -> httpx.Response:
        # Whatever answers here -- the daemon, a relay, or a process that took
        # the endpoint over after the daemon stopped -- sees only the request.
        sent.append(request)
        return httpx.Response(401, json={"error_type": "AuthenticationError", "message": "x"})

    client, lock = _fallback_client(tmp_path, monkeypatch, replaced_daemon)
    try:
        with pytest.raises(Exception):
            client.server_info()
    finally:
        lock.release()

    (request,) = sent
    wire = b"".join(name + b": " + value + b"\r\n" for name, value in request.headers.raw)
    assert b"the-secret" not in wire + request.content
    assert "authorization" not in {name.lower() for name in request.headers}
    assert request.headers[OPERATOR_MAC_HEADER]


def test_a_daemon_that_replaced_the_original_cannot_verify_the_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", "the-replacements-secret")
    reset_operator_nonces()
    reset_registry()
    reset_runtime_credential_store()
    replacement = TestClient(server_app.create_app())

    def forward(request: httpx.Request) -> httpx.Response:
        answered = replacement.request(
            request.method, request.url.path, headers=dict(request.headers)
        )
        return httpx.Response(answered.status_code, content=answered.content)

    client, lock = _fallback_client(tmp_path, monkeypatch, forward)
    try:
        with pytest.raises(Exception) as refused:
            client.server_info()
    finally:
        lock.release()
        reset_registry()
        reset_runtime_credential_store()

    assert (
        "operator_mac_invalid" in str(refused.value)
        or getattr(refused.value, "error_code", None) == "runtime_bootstrap.operator_mac_invalid"
    )


def test_restart_and_status_use_the_local_secret_with_no_env_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    socket = tmp_path / "run" / "d.sock"
    state_root, lock = _held_secret(tmp_path, f"unix socket {socket}")
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(state_root))
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", "the-secret")
    monkeypatch.delenv("CRUXIBLE_SERVER_BEARER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    reset_operator_nonces()
    get_playbill_manager().clear()
    restarted: list[bool] = []
    # Never re-exec the test runner: the scheduled restart is replaced outright.
    monkeypatch.setattr(restart_module, "schedule_server_restart", lambda: restarted.append(True))
    app = server_app.create_app()
    from cruxible_client.transport.lifecycle import DaemonLifecycleClient

    real_init = DaemonLifecycleClient.__init__
    wire: list[bytes] = []

    def in_process(self: DaemonLifecycleClient, **kwargs: Any) -> None:
        real_init(self, **kwargs)
        signer = self._transport._client._client.auth
        in_app = TestClient(app)
        in_app.auth = signer
        real_send = in_app.send

        def recording_send(request: httpx.Request, **send_kwargs: Any) -> httpx.Response:
            response = real_send(request, **send_kwargs)
            wire.append(b"".join(n + b": " + v for n, v in request.headers.raw) + request.content)
            return response

        in_app.send = recording_send  # type: ignore[method-assign]
        self._transport._client = in_app  # type: ignore[assignment]

    monkeypatch.setattr(DaemonLifecycleClient, "__init__", in_process)
    try:
        restart = CliRunner().invoke(
            cli, ["--server-socket", str(socket), "server", "restart", "--no-wait"]
        )
        status = CliRunner().invoke(cli, ["--server-socket", str(socket), "server", "status"])
    finally:
        assert lock is not None
        lock.release()
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
    assert wire and all(b"the-secret" not in sent for sent in wire)


@pytest.mark.parametrize(
    ("recorded", "client_url", "released"),
    [
        ("127.0.0.1:8100", "http://127.0.0.1:8100", True),
        ("::1:8100", "http://[::1]:8100", True),
        # IPv4 and IPv6 loopback can host different listeners on one port, and
        # localhost may resolve to either: no alias counts as the bound endpoint.
        ("127.0.0.1:8100", "http://localhost:8100", False),
        ("127.0.0.1:8100", "http://[::1]:8100", False),
        ("localhost:8100", "http://127.0.0.1:8100", False),
        ("127.0.0.1:8100", "http://127.0.0.1:8101", False),
    ],
)
def test_the_secret_follows_only_the_exact_bound_endpoint(
    tmp_path: Path, recorded: str, client_url: str, released: bool
) -> None:
    state_root, lock = _held_secret(tmp_path, recorded)
    try:
        secret = read_local_bootstrap_secret(state_root, server_url=client_url, server_socket=None)
    finally:
        assert lock is not None
        lock.release()

    assert (secret == "the-secret") is released
