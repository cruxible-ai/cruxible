"""Client package for talking to a governed Cruxible daemon."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cruxible_client.http_client import CruxibleClient

__all__ = ["CruxibleClient"]

__version__ = "0.2.0"


def __getattr__(name: str) -> Any:
    """Load the HTTP client only when the public client class is requested."""
    if name == "CruxibleClient":
        from cruxible_client.http_client import CruxibleClient

        return CruxibleClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
