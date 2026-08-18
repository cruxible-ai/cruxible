"""Closed Claim-native query grammar declarations.

The grammar is Subject/Claim addressed: entry rows are Subjects of declared
Subject kinds, edges are relation-Claim predicates, and every compared value is
a Claim object read under the owning QueryDefinition's verdict policy. It
declares meaning only. The evaluator that consumes these declarations lands in
the PC-F query-engine slice; nothing here reads accepted state.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_core.playbill.canonical import canonical_bytes, normalize_canonical

_BINDING_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PARAMETER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PREDICATE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})+$")
_SUBJECT_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})*$")

QueryValueTypeV1 = Literal[
    "string",
    "integer",
    "boolean",
    "decimal",
    "timestamp",
    "subject_reference",
]
QueryComparisonOperatorV1 = Literal["eq", "ne", "gt", "gte", "lt", "lte"]
QueryTraversalDirectionV1 = Literal["forward", "reverse"]
QuerySubjectFieldV1 = Literal["subject_id", "subject_kind"]


class _StrictQueryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _nfc(value: str, *, label: str) -> str:
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{label} must already be NFC-normalized")
    return value


def sorted_unique(value: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    """Require one canonical byte-ordered, duplicate-free identifier tuple."""

    ordered = tuple(sorted(set(value), key=lambda item: item.encode("utf-8")))
    if value != ordered:
        raise ValueError(f"{label} must be sorted by unsigned UTF-8 bytes and unique")
    return value


def _identifier(value: str, pattern: re.Pattern[str], *, label: str) -> str:
    _nfc(value, label=label)
    if not pattern.fullmatch(value):
        raise ValueError(f"{label} is not a canonical identifier")
    return value


def binding_name(value: str, *, label: str = "query binding") -> str:
    """Validate one canonical row binding shared by traversal and includes."""

    return _identifier(value, _BINDING_RE, label=label)


def predicate_name(value: str, *, label: str = "query predicate") -> str:
    """Validate one canonical ClaimType predicate addressed by the grammar."""

    return _identifier(value, _PREDICATE_RE, label=label)


def subject_kind_name(value: str, *, label: str = "query Subject kind") -> str:
    """Validate one canonical Subject kind in the query's declared vocabulary."""

    return _identifier(value, _SUBJECT_KIND_RE, label=label)


@dataclass(frozen=True)
class QueryReferenceInventoryV1:
    """Exact bindings, parameters, and predicates one declaration references."""

    bindings: frozenset[str] = frozenset()
    parameters: frozenset[str] = frozenset()
    predicates: frozenset[str] = frozenset()

    def merged(self, other: "QueryReferenceInventoryV1") -> "QueryReferenceInventoryV1":
        return QueryReferenceInventoryV1(
            bindings=self.bindings | other.bindings,
            parameters=self.parameters | other.parameters,
            predicates=self.predicates | other.predicates,
        )


def _merge(*items: QueryReferenceInventoryV1) -> QueryReferenceInventoryV1:
    result = QueryReferenceInventoryV1()
    for item in items:
        result = result.merged(item)
    return result


class QueryLiteralRefV1(_StrictQueryModel):
    """One canonical constant supplied by the accepted declaration itself."""

    tag: Literal["playbill-query-literal-ref-v1"] = "playbill-query-literal-ref-v1"
    kind: Literal["literal"] = "literal"
    value: object = None

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object) -> object:
        return normalize_canonical(value)

    @property
    def references(self) -> QueryReferenceInventoryV1:
        return QueryReferenceInventoryV1()


class QueryParameterRefV1(_StrictQueryModel):
    """One caller-bound parameter declared by the owning QueryDefinition."""

    tag: Literal["playbill-query-parameter-ref-v1"] = "playbill-query-parameter-ref-v1"
    kind: Literal["parameter"] = "parameter"
    parameter: str

    @field_validator("parameter")
    @classmethod
    def _parameter(cls, value: str) -> str:
        return _identifier(value, _PARAMETER_RE, label="query parameter name")

    @property
    def references(self) -> QueryReferenceInventoryV1:
        return QueryReferenceInventoryV1(parameters=frozenset({self.parameter}))


class QueryClaimValueRefV1(_StrictQueryModel):
    """The object of a bound Subject's Claim for one exact predicate."""

    tag: Literal["playbill-query-claim-value-ref-v1"] = "playbill-query-claim-value-ref-v1"
    kind: Literal["claim_value"] = "claim_value"
    binding: str
    predicate: str

    @field_validator("binding")
    @classmethod
    def _binding(cls, value: str) -> str:
        return binding_name(value)

    @field_validator("predicate")
    @classmethod
    def _predicate(cls, value: str) -> str:
        return predicate_name(value)

    @property
    def references(self) -> QueryReferenceInventoryV1:
        return QueryReferenceInventoryV1(
            bindings=frozenset({self.binding}),
            predicates=frozenset({self.predicate}),
        )


class QuerySubjectFieldRefV1(_StrictQueryModel):
    """A bound Subject's own stable identity, never a Claim-carried property."""

    tag: Literal["playbill-query-subject-field-ref-v1"] = "playbill-query-subject-field-ref-v1"
    kind: Literal["subject_field"] = "subject_field"
    binding: str
    field: QuerySubjectFieldV1

    @field_validator("binding")
    @classmethod
    def _binding(cls, value: str) -> str:
        return binding_name(value)

    @property
    def references(self) -> QueryReferenceInventoryV1:
        return QueryReferenceInventoryV1(bindings=frozenset({self.binding}))


class QueryEvaluationTimeRefV1(_StrictQueryModel):
    """The run's explicit evaluation time; never an implicit wall clock."""

    tag: Literal["playbill-query-evaluation-time-ref-v1"] = "playbill-query-evaluation-time-ref-v1"
    kind: Literal["evaluation_time"] = "evaluation_time"

    @property
    def references(self) -> QueryReferenceInventoryV1:
        return QueryReferenceInventoryV1()


QueryValueRefV1 = Annotated[
    QueryLiteralRefV1
    | QueryParameterRefV1
    | QueryClaimValueRefV1
    | QuerySubjectFieldRefV1
    | QueryEvaluationTimeRefV1,
    Field(discriminator="kind"),
]


class QueryComparisonFilterV1(_StrictQueryModel):
    """One typed comparison; the declared value type is never inferred at run time."""

    tag: Literal["playbill-query-comparison-filter-v1"] = "playbill-query-comparison-filter-v1"
    kind: Literal["comparison"] = "comparison"
    left: QueryValueRefV1
    operator: QueryComparisonOperatorV1
    right: QueryValueRefV1
    value_type: QueryValueTypeV1

    @property
    def references(self) -> QueryReferenceInventoryV1:
        return _merge(self.left.references, self.right.references)


class QueryMembershipFilterV1(_StrictQueryModel):
    """Set membership over an explicit, nonempty, canonically ordered value list."""

    tag: Literal["playbill-query-membership-filter-v1"] = "playbill-query-membership-filter-v1"
    kind: Literal["membership"] = "membership"
    left: QueryValueRefV1
    values: tuple[QueryValueRefV1, ...]
    value_type: QueryValueTypeV1
    negated: bool = False

    @field_validator("values")
    @classmethod
    def _values(cls, value: tuple[QueryValueRefV1, ...]) -> tuple[QueryValueRefV1, ...]:
        encoded = tuple(canonical_bytes(item.model_dump(mode="json")) for item in value)
        if not value:
            raise ValueError("query membership filter requires at least one candidate value")
        if encoded != tuple(sorted(set(encoded))):
            raise ValueError("query membership values must be sorted by canonical bytes and unique")
        return value

    @property
    def references(self) -> QueryReferenceInventoryV1:
        return _merge(self.left.references, *(item.references for item in self.values))


class QueryClaimPresenceFilterV1(_StrictQueryModel):
    """Whether the bound Subject carries any Claim of one exact predicate."""

    tag: Literal["playbill-query-claim-presence-filter-v1"] = (
        "playbill-query-claim-presence-filter-v1"
    )
    kind: Literal["claim_presence"] = "claim_presence"
    binding: str
    predicate: str
    negated: bool = False

    @field_validator("binding")
    @classmethod
    def _binding(cls, value: str) -> str:
        return binding_name(value)

    @field_validator("predicate")
    @classmethod
    def _predicate(cls, value: str) -> str:
        return predicate_name(value)

    @property
    def references(self) -> QueryReferenceInventoryV1:
        return QueryReferenceInventoryV1(
            bindings=frozenset({self.binding}),
            predicates=frozenset({self.predicate}),
        )


class QueryConjunctionFilterV1(_StrictQueryModel):
    """Deterministic all-of composition over canonically ordered operands."""

    tag: Literal["playbill-query-conjunction-filter-v1"] = "playbill-query-conjunction-filter-v1"
    kind: Literal["all_of"] = "all_of"
    filters: tuple["QueryFilterV1", ...]

    @field_validator("filters")
    @classmethod
    def _filters(cls, value: tuple["QueryFilterV1", ...]) -> tuple["QueryFilterV1", ...]:
        return _validate_operands(value, label="query all_of")

    @property
    def references(self) -> QueryReferenceInventoryV1:
        return _merge(*(item.references for item in self.filters))


class QueryDisjunctionFilterV1(_StrictQueryModel):
    """Deterministic any-of composition over canonically ordered operands."""

    tag: Literal["playbill-query-disjunction-filter-v1"] = "playbill-query-disjunction-filter-v1"
    kind: Literal["any_of"] = "any_of"
    filters: tuple["QueryFilterV1", ...]

    @field_validator("filters")
    @classmethod
    def _filters(cls, value: tuple["QueryFilterV1", ...]) -> tuple["QueryFilterV1", ...]:
        return _validate_operands(value, label="query any_of")

    @property
    def references(self) -> QueryReferenceInventoryV1:
        return _merge(*(item.references for item in self.filters))


class QueryNegationFilterV1(_StrictQueryModel):
    """Negation of exactly one operand; refusal semantics never widen a result."""

    tag: Literal["playbill-query-negation-filter-v1"] = "playbill-query-negation-filter-v1"
    kind: Literal["not"] = "not"
    operand: "QueryFilterV1"

    @property
    def references(self) -> QueryReferenceInventoryV1:
        return self.operand.references


QueryFilterV1 = Annotated[
    QueryComparisonFilterV1
    | QueryMembershipFilterV1
    | QueryClaimPresenceFilterV1
    | QueryConjunctionFilterV1
    | QueryDisjunctionFilterV1
    | QueryNegationFilterV1,
    Field(discriminator="kind"),
]


def _validate_operands(
    value: tuple["QueryFilterV1", ...],
    *,
    label: str,
) -> tuple["QueryFilterV1", ...]:
    if len(value) < 2:
        raise ValueError(f"{label} requires at least two operands")
    encoded = tuple(canonical_bytes(item.model_dump(mode="json")) for item in value)
    if encoded != tuple(sorted(set(encoded))):
        raise ValueError(f"{label} operands must be sorted by canonical bytes and unique")
    return value


QueryConjunctionFilterV1.model_rebuild()
QueryDisjunctionFilterV1.model_rebuild()
QueryNegationFilterV1.model_rebuild()


class QueryParameterDeclarationV1(_StrictQueryModel):
    """One typed caller parameter; unresolved references refuse fail-closed."""

    tag: Literal["playbill-query-parameter-v1"] = "playbill-query-parameter-v1"
    name: str
    value_type: QueryValueTypeV1
    required: bool = True
    default: object = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _identifier(value, _PARAMETER_RE, label="query parameter name")

    @field_validator("default", mode="before")
    @classmethod
    def _default(cls, value: object) -> object:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _shape(self) -> "QueryParameterDeclarationV1":
        if self.required and self.default is not None:
            raise ValueError("a required query parameter cannot declare a default")
        return self


class QueryEntryV1(_StrictQueryModel):
    """The Subject-kind addressed entry row set for one query."""

    tag: Literal["playbill-query-entry-v1"] = "playbill-query-entry-v1"
    binding: str
    subject_kinds: tuple[str, ...]
    subject_id: QueryParameterRefV1 | None = None

    @field_validator("binding")
    @classmethod
    def _binding(cls, value: str) -> str:
        return binding_name(value)

    @field_validator("subject_kinds")
    @classmethod
    def _subject_kinds(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("query entry must declare at least one Subject kind")
        for item in value:
            subject_kind_name(item)
        return sorted_unique(value, label="query entry Subject kinds")

    @property
    def references(self) -> QueryReferenceInventoryV1:
        if self.subject_id is None:
            return QueryReferenceInventoryV1()
        return self.subject_id.references


class QueryTraversalStepV1(_StrictQueryModel):
    """One relation-Claim hop from an earlier binding to a new bound Subject."""

    tag: Literal["playbill-query-traversal-step-v1"] = "playbill-query-traversal-step-v1"
    binding: str
    from_binding: str
    predicate: str
    direction: QueryTraversalDirectionV1
    required: bool = True
    target_subject_kinds: tuple[str, ...] = ()
    where: QueryFilterV1 | None = None

    @field_validator("binding", "from_binding")
    @classmethod
    def _binding(cls, value: str) -> str:
        return binding_name(value)

    @field_validator("predicate")
    @classmethod
    def _predicate(cls, value: str) -> str:
        return predicate_name(value)

    @field_validator("target_subject_kinds")
    @classmethod
    def _target_subject_kinds(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            subject_kind_name(item)
        return sorted_unique(value, label="query traversal target Subject kinds")

    @model_validator(mode="after")
    def _shape(self) -> "QueryTraversalStepV1":
        if self.binding == self.from_binding:
            raise ValueError("a traversal step cannot rebind its own source binding")
        return self

    @property
    def references(self) -> QueryReferenceInventoryV1:
        inventory = QueryReferenceInventoryV1(predicates=frozenset({self.predicate}))
        if self.where is not None:
            inventory = inventory.merged(self.where.references)
        return inventory


class QueryOrderingV1(_StrictQueryModel):
    """One typed ordering key; canonical address bytes remain the final tiebreak."""

    tag: Literal["playbill-query-ordering-v1"] = "playbill-query-ordering-v1"
    key: QueryValueRefV1
    direction: Literal["ascending", "descending"] = "ascending"
    value_type: QueryValueTypeV1

    @property
    def references(self) -> QueryReferenceInventoryV1:
        return self.key.references


def validate_ordering_keys(value: tuple[QueryOrderingV1, ...], *, label: str) -> None:
    """Refuse a repeated ordering key so declared ordering stays a total rule."""

    encoded = tuple(canonical_bytes(item.key.model_dump(mode="json")) for item in value)
    if len(set(encoded)) != len(encoded):
        raise ValueError(f"{label} must not repeat an ordering key")


class QueryProjectionFieldV1(_StrictQueryModel):
    """One named projected value drawn from bound Subjects and their Claims."""

    tag: Literal["playbill-query-projection-field-v1"] = "playbill-query-projection-field-v1"
    name: str
    value: QueryValueRefV1

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _identifier(value, _FIELD_NAME_RE, label="query projection field name")

    @property
    def references(self) -> QueryReferenceInventoryV1:
        return self.value.references


class QueryProjectionV1(_StrictQueryModel):
    """The complete projected row shape; absent projection means whole Subject views."""

    tag: Literal["playbill-query-projection-v1"] = "playbill-query-projection-v1"
    fields: tuple[QueryProjectionFieldV1, ...]

    @field_validator("fields")
    @classmethod
    def _fields(
        cls, value: tuple[QueryProjectionFieldV1, ...]
    ) -> tuple[QueryProjectionFieldV1, ...]:
        if not value:
            raise ValueError("a declared query projection requires at least one field")
        names = tuple(item.name for item in value)
        sorted_unique(names, label="query projection field names")
        return value

    @property
    def references(self) -> QueryReferenceInventoryV1:
        return _merge(*(item.references for item in self.fields))


class QueryIncludeV1(_StrictQueryModel):
    """One bounded one-hop side context attached to each primary row."""

    tag: Literal["playbill-query-include-v1"] = "playbill-query-include-v1"
    name: str
    binding: str
    from_binding: str
    predicate: str
    direction: QueryTraversalDirectionV1
    many: bool = False
    max_items: int = Field(ge=1)
    where: QueryFilterV1 | None = None
    orderings: tuple[QueryOrderingV1, ...] = ()

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _identifier(value, _FIELD_NAME_RE, label="query include name")

    @field_validator("binding", "from_binding")
    @classmethod
    def _binding(cls, value: str) -> str:
        return binding_name(value)

    @field_validator("predicate")
    @classmethod
    def _predicate(cls, value: str) -> str:
        return predicate_name(value)

    @model_validator(mode="after")
    def _shape(self) -> "QueryIncludeV1":
        if self.binding == self.from_binding:
            raise ValueError("a query include cannot rebind its own source binding")
        if not self.many and self.max_items != 1:
            raise ValueError("a single-valued query include must bound itself to one item")
        validate_ordering_keys(self.orderings, label="query include orderings")
        scope = {self.binding, self.from_binding}
        referenced = self.references.bindings
        if not referenced.issubset(scope):
            raise ValueError("a query include may only reference its own source and target binding")
        return self

    @property
    def references(self) -> QueryReferenceInventoryV1:
        inventory = QueryReferenceInventoryV1(predicates=frozenset({self.predicate}))
        if self.where is not None:
            inventory = inventory.merged(self.where.references)
        return _merge(inventory, *(item.references for item in self.orderings))


class QueryBudgetsV1(_StrictQueryModel):
    """Explicit result, depth, and path budgets; an unbounded read is never declarable."""

    tag: Literal["playbill-query-budgets-v1"] = "playbill-query-budgets-v1"
    max_results: int = Field(ge=1)
    max_traversal_depth: int = Field(ge=0)
    max_paths: int | None = Field(default=None, ge=1)
    max_paths_per_result: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _shape(self) -> "QueryBudgetsV1":
        if (self.max_paths is None) != (self.max_paths_per_result is None):
            raise ValueError("query path budgets must be declared together or not at all")
        if (
            self.max_paths is not None
            and self.max_paths_per_result is not None
            and self.max_paths_per_result > self.max_paths
        ):
            raise ValueError("query max_paths_per_result cannot exceed max_paths")
        return self

    def within(self, ceiling: "QueryBudgetsV1") -> bool:
        """Return whether this budget is admissible under a declared ceiling."""

        if (self.max_paths is None) != (ceiling.max_paths is None):
            return False
        if self.max_results > ceiling.max_results:
            return False
        if self.max_traversal_depth > ceiling.max_traversal_depth:
            return False
        if self.max_paths is not None and ceiling.max_paths is not None:
            if self.max_paths > ceiling.max_paths:
                return False
        if self.max_paths_per_result is not None and ceiling.max_paths_per_result is not None:
            if self.max_paths_per_result > ceiling.max_paths_per_result:
                return False
        return True


__all__ = [
    "QueryBudgetsV1",
    "QueryClaimPresenceFilterV1",
    "QueryClaimValueRefV1",
    "QueryComparisonFilterV1",
    "QueryComparisonOperatorV1",
    "QueryConjunctionFilterV1",
    "QueryDisjunctionFilterV1",
    "QueryEntryV1",
    "QueryEvaluationTimeRefV1",
    "QueryFilterV1",
    "QueryIncludeV1",
    "QueryLiteralRefV1",
    "QueryMembershipFilterV1",
    "QueryNegationFilterV1",
    "QueryOrderingV1",
    "QueryParameterDeclarationV1",
    "QueryParameterRefV1",
    "QueryProjectionFieldV1",
    "QueryProjectionV1",
    "QueryReferenceInventoryV1",
    "QuerySubjectFieldRefV1",
    "QuerySubjectFieldV1",
    "QueryTraversalDirectionV1",
    "QueryTraversalStepV1",
    "QueryValueRefV1",
    "QueryValueTypeV1",
    "binding_name",
    "sorted_unique",
    "validate_ordering_keys",
    "predicate_name",
    "subject_kind_name",
]
