"""Client package for talking to a governed Cruxible daemon."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cruxible_client.http_client import CruxibleClient
    from cruxible_client.playbill_workspace import (
        PlaybillWorkspaceError,
        activate_with_workspace_refresh,
        inspect_workspace_floor,
        materialize_playbill_floor,
        observe_playbill_next_workspace,
    )

__all__ = [
    "CruxibleClient",
    "PlaybillInsertionApplication",
    "PlaybillInsertionApplyError",
    "PlaybillWorkspaceError",
    "activate_with_workspace_refresh",
    "apply_playbill_insertion",
    "inspect_workspace_floor",
    "observe_playbill_next_workspace",
    "materialize_playbill_floor",
    "prepare_playbill_brief",
]

__version__ = "0.4.0"


def __getattr__(name: str) -> Any:
    """Load the HTTP client only when the public client class is requested."""
    if name == "CruxibleClient":
        from cruxible_client.http_client import CruxibleClient

        return CruxibleClient
    if name in {
        "PlaybillInsertionApplication",
        "PlaybillInsertionApplyError",
        "apply_playbill_insertion",
    }:
        from cruxible_client import playbill_insertions

        return getattr(playbill_insertions, name)
    if name == "prepare_playbill_brief":
        from cruxible_client.playbill_briefs import prepare_playbill_brief

        return prepare_playbill_brief
    if name in {
        "PlaybillWorkspaceError",
        "activate_with_workspace_refresh",
        "inspect_workspace_floor",
        "observe_playbill_next_workspace",
        "materialize_playbill_floor",
    }:
        from cruxible_client import playbill_workspace

        return getattr(playbill_workspace, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
