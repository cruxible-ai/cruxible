"""Fetch daemon-owned Playbill refs into an attached workspace as advisory refs."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import cast

from cruxible_client.contracts.types import GitObjectFormat
from cruxible_client.contracts.workspace_advertisement import (
    PlaybillWorkspaceAdvertisement,
    WorkspaceAdvertisementFailureCode,
)

_REMOTE_NAME = "playbill"


def _git(workspace: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(workspace), *args]
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 128, "", str(exc))


def workspace_git_object_format(workspace_root: Path) -> GitObjectFormat:
    """Return the attached repository's object format or reject the attachment."""

    workspace = workspace_root.expanduser()
    if workspace.is_symlink() or not workspace.is_dir():
        raise ValueError("workspace_missing")
    resolved = workspace.resolve()
    top = _git(resolved, ["rev-parse", "--show-toplevel"])
    common = _git(resolved, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
    object_format = _git(resolved, ["rev-parse", "--show-object-format"])
    if top.returncode != 0 or common.returncode != 0 or object_format.returncode != 0:
        raise ValueError("workspace_not_git")
    raw_common_path = Path(common.stdout.strip())
    if raw_common_path.is_symlink():
        raise ValueError("workspace_path_invalid")
    try:
        top_path = Path(top.stdout.strip()).resolve(strict=True)
        common_path = raw_common_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("workspace_path_invalid") from exc
    if top_path != resolved or not common_path.exists():
        raise ValueError("workspace_path_invalid")
    value = object_format.stdout.strip()
    if value not in {"sha1", "sha256"}:
        raise ValueError("workspace_not_git")
    return cast(GitObjectFormat, value)


def advertise_workspace_refs(
    *,
    workspace_root: Path | None,
    ledger_path: Path,
    ledger_object_format: GitObjectFormat,
) -> PlaybillWorkspaceAdvertisement:
    """Refresh only the daemon-owned remote-tracking namespace; never raise."""

    if workspace_root is None:
        return PlaybillWorkspaceAdvertisement(status="not_attached", workspace_path=None)
    workspace = workspace_root.expanduser().resolve(strict=False)

    def failed(code: WorkspaceAdvertisementFailureCode) -> PlaybillWorkspaceAdvertisement:
        return PlaybillWorkspaceAdvertisement(
            status="failed",
            workspace_path=str(workspace),
            failure_code=code,
        )

    try:
        workspace_format = workspace_git_object_format(workspace)
    except ValueError as exc:
        return failed(cast(WorkspaceAdvertisementFailureCode, str(exc)))
    if workspace_format != ledger_object_format:
        return failed("object_format_mismatch")

    expected_url = str(ledger_path.resolve())
    current_url = _git(workspace, ["remote", "get-url", _REMOTE_NAME])
    if current_url.returncode == 0:
        raw_url = current_url.stdout.strip()
        try:
            current_resolved = str(Path(raw_url).expanduser().resolve())
        except (OSError, RuntimeError):
            current_resolved = raw_url
        if current_resolved != expected_url:
            return failed("remote_conflict")
    else:
        added = _git(workspace, ["remote", "add", _REMOTE_NAME, expected_url])
        if added.returncode != 0:
            # A racing creator is safe only if it installed the exact URL.
            retry_url = _git(workspace, ["remote", "get-url", _REMOTE_NAME])
            if retry_url.returncode != 0 or retry_url.stdout.strip() != expected_url:
                return failed("remote_conflict")

    fetched = _git(
        workspace,
        [
            "fetch",
            "--atomic",
            "--prune",
            _REMOTE_NAME,
            "+refs/heads/main:refs/remotes/playbill/main",
            "+refs/proposals/*:refs/remotes/playbill/proposals/*",
        ],
    )
    if fetched.returncode != 0:
        return failed("fetch_failed")
    listed = _git(
        workspace,
        [
            "for-each-ref",
            "--format=%(refname)",
            "refs/remotes/playbill/main",
            "refs/remotes/playbill/proposals",
        ],
    )
    if listed.returncode != 0:
        return failed("fetch_failed")
    refs = tuple(
        sorted(
            (line for line in listed.stdout.splitlines() if line),
            key=lambda item: item.encode("utf-8"),
        )
    )
    return PlaybillWorkspaceAdvertisement(
        status="updated",
        workspace_path=str(workspace),
        advertised_refs=refs,
    )


__all__ = ["advertise_workspace_refs", "workspace_git_object_format"]
