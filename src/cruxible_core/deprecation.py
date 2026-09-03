"""Structured deprecation notices and surface-specific emitters.

The notice body is deliberately dependency-free and identical everywhere:
``surface``, ``replacement``, and ``removal_version``.  Transport adapters may
choose where that body travels, but they must not invent a second shape.
"""

from __future__ import annotations

import json
import sys
import warnings
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, TextIO

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


SUBJECT_GET_TWO_ARGUMENT_FORM = DeprecationNotice(
    surface="playbill subject get KIND ID two-argument form",
    replacement="one `kind/name` Subject address argument",
)
SUBJECT_HISTORY_TWO_ARGUMENT_FORM = DeprecationNotice(
    surface="playbill subject history KIND ID two-argument form",
    replacement="one `kind/name` Subject address argument",
)

DEPRECATION_REGISTRY: tuple[DeprecationNotice, ...] = (
    SUBJECT_GET_TWO_ARGUMENT_FORM,
    SUBJECT_HISTORY_TWO_ARGUMENT_FORM,
)
"""Every warning-emitting deprecation registered by cruxible-core."""


def serialize_deprecation(notice: DeprecationNotice) -> str:
    """Serialize one notice deterministically for line- and header-based idioms."""
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


def emit_python_deprecation(notice: DeprecationNotice, *, stacklevel: int = 2) -> None:
    """Warn direct Python callers without making transport adapters depend on them."""
    warnings.warn(
        serialize_deprecation(notice),
        DeprecationWarning,
        stacklevel=stacklevel,
    )


def _payload_dict(result: Any) -> dict[str, Any]:
    if hasattr(result, "model_dump"):
        return dict(result.model_dump(mode="json"))
    if is_dataclass(result) and not isinstance(result, type):
        return asdict(result)
    return dict(result)


def attach_mcp_deprecations(
    result: Any,
    notices: list[DeprecationNotice] | tuple[DeprecationNotice, ...],
) -> dict[str, Any]:
    """Attach structured warnings using the MCP envelope's existing idiom."""
    payload = _payload_dict(result)
    if not notices:
        return payload
    key = "warnings" if "warnings" in payload else "deprecation_warnings"
    existing = list(payload.get(key) or [])
    payload[key] = [*existing, *(notice.as_dict() for notice in notices)]
    return payload


def emit_http_deprecations(
    response: Any,
    result: Any,
    notices: list[DeprecationNotice] | tuple[DeprecationNotice, ...],
) -> Any:
    """Set response headers and add body entries only to existing warning envelopes."""
    for notice in notices:
        response.headers.append("Deprecation", serialize_deprecation(notice))
    if not notices:
        return result

    payload = _payload_dict(result)
    if "warnings" not in payload:
        return result
    existing = list(payload.get("warnings") or [])
    payload["warnings"] = [*existing, *(notice.as_dict() for notice in notices)]
    return payload
