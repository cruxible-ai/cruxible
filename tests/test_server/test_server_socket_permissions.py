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


@pytest.mark.parametrize("mode", [0o770, 0o707, 0o1777, 0o755, 0o750])
def test_a_socket_directory_that_is_not_owner_only_is_refused(short_dir: Path, mode: int) -> None:
    directory = short_dir / "shared"
    directory.mkdir()
    os.chmod(directory, mode)

    with pytest.raises(ConfigError, match="is not owner-only") as caught:
        server_app.prepare_socket_directory(directory)

    assert f"chmod 700 {directory}" in str(caught.value)


@pytest.mark.parametrize("mode", [0o777, 0o1777, 0o770])
def test_a_writable_ancestor_that_is_not_sticky_and_root_owned_is_refused(
    short_dir: Path, mode: int
) -> None:
    ancestor = short_dir / "shared"
    directory = ancestor / "run"
    directory.mkdir(parents=True, mode=0o700)
    os.chmod(ancestor, mode)

    with pytest.raises(ConfigError, match="writable by group or others"):
        server_app.prepare_socket_directory(directory)


def test_a_symlinked_socket_directory_is_refused(short_dir: Path) -> None:
    real = short_dir / "real"
    real.mkdir(mode=0o700)
    link = short_dir / "link"
    link.symlink_to(real)

    with pytest.raises(ConfigError, match="not a directory"):
        server_app.prepare_socket_directory(link)


def test_a_chain_through_an_open_directory_via_an_intermediate_symlink_is_refused(
    short_dir: Path,
) -> None:
    # private/hop -> ../open/relay -> ../real: neither the path as given nor its
    # final resolution names `open`, but the lookup walks through it, and
    # anyone can replace `relay` there.
    open_dir = short_dir / "open"
    open_dir.mkdir()
    os.chmod(open_dir, 0o777)
    real = short_dir / "real"
    (real / "run").mkdir(parents=True, mode=0o700)
    os.chmod(real, 0o700)
    (open_dir / "relay").symlink_to(Path("..") / "real")
    private = short_dir / "private"
    private.mkdir(mode=0o700)
    (private / "hop").symlink_to(Path("..") / "open" / "relay")
    directory = private / "hop" / "run"
    assert directory.resolve() == (real / "run").resolve()

    with pytest.raises(ConfigError, match="open is writable by group or others"):
        server_app.prepare_socket_directory(directory)


def test_a_socket_under_the_system_temp_root_still_passes() -> None:
    # The sticky root-owned temp root; on macOS it is reached through a
    # root-owned symlink into the root-owned private directory.
    temp_root = Path(os.sep) / "tmp"
    status = os.stat(temp_root)
    if not (status.st_mode & stat.S_ISVTX and status.st_uid == 0):
        pytest.skip("this host's temp root is not sticky and root-owned")
    directory = Path(tempfile.mkdtemp(prefix="cxs", dir=temp_root))
    try:
        server_app.prepare_socket_directory(directory / "run")
        sock = server_app.bind_private_unix_socket(directory / "run" / "d.sock")
        sock.close()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_a_private_existing_directory_is_accepted(short_dir: Path) -> None:
    os.chmod(short_dir, 0o700)

    server_app.prepare_socket_directory(short_dir)


def test_a_directory_swapped_between_validation_and_bind_is_refused(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = short_dir / "run"
    socket_file = directory / "d.sock"
    real_bind = server_app._bind_socket_path

    def swap_then_bind(sock: object, path: str) -> None:
        # Another writer replaces the validated directory with an open one.
        directory.rename(short_dir / "validated")
        directory.mkdir()
        os.chmod(directory, 0o777)
        real_bind(sock, path)  # type: ignore[arg-type]

    monkeypatch.setattr(server_app, "_bind_socket_path", swap_then_bind)

    with pytest.raises(ConfigError, match="changed while the socket was being bound"):
        server_app.bind_private_unix_socket(socket_file)

    assert not os.path.lexists(socket_file)
    assert not os.path.lexists(short_dir / "validated" / "d.sock")


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
        with pytest.raises(ConfigError, match="is not owner-only"):
            server_app.run_server(socket_path=str(short_dir / "d.sock"))
    finally:
        reset_runtime_credential_store()
        reset_registry()

    assert called == []
