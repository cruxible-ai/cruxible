"""First run from a bare folder: the folder is the workspace, Git optional.

The shared resolver (`cruxible_core.workspace_root`) picks the nearest folder
holding ``.cruxible/``, else the Git worktree, else the current directory, and
refuses the home directory, a filesystem root, the state root and anything
inside a ``.cruxible`` directory.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from cruxible_client.contracts.workspace_layout import WorkspaceDirectoryConflict
from cruxible_core.cli.commands.playbill import _explicit_workspace_root
from cruxible_core.cli.main import cli
from cruxible_core.mcp.workspace import mcp_workspace_root
from cruxible_core.workspace_root import resolve_workspace_root

# -- the resolver -------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "state"))
    return home.resolve()


def _found(start: Path) -> tuple[Path, str]:
    resolved = resolve_workspace_root(start)
    assert resolved is not None
    return resolved.root, resolved.source


def test_a_plain_folder_is_its_own_workspace_until_one_holds_cruxible(home: Path) -> None:
    folder = home / "project"
    sub = folder / "src" / "pkg"
    sub.mkdir(parents=True)

    assert _found(folder) == (folder, "directory")
    assert _found(sub) == (sub, "directory")
    assert resolve_workspace_root(sub, fallback_to_start=False) is None

    (folder / ".cruxible").mkdir()
    # The nearest ancestor holding .cruxible/ wins from any depth.
    assert _found(sub) == (folder, "cruxible_directory")
    assert _explicit_workspace_root(str(sub)) == folder


def test_a_git_worktree_is_found_and_a_nearer_cruxible_folder_wins(home: Path) -> None:
    repo = home / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    deep = repo / "a" / "b"
    deep.mkdir(parents=True)
    assert _found(deep) == (repo, "git")
    resolved = resolve_workspace_root(deep)
    assert resolved is not None and resolved.is_git

    (repo / "a" / ".cruxible").mkdir()
    assert _found(deep) == (repo / "a", "cruxible_directory")


def test_home_state_root_and_control_directories_are_refused(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(WorkspaceDirectoryConflict) as refused:
        resolve_workspace_root(home)
    assert refused.value.reason == "home"
    with pytest.raises(WorkspaceDirectoryConflict) as refused:
        resolve_workspace_root(Path("/"))
    assert refused.value.reason == "filesystem_root"

    # The home directory's .cruxible is the state root, never a workspace: a
    # project under home with none of its own is its own folder.
    (home / ".cruxible").mkdir()
    project = home / "project"
    project.mkdir()
    assert _found(project) == (project, "directory")

    state_root = tmp_path / "state"
    inside = state_root / "instances" / "inst_x"
    inside.mkdir(parents=True)
    with pytest.raises(WorkspaceDirectoryConflict) as refused:
        resolve_workspace_root(inside)
    assert refused.value.reason == "state_root"

    # Inside a workspace's own .cruxible the folder holding it is selected; a
    # .cruxible nested inside another .cruxible is refused.
    floor = project / ".cruxible" / "floor"
    floor.mkdir(parents=True)
    assert _found(floor) == (project, "cruxible_directory")
    (floor / ".cruxible").mkdir()
    with pytest.raises(WorkspaceDirectoryConflict) as refused:
        resolve_workspace_root(floor)
    assert refused.value.reason == "control_directory"

    # The MCP adapter resolves its implicit root the same way.
    monkeypatch.delenv("CRUXIBLE_MCP_WORKSPACE_ROOT", raising=False)
    nested = project / "docs"
    nested.mkdir()
    monkeypatch.chdir(nested)
    assert mcp_workspace_root({}) == project


def test_cli_commands_that_need_a_workspace_name_why_home_is_refused(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(home)
    for command in (
        ["--server-socket", str(home.parent / "d.sock"), "workspace", "attach"],
        ["files", "add", "x", "notes.md"],
    ):
        result = CliRunner().invoke(cli, command)
        assert result.exit_code == 2, result.output
        assert "needs a workspace folder" in result.output
        assert "project folder" in result.output
