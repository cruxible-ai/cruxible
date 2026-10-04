"""Frozen paths in a repository attached to one Cruxible instance, and their guard.

The workspace directory is ``.cruxible/`` at the worktree root. Other places
use that name, or that directory, for something else: the home directory's
``.cruxible`` is the default daemon state root, ``CRUXIBLE_STATE_ROOT`` can put
the daemon state root anywhere, and a project directory a 0.3 release
initialized keeps its instance (``instance.json``, ``state.db``) there. The
workspace directory may not be, lie inside, or hold the daemon state root.
Every workspace selection, read and write, and every daemon create or attach,
goes through ``workspace_path`` or ``ensure_workspace_directory`` and refuses
those roots with a typed error and a repair before it touches a file.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from cruxible_client.contracts.errors import CruxibleError

#: The workspace directory: client custody, sources, manifests and the floor.
WORKSPACE_DIRECTORY = ".cruxible"
FLOOR_PATH = ".cruxible/floor"
#: What a 0.3-era per-project instance kept in the same directory name.
LEGACY_INSTANCE_FILES = ("instance.json", "state.db")
#: Relocates the daemon state root from ``~/.cruxible``.
STATE_ROOT_ENV = "CRUXIBLE_STATE_ROOT"


class WorkspaceError(CruxibleError, ValueError):
    """A client workspace or exported floor failed deterministic validation."""


class WorkspaceDirectoryConflict(WorkspaceError):
    """The workspace root cannot hold the ``.cruxible`` workspace directory."""

    error_code = "cruxible.workspace.directory_conflict"

    def __init__(self, *, workspace: Path, reason: str, state_root: Path | None = None) -> None:
        self.workspace = str(workspace)
        self.reason = reason
        self.state_root = None if state_root is None else str(state_root)
        if reason == "home":
            self.repair_commands: tuple[str, ...] = ()
            detail = (
                "the home directory's .cruxible is the daemon state root; run from the "
                "project's Git worktree instead (or set CRUXIBLE_NO_WORKSPACE=1 when the "
                "command needs no workspace)"
            )
        elif reason == "state_root":
            self.repair_commands = ()
            detail = (
                f"{workspace}/.cruxible overlaps the daemon state root {state_root} (it is "
                "that root, lies inside it, or holds it); keep the state root outside every "
                f"workspace: start the daemon with {STATE_ROOT_ENV} naming a directory outside "
                f"{workspace}/.cruxible, or run from a worktree outside {state_root}"
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


def daemon_state_root(environ: Mapping[str, str] | None = None) -> Path:
    """This process's daemon state root: ``CRUXIBLE_STATE_ROOT``, else ``~/.cruxible``.

    The location only; the daemon validates the root itself when it opens it.
    """

    env = os.environ if environ is None else environ
    raw = env.get(STATE_ROOT_ENV)
    if raw is not None and raw.strip():
        return Path(raw).expanduser().resolve()
    return (Path.home() / WORKSPACE_DIRECTORY).resolve()


def _within(inner: Path, outer: Path) -> bool:
    """Whether ``inner`` is ``outer`` or lies under it, by directory identity."""

    resolved = inner.resolve(strict=False)
    return any(same_directory(candidate, outer) for candidate in (resolved, *resolved.parents))


def workspace_directory_conflict(root: Path, *, state_root: Path | None = None) -> str | None:
    """Why ``root/.cruxible`` cannot be this workspace's directory, or None.

    ``state_root`` is the daemon's own state root where the caller knows it (the
    daemon passes its registry's); otherwise this process's configured one.
    """

    if same_directory(root, Path.home()):
        return "home"
    directory = root / WORKSPACE_DIRECTORY
    daemon_root = daemon_state_root() if state_root is None else state_root
    if _within(directory, daemon_root) or _within(daemon_root, directory):
        return "state_root"
    for name in LEGACY_INSTANCE_FILES:
        if (directory / name).exists():
            return name
    return None


def ensure_workspace_directory(root: Path, *, state_root: Path | None = None) -> Path:
    """Refuse a workspace root whose ``.cruxible`` is not a workspace directory."""

    daemon_root = daemon_state_root() if state_root is None else state_root
    reason = workspace_directory_conflict(root, state_root=daemon_root)
    if reason is not None:
        raise WorkspaceDirectoryConflict(workspace=root, reason=reason, state_root=daemon_root)
    return root


def workspace_path(root: Path, *parts: str) -> Path:
    """``root/.cruxible/<parts>``, after refusing a root that is no workspace."""

    return ensure_workspace_directory(root) / WORKSPACE_DIRECTORY / Path(*parts)


__all__ = [
    "FLOOR_PATH",
    "LEGACY_INSTANCE_FILES",
    "STATE_ROOT_ENV",
    "WORKSPACE_DIRECTORY",
    "WorkspaceDirectoryConflict",
    "WorkspaceError",
    "daemon_state_root",
    "ensure_workspace_directory",
    "same_directory",
    "workspace_directory_conflict",
    "workspace_path",
]
