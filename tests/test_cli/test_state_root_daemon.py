"""The CLI finds the daemon at the state root's default socket, as the MCP adapter does.

With no transport named (no --server-socket/--server-url, no CRUXIBLE_SERVER_*,
no remembered context), a daemon answering at ``<CRUXIBLE_STATE_ROOT>/run/daemon.sock``
is the target, so ``init`` and ``server stop`` work in server mode without
flags. With none answering, ``init`` says what server mode is and how to get it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from cruxible_core.cli.main import cli
from cruxible_core.mcp import daemon


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in (
        "CRUXIBLE_SERVER_URL",
        "CRUXIBLE_SERVER_SOCKET",
        "CRUXIBLE_SERVER_BEARER_TOKEN",
        "CRUXIBLE_WORKSPACE",
        "CRUXIBLE_PRINCIPAL_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".config"))
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    return tmp_path / "state" / "run" / "daemon.sock"


def test_init_with_no_daemon_explains_server_mode(isolated: Path) -> None:
    result = CliRunner().invoke(cli, ["--no-workspace", "init", "--principal-id", "owner"])
    assert result.exit_code == 2
    assert "cruxible init needs server mode" in result.output
    assert "running Cruxible daemon" in result.output
    assert str(isolated) in result.output
    assert f"cruxible server start --socket {isolated}" in result.output


def test_a_daemon_answering_at_the_state_root_is_the_target(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated.parent.mkdir(parents=True)
    isolated.touch()
    probed: list[str | None] = []

    def answers(*, server_url: str | None = None, server_socket: str | None = None) -> bool:
        probed.append(server_socket)
        return True

    monkeypatch.setattr(daemon, "_answers", answers)
    assert daemon.answering_default_socket() == str(isolated)

    result = CliRunner().invoke(cli, ["--no-workspace", "init", "--principal-id", "owner"])
    # The stand-in socket answers nothing real; the command got as far as the daemon.
    assert "needs server mode" not in result.output
    assert f"unix://{isolated.resolve()}" in result.output
    assert "transport=state_root" in result.output
    assert str(isolated) in probed


def test_no_answer_at_the_default_socket_is_no_target(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated.parent.mkdir(parents=True)
    isolated.touch()
    monkeypatch.setattr(daemon, "_answers", lambda **_kwargs: False)
    assert daemon.answering_default_socket() is None
