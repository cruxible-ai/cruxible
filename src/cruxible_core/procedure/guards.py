"""The guard predicate grammar -- typed predicates, never embedded code.

Total static analyzability is the differentiator, so the predicate language is
a CLOSED grammar::

    pred    := cmp | "all_of" [pred+] | "any_of" [pred+] | "not_of" pred
    cmp     := operand OP operand [ ":" value_type ]
    operand := literal
             | "$input" ["." path]
             | "$steps." alias ["." path]
             | "count(" alias "," selector ")"
             | "exists(" ref ")"
             | "truncated(" alias ")"
             | "@param:" name

No arithmetic, no string functions, no user-defined predicates, no composition
beyond the three connectives, and no ``$item`` -- per-item payloads do not
exist at branch time.

The operand WIRE form is the scalar the BNF describes; :class:`PredicateOperand`
is its parsed, discriminated form, which is what every analysis consumes. A
model union on the wire would make the terse form above unwritable.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from cruxible_core.config.schema import CountSelector, FilterComparisonOp
from cruxible_core.predicate import PredicateValueType

DERIVED_ACCESSORS: frozenset[str] = frozenset({"count", "exists", "truncated"})
"""The three accessors that supersede the derived assert kinds.

``assert_count`` / ``assert_exists`` / ``assert_not_truncated`` are the same
predicate with a left-hand accessor over metadata the engine already builds.
Adding a member requires a decision record; the closure guardrail pins the set.
"""

PREDICATE_OPERAND_FORMS: frozenset[str] = frozenset(
    {"literal", "input_path", "steps_path", "count", "exists", "truncated", "param"}
)
"""The seven operand forms, and no eighth is reachable."""

PARAMETER_REFERENCE_PREFIX = "@param:"

_COUNT_PATTERN = re.compile(r"^count\(\s*([^,()\s]+)\s*,\s*([^,()\s]+)\s*\)$")
_EXISTS_PATTERN = re.compile(r"^exists\(\s*([^()]+?)\s*\)$")
_TRUNCATED_PATTERN = re.compile(r"^truncated\(\s*([^,()\s]+)\s*\)$")
_ACCESSOR_PREFIX = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(")
_PARAMETER_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_ALIAS_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
"""A step alias is a bare name.

The accessor regexes below match a permissive character class so a
malformed argument produces a NAMED refusal instead of a confusing
"is not count(<alias>, <selector>)"; the name itself is checked here.
Without this, ``count(rows+1, items)`` parses and the arithmetic rides
through the one position that took free text."""
_REFERENCE_PATH = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\[\d+\])*(\.[A-Za-z_][A-Za-z0-9_]*(\[\d+\])*)*$"
)
"""A reference path is a dotted chain of names with optional integer indices.

Nothing else. Without this the operand ``"$steps.rows.count + 1"`` parses
happily as a path named ``count + 1`` -- an arithmetic expression admitted
through the one operand form that takes free text, which is exactly how a
closed grammar stops being closed."""

PredicateOperandForm = Literal[
    "literal", "input_path", "steps_path", "count", "exists", "truncated", "param"
]


class PredicateOperand(BaseModel):
    """One parsed guard operand, discriminated by ``form``."""

    form: PredicateOperandForm
    literal: str | int | float | bool | None = None
    path: str | None = None
    alias: str | None = None
    selector: CountSelector | None = None
    ref: str | None = None
    parameter_name: str | None = None

    model_config = ConfigDict(extra="forbid", frozen=True)


class PredicateGrammarError(ValueError):
    """An operand outside the closed grammar."""


def parse_predicate_operand(raw: Any) -> PredicateOperand:
    """Parse one wire operand into its discriminated form, or refuse.

    Refusing here is what keeps the grammar closed: an unrecognised
    ``something(...)`` call is a would-be eighth operand form, and admitting it
    is how a typed predicate language becomes a worse Python.
    """
    if not isinstance(raw, str):
        if isinstance(raw, str | int | float | bool):
            return PredicateOperand(form="literal", literal=raw)
        raise PredicateGrammarError(
            f"guard operands are scalars or grammar references; got {type(raw).__name__}"
        )

    if raw.startswith("$item"):
        raise PredicateGrammarError(
            "'$item' is not a guard operand: per-item payloads do not exist at "
            "branch time, so a predicate over one cannot be evaluated or analysed"
        )
    if raw == "$input":
        return PredicateOperand(form="input_path", path=None)
    if raw.startswith("$input."):
        path = raw[len("$input.") :]
        _require_reference_path(raw, path)
        return PredicateOperand(form="input_path", path=path)
    if raw.startswith("$steps."):
        remainder = raw[len("$steps.") :]
        _require_reference_path(raw, remainder)
        alias = remainder.split(".", 1)[0].split("[", 1)[0]
        tail = remainder[len(alias) :].lstrip(".")
        return PredicateOperand(form="steps_path", alias=alias, path=tail or None)
    if raw.startswith("$"):
        raise PredicateGrammarError(
            f"'{raw}' is not a guard operand; only $input and $steps references exist"
        )
    if raw.startswith(PARAMETER_REFERENCE_PREFIX):
        name = raw[len(PARAMETER_REFERENCE_PREFIX) :]
        if not _PARAMETER_NAME.match(name):
            raise PredicateGrammarError(
                f"'{raw}' does not name a governed parameter (lower_snake_case required)"
            )
        return PredicateOperand(form="param", parameter_name=name)

    accessor = _ACCESSOR_PREFIX.match(raw)
    if accessor is None:
        return PredicateOperand(form="literal", literal=raw)
    name = accessor.group(1)
    if name not in DERIVED_ACCESSORS:
        raise PredicateGrammarError(
            f"'{name}' is not a derived accessor; the accessor set is "
            f"{sorted(DERIVED_ACCESSORS)} and adding a member requires a decision record"
        )
    if name == "count":
        matched = _COUNT_PATTERN.match(raw)
        if matched is None:
            raise PredicateGrammarError(f"'{raw}' is not 'count(<alias>, <selector>)'")
        alias, selector = matched.group(1), matched.group(2)
        _require_alias_name(raw, alias)
        if selector not in {"returned_results", "total_results", "items", "results"}:
            raise PredicateGrammarError(
                f"'{selector}' is not a count selector; the selectors are "
                "returned_results, total_results, items, results"
            )
        return PredicateOperand(form="count", alias=alias, selector=cast_count_selector(selector))
    if name == "exists":
        matched = _EXISTS_PATTERN.match(raw)
        if matched is None:
            raise PredicateGrammarError(f"'{raw}' is not 'exists(<ref>)'")
        ref = matched.group(1)
        # The argument is a REFERENCE, parsed by the same grammar as any other
        # operand. Without this, `exists(always_true)` reads its argument as a
        # bare literal that resolves to itself and is therefore always
        # present -- a constant-true branch wearing the costume of a test.
        inner = parse_predicate_operand(ref)
        if inner.form not in {"input_path", "steps_path"}:
            raise PredicateGrammarError(
                f"'{raw}' must test a $input or $steps reference; '{ref}' is a "
                f"{inner.form}, and asking whether a literal exists is a constant, "
                "not a predicate"
            )
        return PredicateOperand(form="exists", ref=ref, alias=inner.alias)
    matched = _TRUNCATED_PATTERN.match(raw)
    if matched is None:
        raise PredicateGrammarError(f"'{raw}' is not 'truncated(<alias>)'")
    alias = matched.group(1)
    _require_alias_name(raw, alias)
    return PredicateOperand(form="truncated", alias=alias)


def _require_alias_name(raw: str, alias: str) -> None:
    if not _ALIAS_NAME.match(alias):
        raise PredicateGrammarError(
            f"'{raw}' names step alias '{alias}', which is not a bare name. "
            "An accessor argument is an alias, not an expression."
        )


def _require_reference_path(raw: str, path: str) -> None:
    if not _REFERENCE_PATH.match(path):
        raise PredicateGrammarError(
            f"'{raw}' is not a reference path. A path is a dotted chain of names "
            "with optional integer indices; there is no arithmetic and no "
            "expression syntax in this grammar."
        )


def cast_count_selector(value: str) -> CountSelector:
    """Narrow a validated selector string to its literal type."""
    if value == "returned_results":
        return "returned_results"
    if value == "total_results":
        return "total_results"
    if value == "items":
        return "items"
    return "results"


def _require_operand_in_grammar(value: Any) -> Any:
    parse_predicate_operand(value)
    return value


GuardOperand = Annotated[str | int | float | bool, AfterValidator(_require_operand_in_grammar)]
"""A guard operand on the wire: a scalar the closed grammar accepts."""


class GuardSpec(BaseModel):
    """A predicate over one decision point.

    ``extra="forbid"`` and ``min_length=1`` do NOT close a grammar; the
    exactly-one-production validator does. Without it this model admits ``{}``
    (no production at all -- a vacuous constant), ``{"left": ...}`` (half a
    comparison, silently evaluating against ``op=None``), and
    ``{"all_of": [...], "not_of": {...}}`` (two productions with no rule for
    which wins). All three are outside the declared BNF, and a reviewer cannot
    see any of them.
    """

    left: GuardOperand | None = None
    op: FilterComparisonOp | None = None
    right: GuardOperand | None = None
    value_type: PredicateValueType | None = None
    all_of: list[GuardSpec] | None = Field(default=None, min_length=1)
    any_of: list[GuardSpec] | None = Field(default=None, min_length=1)
    not_of: GuardSpec | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_exactly_one_production(self) -> GuardSpec:
        """The BNF, enforced. R18."""
        triple = (self.left, self.op, self.right)
        set_count = sum(part is not None for part in triple)
        if set_count not in (0, 3):
            raise ValueError(
                f"a guard comparison requires left, op and right together; got {set_count} of 3"
            )
        has_comparison = set_count == 3
        productions = (
            has_comparison,
            self.all_of is not None,
            self.any_of is not None,
            self.not_of is not None,
        )
        if sum(productions) != 1:
            raise ValueError(
                "a guard predicate must declare exactly one production: "
                "a complete comparison, all_of, any_of, or not_of"
            )
        if self.value_type is not None and not has_comparison:
            raise ValueError("value_type belongs to a comparison, not a connective")
        return self

    def operands(self) -> list[PredicateOperand]:
        """Return every parsed operand in this predicate, connectives included."""
        if self.all_of is not None:
            return [operand for child in self.all_of for operand in child.operands()]
        if self.any_of is not None:
            return [operand for child in self.any_of for operand in child.operands()]
        if self.not_of is not None:
            return self.not_of.operands()
        return [
            parse_predicate_operand(self.left),
            parse_predicate_operand(self.right),
        ]


__all__ = [
    "DERIVED_ACCESSORS",
    "PARAMETER_REFERENCE_PREFIX",
    "PREDICATE_OPERAND_FORMS",
    "CountSelector",
    "FilterComparisonOp",
    "GuardOperand",
    "GuardSpec",
    "PredicateGrammarError",
    "PredicateOperand",
    "PredicateOperandForm",
    "PredicateValueType",
    "parse_predicate_operand",
]
