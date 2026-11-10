"""The one workspace-root resolver the CLI and the MCP adapter share.

The folder is the workspace; Git is optional. From a start directory the root
is the nearest ancestor holding a real ``.cruxible/`` workspace directory (the
home directory's and any daemon state root's ``.cruxible`` are skipped, so is
one holding a state root's own entries), else
the enclosing Git worktree, else the start directory itself. Whatever is
selected, a root that is the home directory, a filesystem root, the daemon
state root (or inside it, which covers every managed instance root), a 0.3
instance, or a directory inside a ``.cruxible`` control directory is refused
with ``WorkspaceDirectoryConflict`` and its repair.

The walk reads directory entries only: no Git subprocess and no file content.
"""

from __future__ import annotations

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
from cruxible_core.floor.workspace_advertisement import containing_git_workspace_root

#: How a root was found: an existing workspace directory, a Git worktree, or
#: the start directory itself.
WorkspaceRootSource = Literal["cruxible_directory", "git", "directory"]


@dataclass(frozen=True)
class WorkspaceRoot:
    """One resolved workspace root and how it was found."""

    root: Path
    source: WorkspaceRootSource

    @property
    def is_git(self) -> bool:
        """Whether the root is itself a Git worktree's top level."""

        return containing_git_workspace_root(self.root) == self.root


#: Entries only a daemon state root holds: a ``.cruxible`` holding one is some
#: daemon's state root (another ``CRUXIBLE_STATE_ROOT``'s, say), never a workspace.
_STATE_ROOT_MARKERS = ("daemon", "instances", "registry.db")


def _holds_workspace_directory(candidate: Path, *, state_root: Path) -> bool:
    directory = candidate / WORKSPACE_DIRECTORY
    try:
        if directory.is_symlink() or not directory.is_dir():
            return False
        if any((directory / marker).exists() for marker in _STATE_ROOT_MARKERS):
            return False
    except OSError:
        return False
    # The home directory's and a daemon state root's .cruxible are no workspace.
    return workspace_directory_conflict(candidate, state_root=state_root) not in {
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


def resolve_workspace_root(
    start: Path,
    *,
    fallback_to_start: bool = True,
    state_root: Path | None = None,
) -> WorkspaceRoot | None:
    """The workspace root for ``start``, refused when it is no usable root.

    ``fallback_to_start=False`` answers None instead of choosing a plain start
    directory that holds no ``.cruxible`` and lies in no Git worktree (callers
    that only need to know about an existing workspace, such as custody's
    forbidden roots).
    """

    daemon_root = daemon_state_root() if state_root is None else state_root
    selected = start.expanduser().resolve(strict=True)
    home = Path.home()
    found: WorkspaceRoot | None = None
    for candidate in (selected, *selected.parents):
        if candidate.parent == candidate or same_directory(candidate, home):
            break
        if _holds_workspace_directory(candidate, state_root=daemon_root):
            found = WorkspaceRoot(candidate, "cruxible_directory")
            break
    if found is None:
        git_root = containing_git_workspace_root(selected)
        if git_root is not None:
            found = WorkspaceRoot(git_root, "git")
        elif fallback_to_start:
            found = WorkspaceRoot(selected, "directory")
        else:
            return None
    refuse_unusable_root(found.root, state_root=daemon_root)
    return found


__all__ = [
    "WorkspaceRoot",
    "WorkspaceRootSource",
    "refuse_unusable_root",
    "resolve_workspace_root",
]
