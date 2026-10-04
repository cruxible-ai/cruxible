"""The floor's rendering rule, named by digest.

Two floors made by the same renderer at the same generation are the same
bytes. A change to how any floor file renders bumps the revision, and a client
holding a floor of another renderer is sent the whole floor, never a delta.
"""

from __future__ import annotations

from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.floor import FLOOR_FORMAT

FLOOR_RENDERER_REVISION = "playbill-floor-renderer-v5.3"


def floor_renderer(compiler_digest: str) -> str:
    """The rendering rule a floor was made with: format revision plus compiler."""

    return typed_digest(
        Sha256Value,
        "playbill-floor-renderer-v1",
        {
            "format": FLOOR_FORMAT,
            "revision": FLOOR_RENDERER_REVISION,
            "compiler_digest": compiler_digest,
        },
    ).tagged


__all__ = ["FLOOR_RENDERER_REVISION", "floor_renderer"]
