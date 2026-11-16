"""CLI tests for `cruxible server restart`."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_client.errors import CoreError
from cruxible_core.cli.main import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def cli_context_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: object) -> None:
    # Mirrors _get_lifecycle_client: the command passes its --state-root (or None).
    def lifecycle_client(state_root: Path | None = None) -> object:
        return client

    monkeypatch.setattr("cruxible_core.cli.commands.server._get_client", lifecycle_client)


def test_server_restart_requires_server_mode(monkeypatch, runner: CliRunner):
    _patch_client(monkeypatch, None)
    result = runner.invoke(cli, ["server", "restart"])
    assert result.exit_code == 2
    assert "Server mode is required" in result.output


def test_server_restart_waits_for_daemon_and_reports_version(monkeypatch, runner: CliRunner):
    class StubClient:
        def __init__(self) -> None:
            self.restart_calls = 0
            self.version_calls = 0

        def server_restart(self) -> contracts.ServerRestartResult:
            self.restart_calls += 1
            return contracts.ServerRestartResult(
                scheduled=True, version="0.1.5", state_root="/srv/state", boot_id="old-image"
            )

        def daemon_identity(self, *, timeout: float | None = None) -> tuple[str, str | None]:
            self.version_calls += 1
            # First poll fails (image being replaced), second succeeds.
            if self.version_calls < 2:
                raise CoreError("connection refused")
            return "0.1.6", "new-image"

    client = StubClient()
    _patch_client(monkeypatch, client)
    result = runner.invoke(cli, ["--server-url", "http://server", "server", "restart"])

    assert result.exit_code == 0, result.output
    assert client.restart_calls == 1
    assert client.version_calls == 2
    assert "Restart scheduled (was version 0.1.5)." in result.output
    assert "Daemon is back on version 0.1.6." in result.output


def test_server_restart_no_wait_skips_polling(monkeypatch, runner: CliRunner):
    class StubClient:
        def __init__(self) -> None:
            self.version_calls = 0

        def server_restart(self) -> contracts.ServerRestartResult:
            return contracts.ServerRestartResult(
                scheduled=True, version="0.1.5", state_root="/srv/state", boot_id="old-image"
            )

        def daemon_identity(self, *, timeout: float | None = None) -> tuple[str, str | None]:
            self.version_calls += 1
            return "0.1.5", "old-image"

    client = StubClient()
    _patch_client(monkeypatch, client)
    result = runner.invoke(cli, ["--server-url", "http://server", "server", "restart", "--no-wait"])

    assert result.exit_code == 0, result.output
    assert client.version_calls == 0
    assert "Not waiting for the daemon" in result.output


def test_server_restart_json_output(monkeypatch, runner: CliRunner):
    class StubClient:
        def server_restart(self) -> contracts.ServerRestartResult:
            return contracts.ServerRestartResult(
                scheduled=True, version="0.1.5", state_root="/srv/state", boot_id="old-image"
            )

        def daemon_identity(self, *, timeout: float | None = None) -> tuple[str, str | None]:
            return "0.1.6", "new-image"

    _patch_client(monkeypatch, StubClient())
    result = runner.invoke(cli, ["--server-url", "http://server", "server", "restart", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scheduled"] is True
    assert payload["version"] == "0.1.5"
    assert payload["state_root"] == "/srv/state"
    assert payload["waited"] is True
    assert payload["confirmed_version"] == "0.1.6"


def test_server_restart_times_out_when_daemon_never_returns(monkeypatch, runner: CliRunner):
    class StubClient:
        def server_restart(self) -> contracts.ServerRestartResult:
            return contracts.ServerRestartResult(
                scheduled=True, version="0.1.5", state_root="/srv/state", boot_id="old-image"
            )

        def daemon_identity(self, *, timeout: float | None = None) -> tuple[str, str | None]:
            raise CoreError("connection refused")

    _patch_client(monkeypatch, StubClient())
    # Patch sleep so the timeout budget burns down without real waiting.
    monkeypatch.setattr("cruxible_core.cli.commands.server.time.sleep", lambda _s: None)
    result = runner.invoke(
        cli,
        ["--server-url", "http://server", "server", "restart", "--timeout", "0.05"],
    )

    assert result.exit_code != 0
    assert "did not come back" in result.output


def test_server_restart_does_not_confirm_against_the_old_process_image(
    monkeypatch, runner: CliRunner
):
    """The old image answers before it re-execs; only a new boot id confirms."""

    class StubClient:
        def __init__(self) -> None:
            self.polls = 0

        def server_restart(self) -> contracts.ServerRestartResult:
            return contracts.ServerRestartResult(
                scheduled=True, version="0.1.5", state_root="/srv/state", boot_id="old-image"
            )

        def daemon_identity(self, *, timeout: float | None = None) -> tuple[str, str | None]:
            self.polls += 1
            if self.polls < 3:
                return "0.1.5", "old-image"
            return "0.1.6", "new-image"

    client = StubClient()
    _patch_client(monkeypatch, client)
    monkeypatch.setattr("cruxible_core.cli.commands.server.time.sleep", lambda _s: None)
    result = runner.invoke(cli, ["--server-url", "http://server", "server", "restart"])

    assert result.exit_code == 0, result.output
    assert client.polls == 3
    assert "Daemon is back on version 0.1.6." in result.output


def test_server_restart_times_out_while_the_old_image_keeps_answering(
    monkeypatch, runner: CliRunner
):
    class StubClient:
        def server_restart(self) -> contracts.ServerRestartResult:
            return contracts.ServerRestartResult(
                scheduled=True, version="0.1.5", state_root="/srv/state", boot_id="old-image"
            )

        def daemon_identity(self, *, timeout: float | None = None) -> tuple[str, str | None]:
            return "0.1.5", "old-image"

    _patch_client(monkeypatch, StubClient())
    monkeypatch.setattr("cruxible_core.cli.commands.server.time.sleep", lambda _s: None)
    result = runner.invoke(
        cli,
        ["--server-url", "http://server", "server", "restart", "--timeout", "0.05"],
    )

    assert result.exit_code != 0
    assert "old process image is still answering" in result.output


@pytest.mark.parametrize(
    "old_boot_id,new_boot_id",
    [
        (None, "new-image"),
        ("old-image", None),
        (None, None),
        ("", "new-image"),
        ("old-image", " "),
    ],
)
@pytest.mark.parametrize("output_json", [False, True])
def test_restart_never_confirms_without_identifiable_old_and_new_images(
    monkeypatch,
    runner: CliRunner,
    old_boot_id,
    new_boot_id,
    output_json: bool,
):
    class StubClient:
        def server_restart(self) -> contracts.ServerRestartResult:
            return contracts.ServerRestartResult(
                scheduled=True,
                version="0.5.1",
                state_root="/srv/state",
                boot_id=old_boot_id,
            )

        def daemon_identity(self, *, timeout: float | None = None) -> tuple[str, str | None]:
            return "0.5.1", new_boot_id

    _patch_client(monkeypatch, StubClient())
    monkeypatch.setattr("cruxible_core.cli.commands.server.time.sleep", lambda _s: None)
    args = ["--server-url", "http://server", "server", "restart", "--timeout", "0.01"]
    if output_json:
        args.append("--json")
    result = runner.invoke(cli, args)
    assert result.exit_code == 1
    assert "replacement could not be confirmed" in result.output
    assert "boot ID" in result.output
    assert "confirmed_version" not in result.stdout
    assert "Daemon is back" not in result.output


def test_restart_keeps_waiting_after_a_probe_without_a_boot_id(monkeypatch, runner: CliRunner):
    probes = iter([("0.5.1", None), ("0.5.1", "old-image"), ("0.5.1", "new-image")])

    class StubClient:
        def server_restart(self) -> contracts.ServerRestartResult:
            return contracts.ServerRestartResult(
                scheduled=True,
                version="0.5.1",
                state_root="/srv/state",
                boot_id="old-image",
            )

        def daemon_identity(self, *, timeout: float | None = None) -> tuple[str, str | None]:
            return next(probes)

    _patch_client(monkeypatch, StubClient())
    monkeypatch.setattr("cruxible_core.cli.commands.server.time.sleep", lambda _s: None)
    result = runner.invoke(cli, ["--server-url", "http://server", "server", "restart", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["confirmed_version"] == "0.5.1"
    assert list(probes) == []


def test_each_probe_is_bounded_and_a_silent_listener_fails_within_the_timeout(
    monkeypatch, runner: CliRunner
):
    """A listener that accepts but never answers costs one bounded probe, not a hang."""
    import httpx

    from cruxible_client.errors import ServerUnreachableError

    timeouts: list[float | None] = []

    class StubClient:
        def server_restart(self) -> contracts.ServerRestartResult:
            return contracts.ServerRestartResult(
                scheduled=True, version="0.5.1", state_root="/srv/state", boot_id="old-image"
            )

        def daemon_identity(self, *, timeout: float | None = None) -> tuple[str, str | None]:
            timeouts.append(timeout)
            try:
                raise httpx.ReadTimeout("timed out")
            except httpx.ReadTimeout as exc:
                raise ServerUnreachableError("unix:/s.sock", "no response") from exc

    _patch_client(monkeypatch, StubClient())
    monkeypatch.setattr("cruxible_core.cli.commands.server.time.sleep", lambda _s: None)
    result = runner.invoke(
        cli, ["--server-url", "http://server", "server", "restart", "--timeout", "0.05"]
    )

    assert result.exit_code == 1
    assert "did not come back" in result.output
    assert "got no answer" in result.output
    assert timeouts and all(t is not None and 0 < t <= 5.0 for t in timeouts)


def _listener_descriptors(pid: int, socket_path: Path) -> int:
    """How many descriptors of process `pid` are Unix sockets bound to `socket_path`."""
    import subprocess
    import sys

    if sys.platform.startswith("linux"):
        inodes = set()
        with open("/proc/net/unix", encoding="utf-8") as table:
            next(table)
            for row in table:
                fields = row.split()
                if len(fields) >= 8 and fields[7] == str(socket_path):
                    inodes.add(fields[6])
        count = 0
        for entry in Path(f"/proc/{pid}/fd").iterdir():
            try:
                target = os.readlink(entry)
            except OSError:
                continue
            if target.startswith("socket:[") and target[8:-1] in inodes:
                count += 1
        return count
    listing = subprocess.run(
        ["lsof", "-a", "-p", str(pid), "-U", "-Fn"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return sum(1 for line in listing.splitlines() if line == f"n{socket_path}")


def test_restarting_a_real_daemon_twice_returns_and_keeps_one_listener(tmp_path: Path) -> None:
    """`server restart` re-execs in place; the old listener must not survive the exec.

    A listener carried across the exec kept accepting connections nobody served,
    and the waiting client hung on it; each restart added one more descriptor.
    """
    import shutil
    import subprocess
    import sys
    import tempfile
    import time

    if not sys.platform.startswith("linux") and shutil.which("lsof") is None:
        pytest.skip("needs /proc or lsof to count the daemon's descriptors")
    # The daemon re-execs its own argv, so it must be launched as `cruxible`, not `python -c`.
    executable = Path(sys.executable).parent / "cruxible"
    if not executable.is_file():
        pytest.skip("needs the cruxible executable to start a daemon")
    repo = Path(__file__).resolve().parents[2]
    # AF_UNIX paths are short; the pytest tmp_path is not, the default temp dir is.
    socket_dir = Path(tempfile.mkdtemp(prefix="cxr"))
    socket_path = socket_dir / "d.sock"
    state_root = tmp_path / "state"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CRUXIBLE_") and key != "PYTHONPATH"
    }
    env["PYTHONPATH"] = os.pathsep.join(
        (str(repo / "src"), str(repo / "packages/cruxible-client/src"))
    )
    env["CRUXIBLE_STATE_ROOT"] = str(state_root)
    env["CRUXIBLE_CLI_CONTEXT_PATH"] = str(tmp_path / "cli-context.json")
    entry = [sys.executable, "-c", "from cruxible_core.cli.main import cli; cli()"]
    daemon = subprocess.Popen(
        [str(executable), "server", "start", "--socket", str(socket_path)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = [*entry, "--server-socket", str(socket_path), "server"]
    try:
        status = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            assert daemon.poll() is None, "daemon exited during start"
            status = subprocess.run(
                [*client, "status"], env=env, capture_output=True, text=True, timeout=30
            )
            if status.returncode == 0:
                break
            time.sleep(0.2)
        assert status is not None and status.returncode == 0, status and status.stderr
        before = _listener_descriptors(daemon.pid, socket_path)
        assert before >= 1

        counts = []
        for _ in range(2):
            restart = subprocess.run(
                [*client, "restart", "--timeout", "30"],
                env=env,
                capture_output=True,
                text=True,
                timeout=90,
            )
            assert restart.returncode == 0, restart.stdout + restart.stderr
            assert "Daemon is back on version" in restart.stdout
            counts.append(_listener_descriptors(daemon.pid, socket_path))

        assert counts == [before, before]
        stop = subprocess.run([*client, "stop"], env=env, capture_output=True, timeout=60)
        assert stop.returncode == 0
        assert daemon.wait(timeout=30) == 0
    finally:
        if daemon.poll() is None:
            daemon.kill()
            daemon.wait()
        shutil.rmtree(socket_dir, ignore_errors=True)
