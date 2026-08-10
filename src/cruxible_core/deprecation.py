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

DEFAULT_REMOVAL_VERSION = "0.5.0"
"""Earliest release a NEWLY registered deprecation may honestly name.

0.4 is the release under development, so a notice that took the old default
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


LEGACY_OUTCOME_RECORD = DeprecationNotice(
    surface="legacy outcome record functions",
    replacement="resolution contracts and attestations",
    removal_version="0.5.0",
)
LEGACY_OUTCOME_PROFILE = DeprecationNotice(
    surface="legacy outcome profile functions",
    replacement="resolution contract declarations",
    removal_version="0.5.0",
)
"""Rescheduled from 0.4.0 to 0.5.0 by maintainer ruling, stated EXPLICITLY.

Both were stamped 0.4.0 and both survived the 0.4.0 sweep, because the named
replacement does not exist yet: resolution contracts carry no equivalent of an
outcome profile's coded vocabulary, its ``required_scope_keys``, or the
profile-drift analysis ``analyze outcomes`` reports. Porting that machinery is
post-Playbill work, so the window moves rather than the surface. Written out
instead of left on the default so the commitment survives a default change.
"""
PROCEDURE_STRING_WARNINGS = DeprecationNotice(
    surface="ProcedureTransitionResult.warnings string list",
    replacement="ProcedureTransitionResult.typed_warnings",
    removal_version="0.5.0",
)
"""Dual-emitted through 0.4 per ``dd-deprecation-policy`` class (3).

An OUTPUT surface, so no call site can emit a notice honestly: the field is
always populated and nothing observes whether a caller read it. The registry
entry and the schedule row are the whole mechanism here -- the transport
emitters exist for INPUT deprecations, where a caller's use is visible.
"""

DEPRECATION_REGISTRY: tuple[DeprecationNotice, ...] = (
    LEGACY_OUTCOME_RECORD,
    LEGACY_OUTCOME_PROFILE,
    PROCEDURE_STRING_WARNINGS,
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
