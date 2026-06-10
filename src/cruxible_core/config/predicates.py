"""Shared config models for structured predicate maps."""

from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import RootModel, model_validator

PredicateOperator = Literal[
    "eq",
    "ne",
    "in",
    "not_in",
    "lt",
    "lte",
    "gt",
    "gte",
    "exists",
    "contains",
    "icontains",
]

PREDICATE_OPERATORS = set(get_args(PredicateOperator))


class StructuredPredicateSpec(RootModel[dict[str, dict[str, Any]]]):
    """Generic structured predicate map for config-authored predicates."""

    @model_validator(mode="after")
    def validate_predicates(self) -> StructuredPredicateSpec:
        if not self.root:
            msg = "predicate map must not be empty"
            raise ValueError(msg)
        for path, operators in self.root.items():
            if not path or not path.strip():
                msg = "predicate paths must be non-empty strings"
                raise ValueError(msg)
            if not isinstance(operators, dict) or not operators:
                msg = f"predicate path '{path}' must define at least one operator"
                raise ValueError(msg)
            for operator, value in operators.items():
                if operator not in PREDICATE_OPERATORS:
                    allowed = ", ".join(sorted(PREDICATE_OPERATORS))
                    msg = f"unsupported predicate operator '{operator}'. Allowed: {allowed}"
                    raise ValueError(msg)
                if operator == "exists" and not isinstance(value, bool):
                    msg = "predicate operator 'exists' requires a boolean value"
                    raise ValueError(msg)
                if operator in {"contains", "icontains"} and not isinstance(value, str):
                    msg = f"predicate operator '{operator}' requires a string value"
                    raise ValueError(msg)
        return self


__all__ = [
    "PREDICATE_OPERATORS",
    "PredicateOperator",
    "StructuredPredicateSpec",
]
