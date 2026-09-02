"""Closed clock taxonomy for Playbill contract and runtime fields.

The names here are wire-law language.  A time-bearing field has exactly one
domain; code may order values within a domain, test an instant for membership
in a validity window, or derive a window from an instant and duration.  It may
not use ordering between two instant domains as a law input.
"""

from __future__ import annotations

from typing import Literal, TypeAlias

ClockDomainV1: TypeAlias = Literal[
    "SETTLEMENT ORDER",
    "ASSERTION TIME",
    "VALIDITY WINDOW",
    "EVALUATION INSTANT",
]

CLOCK_DOMAINS: frozenset[ClockDomainV1] = frozenset(
    {
        "SETTLEMENT ORDER",
        "ASSERTION TIME",
        "VALIDITY WINDOW",
        "EVALUATION INSTANT",
    }
)

TIME_FIELD_SUFFIXES = (
    "_at",
    "_time",
    "_instant",
    "_seconds",
    "_microseconds",
    "_until",
    "generation",
    "sequence",
)


def classify_clock_field(name: str, annotation: str) -> ClockDomainV1 | None:
    """Classify exactly the Phase-B ruled AST discovery predicate."""

    discovered = (
        "datetime" in annotation
        or "timedelta" in annotation
        or "CanonicalDuration" in annotation
        or name.endswith(TIME_FIELD_SUFFIXES)
    )
    if not discovered:
        return None
    if name.endswith(("generation", "sequence")):
        return "SETTLEMENT ORDER"
    if name.endswith(("_seconds", "_microseconds")) or any(
        token in name for token in ("valid", "expire", "retention", "retain", "duration", "window")
    ):
        return "VALIDITY WINDOW"
    if any(token in name for token in ("evaluation", "evaluated", "prepared", "check_at")):
        return "EVALUATION INSTANT"
    return "ASSERTION TIME"


def clock_description(domain: ClockDomainV1) -> str:
    """Return the canonical Pydantic Field-description sentence."""

    return f"Reads {domain}."


__all__ = [
    "CLOCK_DOMAINS",
    "TIME_FIELD_SUFFIXES",
    "ClockDomainV1",
    "classify_clock_field",
    "clock_description",
]
