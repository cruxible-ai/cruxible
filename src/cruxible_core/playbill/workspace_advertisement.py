"""Fetch daemon-owned Playbill refs into an attached workspace as advisory refs."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import cast

from cruxible_client.contracts.types import GitObjectFormat
from cruxible_client.contracts.workspace_advertisement import (
    PlaybillWorkspaceAdvertisement,
    WorkspaceAdvertisementFailureCode,
)
from cruxible_core.playbill.git import NOTE_REFS

_REMOTE_NAME = "playbill"
_ACCEPTED_REFSPEC = "+refs/heads/main:refs/remotes/playbill/accepted"
_PROPOSAL_REFSPEC = "+refs/heads/proposals/*:refs/remotes/playbill/proposals/*"
# The note refs land under their own names rather than inside
# `refs/remotes/playbill/`, because `git notes --ref=` prefixes anything that
# does not already begin with `refs/notes/` -- so a note fetched to
# `refs/remotes/playbill/notes/playbill-eval` reads back as "no note found",
# which is the silent failure this refspec exists to remove. The names are
# already the product's own (`playbill-gen`, `playbill-eval`,
# `playbill-approval`), so one vocabulary serves the attached workspace and a
# clone of the mirror alike: the command `proposal review` prints is the
# command that runs, in both.
_NOTE_REFS: tuple[str, ...] = tuple(sorted(NOTE_REFS.values()))
_PROPOSAL_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_REVIEW_EXCLUDE = b"/.playbill/review/\n"
_PASSTHROUGH_ENVIRONMENT = ("PATH", "TMPDIR", "TMP", "TEMP", "SYSTEMROOT")
# Fetch can consult fsmonitor, hooks, alternate-reference enumeration, transport
# commands/helpers, and upload-pack. Pin each process-bearing seam at command-line
# precedence; the environment allowlist below removes the ambient equivalents.
_GIT_OVERRIDES = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.alternateRefsCommand=:",
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.file.allow=always",
    "-c",
    "protocol.ext.allow=never",
    "-c",
    "remote.playbill.uploadpack=git-upload-pack",
)


def _git(
    workspace: Path,
    args: list[str],
    *,
    input_data: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
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
            input=input_data,
            timeout=30,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(command, 127, b"", str(exc).encode())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 128, b"", str(exc).encode())


def _text(value: bytes) -> str:
    return value.decode("utf-8", errors="replace").strip()


def _local_url_matches(workspace: Path, raw_url: str, expected_url: str) -> bool:
    candidate = Path(raw_url).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        return str(candidate.resolve()) == expected_url
    except (OSError, RuntimeError, ValueError):
        return False


def _effective_remote_urls(workspace: Path) -> subprocess.CompletedProcess[bytes]:
    """Ask Git for post-``insteadOf`` fetch URLs without opening a transport."""

    return _git(workspace, ["remote", "get-url", "--all", _REMOTE_NAME])


def containing_git_workspace_root(workspace_path: Path) -> Path | None:
    """Return the containing worktree root through the hardened Git boundary."""

    try:
        candidate = workspace_path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    top = _git(candidate, ["rev-parse", "--show-toplevel"])
    if top.returncode != 0:
        return None
    try:
        root = Path(_text(top.stdout)).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if not candidate.is_relative_to(root):
        return None
    return root


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
        if any(not _local_url_matches(workspace, item, expected_url) for item in urls):
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
            if not _local_url_matches(workspace, raw_url, expected_url):
                return failed("remote_conflict")

    effective_urls = _effective_remote_urls(workspace)
    if effective_urls.returncode == 127:
        return failed("git_unavailable")
    if effective_urls.returncode != 0:
        return failed("remote_conflict")
    resolved_fetch_urls = tuple(line for line in _text(effective_urls.stdout).splitlines() if line)
    if not resolved_fetch_urls or any(
        not _local_url_matches(workspace, item, expected_url) for item in resolved_fetch_urls
    ):
        return failed("remote_conflict")

    configured = _git(
        workspace,
        [
            "config",
            "--local",
            "--replace-all",
            f"remote.{_REMOTE_NAME}.fetch",
            _ACCEPTED_REFSPEC,
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
            "--no-recurse-submodules",
            "--upload-pack=git-upload-pack",
            "--prune",
            _REMOTE_NAME,
            _ACCEPTED_REFSPEC,
            _PROPOSAL_REFSPEC,
        ],
    )
    if fetched.returncode != 0:
        return failed("fetch_failed")
    # Which note refs exist is asked before they are fetched: Git refuses a
    # whole fetch that names one absent ref, and an instance with no proposal
    # yet -- or no approval yet -- legitimately has none of them. A wildcard
    # would tolerate that and is not usable here: `--prune` over
    # `refs/notes/*` would delete the author's OWN notes, which the daemon has
    # no business touching.
    advertised_notes = _git(
        workspace,
        [
            "ls-remote",
            # The same explicit pin the fetch carries. `remote.<name>.uploadpack`
            # is workspace-writable and names a program to run; the command-line
            # flag is what actually overrides it, which the executable-config
            # guardrail proves by firing when it is missing.
            "--upload-pack=git-upload-pack",
            "--refs",
            _REMOTE_NAME,
            *_NOTE_REFS,
        ],
    )
    if advertised_notes.returncode != 0:
        return failed("fetch_failed")
    present_notes = tuple(
        line.split("\t")[1]
        for line in _text(advertised_notes.stdout).splitlines()
        if "\t" in line and line.split("\t")[1] in _NOTE_REFS
    )
    if present_notes:
        fetched_notes = _git(
            workspace,
            [
                "fetch",
                "--atomic",
                "--no-tags",
                "--no-recurse-submodules",
                "--upload-pack=git-upload-pack",
                _REMOTE_NAME,
                *(f"+{ref}:{ref}" for ref in present_notes),
            ],
        )
        if fetched_notes.returncode != 0:
            return failed("fetch_failed")
    listed = _git(
        workspace,
        [
            "for-each-ref",
            "--format=%(refname)",
            "refs/remotes/playbill",
        ],
    )
    if listed.returncode != 0:
        return failed("fetch_failed")
    all_refs = tuple(line for line in _text(listed.stdout).splitlines() if line)
    stale_refs = tuple(
        ref
        for ref in all_refs
        if ref != "refs/remotes/playbill/accepted"
        and not ref.startswith("refs/remotes/playbill/proposals/")
    )
    if stale_refs:
        deleted = _git(
            workspace,
            ["update-ref", "--stdin"],
            input_data="".join(f"option no-deref\ndelete {ref}\n" for ref in stale_refs).encode(
                "utf-8"
            ),
        )
        if deleted.returncode != 0:
            return failed("fetch_failed")
    refs = tuple(
        sorted(
            (ref for ref in all_refs if ref not in stale_refs),
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


def _review_proposal_key(proposal_id: str) -> str:
    key = proposal_id.removeprefix("sha256:")
    if _PROPOSAL_ID_RE.fullmatch(key) is None:
        raise ValueError("review workspace requires one full sha256 proposal ID")
    return key


def _ensure_review_worktrees_ignored(workspace_root: Path) -> None:
    common = _git(
        workspace_root,
        ["rev-parse", "--path-format=absolute", "--git-common-dir"],
    )
    if common.returncode != 0:
        raise ValueError("review workspace cannot resolve Git metadata")
    try:
        common_path = Path(_text(common.stdout)).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("review workspace Git metadata is invalid") from exc
    exclude = common_path / "info" / "exclude"
    if exclude.is_symlink():
        raise ValueError("review workspace exclude file must not be a symbolic link")
    try:
        current = exclude.read_bytes() if exclude.exists() else b""
        if _REVIEW_EXCLUDE in current.splitlines(keepends=True):
            return
        replacement = current
        if replacement and not replacement.endswith(b"\n"):
            replacement += b"\n"
        replacement += _REVIEW_EXCLUDE
        exclude.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with NamedTemporaryFile(mode="wb", dir=exclude.parent, delete=False) as output:
                temporary = Path(output.name)
                output.write(replacement)
                output.flush()
                os.fsync(output.fileno())
            if exclude.exists():
                temporary.chmod(exclude.stat().st_mode)
            os.replace(temporary, exclude)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    except OSError as exc:
        raise ValueError("review workspace ignore rule could not be written") from exc


def _review_worktree_target(
    workspace_root: Path,
    *,
    key: str,
    create_parent: bool,
) -> Path:
    playbill_root = workspace_root / ".playbill"
    review_root = playbill_root / "review"
    if playbill_root.is_symlink():
        raise ValueError(f"review workspace path escapes the Git worktree: {playbill_root}")
    try:
        if create_parent:
            review_root.mkdir(parents=True, exist_ok=True)
        resolved_review_root = review_root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"review workspace path is absent or unsafe: {review_root}") from exc
    if review_root.is_symlink() or resolved_review_root.parent.parent != workspace_root:
        raise ValueError(f"review workspace path escapes the Git worktree: {review_root}")
    target = review_root / key
    if target.is_symlink():
        raise ValueError(f"review workspace path escapes the Git worktree: {target}")
    return target


def open_proposal_review_worktree(*, workspace_path: Path, proposal_id: str) -> Path:
    """Materialize one advertised proposal tree as a detached, ignored worktree."""

    workspace_root = containing_git_workspace_root(workspace_path)
    if workspace_root is None:
        raise ValueError("review workspace must be inside one Git worktree")
    key = _review_proposal_key(proposal_id)
    reference = f"refs/remotes/playbill/proposals/{key}"
    resolved = _git(workspace_root, ["rev-parse", "--verify", f"{reference}^{{commit}}"])
    if resolved.returncode != 0:
        raise ValueError("proposal is not an advertised open proposal")
    target = _review_worktree_target(workspace_root, key=key, create_parent=True)
    if target.exists():
        raise ValueError(f"review workspace already exists or has an unsafe path: {target}")
    _ensure_review_worktrees_ignored(workspace_root)
    created = _git(
        workspace_root,
        ["worktree", "add", "--detach", str(target), reference],
    )
    if created.returncode != 0:
        raise ValueError(f"review workspace could not be opened: {_text(created.stderr)}")
    detached = _git(target, ["symbolic-ref", "-q", "HEAD"])
    if detached.returncode == 0:
        raise ValueError("review workspace unexpectedly created a local branch")
    return target


def close_proposal_review_worktree(*, workspace_path: Path, proposal_id: str) -> Path:
    """Remove one clean detached review worktree without deleting a branch."""

    workspace_root = containing_git_workspace_root(workspace_path)
    if workspace_root is None:
        raise ValueError("review workspace must be inside one Git worktree")
    key = _review_proposal_key(proposal_id)
    target = _review_worktree_target(workspace_root, key=key, create_parent=False)
    if not target.exists():
        listed = _git(workspace_root, ["worktree", "list", "--porcelain", "-z"])
        if listed.returncode != 0:
            raise ValueError(
                f"review workspace registration could not be inspected: {_text(listed.stderr)}"
            )
        if b"worktree " + os.fsencode(target) not in listed.stdout.split(b"\0"):
            raise ValueError(f"review workspace was never opened: {target}")
        removed = _git(workspace_root, ["worktree", "remove", "--force", str(target)])
        if removed.returncode != 0:
            raise ValueError(
                f"review workspace registration could not be removed: {_text(removed.stderr)}"
            )
        return target
    if not target.is_dir():
        raise ValueError(f"review workspace is absent or has an unsafe path: {target}")
    removed = _git(workspace_root, ["worktree", "remove", str(target)])
    if removed.returncode != 0:
        raise ValueError(
            "review workspace is modified or could not be closed; preserve or discard "
            f"its edits explicitly: {_text(removed.stderr)}"
        )
    return target


__all__ = [
    "advertise_workspace_refs",
    "close_proposal_review_worktree",
    "containing_git_workspace_root",
    "open_proposal_review_worktree",
    "workspace_git_object_format",
]
