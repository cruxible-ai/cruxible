"""CLI tests for `cruxible server start` (the daemon launch verb).

These pin the fix for wi-server-cli-verb-consistency: the old `cruxible-server`
console-script ignored argv, so `cruxible-server --help` started *serving*
instead of printing help. `cruxible server start` is real Click subcommand, so
`--help` must print help and exit, and the flags must reach the daemon launcher.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from cruxible_core.cli.main import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def cli_context_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))


def test_start_help_prints_and_exits_without_serving(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """Regression for the argv-ignored launcher: --help must NOT start the server.

    The original ``cruxible-server`` entry point called ``uvicorn.run`` without
    parsing argv, so ``cruxible-server --help`` hung serving. As a real Click
    subcommand, ``cruxible server start --help`` prints help and exits 0 *before*
    the callback runs. We trip a sentinel if the launcher is ever reached so the
    regression cannot silently come back.
    """

    def _boom(**_kwargs: object) -> None:
        raise AssertionError("run_server must not be called for --help")

    monkeypatch.setattr("cruxible_core.server.app.run_server", _boom)

    result = runner.invoke(cli, ["server", "start", "--help"])

    assert result.exit_code == 0, result.output
    # CliRunner invokes the root group as "cli"; the path after it is what matters.
    assert "server start [OPTIONS]" in result.output
    assert "Launch the Cruxible daemon" in result.output


def test_start_passes_flags_to_run_server(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """Flags map to run_server kwargs (env defaults are applied inside run_server)."""
    captured: dict[str, object] = {}

    def _capture(**kwargs: object) -> None:
        captured.update(kwargs)

    # The command does `from cruxible_core.server.app import run_server` lazily,
    # so patch the attribute on that module.
    monkeypatch.setattr("cruxible_core.server.app.run_server", _capture)

    result = runner.invoke(
        cli,
        [
            "server",
            "start",
            "--host",
            "0.0.0.0",
            "--port",
            "8137",
            "--state-dir",
            "/var/lib/cruxible/server",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured == {
        "host": "0.0.0.0",
        "port": 8137,
        "state_dir": "/var/lib/cruxible/server",
        "socket_path": None,
    }


def test_start_defaults_are_none_so_env_wins(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """With no flags, run_server receives all-None (env/defaults resolved inside)."""
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "cruxible_core.server.app.run_server", lambda **kwargs: captured.update(kwargs)
    )

    result = runner.invoke(cli, ["server", "start"])

    assert result.exit_code == 0, result.output
    assert captured == {"host": None, "port": None, "state_dir": None, "socket_path": None}
