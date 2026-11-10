"""The one workspace-root resolver: the CLI, the SDK context and the MCP adapter.

The folder is the workspace; Git is optional. From a start directory the walk
goes up, never past the home directory, a filesystem root, a device boundary
or a directory this user does not own, and the root is the first of:

1. the nearest ancestor holding a real ``.cruxible/`` workspace directory (the
   home directory's, any daemon state root's, and one holding a state root's
   own entries are skipped) -- an inner Git repository never hides it;
2. the nearest Git worktree top level met on the way;
3. the start directory itself (``fallback_to_start``).

The selected root is then refused, with ``WorkspaceDirectoryConflict`` and its
repair, when it is the home directory, a filesystem root, the daemon state root
(or inside it, which covers every managed root), a 0.3 instance, or inside a
``.cruxible`` directory. A workspace binding (``.cruxible/coverage.json``) is
read only at the root this chooses, never beyond it.

The walk reads directory entries only (``stat``, ``is_dir``, the ``.git``
pointer file): no Git subprocess and no recursive scan.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cruxible_client.contracts.workspace_layout import (
    WORKSPACE_DIRECTORY,
    WorkspaceDirectoryConflict,
    daemon_state_root,
    same_directory,
    workspace_directory_conflict,
)

#: How a root was found: an existing workspace directory, a Git worktree, or
#: the start directory itself.
WorkspaceRootSource = Literal["cruxible_directory", "git", "directory"]

#: Entries only a daemon state root holds: a ``.cruxible`` holding one is some
#: daemon's state root (another ``CRUXIBLE_STATE_ROOT``'s, say), never a workspace.
STATE_ROOT_MARKERS = ("daemon", "instances", "registry.db")


def git_repository_directories(workspace: Path) -> tuple[Path, Path]:
    """The git dir and common dir of a worktree root, without ambient Git variables.

    Raises ``ValueError`` with ``workspace_path_invalid`` (a symlinked ``.git``)
    or ``workspace_not_git`` (no usable repository there).
    """

    marker = workspace / ".git"
    if marker.is_symlink():
        raise ValueError("workspace_path_invalid")
    if marker.is_file():
        raw = marker.read_text().strip()
        if not raw.startswith("gitdir: "):
            raise ValueError("workspace_not_git")
        gitdir = (workspace / raw[8:]).resolve(strict=True)
    elif marker.is_dir():
        gitdir = marker.resolve(strict=True)
    else:
        raise ValueError("workspace_not_git")
    common_file = gitdir / "commondir"
    common = (
        (gitdir / common_file.read_text().strip()).resolve(strict=True)
        if common_file.is_file()
        else gitdir
    )
    if not (common / "config").is_file() or not (common / "objects").is_dir():
        raise ValueError("workspace_not_git")
    return gitdir, common


def _is_git_root(candidate: Path) -> bool:
    try:
        if not (candidate / ".git").exists():
            return False
        git_repository_directories(candidate)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def containing_git_workspace_root(workspace_path: Path) -> Path | None:
    """The top level of the Git worktree ``workspace_path`` lies in, or None."""

    try:
        candidate = workspace_path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    for root in (candidate, *candidate.parents):
        try:
            if (root / ".git").exists():
                git_repository_directories(root)
                return root
        except (OSError, RuntimeError, ValueError):
            return None
    return None


@dataclass(frozen=True)
class WorkspaceRoot:
    """One resolved workspace root and how it was found."""

    root: Path
    source: WorkspaceRootSource

    @property
    def is_git(self) -> bool:
        """Whether the root is itself a Git worktree's top level."""

        return containing_git_workspace_root(self.root) == self.root


def holds_workspace_directory(candidate: Path, *, state_root: Path | None = None) -> bool:
    """Whether ``candidate/.cruxible`` is a real workspace directory (not a state root)."""

    daemon_root = daemon_state_root() if state_root is None else state_root
    directory = candidate / WORKSPACE_DIRECTORY
    try:
        if directory.is_symlink() or not directory.is_dir():
            return False
        if any((directory / marker).exists() for marker in STATE_ROOT_MARKERS):
            return False
    except OSError:
        return False
    return workspace_directory_conflict(candidate, state_root=daemon_root) not in {
        "home",
        "state_root",
    }


def refuse_unusable_root(root: Path, *, state_root: Path | None = None) -> Path:
    """``root`` when it can be a workspace root; else ``WorkspaceDirectoryConflict``."""

    daemon_root = daemon_state_root() if state_root is None else state_root
    if root.parent == root:
        raise WorkspaceDirectoryConflict(workspace=root, reason="filesystem_root")
    reason = workspace_directory_conflict(root, state_root=daemon_root)
    if reason is not None:
        raise WorkspaceDirectoryConflict(workspace=root, reason=reason, state_root=daemon_root)
    if any(part.casefold() == WORKSPACE_DIRECTORY for part in root.parts):
        raise WorkspaceDirectoryConflict(workspace=root, reason="control_directory")
    return root


def _owner() -> int | None:
    getuid = getattr(os, "getuid", None)
    return None if getuid is None else int(getuid())


def resolve_workspace_root(
    start: Path,
    *,
    fallback_to_start: bool = True,
    state_root: Path | None = None,
    home: Path | None = None,
) -> WorkspaceRoot | None:
    """The workspace root for ``start``, refused when it is no usable root.

    ``fallback_to_start=False`` answers None instead of choosing a plain start
    directory that holds no ``.cruxible`` and lies in no Git worktree (callers
    that only need to know about an existing workspace, such as custody's
    forbidden roots). ``OSError`` when ``start`` does not exist.
    """

    daemon_root = daemon_state_root() if state_root is None else state_root
    selected = start.expanduser().resolve(strict=True)
    home_root = (Path.home() if home is None else home).expanduser()
    owner = _owner()
    device = selected.stat().st_dev
    git_root: Path | None = None
    found: WorkspaceRoot | None = None
    for candidate in (selected, *selected.parents):
        if candidate.parent == candidate or same_directory(candidate, home_root):
            break
        try:
            status = candidate.stat()
        except OSError:
            break
        # Another device, or a directory someone else owns (a shared /tmp), is
        # the ceiling: nothing planted above it can become this workspace.
        if status.st_dev != device or (owner is not None and status.st_uid != owner):
            break
        if holds_workspace_directory(candidate, state_root=daemon_root):
            found = WorkspaceRoot(candidate, "cruxible_directory")
            break
        if git_root is None and _is_git_root(candidate):
            git_root = candidate
    if found is None:
        if git_root is not None:
            found = WorkspaceRoot(git_root, "git")
        elif fallback_to_start:
            found = WorkspaceRoot(selected, "directory")
        else:
            return None
    refuse_unusable_root(found.root, state_root=daemon_root)
    return found


__all__ = [
    "STATE_ROOT_MARKERS",
    "WorkspaceRoot",
    "WorkspaceRootSource",
    "containing_git_workspace_root",
    "git_repository_directories",
    "holds_workspace_directory",
    "refuse_unusable_root",
    "resolve_workspace_root",
]
