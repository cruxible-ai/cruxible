"""Detach retired projection blocks from workspace pages: one road for the CLI and MCP adapters.

Detaching strips a retired block's marker pair from its page and keeps the body.
It edits workspace pages only, never governed state, and it runs in the adapter
(the daemon never reads client paths). The change is previewed by default and a
commit carrying ``at`` is pinned to the exact page bytes its preview read (R12):
each replacement compare-and-swaps against those bytes, so a page edited in
between refuses rather than being overwritten.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path

from cruxible_client.authoring.blocks import sync_projection_blocks
from cruxible_client.contracts.authoring.models import BlockDetachResult
from cruxible_client.contracts.change_control import StateCoordinate
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.service.change_preview import state_change_scope


def _workspace_relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def workspace_pages_state(root: Path, preimages: Mapping[Path, bytes]) -> StateCoordinate:
    """The state coordinate of the pages a detach edits: each one's exact bytes."""

    return StateCoordinate.of(
        "workspace_pages",
        {
            _workspace_relative(root, path): hashlib.sha256(content).hexdigest()
            for path, content in sorted(preimages.items())
        },
    )


def detach_projection_pages(
    client: CruxibleClient,
    instance_id: str,
    *,
    root: Path,
    pages: Sequence[Path],
    dry_run: bool | None = None,
    at: str | None = None,
) -> BlockDetachResult:
    """Remove retired blocks' markers from ``pages``, keeping their bodies."""

    with state_change_scope(
        dry_run=dry_run,
        at=at,
        kind="direct",
        operation="cruxible.block.detach",
        describe="detaching retired projection blocks",
    ) as change:
        synced = sync_projection_blocks(
            client,
            instance_id,
            workspace=root,
            check=change.previewing,
            detach_paths=pages,
            observe_preimages=lambda preimages: change.observe(
                workspace_pages_state(root, preimages)
            ),
        )
        if change.coordinate is None:
            # A refusal before any page was read (an unattached workspace)
            # read no bytes, and pins (and is checked against) the empty set.
            change.observe(workspace_pages_state(root, {}))
    assert change.coordinate is not None
    return BlockDetachResult(
        status="would_detach" if change.previewing else "detached",
        sync=synced,
        coordinate=change.coordinate,
    )


__all__ = ["detach_projection_pages", "workspace_pages_state"]
