"""A refused workspace blocks only the commands that read the folder binding.

Workspace discovery runs at the CLI root, but a refusal there (a ``.cruxible``
or Git marker in a folder another user owns) is held back: a command that
reads the folder binding refuses with it before its callback, and a command
listed in ``WORKSPACE_INDEPENDENT_COMMANDS`` (a state-root admin command, the
remembered-context file, a local template) runs. Such a command sees no
workspace and no remembered context, so a remembered instance never stands in
for the refused one. No daemon is started here.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from cruxible_client.contracts import workspace_root as workspace_root_contract
from cruxible_core.cli.commands import context as context_commands
from cruxible_core.cli.commands._common import _root_ctx_obj
from cruxible_core.cli.main import (
    CLI_COMMANDS,
    WORKSPACE_INDEPENDENT_COMMANDS,
    LazyCommandSpec,
    cli,
    handle_errors,
)

BOUNDARY = "cruxible.workspace.boundary_untrusted"


@pytest.fixture
def other_owned_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A bound ``.cruxible`` folder another user owns, with a remembered local instance."""

    for name in (
        "CRUXIBLE_SERVER_URL",
        "CRUXIBLE_SERVER_SOCKET",
        "CRUXIBLE_SERVER_BEARER_TOKEN",
        "CRUXIBLE_PRINCIPAL_ID",
        "CRUXIBLE_PRINCIPAL_KEY",
        "CRUXIBLE_INSTANCE_ID",
        "CRUXIBLE_WORKSPACE",
        "CRUXIBLE_NO_WORKSPACE",
        "CRUXIBLE_REQUIRE_SERVER",
        "GIT_DIR",
        "GIT_WORK_TREE",
    ):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("CRUXIBLE_NO_AUTOSTART", "1")
    context_path = tmp_path / "context.json"
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(context_path))
    remembered_socket = (tmp_path / "remembered.sock").resolve()
    context_path.write_text(
        json.dumps(
            {
                "server_socket": str(remembered_socket),
                "instance_id": "inst_remembered",
                "instance_transport": f"unix://{remembered_socket}",
            }
        ),
        encoding="utf-8",
    )
    folder = (tmp_path / "outside" / "B").resolve()
    (folder / ".cruxible").mkdir(parents=True)
    (folder / ".cruxible" / "coverage.json").write_text(
        json.dumps(
            {
                "tag": "playbill-coverage-workspace-config-v2",
                "instance_id": "inst_bound",
                "server_url": "https://inst-bound.example.test",
            }
        ),
        encoding="utf-8",
    )
    # Model a root-owned folder (or a root-run command in the user's checkout).
    monkeypatch.setattr(
        workspace_root_contract, "_owned_by_user", lambda path, _status: path != folder
    )
    monkeypatch.chdir(folder)
    yield folder


def _invoke(*args: str) -> Any:
    return CliRunner().invoke(cli, list(args))


def test_workspace_independent_commands_run_from_an_other_owned_folder(
    other_owned_folder: Path,
) -> None:
    listed = _invoke("authoring", "example")
    assert listed.exit_code == 0, listed.output
    assert BOUNDARY not in listed.output

    schema = _invoke("write", "--schema")
    assert schema.exit_code == 0, schema.output
    assert json.loads(schema.stdout)

    # A daemon lifecycle change on a daemon named explicitly passes the gate (and
    # then fails for want of a daemon there, not for the folder).
    stopped = _invoke("--server-socket", str(other_owned_folder / "none.sock"), "server", "stop")
    assert BOUNDARY not in stopped.output

    cleared = _invoke("context", "clear")
    assert cleared.exit_code == 0, cleared.output


def test_workspace_dependent_commands_still_refuse_there(other_owned_folder: Path) -> None:
    for args in (
        ("whoami", "--json"),
        ("context", "show", "--json"),
        ("write", "-"),
        ("server", "stop"),
        ("console", "--no-open"),
    ):
        result = _invoke(*args)
        assert result.exit_code == 2, (args, result.output)
        assert BOUNDARY in result.output, (args, result.output)
        assert "CRUXIBLE_WORKSPACE=" in result.output
        assert "inst_remembered" not in result.output
        assert "inst_bound" not in result.stdout


def test_help_never_needs_the_workspace(other_owned_folder: Path) -> None:
    for args in (("whoami", "--help"), ("credential", "mint", "--help"), ("--version",)):
        result = _invoke(*args)
        assert result.exit_code == 0, (args, result.output)
        assert BOUNDARY not in result.output


def test_a_refused_folder_leaves_no_remembered_or_bound_target(
    other_owned_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def capture() -> None:
        seen.update(_root_ctx_obj())

    monkeypatch.setattr(context_commands, "_clear_persisted_cli_context", capture)

    result = _invoke("context", "clear")

    assert result.exit_code == 0, result.output
    assert BOUNDARY in str(seen["workspace_refusal"])
    assert seen["instance_id"] is None
    assert seen["target_instance_source"] == "local"
    assert seen["server_url"] is None
    assert seen["server_socket"] is None
    assert seen["workspace_binding_path"] is None
    assert seen["no_workspace"] is True


def test_a_trusted_folder_holds_nothing_back(
    other_owned_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_root_contract, "_owned_by_user", lambda _path, _status: True)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        context_commands, "_clear_persisted_cli_context", lambda: seen.update(_root_ctx_obj())
    )

    assert _invoke("context", "clear").exit_code == 0
    assert seen["workspace_refusal"] is None
    assert (seen["instance_id"], seen["target_instance_source"]) == ("inst_bound", "workspace")


# -- guardrail: the held-back refusal is raised for every command -------------


def _leaves(
    specs: dict[str, LazyCommandSpec], prefix: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], click.Command]]:
    for name, spec in specs.items():
        if spec.commands is None:
            module = importlib.import_module(str(spec.module))
            yield (*prefix, name), getattr(module, str(spec.attr))
        else:
            yield from _leaves(spec.commands, (*prefix, name))


def _runs_handle_errors(callback: Any) -> bool:
    gate = handle_errors(lambda: None).__code__
    current = callback
    for _ in range(20):
        if current is None:
            return False
        if getattr(current, "__code__", None) is gate:
            return True
        current = getattr(current, "__wrapped__", None)
    return False


def test_every_command_but_mcp_passes_the_workspace_gate() -> None:
    """``handle_errors`` raises the held-back refusal, so every command must run it."""

    leaves = dict(_leaves(CLI_COMMANDS))
    missing = sorted(
        " ".join(path)
        for path, command in leaves.items()
        if path != ("mcp",) and not _runs_handle_errors(command.callback)
    )
    assert missing == []
    assert set(WORKSPACE_INDEPENDENT_COMMANDS) <= set(leaves)
