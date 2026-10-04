"""Every workspace path refuses the home directory and a 0.3 instance, through public entries.

``.cruxible/`` is the workspace directory, but the home directory's
``.cruxible`` is the daemon state root and a 0.3-initialized project keeps its
instance there. Selection, reads, writes and daemon create/attach refuse such a
root with ``WorkspaceDirectoryConflict`` before they read a binding or write a
file; the home check compares directory identity, so a differently cased
spelling of home refuses too.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cruxible_client.authoring.context import resolve_context
from cruxible_client.authoring.projection_manifests import retain_local_manifests
from cruxible_client.authoring.selectors import WorkspaceSources
from cruxible_client.authoring.workspace import write_workspace_config
from cruxible_client.contracts.workspace_layout import (
    WorkspaceDirectoryConflict,
    same_directory,
)
from cruxible_core.coverage.middleware import load_coverage_config
from cruxible_core.runtime import host_api
from cruxible_core.server.registry import get_registry
from tests.test_server.test_playbill_host import host_client  # noqa: F401

LEGACY = ("instance.json", "state.db")


def _legacy_worktree(root: Path, name: str) -> Path:
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    (root / ".cruxible").mkdir()
    (root / ".cruxible" / name).write_text("{}", encoding="utf-8")
    return root


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
