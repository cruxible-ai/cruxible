"""Structured deprecation notices and the CLI emitter.

The notice body is deliberately dependency-free and identical everywhere:
``surface``, ``replacement``, and ``removal_version``.  Transport adapters may
choose where that body travels, but they must not invent a second shape.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from typing import TextIO

DEFAULT_REMOVAL_VERSION = "0.6.0"
"""Earliest release a NEWLY registered deprecation may honestly name.

0.5 is the release under development, so a notice that took the old default
promised removal in the very release it was born into -- past due on day one.
The default now names the release after that; anything else states its own.
"""


@dataclass(frozen=True)
class DeprecationNotice:
    """One public deprecation warning."""

    surface: str
    replacement: str
    removal_version: str = DEFAULT_REMOVAL_VERSION

    def as_dict(self) -> dict[str, str]:
        """Return the one transport-neutral warning shape."""
        return asdict(self)


DEPRECATION_REGISTRY: tuple[DeprecationNotice, ...] = ()
"""Every warning-emitting deprecation registered by cruxible-core."""


def serialize_deprecation(notice: DeprecationNotice) -> str:
    """Serialize one notice deterministically as one line."""
    return json.dumps(notice.as_dict(), separators=(",", ":"), sort_keys=True)


def emit_cli_deprecation(
    notice: DeprecationNotice,
    *,
    stream: TextIO | None = None,
) -> None:
    """Emit exactly one stderr line for one CLI deprecation."""
    print(
        f"Deprecation: {serialize_deprecation(notice)}",
        file=stream or sys.stderr,
    )
