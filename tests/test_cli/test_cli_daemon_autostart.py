"""The CLI starts the local daemon when a command needs one and none answers.

Only the default local socket (``<state root>/run/daemon.sock``) is started,
through the MCP adapter's own `ensure_local_daemon`, and one line on standard
error says so. A daemon already answering there is reused; ``--no-autostart``
or ``CRUXIBLE_NO_AUTOSTART`` opts out; an explicit ``--server-url`` or a socket
that is not the default keeps today's refusal; diagnostics and lifecycle
commands never start one. The real start (and its stop) is driven end to end
in ``test_first_run_workspace.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from cruxible_core.cli.main import cli
from cruxible_core.mcp import daemon
from cruxible_core.mcp.daemon import DaemonTarget, default_socket_path


class _Starts:
    """A stand-in for `ensure_local_daemon` that records each start."""

    def __init__(self) -> None:
        self.calls: list[Path] = []

    def __call__(self, state_root: Path, **_: object) -> DaemonTarget:
        self.calls.append(state_root)
        return DaemonTarget(None, str(default_socket_path(state_root)), "started")


@pytest.fixture
def starts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Starts:
    monkeypatch.delenv("CRUXIBLE_NO_AUTOSTART", raising=False)
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    recorder = _Starts()
    monkeypatch.setattr(daemon, "ensure_local_daemon", recorder)
    monkeypatch.setattr(daemon, "_answers", lambda **_kwargs: False)
    return recorder


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(cli, ["--no-workspace", *args])


def _socket(tmp_path: Path) -> Path:
    return default_socket_path(tmp_path / "state")


def test_a_command_that_needs_a_daemon_starts_one_on_the_default_socket(
    starts: _Starts, tmp_path: Path
) -> None:
    result = _invoke("host", "show", "inst_x")

    assert starts.calls == [(tmp_path / "state").resolve()]
    assert f"Started a Cruxible daemon on unix socket {_socket(tmp_path)}" in result.stderr
    assert "`cruxible server stop` stops it" in result.stderr
    # The stand-in started nothing real, so the command got as far as the daemon.
    assert "needs server mode" not in result.output


def test_an_explicit_default_socket_that_does_not_answer_is_started_too(
    starts: _Starts, tmp_path: Path
) -> None:
    _invoke("--server-socket", str(_socket(tmp_path)), "host", "show", "inst_x")

    assert len(starts.calls) == 1


def test_a_daemon_answering_on_the_default_socket_is_reused(
    starts: _Starts, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket = _socket(tmp_path)
    socket.parent.mkdir(parents=True)
    socket.touch()
    monkeypatch.setattr(daemon, "_answers", lambda **_kwargs: True)

    result = _invoke("host", "show", "inst_x")

    assert starts.calls == []
    assert "Started a Cruxible daemon" not in result.stderr
    assert "transport=state_root" in result.stderr


@pytest.mark.parametrize("opt_out", ["flag", "environment"])
def test_no_autostart_opts_out_and_keeps_the_refusal(
    starts: _Starts, monkeypatch: pytest.MonkeyPatch, opt_out: str
) -> None:
    if opt_out == "environment":
        monkeypatch.setenv("CRUXIBLE_NO_AUTOSTART", "1")
        args: tuple[str, ...] = ()
    else:
        args = ("--no-autostart",)

    result = _invoke(*args, "init", "--principal-id", "owner")

    assert starts.calls == []
    assert result.exit_code == 2
    assert "cruxible init needs server mode" in result.output
    assert "--no-autostart" in result.output


@pytest.mark.parametrize(
    "target",
    [
        ("--server-url", "http://127.0.0.1:9"),
        ("--server-socket", "elsewhere.sock"),
    ],
)
def test_an_explicit_other_target_is_never_replaced_by_a_started_daemon(
    starts: _Starts, tmp_path: Path, target: tuple[str, str]
) -> None:
    flag, value = target
    if flag == "--server-socket":
        value = str(tmp_path / value)

    result = _invoke(flag, value, "host", "show", "inst_x")

    assert starts.calls == []
    assert result.exit_code != 0
    assert "Started a Cruxible daemon" not in result.stderr


def test_diagnostics_and_lifecycle_commands_never_start_a_daemon(
    starts: _Starts, tmp_path: Path
) -> None:
    shown = _invoke("--instance-id", "inst_x", "context", "show", "--json")
    stopped = _invoke("server", "stop")
    status = _invoke("server", "status")

    assert starts.calls == []
    assert shown.exit_code == 0, shown.output
    for result in (stopped, status):
        assert "Started a Cruxible daemon" not in result.stderr


def test_a_start_that_fails_is_a_refusal_naming_the_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CRUXIBLE_NO_AUTOSTART", raising=False)
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(daemon, "_answers", lambda **_kwargs: False)

    def fails(state_root: Path, **_: object) -> DaemonTarget:
        raise daemon.DaemonUnavailableError(f"could not start under {state_root}")

    monkeypatch.setattr(daemon, "ensure_local_daemon", fails)

    result = _invoke("host", "show", "inst_x")

    assert result.exit_code == 2
    assert "cruxible.mcp.daemon_unavailable" in result.output
