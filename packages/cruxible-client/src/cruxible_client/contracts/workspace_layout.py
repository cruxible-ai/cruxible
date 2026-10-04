"""Frozen paths in a repository attached to one Cruxible instance."""

from __future__ import annotations

from pathlib import Path

#: The workspace directory: client custody, sources, manifests and the floor.
WORKSPACE_DIRECTORY = ".cruxible"
FLOOR_PATH = ".cruxible/floor"
#: What a 0.3-era per-project instance kept in the same directory name.
LEGACY_INSTANCE_FILES = ("instance.json", "state.db")


def workspace_directory_conflict(root: Path) -> str | None:
    """Why ``root/.cruxible`` cannot be this workspace's directory, or None.

    The home directory's ``.cruxible`` is the daemon state root, and a
    directory holding a 0.3 instance is that instance, not a workspace.
    """

    if root == Path.home().resolve():
        return "home"
    for name in LEGACY_INSTANCE_FILES:
        if (root / WORKSPACE_DIRECTORY / name).exists():
            return name
    return None


__all__ = [
    "FLOOR_PATH",
    "LEGACY_INSTANCE_FILES",
    "WORKSPACE_DIRECTORY",
    "workspace_directory_conflict",
]
