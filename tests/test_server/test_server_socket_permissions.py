"""The daemon's Unix socket is owner-only, in a directory others cannot write."""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_core.errors import ConfigError
from cruxible_core.server import app as server_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import reset_registry


@pytest.fixture
def short_dir() -> Iterator[Path]:
    # AF_UNIX paths are capped near 104 bytes; pytest's tmp_path can exceed it.
    path = Path(tempfile.mkdtemp(prefix="cxs"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_a_missing_socket_directory_is_created_owner_only(short_dir: Path) -> None:
    directory = short_dir / "run"

    server_app.prepare_socket_directory(directory)

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.parametrize("mode", [0o770, 0o707, 0o1777])
def test_a_group_or_other_writable_socket_directory_is_refused(short_dir: Path, mode: int) -> None:
    directory = short_dir / "shared"
    directory.mkdir()
    os.chmod(directory, mode)

    with pytest.raises(ConfigError, match="writable by group or others") as caught:
        server_app.prepare_socket_directory(directory)

    assert f"chmod go-w {directory}" in str(caught.value)


def test_a_private_existing_directory_is_accepted(short_dir: Path) -> None:
    os.chmod(short_dir, 0o755)

    server_app.prepare_socket_directory(short_dir)


def test_the_socket_is_bound_owner_only(short_dir: Path) -> None:
    socket_file = short_dir / "d.sock"
    socket_file.write_text("stale")

    sock = server_app.bind_private_unix_socket(socket_file)
    try:
        mode = socket_file.stat().st_mode
        assert stat.S_ISSOCK(mode)
        assert stat.S_IMODE(mode) == 0o600
    finally:
        sock.close()


def test_run_server_serves_the_owner_only_socket_by_descriptor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, short_dir: Path
) -> None:
    socket_file = short_dir / "run" / "d.sock"
    seen: dict[str, object] = {}

    def capture_run(*_args: object, **kwargs: object) -> None:
        seen.update(kwargs)
        seen["mode"] = stat.S_IMODE(socket_file.stat().st_mode)
        seen["dir_mode"] = stat.S_IMODE(socket_file.parent.stat().st_mode)

    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=capture_run))
    reset_runtime_credential_store()
    reset_registry()
    try:
        server_app.run_server(socket_path=str(socket_file))
    finally:
        reset_runtime_credential_store()
        reset_registry()

    assert "uds" not in seen
    assert isinstance(seen["fd"], int)
    assert seen["mode"] == 0o600
    assert seen["dir_mode"] == 0o700
    assert not socket_file.exists()


def test_run_server_refuses_a_shared_socket_directory_before_uvicorn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, short_dir: Path
) -> None:
    os.chmod(short_dir, 0o777)
    called: list[object] = []
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.setitem(
        sys.modules, "uvicorn", SimpleNamespace(run=lambda *a, **k: called.append(k))
    )
    reset_runtime_credential_store()
    reset_registry()
    try:
        with pytest.raises(ConfigError, match="writable by group or others"):
            server_app.run_server(socket_path=str(short_dir / "d.sock"))
    finally:
        reset_runtime_credential_store()
        reset_registry()

    assert called == []
