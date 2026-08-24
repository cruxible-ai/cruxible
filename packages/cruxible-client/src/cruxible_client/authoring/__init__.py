"""Client-owned Playbill authoring, SDK, and workspace adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cruxible_client.authoring.sdk import Playbill

__all__ = ["Playbill"]


def __getattr__(name: str) -> Any:
    if name == "Playbill":
        from cruxible_client.authoring.sdk import Playbill

        return Playbill
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
