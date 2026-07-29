"""Structured deprecation notices and surface-specific emitters.

The notice body is deliberately dependency-free and identical everywhere:
``surface``, ``replacement``, and ``removal_version``.  Transport adapters may
choose where that body travels, but they must not invent a second shape.
"""

from __future__ import annotations

import json
import sys
import warnings
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, TextIO

DEFAULT_REMOVAL_VERSION = "0.4.0"


@dataclass(frozen=True)
class DeprecationNotice:
    """One public deprecation warning."""

    surface: str
    replacement: str
    removal_version: str = DEFAULT_REMOVAL_VERSION

    def as_dict(self) -> dict[str, str]:
        """Return the one transport-neutral warning shape."""
        return asdict(self)


FLAG_FEEDBACK_ACTION = DeprecationNotice(
    surface="feedback action 'flag'",
    replacement="attest --stance contradict",
)
APPROVE_FEEDBACK_ACTION = DeprecationNotice(
    surface="feedback action 'approve'",
    replacement="feedback action 'accept'",
)
LEGACY_OUTCOME_RECORD = DeprecationNotice(
    surface="legacy outcome record functions",
    replacement="resolution contracts and attestations",
)
LEGACY_OUTCOME_PROFILE = DeprecationNotice(
    surface="legacy outcome profile functions",
    replacement="resolution contract declarations",
)
GROUP_OVERRIDE = DeprecationNotice(
    surface="feedback group_override write path",
    replacement="force_review",
)
FEEDBACK_SOURCE_INPUT = DeprecationNotice(
    surface="FeedbackRecord.source input",
    replacement="actor_context",
)
OUTCOME_SOURCE_INPUT = DeprecationNotice(
    surface="OutcomeRecord.source input",
    replacement="actor_context",
)
GROUP_RESOLVED_BY_INPUT = DeprecationNotice(
    surface="GroupResolution.resolved_by input",
    replacement="resolved_actor_context",
)
GROUP_PROPOSED_BY_INPUT = DeprecationNotice(
    surface="CandidateGroup.proposed_by input",
    replacement="proposed_actor_context",
)
DECISION_OPENED_BY_INPUT = DeprecationNotice(
    surface="DecisionRecord.opened_by input",
    replacement="opened_actor_context",
)

DEPRECATION_REGISTRY: tuple[DeprecationNotice, ...] = (
    FLAG_FEEDBACK_ACTION,
    APPROVE_FEEDBACK_ACTION,
    LEGACY_OUTCOME_RECORD,
    LEGACY_OUTCOME_PROFILE,
    GROUP_OVERRIDE,
    FEEDBACK_SOURCE_INPUT,
    OUTCOME_SOURCE_INPUT,
    GROUP_RESOLVED_BY_INPUT,
    GROUP_PROPOSED_BY_INPUT,
    DECISION_OPENED_BY_INPUT,
)
"""Every warning-emitting deprecation registered by cruxible-core."""


def serialize_deprecation(notice: DeprecationNotice) -> str:
    """Serialize one notice deterministically for line- and header-based idioms."""
    return json.dumps(notice.as_dict(), separators=(",", ":"), sort_keys=True)


def deprecation_refusal_message(notice: DeprecationNotice, detail: str) -> str:
    """Build a refusal that still carries the standard structured warning."""
    return f"Deprecated surface refused: {serialize_deprecation(notice)}. {detail}"


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


def accept_deprecated_model_input(
    value: Any,
    *,
    field: str,
    notice: DeprecationNotice,
) -> Any:
    """Strip one ignored legacy model field after emitting its notice."""
    if not isinstance(value, Mapping) or field not in value:
        return value
    emit_python_deprecation(notice, stacklevel=3)
    payload = dict(value)
    payload.pop(field)
    return payload


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
