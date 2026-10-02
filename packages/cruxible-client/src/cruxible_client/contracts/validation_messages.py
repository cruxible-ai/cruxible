"""Validation failures as a caller reads them: JSON paths and messages (rule R10).

``str(pydantic.ValidationError)`` names internal model types, carries
documentation URLs and spans many lines. Every surface renders a validation
failure through these helpers instead, so what reaches a user is where in their
input the fault is and what is wrong there.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import ValidationError

_MAX_LINES = 10

# Pydantic's internal validator names that appear as location segments: they
# name how a model is validated, never where in the caller's input the fault is.
_INTERNAL_SEGMENT = re.compile(
    r"^(function-(after|before|wrap|plain)\[.*\]|constrained-\w+|tagged-union\[.*\]|"
    r"union\[.*\]|json-or-python\[.*\]|lax-or-strict\[.*\]|chain\[.*\]|"
    r"is-instance\[.*\]|default\[.*\]|nullable\[.*\]|playbill-[a-z0-9-]+)$"
)


def internal_validation_segment(segment: object) -> bool:
    """Whether a location segment names a pydantic validator, not the caller's input."""

    return isinstance(segment, str) and bool(_INTERNAL_SEGMENT.match(segment))


def validation_path(location: Iterable[object]) -> str:
    """One validation location as a JSON path (``$.a[0].b``), internals dropped."""

    rendered = "$"
    for item in location:
        if isinstance(item, int):
            rendered += f"[{item}]"
        elif isinstance(item, str) and not _INTERNAL_SEGMENT.match(item):
            rendered += f".{item}"
    return rendered


def validation_lines(
    error: ValidationError | Iterable[Mapping[str, Any]], *, limit: int = _MAX_LINES
) -> list[str]:
    """Each validation failure as ``$.path: message`` -- never pydantic's own dump.

    ``str(ValidationError)`` names internal model types and carries
    documentation URLs; this keeps what the caller can act on: where in their
    input, and what is wrong there (rule R10). At most ``limit`` lines, then a
    count of the rest.
    """

    items: list[Mapping[str, Any]] = (
        list(error.errors(include_url=False, include_context=False, include_input=False))
        if isinstance(error, ValidationError)
        else list(error)
    )
    lines: list[str] = []
    for item in items:
        line = f"{validation_path(item.get('loc') or ())}: {item.get('msg', 'invalid')}"
        if line not in lines:
            lines.append(line)
    if len(lines) > limit:
        return [*lines[:limit], f"... and {len(lines) - limit} more"]
    return lines


def validation_summary(error: ValidationError | Iterable[Mapping[str, Any]]) -> str:
    """``validation_lines`` joined for a one-line message."""

    return "; ".join(validation_lines(error))


__all__ = [
    "internal_validation_segment",
    "validation_lines",
    "validation_path",
    "validation_summary",
]
