"""Shared isolation fixtures for Cruxible MCP tests."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from cruxible_core.mcp import handlers
from cruxible_core.mcp.daemon import _cruxible_executable
from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import reset_registry
from tests.support.short_temporary_root import short_temporary_directory


@pytest.fixture(autouse=True)
def reset_mcp_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    for name in (
        "CRUXIBLE_MODE",
        "CRUXIBLE_REQUIRE_SERVER",
        "CRUXIBLE_SERVER_URL",
        "CRUXIBLE_SERVER_SOCKET",
        "CRUXIBLE_SERVER_BEARER_TOKEN",
        "CRUXIBLE_SERVER_TOKEN",
        "CRUXIBLE_SERVER_AUTH",
        "CRUXIBLE_MCP_PROFILE",
        "CRUXIBLE_MCP_TOOLS",
        "CRUXIBLE_MCP_TOOL_ALLOWLIST",
        "CRUXIBLE_INSTANCE_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / ".server-state"))
    reset_client_cache()
    reset_permissions()
    reset_registry()
    # The credential store caches the state root it first saw; a store left by an
    # earlier test points at that test's deleted temporary directory.
    reset_runtime_credential_store()
    get_playbill_manager().clear()
    yield
    get_playbill_manager().clear()
    reset_runtime_credential_store()
    reset_registry()
    reset_permissions()
    reset_client_cache()


@pytest.fixture
def daemon_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Path]]:
    """One real daemon on a scratch socket at the state root's default path.

    Started and stopped here; ``CRUXIBLE_NO_AUTOSTART=1`` so the CLI starts
    nothing else. ``folder`` is the working directory; ``other`` a second project.
    """

    executable = _cruxible_executable()
    if executable is None:
        pytest.skip("no cruxible executable beside this interpreter to start a daemon")
    scratch = short_temporary_directory("cxi").resolve()
    base = tmp_path.resolve()
    paths = {
        "scratch": base,
        "state": scratch / "s",
        "socket": scratch / "s" / "run" / "daemon.sock",
        "home": base / "h",
        "folder": base / "h" / "project",
        "other": base / "h" / "other",
        "config": base / "config",
    }
    paths["folder"].mkdir(parents=True)
    paths["other"].mkdir(parents=True)
    for name in (
        "CRUXIBLE_PRINCIPAL_ID",
        "CRUXIBLE_PRINCIPAL_KEY",
        "CRUXIBLE_MCP_KEY_DIR",
        "CRUXIBLE_SERVER_BEARER_TOKEN",
        "CRUXIBLE_SERVER_URL",
        "CRUXIBLE_SERVER_SOCKET",
        "CRUXIBLE_INSTANCE_ID",
        "CRUXIBLE_WORKSPACE",
        "CRUXIBLE_NO_WORKSPACE",
        "CRUXIBLE_MCP_WORKSPACE_ROOT",
        "CRUXIBLE_SERVER_AUTH",
        "CRUXIBLE_MODE",
        "GIT_DIR",
        "GIT_WORK_TREE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CRUXIBLE_NO_AUTOSTART", "1")
    monkeypatch.setenv("HOME", str(paths["home"]))
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(paths["state"]))
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(base / "context.json"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(paths["config"]))
    pythonpath = os.pathsep.join(
        str(Path(item).resolve())
        for item in os.environ.get("PYTHONPATH", "").split(os.pathsep)
        if item
    )
    monkeypatch.setenv("PYTHONPATH", pythonpath)
    paths["state"].mkdir(mode=0o700, parents=True)
    log = open(base / "daemon.out", "ab")
    process = subprocess.Popen(
        [
            executable,
            "server",
            "start",
            "--state-root",
            str(paths["state"]),
            "--socket",
            str(paths["socket"]),
        ],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    try:
        _wait_for_socket(paths["socket"], process, base / "daemon.out")
        handlers.reset_client_cache()
        monkeypatch.chdir(paths["folder"])
        yield paths
    finally:
        handlers.reset_client_cache()
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()
        shutil.rmtree(scratch, ignore_errors=True)
        shutil.rmtree(base, ignore_errors=True)


def _wait_for_socket(socket: Path, process: subprocess.Popen[bytes], log: Path) -> None:
    import httpx

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"daemon exited: {log.read_text(encoding='utf-8', errors='replace')}")
        if socket.exists():
            try:
                with httpx.Client(
                    base_url="http://cruxible", transport=httpx.HTTPTransport(uds=str(socket))
                ) as probe:
                    if probe.get("/version").status_code == 200:
                        return
            except httpx.HTTPError:
                pass
        time.sleep(0.1)
    pytest.fail("daemon did not answer within 30s")
