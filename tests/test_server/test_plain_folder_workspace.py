"""The folder is the workspace, Git optional: the daemon attaches a plain folder.

A host attaches any folder that is not the home directory, a filesystem root,
the state root or inside a ``.cruxible`` directory. Its ledger mirror (the
advisory ``cruxible-ledger`` refs) is skipped with the typed ``not_git``
status; everything else -- the workspace file reader with its containment
rules, a Procedure run reading a workspace file -- works the same. After
``git init`` in the folder, attaching it again turns the mirror on, with the
object-format check a first attach makes.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client.contracts.errors import ObjectFormatConflict
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import utc_now
from cruxible_client.contracts.workspace_advertisement import WorkspaceAdvertisement
from cruxible_client.contracts.workspace_file import ExecutedWorkspaceFileSourceRequestV2
from cruxible_client.contracts.workspace_layout import WorkspaceDirectoryConflict
from cruxible_core.documents.workspace_file import (
    WorkspaceFileReader,
    WorkspaceFileReadRefused,
    workspace_binding_digest,
)
from cruxible_core.errors import ConfigError
from cruxible_core.governance.keys import GeneratedKeyMaterial, generate_client_principal_key
from cruxible_core.runtime import host_api, playbill_api
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.registry import get_registry
from tests.test_server.test_playbill_host import host_client  # noqa: F401


def _host(tmp_path: Path, folder: Path, instance_id: str) -> GeneratedKeyMaterial:
    """A host attached to ``folder``, initialized by ``operator`` with ``owner`` and
    ``reviewer`` beside it (the principals the Procedure-run world signs as)."""

    host_api.create_playbill_host(
        instance_id=instance_id,
        workspace_root=str(folder),
        workspace_attachment_authorized=True,
    )
    record = get_registry().get(instance_id)
    assert record is not None
    # Where the shared test worlds keep each principal's custody, beside the root.
    custody = get_registry().instance_root(record).parent
    operator, owner, reviewer = (
        generate_client_principal_key(
            custody / f"client-custody-{principal_id}",
            principal_id=principal_id,
            kind="ordinary",
            forbidden_roots=(folder,),
        )
        for principal_id in ("operator", "owner", "reviewer")
    )
    playbill_api.playbill_init(
        instance_id,
        principals=(operator.principal, owner.principal, reviewer.principal),
        workspace_root=str(folder),
        workspace_attachment_authorized=True,
    )
    return owner


def _read(reader: WorkspaceFileReader, root: Path, relative_path: str) -> bytes:
    request = ExecutedWorkspaceFileSourceRequestV2(
        logical_source="workspace.docs",
        workspace=root.name,
        workspace_binding_digest=workspace_binding_digest(
            instance_id=reader.instance_id, canonical_root=root
        ),
        relative_path=relative_path,
        coordinate_type="workspace-snapshot-v1",
        coordinate={"revision": "working"},
        selector_type="workspace-file-v1",
        selector={"document": "docs"},
    )
    result = reader.read(
        request,
        run_id="RUN-plain-folder",
        admission_binding_digest="sha256:" + "1" * 64,
        occurrence_path="source:read",
        policy_coordinate=AcceptedCoordinate(
            git_oid="a" * 64,
            semantic_root="sha256:" + "b" * 64,
            generation_root="sha256:" + "c" * 64,
            compiler_digest="sha256:" + "d" * 64,
        ),
        resolved_max_bytes=1024,
        derived_request_digest="sha256:" + "2" * 64,
        read_at=utc_now(),
    )
    return bytes.fromhex(result.receipt.bytes_digest.removeprefix("sha256:"))


def test_a_plain_folder_attaches_and_its_ledger_mirror_reports_not_git(
    host_client: TestClient,  # noqa: F811
    tmp_path: Path,
) -> None:
    del host_client
    folder = tmp_path / "project"
    folder.mkdir()

    _host(tmp_path, folder, "inst_plain")

    record = get_registry().get("inst_plain")
    assert record is not None and record.workspace_root == str(folder.resolve())
    instance = get_playbill_manager().get("inst_plain")
    assert instance.descriptor.git_object_format == "sha1"
    advertisement = instance.settled_workspace_advertisement()
    assert advertisement == WorkspaceAdvertisement(
        status="not_git", workspace_path=str(folder.resolve())
    )
    # Nothing Git-shaped was written into the folder.
    assert not (folder / ".git").exists()


def test_the_daemon_reader_reads_a_plain_folder_with_every_containment_rule(
    host_client: TestClient,  # noqa: F811
    tmp_path: Path,
) -> None:
    del host_client
    folder = tmp_path / "project"
    (folder / "docs").mkdir(parents=True)
    body = b"# Notes\n\nThe plain folder is readable.\n"
    (folder / "docs" / "notes.md").write_bytes(body)
    _host(tmp_path, folder, "inst_reader")
    reader = get_playbill_manager().workspace_file_reader("inst_reader")
    root = folder.resolve()

    assert _read(reader, root, "docs/notes.md") == hashlib.sha256(body).digest()

    # The rules a Git worktree gets hold here too.
    (folder / ".git").mkdir()
    (folder / ".git" / "config").write_text("[core]\n")
    (folder / "owner.ed25519").write_text("not a key")
    (folder / "linked.md").symlink_to(folder / "docs" / "notes.md")
    (folder / "hard.md").hardlink_to(folder / "docs" / "notes.md")
    for relative, path_class in (
        (".git/config", "git_metadata"),
        (".cruxible/coverage.json", "playbill_control"),
        ("owner.ed25519", "client_custody"),
        ("linked.md", "symlink"),
        ("hard.md", "hardlink"),
    ):
        with pytest.raises(WorkspaceFileReadRefused) as refused:
            _read(reader, root, relative)
        assert refused.value.path_class == path_class, relative


def test_attach_refuses_roots_that_are_no_workspace(
    host_client: TestClient,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del host_client
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    control = tmp_path / "project" / ".cruxible" / "inner"
    control.mkdir(parents=True)
    state_inner = get_registry().state_root / "instances" / "somewhere"
    state_inner.mkdir(parents=True)
    for root, reason in (
        (Path("/"), "filesystem_root"),
        (home, "home"),
        (control, "control_directory"),
        (state_inner, "state_root"),
    ):
        with pytest.raises(WorkspaceDirectoryConflict) as refused:
            host_api.create_playbill_host(
                instance_id="inst_refused",
                workspace_root=str(root),
                workspace_attachment_authorized=True,
            )
        assert refused.value.reason == reason, root
    missing = tmp_path / "missing"
    with pytest.raises(ConfigError, match="existing local folder"):
        host_api.create_playbill_host(
            instance_id="inst_refused",
            workspace_root=str(missing),
            workspace_attachment_authorized=True,
        )
    broken = tmp_path / "broken"
    (broken / ".git").mkdir(parents=True)
    with pytest.raises(ConfigError, match="no usable Git repository"):
        host_api.create_playbill_host(
            instance_id="inst_refused",
            workspace_root=str(broken),
            workspace_attachment_authorized=True,
        )
    assert get_registry().get("inst_refused") is None


def test_git_init_then_attach_again_turns_the_ledger_mirror_on(
    host_client: TestClient,  # noqa: F811
    tmp_path: Path,
) -> None:
    del host_client
    folder = tmp_path / "project"
    folder.mkdir()
    _host(tmp_path, folder, "inst_late_git")
    instance = get_playbill_manager().get("inst_late_git")
    assert instance.settled_workspace_advertisement().status == "not_git"

    subprocess.run(
        ["git", "init", "-q", "-b", "main", "--object-format=sha1", str(folder)], check=True
    )
    # The mirror stays off until the folder is attached again; no write turns it on.
    assert instance.settled_workspace_advertisement().status == "not_git"

    again = host_api.playbill_host_workspace_attach(
        "inst_late_git",
        workspace_root=str(folder),
        workspace_attachment_authorized=True,
    )

    assert again.status == "already_attached"
    advertised = instance.settled_workspace_advertisement()
    assert advertised.status == "updated"
    assert "refs/remotes/cruxible-ledger/accepted" in advertised.advertised_refs
    refs = subprocess.run(
        ["git", "-C", str(folder), "for-each-ref", "--format=%(refname)"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert "refs/remotes/cruxible-ledger/accepted" in refs


def test_a_later_git_init_in_another_object_format_is_refused_on_attach(
    host_client: TestClient,  # noqa: F811
    tmp_path: Path,
) -> None:
    del host_client
    folder = tmp_path / "project"
    folder.mkdir()
    _host(tmp_path, folder, "inst_late_sha256")
    subprocess.run(
        ["git", "init", "-q", "-b", "main", "--object-format=sha256", str(folder)], check=True
    )

    with pytest.raises(ObjectFormatConflict):
        host_api.playbill_host_workspace_attach(
            "inst_late_sha256",
            workspace_root=str(folder),
            workspace_attachment_authorized=True,
        )
    instance = get_playbill_manager().get("inst_late_sha256")
    assert instance.settled_workspace_advertisement().status == "not_git"


def test_a_procedure_run_reads_a_file_from_the_attached_plain_folder(
    host_client: TestClient,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reader the daemon builds from the registry serves a real run's source read."""

    del host_client
    from tests.test_procedures import test_procedure_source_runs as source_runs

    folder = tmp_path / "workspace"
    folder.mkdir()
    owner = _host(tmp_path, folder, "inst_plain_run")
    manager = get_playbill_manager()
    monkeypatch.setattr(
        source_runs,
        "initialize_local",
        lambda _root: (manager.get("inst_plain_run"), owner),
    )
    instance, _owner, _procedure, root, _policy = source_runs._world(tmp_path)

    state, _invoker = source_runs._run(
        instance, root, reader=manager.workspace_file_reader("inst_plain_run")
    )

    assert state.status == "succeeded", state.terminal
    receipt = state.source_observations[0].source_read_receipt
    assert receipt is not None
    assert receipt.relative_path == source_runs.RELATIVE_PATH
    assert receipt.bytes_digest == "sha256:" + hashlib.sha256(source_runs.ADVISORY).hexdigest()
    assert receipt.workspace_binding_digest == workspace_binding_digest(
        instance_id="inst_plain_run", canonical_root=folder.resolve()
    )
