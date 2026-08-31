"""Advisory workspace ref advertisement and Git object-format inheritance."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cruxible_core.playbill.workspace_advertisement import (
    advertise_workspace_refs,
    workspace_git_object_format,
)


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repositories(tmp_path: Path, object_format: str) -> tuple[Path, Path]:
    workspace = tmp_path / f"workspace-{object_format}"
    ledger = tmp_path / f"ledger-{object_format}.git"
    subprocess.run(
        ["git", "init", "-b", "main", f"--object-format={object_format}", str(workspace)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "init", "--bare", f"--object-format={object_format}", str(ledger)],
        check=True,
        capture_output=True,
    )
    _git(workspace, "config", "user.name", "test")
    _git(workspace, "config", "user.email", "test@example.invalid")
    (workspace / "README.md").write_text("workspace\n")
    _git(workspace, "add", "README.md")
    _git(workspace, "commit", "-m", "workspace")
    _git(workspace, "push", str(ledger), "HEAD:refs/heads/main")
    head = _git(workspace, "rev-parse", "HEAD")
    _git(ledger, "update-ref", "refs/proposals/owner/example", head)
    return workspace, ledger


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_advertisement_fetches_only_remote_tracking_refs(
    tmp_path: Path,
    object_format: str,
) -> None:
    workspace, ledger = _repositories(tmp_path, object_format)
    before_status = _git(workspace, "status", "--porcelain=v1")

    result = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format=object_format,  # type: ignore[arg-type]
    )

    assert workspace_git_object_format(workspace) == object_format
    assert result.status == "updated"
    assert result.advertised_refs == (
        "refs/remotes/playbill/main",
        "refs/remotes/playbill/proposals/owner/example",
    )
    assert _git(workspace, "status", "--porcelain=v1") == before_status
    assert _git(workspace, "symbolic-ref", "--short", "HEAD") == "main"


def test_advertisement_refuses_remote_name_conflict(tmp_path: Path) -> None:
    workspace, ledger = _repositories(tmp_path, "sha1")
    _git(workspace, "remote", "add", "playbill", str(tmp_path / "someone-elses-ledger.git"))

    result = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha1",
    )

    assert result.status == "failed"
    assert result.failure_code == "remote_conflict"


def test_advertisement_refuses_object_format_mismatch(tmp_path: Path) -> None:
    workspace, _unused = _repositories(tmp_path, "sha1")
    ledger = tmp_path / "sha256-ledger.git"
    subprocess.run(
        ["git", "init", "--bare", "--object-format=sha256", str(ledger)],
        check=True,
        capture_output=True,
    )

    result = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha256",
    )

    assert result.status == "failed"
    assert result.failure_code == "object_format_mismatch"


def test_advertisement_refreshes_main_and_prunes_only_proposal_refs(tmp_path: Path) -> None:
    workspace, ledger = _repositories(tmp_path, "sha1")
    first = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha1",
    )
    assert first.status == "updated"
    local_main = _git(workspace, "rev-parse", "refs/heads/main")

    producer = tmp_path / "producer"
    subprocess.run(
        ["git", "clone", str(ledger), str(producer)],
        check=True,
        capture_output=True,
    )
    _git(producer, "config", "user.name", "test")
    _git(producer, "config", "user.email", "test@example.invalid")
    (producer / "accepted.txt").write_text("accepted\n")
    _git(producer, "add", "accepted.txt")
    _git(producer, "commit", "-m", "accepted")
    _git(producer, "push", "origin", "main")
    _git(ledger, "update-ref", "-d", "refs/proposals/owner/example")

    refreshed = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha1",
    )

    assert refreshed.status == "updated"
    assert refreshed.advertised_refs == ("refs/remotes/playbill/main",)
    assert _git(workspace, "rev-parse", "refs/heads/main") == local_main
    assert _git(workspace, "rev-parse", "refs/remotes/playbill/main") == _git(
        producer, "rev-parse", "HEAD"
    )
