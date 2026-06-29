"""Smoke coverage for every registered CLI command path."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from cruxible_core.cli.main import cli


def _walk_cli_commands(command, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    rows: list[tuple[str, ...]] = []
    if hasattr(command, "commands"):
        for name, subcommand in sorted(command.commands.items()):
            path = prefix + (name,)
            rows.append(path)
            rows.extend(_walk_cli_commands(subcommand, path))
    return rows


@pytest.mark.parametrize("command_path", _walk_cli_commands(cli), ids=lambda p: " ".join(p))
def test_every_cli_command_path_has_help(command_path: tuple[str, ...]) -> None:
    result = CliRunner().invoke(cli, [*command_path, "--help"])

    assert result.exit_code == 0
    assert "Usage:" in result.output
