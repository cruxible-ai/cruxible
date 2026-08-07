"""The guard predicate grammar -- typed predicates, never embedded code.

Total static analyzability is the differentiator, so the predicate language is
a CLOSED grammar with three connectives and a fixed operand vocabulary. There
is no arithmetic, no string function, no user-defined predicate, and no
composition beyond ``all_of`` / ``any_of`` / ``not_of``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from cruxible_core.config.schema import CountSelector, FilterComparisonOp
from cruxible_core.predicate import PredicateValueType

DERIVED_ACCESSORS: frozenset[str] = frozenset({"count", "exists", "truncated"})
"""The three accessors that supersede the derived assert kinds.

``assert_count`` / ``assert_exists`` / ``assert_not_truncated`` are the same
predicate with a left-hand accessor over metadata the engine already builds.
Adding a member requires a decision record; the closure guardrail pins the set.
"""

__all__ = [
    "DERIVED_ACCESSORS",
    "CountSelector",
    "FilterComparisonOp",
    "GuardSpec",
    "PredicateValueType",
]


class GuardSpec(BaseModel):
    """A predicate over one decision point.

    ``pred := cmp | "all_of" [pred+] | "any_of" [pred+] | "not_of" pred``

    The exactly-one-production closure and the typed operand union land with
    the refusal that enforces them; this commit declares the shape the guard
    node carries so the node kind and its handler can be staged together.
    """

    left: Any | None = None
    op: FilterComparisonOp | None = None
    right: Any | None = None
    value_type: PredicateValueType | None = None
    all_of: list[GuardSpec] | None = None
    any_of: list[GuardSpec] | None = None
    not_of: GuardSpec | None = Field(default=None)

    model_config = ConfigDict(extra="forbid")
