"""Fetch daemon-owned Playbill refs into an attached workspace as advisory refs."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import cast

from cruxible_client.contracts.types import GitObjectFormat
from cruxible_client.contracts.workspace_advertisement import (
    PlaybillWorkspaceAdvertisement,
    WorkspaceAdvertisementFailureCode,
)

_REMOTE_NAME = "playbill"
_MAIN_REFSPEC = "+refs/heads/main:refs/remotes/playbill/main"
_PROPOSAL_REFSPEC = "+refs/proposals/*:refs/remotes/playbill/proposals/*"
_PASSTHROUGH_ENVIRONMENT = ("PATH", "TMPDIR", "TMP", "TEMP", "SYSTEMROOT")
_GIT_OVERRIDES = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "protocol.ext.allow=never",
    "-c",
    "remote.playbill.uploadpack=git-upload-pack",
)


def _git(workspace: Path, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    command = ["git", *_GIT_OVERRIDES, "-C", str(workspace), *args]
    environment = {
        name: os.environ[name] for name in _PASSTHROUGH_ENVIRONMENT if name in os.environ
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            env=environment,
            timeout=30,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(command, 127, b"", str(exc).encode())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 128, b"", str(exc).encode())


def _text(value: bytes) -> str:
    return value.decode("utf-8", errors="replace").strip()


def workspace_git_object_format(workspace_root: Path) -> GitObjectFormat:
    """Return the attached repository's object format or reject the attachment."""

    workspace = workspace_root.expanduser()
    if workspace.is_symlink() or not workspace.is_dir():
        raise ValueError("workspace_missing")
    resolved = workspace.resolve()
    top = _git(resolved, ["rev-parse", "--show-toplevel"])
    common = _git(resolved, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
    object_format = _git(resolved, ["rev-parse", "--show-object-format"])
    if 127 in {top.returncode, common.returncode, object_format.returncode}:
        raise ValueError("git_unavailable")
    if top.returncode != 0 or common.returncode != 0 or object_format.returncode != 0:
        raise ValueError("workspace_not_git")
    raw_common_path = Path(_text(common.stdout))
    if raw_common_path.is_symlink():
        raise ValueError("workspace_path_invalid")
    try:
        top_path = Path(_text(top.stdout)).resolve(strict=True)
        common_path = raw_common_path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("workspace_path_invalid") from exc
    if top_path != resolved or not common_path.exists():
        raise ValueError("workspace_path_invalid")
    value = _text(object_format.stdout)
    if value not in {"sha1", "sha256"}:
        raise ValueError("workspace_not_git")
    return cast(GitObjectFormat, value)


def _advertise_workspace_refs(
    *,
    workspace_root: Path | None,
    ledger_path: Path,
    ledger_object_format: GitObjectFormat,
) -> PlaybillWorkspaceAdvertisement:
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
        code = exc.args[0] if exc.args else "unexpected_failure"
        if code not in {
            "workspace_missing",
            "workspace_not_git",
            "workspace_path_invalid",
            "git_unavailable",
        }:
            code = "unexpected_failure"
        return failed(cast(WorkspaceAdvertisementFailureCode, code))
    if workspace_format != ledger_object_format:
        return failed("object_format_mismatch")

    expected_url = str(ledger_path.resolve())
    current_urls = _git(
        workspace,
        ["config", "--local", "--get-all", f"remote.{_REMOTE_NAME}.url"],
    )
    if current_urls.returncode == 0:
        urls = tuple(line for line in _text(current_urls.stdout).splitlines() if line)
        if not urls:
            return failed("remote_conflict")
        resolved_urls: list[str] = []
        for raw_url in urls:
            candidate = Path(raw_url).expanduser()
            if not candidate.is_absolute():
                candidate = workspace / candidate
            try:
                resolved_urls.append(str(candidate.resolve()))
            except (OSError, RuntimeError, ValueError):
                resolved_urls.append(raw_url)
        if any(item != expected_url for item in resolved_urls):
            return failed("remote_conflict")
    elif current_urls.returncode == 127:
        return failed("git_unavailable")
    else:
        added = _git(
            workspace,
            ["config", "--local", "--add", f"remote.{_REMOTE_NAME}.url", expected_url],
        )
        if added.returncode != 0:
            return failed("remote_conflict")
        retry_urls = _git(
            workspace,
            ["config", "--local", "--get-all", f"remote.{_REMOTE_NAME}.url"],
        )
        if retry_urls.returncode != 0:
            return failed("remote_conflict")
        for raw_url in _text(retry_urls.stdout).splitlines():
            candidate = Path(raw_url).expanduser()
            if not candidate.is_absolute():
                candidate = workspace / candidate
            try:
                if str(candidate.resolve()) != expected_url:
                    return failed("remote_conflict")
            except (OSError, RuntimeError, ValueError):
                return failed("remote_conflict")

    configured = _git(
        workspace,
        [
            "config",
            "--local",
            "--replace-all",
            f"remote.{_REMOTE_NAME}.fetch",
            _MAIN_REFSPEC,
        ],
    )
    if configured.returncode != 0:
        return failed("fetch_failed")
    for key, value in (
        (f"remote.{_REMOTE_NAME}.fetch", _PROPOSAL_REFSPEC),
        (f"remote.{_REMOTE_NAME}.tagOpt", "--no-tags"),
        (f"remote.{_REMOTE_NAME}.skipFetchAll", "true"),
    ):
        args = ["config", "--local"]
        args.append("--add" if key.endswith(".fetch") else "--replace-all")
        if _git(workspace, [*args, key, value]).returncode != 0:
            return failed("fetch_failed")

    fetched = _git(
        workspace,
        [
            "fetch",
            "--atomic",
            "--no-tags",
            "--upload-pack=git-upload-pack",
            "--prune",
            _REMOTE_NAME,
            _MAIN_REFSPEC,
            _PROPOSAL_REFSPEC,
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
            (line for line in _text(listed.stdout).splitlines() if line),
            key=lambda item: item.encode("utf-8"),
        )
    )
    return PlaybillWorkspaceAdvertisement(
        status="updated",
        workspace_path=str(workspace),
        advertised_refs=refs,
    )


def advertise_workspace_refs(
    *,
    workspace_root: Path | None,
    ledger_path: Path,
    ledger_object_format: GitObjectFormat,
) -> PlaybillWorkspaceAdvertisement:
    """Refresh the daemon-owned namespace and reduce every failure to typed advice."""

    try:
        return _advertise_workspace_refs(
            workspace_root=workspace_root,
            ledger_path=ledger_path,
            ledger_object_format=ledger_object_format,
        )
    except BaseException:
        return PlaybillWorkspaceAdvertisement(
            status="failed",
            workspace_path=None if workspace_root is None else str(workspace_root),
            failure_code="unexpected_failure",
        )


__all__ = ["advertise_workspace_refs", "workspace_git_object_format"]
