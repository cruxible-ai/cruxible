"""Every workspace path refuses the home directory and a 0.3 instance, through public entries.

``.cruxible/`` is the workspace directory, but the home directory's
``.cruxible`` is the daemon state root and a 0.3-initialized project keeps its
instance there. Selection, reads, writes and daemon create/attach refuse such a
root with ``WorkspaceDirectoryConflict`` before they read a binding or write a
file; the home check compares directory identity, so a differently cased
spelling of home refuses too.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from cruxible_client.authoring.context import resolve_context
from cruxible_client.authoring.projection_manifests import retain_local_manifests
from cruxible_client.authoring.selectors import WorkspaceSources
from cruxible_client.authoring.workspace import observe_next_workspace, write_workspace_config
from cruxible_client.contracts.workspace_layout import (
    WorkspaceDirectoryConflict,
    same_directory,
)
from cruxible_core.cli.context import CliContextState, save_cli_context
from cruxible_core.cli.main import cli
from cruxible_core.coverage.middleware import load_coverage_config
from cruxible_core.mcp.workspace import (
    MCP_WORKSPACE_ROOT_ENV,
    mcp_git_workspace_root,
    mcp_workspace_root,
    optional_mcp_git_workspace_root,
    resolve_workspace_path,
)
from cruxible_core.runtime import host_api
from cruxible_core.server.registry import get_registry
from tests.test_server.test_playbill_host import host_client  # noqa: F401

LEGACY = ("instance.json", "state.db")


def _legacy_worktree(root: Path, name: str) -> Path:
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    (root / ".cruxible").mkdir()
    (root / ".cruxible" / name).write_text("{}", encoding="utf-8")
    return root


def _git_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "Home"
    subprocess.run(["git", "init", "-b", "main", str(home)], check=True, capture_output=True)
    (home / ".cruxible").mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home.resolve()


def _conflicted_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Path]]:
    """Each root that is no workspace: both 0.3 sentinels, home and a spelling of home."""

    home = _git_home(tmp_path, monkeypatch)
    roots = [(name, _legacy_worktree(tmp_path / f"project-{name}", name)) for name in LEGACY]
    return [*roots, ("home", home), ("home-alias", _home_alias(home))]


def _home_alias(home: Path) -> Path:
    """Another spelling of ``home``: a case variant where the filesystem allows one."""

    alias = home.parent / home.name.swapcase()
    return alias if alias.exists() else home


@pytest.mark.parametrize("name", LEGACY)
def test_selection_reads_and_writes_refuse_a_0_3_instance(tmp_path: Path, name: str) -> None:
    root = _legacy_worktree(tmp_path / "project", name)
    before = sorted(path.name for path in (root / ".cruxible").iterdir())

    with pytest.raises(WorkspaceDirectoryConflict):
        resolve_context(workspace=root, environ={}, cwd=root, home=tmp_path)
    with pytest.raises(WorkspaceDirectoryConflict):
        resolve_context(environ={"CRUXIBLE_WORKSPACE": str(root)}, cwd=root, home=tmp_path)
    with pytest.raises(WorkspaceDirectoryConflict):
        retain_local_manifests(root, {"sha256:" + "0" * 64: b"x"})
    with pytest.raises(WorkspaceDirectoryConflict):
        WorkspaceSources(root)
    with pytest.raises(WorkspaceDirectoryConflict):
        write_workspace_config(root, instance_id="inst", server_socket=str(tmp_path / "s.sock"))
    with pytest.raises(WorkspaceDirectoryConflict):
        load_coverage_config(root)
    # Nothing was created beside the 0.3 instance's files.
    assert sorted(path.name for path in (root / ".cruxible").iterdir()) == before


def test_a_cased_spelling_of_home_is_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "Home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    alias = _home_alias(home)
    assert same_directory(alias, home)

    with pytest.raises(WorkspaceDirectoryConflict, match="daemon state root"):
        resolve_context(workspace=alias, environ={}, cwd=alias, home=home)
    with pytest.raises(WorkspaceDirectoryConflict):
        write_workspace_config(alias, instance_id="inst", server_socket=str(tmp_path / "s.sock"))
    with pytest.raises(WorkspaceDirectoryConflict):
        retain_local_manifests(alias, {"sha256:" + "0" * 64: b"x"})
    assert not (home / ".cruxible").exists()


@pytest.mark.parametrize("name", LEGACY)
def test_daemon_create_and_attach_refuse_before_registering(
    host_client: object,  # noqa: F811
    tmp_path: Path,
    name: str,
) -> None:
    root = _legacy_worktree(tmp_path / "project", name)

    with pytest.raises(WorkspaceDirectoryConflict):
        host_api.create_playbill_host(
            instance_id="inst_guarded_create",
            workspace_root=str(root),
            workspace_attachment_authorized=True,
        )
    assert get_registry().get("inst_guarded_create") is None

    host_api.create_playbill_host(instance_id="inst_guarded_attach")
    with pytest.raises(WorkspaceDirectoryConflict):
        host_api.attach_workspace("inst_guarded_attach", str(root))
    record = get_registry().get("inst_guarded_attach")
    assert record is not None and record.workspace_root is None


def test_daemon_attach_refuses_the_home_directory(
    host_client: object,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "Home"
    subprocess.run(["git", "init", "-b", "main", str(home)], check=True, capture_output=True)
    monkeypatch.setenv("HOME", str(home))
    host_api.create_playbill_host(instance_id="inst_guarded_home")

    with pytest.raises(WorkspaceDirectoryConflict):
        host_api.attach_workspace("inst_guarded_home", str(_home_alias(home)))
    record = get_registry().get("inst_guarded_home")
    assert record is not None and record.workspace_root is None


def test_mcp_workspace_resolution_refuses_every_conflicted_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(MCP_WORKSPACE_ROOT_ENV, raising=False)
    for label, root in _conflicted_roots(tmp_path, monkeypatch):
        explicit = {MCP_WORKSPACE_ROOT_ENV: str(root)}
        before = sorted(path.name for path in (root / ".cruxible").iterdir())
        # Configured root, and the process cwd when nothing is configured.
        with pytest.raises(WorkspaceDirectoryConflict):
            mcp_workspace_root(explicit)
        monkeypatch.chdir(root)
        with pytest.raises(WorkspaceDirectoryConflict):
            mcp_workspace_root({})
        # Path resolution under an explicit root and under the adapter root.
        with pytest.raises(WorkspaceDirectoryConflict):
            resolve_workspace_path(".cruxible", root=root, kind="directory")
        with pytest.raises(WorkspaceDirectoryConflict):
            resolve_workspace_path(".cruxible", kind="directory")
        # The required worktree refuses whether configured or implicit.
        with pytest.raises(WorkspaceDirectoryConflict):
            mcp_git_workspace_root(explicit)
        with pytest.raises(WorkspaceDirectoryConflict):
            mcp_git_workspace_root({})
        # The optional worktree: a configured conflict refuses, an implicit one
        # means "no workspace".
        with pytest.raises(WorkspaceDirectoryConflict):
            optional_mcp_git_workspace_root(explicit)
        assert optional_mcp_git_workspace_root({}) is None, label
        # Callers that only list forbidden roots or observe optionally opt out.
        assert same_directory(mcp_workspace_root({}, guard=False), root)
        assert observe_next_workspace(root)["floor_status"] == "not_configured"
        assert sorted(path.name for path in (root / ".cruxible").iterdir()) == before


def test_implicit_selection_refuses_every_conflicted_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for label, root in _conflicted_roots(tmp_path, monkeypatch):
        assert not (root / ".cruxible" / "coverage.json").exists(), label
        with pytest.raises(WorkspaceDirectoryConflict):
            resolve_context(environ={}, cwd=root, home=tmp_path)
        # Opting out of a workspace still resolves, from the same directory.
        assert resolve_context(environ={}, cwd=root, no_workspace=True).workspace_source == "local"
        assert (
            resolve_context(environ={"CRUXIBLE_NO_WORKSPACE": "1"}, cwd=root).workspace_source
            == "local"
        )


@pytest.mark.parametrize("name", LEGACY)
def test_implicit_selection_skips_a_conflicted_ancestor(tmp_path: Path, name: str) -> None:
    root = _legacy_worktree(tmp_path / "project", name)
    nested = root / "pkg" / "sub"
    nested.mkdir(parents=True)

    resolved = resolve_context(environ={}, cwd=nested, home=tmp_path)

    assert resolved.workspace == nested.resolve()
    assert resolved.workspace_source == "local"


@pytest.mark.parametrize("name", LEGACY)
def test_cli_runs_without_a_workspace_from_a_conflicted_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    for variable in ("CRUXIBLE_WORKSPACE", "CRUXIBLE_NO_WORKSPACE", "CRUXIBLE_INSTANCE_ID"):
        monkeypatch.delenv(variable, raising=False)
    home = _git_home(tmp_path, monkeypatch)
    root = _legacy_worktree(tmp_path / "project", name)
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    save_cli_context(
        CliContextState(server_url="https://global.example.test", instance_id="inst_global")
    )

    # A 0.3 project: the command still targets its instance and says why.
    monkeypatch.chdir(root)
    result = CliRunner().invoke(cli, ["context", "show", "--json"])
    assert result.exit_code == 0, result.output
    assert "cruxible.workspace.directory_conflict" in result.stderr
    assert json.loads(result.stdout)["instance_id"] == "inst_global"
    assert json.loads(result.stdout)["workspace_attached"] is False

    # Home is never a workspace: the command runs quietly.
    monkeypatch.chdir(home)
    result = CliRunner().invoke(cli, ["context", "show", "--json"])
    assert result.exit_code == 0, result.output
    assert "directory_conflict" not in result.stderr

    # A workspace named by the environment refuses.
    monkeypatch.setenv("CRUXIBLE_WORKSPACE", str(root))
    result = CliRunner().invoke(cli, ["context", "show", "--json"])
    assert result.exit_code != 0
    assert "cruxible.workspace.directory_conflict" in result.output
