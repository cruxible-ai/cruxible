"""Inputs the 0.4.0 removals retired, and the refusal every boundary owes them.

The removals took these keys off every request model, tool signature and CLI
option. That alone is not enough: HTTP and MCP request parsing IGNORE unknown
keys, so a stale caller that kept sending one got a success response and no
hint that its input had been dropped. For ``group_override=True`` -- an input
whose whole point was to hold an edge for review -- silently discarding it and
answering ``200`` is unsafe.

So the named keys are REFUSED, per surface, by name. This is deliberately not
``extra="forbid"``: an unrelated unknown key stays tolerated exactly as before,
and only the inputs that were once accepted and carried meaning now fail.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

RETIRED_IN_VERSION = "0.4.0"

RETIRED_INPUT_REPLACEMENTS: dict[str, str | None] = {
    "source": "actor_context",
    "opened_by": "actor_context",
    "proposed_by": "actor_context",
    "resolved_by": "actor_context",
    # Retired with NO public equivalent. ``force_review`` is a service-layer,
    # per-proposal argument, not a persistent edge-level flag, so naming it as
    # the replacement would misdescribe what a caller gets back.
    "group_override": None,
}

_DERIVED_ACTOR_INPUTS = frozenset({"source", "opened_by", "proposed_by", "resolved_by"})
"""Retired declared-actor claims. The same NAME survives as a derived read field."""

_RETIREMENT_GUIDANCE: dict[str, str] = {
    "group_override": (
        "persistent edge-level group override is retired with no public "
        "replacement (force_review is a service-layer, per-proposal argument, "
        "not an equivalent); historical stored flags still read and still "
        "govern review"
    ),
}


def retired_input_message(key: str) -> str:
    """Return the one refusal sentence every boundary uses for one retired key."""
    replacement = RETIRED_INPUT_REPLACEMENTS.get(key)
    if replacement is None:
        return (
            f"'{key}' was removed in {RETIRED_IN_VERSION} and is no longer "
            f"accepted; {_RETIREMENT_GUIDANCE[key]}."
        )
    derived = (
        f" (the declared-actor kind is derived from it and still read back as '{key}')"
        if key in _DERIVED_ACTOR_INPUTS
        else ""
    )
    return (
        f"'{key}' was removed in {RETIRED_IN_VERSION} and is no longer "
        f"accepted; pass '{replacement}' instead{derived}."
    )


def find_retired_inputs(payload: Any, keys: Iterable[str]) -> list[str]:
    """Return the retired keys present in a raw request payload, in ``keys`` order."""
    if not isinstance(payload, Mapping):
        return []
    return [key for key in keys if key in payload]


def refuse_retired_inputs(payload: Any, keys: Iterable[str]) -> None:
    """Raise ``ValueError`` naming the first retired key a payload still carries.

    ``ValueError`` is what a pydantic validator needs to turn this into an
    ordinary field-level validation failure (a 422 through FastAPI). Boundaries
    that do not validate through pydantic call :func:`find_retired_inputs` and
    raise their own idiom instead.
    """
    present = find_retired_inputs(payload, keys)
    if present:
        raise ValueError(retired_input_message(present[0]))
