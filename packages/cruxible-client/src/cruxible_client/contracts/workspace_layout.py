"""Frozen paths in a repository attached to one Cruxible instance, and their guard.

The workspace directory is ``.cruxible/`` at the worktree root. Two places use
that name for something else: the home directory's ``.cruxible`` is the daemon
state root, and a project directory a 0.3 release initialized keeps its
instance (``instance.json``, ``state.db``) there. Every workspace selection,
read and write, and every daemon create or attach, goes through
``workspace_path`` or ``ensure_workspace_directory`` and refuses those roots
with a typed error and a repair before it touches a file.
"""

from __future__ import annotations

import os
from pathlib import Path

from cruxible_client.contracts.errors import CruxibleError

#: The workspace directory: client custody, sources, manifests and the floor.
WORKSPACE_DIRECTORY = ".cruxible"
FLOOR_PATH = ".cruxible/floor"
#: What a 0.3-era per-project instance kept in the same directory name.
LEGACY_INSTANCE_FILES = ("instance.json", "state.db")


class WorkspaceError(CruxibleError, ValueError):
    """A client workspace or exported floor failed deterministic validation."""


class WorkspaceDirectoryConflict(WorkspaceError):
    """The workspace root cannot hold the ``.cruxible`` workspace directory."""

    error_code = "cruxible.workspace.directory_conflict"

    def __init__(self, *, workspace: Path, reason: str) -> None:
        self.workspace = str(workspace)
        self.reason = reason
        if reason == "home":
            self.repair_commands: tuple[str, ...] = ()
            detail = (
                "the home directory's .cruxible is the daemon state root; run from the "
                "project's Git worktree instead (or set CRUXIBLE_NO_WORKSPACE=1 when the "
                "command needs no workspace)"
            )
        else:
            moved = f"{workspace}/.cruxible-0.3"
            self.repair_commands = (f"mv {workspace}/.cruxible {moved}",)
            detail = (
                f"{workspace}/.cruxible holds a 0.3 instance ({reason}); move it aside "
                f"with `{self.repair_commands[0]}` (or remove it once retired), then retry"
            )
        super().__init__(f"{self.error_code}: {detail}")


def same_directory(left: Path, right: Path) -> bool:
    """Whether two paths name one directory, by identity where both exist.

    A case-insensitive filesystem gives one directory several spellings, so
    spelling equality is not identity; ``samefile`` compares the device and
    inode. Where either does not exist, the resolved spellings are compared
    case-folded, which can only over-refuse.
    """

    try:
        return os.path.samefile(left, right)
    except OSError:
        return (
            str(left.resolve(strict=False)).casefold()
            == str(right.resolve(strict=False)).casefold()
        )


def workspace_directory_conflict(root: Path) -> str | None:
    """Why ``root/.cruxible`` cannot be this workspace's directory, or None."""

    if same_directory(root, Path.home()):
        return "home"
    directory = root / WORKSPACE_DIRECTORY
    for name in LEGACY_INSTANCE_FILES:
        if (directory / name).exists():
            return name
    return None


def ensure_workspace_directory(root: Path) -> Path:
    """Refuse a workspace root whose ``.cruxible`` is not a workspace directory."""

    reason = workspace_directory_conflict(root)
    if reason is not None:
        raise WorkspaceDirectoryConflict(workspace=root, reason=reason)
    return root


def workspace_path(root: Path, *parts: str) -> Path:
    """``root/.cruxible/<parts>``, after refusing a root that is no workspace."""

    return ensure_workspace_directory(root) / WORKSPACE_DIRECTORY / Path(*parts)


__all__ = [
    "FLOOR_PATH",
    "LEGACY_INSTANCE_FILES",
    "WORKSPACE_DIRECTORY",
    "WorkspaceDirectoryConflict",
    "WorkspaceError",
    "ensure_workspace_directory",
    "same_directory",
    "workspace_directory_conflict",
    "workspace_path",
]
