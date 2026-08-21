"""Client package for talking to a governed Cruxible daemon."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cruxible_client.http_client import CruxibleClient

__all__ = [
    "CruxibleClient",
    "PlaybillInsertionApplication",
    "PlaybillInsertionApplyError",
    "apply_playbill_insertion",
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
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
