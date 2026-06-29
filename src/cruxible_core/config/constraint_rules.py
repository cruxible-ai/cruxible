"""Constraint rule parser shared by config validation and graph evaluation."""

from __future__ import annotations

import re
from dataclasses import dataclass

from cruxible_core.predicate import COMPARISON_SYMBOL_PATTERN

_TOKEN = r"[\w-]+"
_CONSTRAINT_PATTERN = re.compile(
    rf"^({_TOKEN})\.FROM\.({_TOKEN})\s*{COMPARISON_SYMBOL_PATTERN}\s*\1\.TO\.({_TOKEN})$"
)


@dataclass(frozen=True, slots=True)
class ParsedConstraintRule:
    """Parsed top-level constraint rule."""

    relationship: str
    from_property: str
    operator: str
    to_property: str


def parse_constraint_rule(rule: str) -> ParsedConstraintRule | None:
    """Parse a constraint rule into a structured representation.

    Returns None if the rule doesn't match the supported syntax.
    """
    match = _CONSTRAINT_PATTERN.match(rule)
    if not match:
        return None
    return ParsedConstraintRule(
        relationship=match.group(1),
        from_property=match.group(2),
        operator=match.group(3),
        to_property=match.group(4),
    )
