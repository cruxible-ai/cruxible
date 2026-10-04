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

from cruxible_client.contracts.canonical import canonical_bytes, normalize_canonical

_BINDING_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PARAMETER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PREDICATE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})+$")
_SUBJECT_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})*$")

QueryValueType = Literal[
    "string",
    "integer",
    "boolean",
    "decimal",
    "timestamp",
    "subject_reference",
]
QueryComparisonOperator = Literal["eq", "ne", "gt", "gte", "lt", "lte"]
QueryTraversalDirection = Literal["forward", "reverse"]
QuerySubjectField = Literal["subject_id", "subject_kind"]


class _StrictQueryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")


def _nfc(value: str, *, label: str) -> str:
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{label} must already be NFC-normalized")
    return value


def byte_sorted(value: tuple[str, ...]) -> tuple[str, ...]:
    """Return the canonical unsigned UTF-8 byte ordering of a string tuple, deduplicated."""

    return tuple(sorted(set(value), key=lambda item: item.encode("utf-8")))


def sorted_unique(value: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    """Require one canonical byte-ordered, duplicate-free identifier tuple."""

    if value != byte_sorted(value):
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
class QueryReferenceInventory:
    """Exact bindings, parameters, and predicates one declaration references."""

    bindings: frozenset[str] = frozenset()
    parameters: frozenset[str] = frozenset()
    predicates: frozenset[str] = frozenset()

    def merged(self, other: "QueryReferenceInventory") -> "QueryReferenceInventory":
        return QueryReferenceInventory(
            bindings=self.bindings | other.bindings,
            parameters=self.parameters | other.parameters,
            predicates=self.predicates | other.predicates,
        )


def _merge(*items: QueryReferenceInventory) -> QueryReferenceInventory:
    result = QueryReferenceInventory()
    for item in items:
        result = result.merged(item)
    return result


class QueryLiteralRef(_StrictQueryModel):
    """One canonical constant supplied by the accepted declaration itself."""

    tag: Literal["playbill-query-literal-ref-v1"] = "playbill-query-literal-ref-v1"
    kind: Literal["literal"] = "literal"
    value: object = None

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object) -> object:
        return normalize_canonical(value)

    @property
    def references(self) -> QueryReferenceInventory:
        return QueryReferenceInventory()


class QueryParameterRef(_StrictQueryModel):
    """One caller-bound parameter declared by the owning QueryDefinition."""

    tag: Literal["playbill-query-parameter-ref-v1"] = "playbill-query-parameter-ref-v1"
    kind: Literal["parameter"] = "parameter"
    parameter: str

    @field_validator("parameter")
    @classmethod
    def _parameter(cls, value: str) -> str:
        return _identifier(value, _PARAMETER_RE, label="query parameter name")

    @property
    def references(self) -> QueryReferenceInventory:
        return QueryReferenceInventory(parameters=frozenset({self.parameter}))


class QueryClaimValueRef(_StrictQueryModel):
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
    def references(self) -> QueryReferenceInventory:
        return QueryReferenceInventory(
            bindings=frozenset({self.binding}),
            predicates=frozenset({self.predicate}),
        )


class QuerySubjectFieldRef(_StrictQueryModel):
    """A bound Subject's own stable identity, never a Claim-carried property."""

    tag: Literal["playbill-query-subject-field-ref-v1"] = "playbill-query-subject-field-ref-v1"
    kind: Literal["subject_field"] = "subject_field"
    binding: str
    field: QuerySubjectField

    @field_validator("binding")
    @classmethod
    def _binding(cls, value: str) -> str:
        return binding_name(value)

    @property
    def references(self) -> QueryReferenceInventory:
        return QueryReferenceInventory(bindings=frozenset({self.binding}))


class QueryEvaluationTimeRef(_StrictQueryModel):
    """The run's explicit evaluation time; never an implicit wall clock."""

    tag: Literal["playbill-query-evaluation-time-ref-v1"] = "playbill-query-evaluation-time-ref-v1"
    kind: Literal["evaluation_time"] = "evaluation_time"

    @property
    def references(self) -> QueryReferenceInventory:
        return QueryReferenceInventory()


QueryValueRef = Annotated[
    QueryLiteralRef
    | QueryParameterRef
    | QueryClaimValueRef
    | QuerySubjectFieldRef
    | QueryEvaluationTimeRef,
    Field(discriminator="kind"),
]


class QueryComparisonFilter(_StrictQueryModel):
    """One typed comparison; the declared value type is never inferred at run time."""

    tag: Literal["playbill-query-comparison-filter-v1"] = "playbill-query-comparison-filter-v1"
    kind: Literal["comparison"] = "comparison"
    left: QueryValueRef
    operator: QueryComparisonOperator
    right: QueryValueRef
    value_type: QueryValueType

    @property
    def references(self) -> QueryReferenceInventory:
        return _merge(self.left.references, self.right.references)


class QueryMembershipFilter(_StrictQueryModel):
    """Set membership over an explicit, nonempty, canonically ordered value list."""

    tag: Literal["playbill-query-membership-filter-v1"] = "playbill-query-membership-filter-v1"
    kind: Literal["membership"] = "membership"
    left: QueryValueRef
    values: tuple[QueryValueRef, ...]
    value_type: QueryValueType
    negated: bool = False

    @field_validator("values")
    @classmethod
    def _values(cls, value: tuple[QueryValueRef, ...]) -> tuple[QueryValueRef, ...]:
        encoded = tuple(canonical_bytes(item.model_dump(mode="json")) for item in value)
        if not value:
            raise ValueError("query membership filter requires at least one candidate value")
        if encoded != tuple(sorted(set(encoded))):
            raise ValueError("query membership values must be sorted by canonical bytes and unique")
        return value

    @property
    def references(self) -> QueryReferenceInventory:
        return _merge(self.left.references, *(item.references for item in self.values))


class QueryClaimPresenceFilter(_StrictQueryModel):
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
    def references(self) -> QueryReferenceInventory:
        return QueryReferenceInventory(
            bindings=frozenset({self.binding}),
            predicates=frozenset({self.predicate}),
        )


class QueryConjunctionFilter(_StrictQueryModel):
    """Deterministic all-of composition over canonically ordered operands."""

    tag: Literal["playbill-query-conjunction-filter-v1"] = "playbill-query-conjunction-filter-v1"
    kind: Literal["all_of"] = "all_of"
    filters: tuple["QueryFilter", ...]

    @field_validator("filters")
    @classmethod
    def _filters(cls, value: tuple["QueryFilter", ...]) -> tuple["QueryFilter", ...]:
        return _validate_operands(value, label="query all_of")

    @property
    def references(self) -> QueryReferenceInventory:
        return _merge(*(item.references for item in self.filters))


class QueryDisjunctionFilter(_StrictQueryModel):
    """Deterministic any-of composition over canonically ordered operands."""

    tag: Literal["playbill-query-disjunction-filter-v1"] = "playbill-query-disjunction-filter-v1"
    kind: Literal["any_of"] = "any_of"
    filters: tuple["QueryFilter", ...]

    @field_validator("filters")
    @classmethod
    def _filters(cls, value: tuple["QueryFilter", ...]) -> tuple["QueryFilter", ...]:
        return _validate_operands(value, label="query any_of")

    @property
    def references(self) -> QueryReferenceInventory:
        return _merge(*(item.references for item in self.filters))


class QueryNegationFilter(_StrictQueryModel):
    """Negation of exactly one operand; refusal semantics never widen a result."""

    tag: Literal["playbill-query-negation-filter-v1"] = "playbill-query-negation-filter-v1"
    kind: Literal["not"] = "not"
    operand: "QueryFilter"

    @property
    def references(self) -> QueryReferenceInventory:
        return self.operand.references


QueryFilter = Annotated[
    QueryComparisonFilter
    | QueryMembershipFilter
    | QueryClaimPresenceFilter
    | QueryConjunctionFilter
    | QueryDisjunctionFilter
    | QueryNegationFilter,
    Field(discriminator="kind"),
]


def _validate_operands(
    value: tuple["QueryFilter", ...],
    *,
    label: str,
) -> tuple["QueryFilter", ...]:
    if len(value) < 2:
        raise ValueError(f"{label} requires at least two operands")
    encoded = tuple(canonical_bytes(item.model_dump(mode="json")) for item in value)
    if encoded != tuple(sorted(set(encoded))):
        raise ValueError(f"{label} operands must be sorted by canonical bytes and unique")
    return value


QueryConjunctionFilter.model_rebuild()
QueryDisjunctionFilter.model_rebuild()
QueryNegationFilter.model_rebuild()


class QueryParameterDeclaration(_StrictQueryModel):
    """One typed caller parameter; unresolved references refuse fail-closed."""

    tag: Literal["playbill-query-parameter-v1"] = "playbill-query-parameter-v1"
    name: str
    value_type: QueryValueType
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
    def _shape(self) -> "QueryParameterDeclaration":
        if self.required and self.default is not None:
            raise ValueError("a required query parameter cannot declare a default")
        return self


class QueryEntry(_StrictQueryModel):
    """The Subject-kind addressed entry row set for one query."""

    tag: Literal["playbill-query-entry-v1"] = "playbill-query-entry-v1"
    binding: str
    subject_kinds: tuple[str, ...]
    subject_id: QueryParameterRef | None = None

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
    def references(self) -> QueryReferenceInventory:
        if self.subject_id is None:
            return QueryReferenceInventory()
        return self.subject_id.references


class QueryArtifactsEntry(_StrictQueryModel):
    """Select live definitions independently of Claim connectivity.

    A predicate ``security.asset.owner`` belongs to ``security.asset`` only.
    ``all`` includes every namespace; ``namespaces`` requires a nonempty,
    sorted, unique list. Descendants are never implicitly included.
    Procedures use explicit lexical name prefixes ending in a dot, not namespaces.
    """

    tag: Literal["playbill-query-artifacts-entry-v2"] = "playbill-query-artifacts-entry-v2"
    binding: str = "definition"
    artifact_kind: Literal["ClaimType", "Procedure"]
    selection: Literal["all", "namespaces", "name_prefixes"]
    namespaces: tuple[str, ...] = ()
    name_prefixes: tuple[str, ...] = ()

    @field_validator("binding")
    @classmethod
    def _binding(cls, value: str) -> str:
        return binding_name(value)

    @model_validator(mode="after")
    def _selection(self) -> "QueryArtifactsEntry":
        if (self.selection == "namespaces") != bool(self.namespaces):
            raise ValueError("namespaces selection requires names; all selection forbids them")
        if (self.selection == "name_prefixes") != bool(self.name_prefixes):
            raise ValueError(
                "name_prefixes selection requires prefixes and forbids other selectors"
            )
        if self.namespaces and self.artifact_kind != "ClaimType":
            raise ValueError("only ClaimTypes have predicate namespaces")
        if self.name_prefixes and self.artifact_kind != "Procedure":
            raise ValueError("name_prefixes select Procedure names")
        for prefix in self.name_prefixes:
            if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,254}\.", prefix):
                raise ValueError("Procedure name prefixes must end at an explicit dot boundary")
        sorted_unique(self.name_prefixes, label="Procedure name prefixes")
        for name in self.namespaces:
            subject_kind_name(name, label="ClaimType namespace")
        sorted_unique(self.namespaces, label="ClaimType namespaces")
        return self

    @property
    def subject_kinds(self) -> tuple[str, ...]:
        return ()

    @property
    def references(self) -> QueryReferenceInventory:
        return QueryReferenceInventory()


class QueryTraversalStep(_StrictQueryModel):
    """One relation-Claim hop from an earlier binding to a new bound Subject."""

    tag: Literal["playbill-query-traversal-step-v1"] = "playbill-query-traversal-step-v1"
    binding: str
    from_binding: str
    predicate: str
    direction: QueryTraversalDirection
    required: bool = True
    target_subject_kinds: tuple[str, ...] = ()
    where: QueryFilter | None = None

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
    def _shape(self) -> "QueryTraversalStep":
        if self.binding == self.from_binding:
            raise ValueError("a traversal step cannot rebind its own source binding")
        return self

    @property
    def references(self) -> QueryReferenceInventory:
        inventory = QueryReferenceInventory(predicates=frozenset({self.predicate}))
        if self.where is not None:
            inventory = inventory.merged(self.where.references)
        return inventory


class QueryOrdering(_StrictQueryModel):
    """One typed ordering key; canonical address bytes remain the final tiebreak."""

    tag: Literal["playbill-query-ordering-v1"] = "playbill-query-ordering-v1"
    key: QueryValueRef
    direction: Literal["ascending", "descending"] = "ascending"
    value_type: QueryValueType

    @property
    def references(self) -> QueryReferenceInventory:
        return self.key.references


def validate_ordering_keys(value: tuple[QueryOrdering, ...], *, label: str) -> None:
    """Refuse a repeated ordering key so declared ordering stays a total rule."""

    encoded = tuple(canonical_bytes(item.key.model_dump(mode="json")) for item in value)
    if len(set(encoded)) != len(encoded):
        raise ValueError(f"{label} must not repeat an ordering key")


class QueryProjectionField(_StrictQueryModel):
    """One named projected value drawn from bound Subjects and their Claims."""

    tag: Literal["playbill-query-projection-field-v1"] = "playbill-query-projection-field-v1"
    name: str
    value: QueryValueRef

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _identifier(value, _FIELD_NAME_RE, label="query projection field name")

    @property
    def references(self) -> QueryReferenceInventory:
        return self.value.references


class QueryProjection(_StrictQueryModel):
    """The complete projected row shape; absent projection means whole Subject views."""

    tag: Literal["playbill-query-projection-v1"] = "playbill-query-projection-v1"
    fields: tuple[QueryProjectionField, ...]

    @field_validator("fields")
    @classmethod
    def _fields(cls, value: tuple[QueryProjectionField, ...]) -> tuple[QueryProjectionField, ...]:
        if not value:
            raise ValueError("a declared query projection requires at least one field")
        names = tuple(item.name for item in value)
        sorted_unique(names, label="query projection field names")
        return value

    @property
    def references(self) -> QueryReferenceInventory:
        return _merge(*(item.references for item in self.fields))


class QueryInclude(_StrictQueryModel):
    """One bounded one-hop side context attached to each primary row."""

    tag: Literal["playbill-query-include-v1"] = "playbill-query-include-v1"
    name: str
    binding: str
    from_binding: str
    predicate: str
    direction: QueryTraversalDirection
    many: bool = False
    max_items: int = Field(ge=1)
    where: QueryFilter | None = None
    orderings: tuple[QueryOrdering, ...] = ()

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
    def _shape(self) -> "QueryInclude":
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
    def references(self) -> QueryReferenceInventory:
        inventory = QueryReferenceInventory(predicates=frozenset({self.predicate}))
        if self.where is not None:
            inventory = inventory.merged(self.where.references)
        return _merge(inventory, *(item.references for item in self.orderings))


class QueryBudgets(_StrictQueryModel):
    """Explicit result, depth, and path budgets; an unbounded read is never declarable."""

    tag: Literal["playbill-query-budgets-v1"] = "playbill-query-budgets-v1"
    max_results: int = Field(ge=1)
    max_traversal_depth: int = Field(ge=0)
    max_paths: int | None = Field(default=None, ge=1)
    max_paths_per_result: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _shape(self) -> "QueryBudgets":
        if (self.max_paths is None) != (self.max_paths_per_result is None):
            raise ValueError("query path budgets must be declared together or not at all")
        if (
            self.max_paths is not None
            and self.max_paths_per_result is not None
            and self.max_paths_per_result > self.max_paths
        ):
            raise ValueError("query max_paths_per_result cannot exceed max_paths")
        return self

    def within(self, ceiling: "QueryBudgets") -> bool:
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
    "QueryBudgets",
    "QueryClaimPresenceFilter",
    "QueryClaimValueRef",
    "QueryComparisonFilter",
    "QueryComparisonOperator",
    "QueryConjunctionFilter",
    "QueryDisjunctionFilter",
    "QueryEntry",
    "QueryArtifactsEntry",
    "QueryArtifactsEntry",
    "QueryEvaluationTimeRef",
    "QueryFilter",
    "QueryInclude",
    "QueryLiteralRef",
    "QueryMembershipFilter",
    "QueryNegationFilter",
    "QueryOrdering",
    "QueryParameterDeclaration",
    "QueryParameterRef",
    "QueryProjectionField",
    "QueryProjection",
    "QueryReferenceInventory",
    "QuerySubjectFieldRef",
    "QuerySubjectField",
    "QueryTraversalDirection",
    "QueryTraversalStep",
    "QueryValueRef",
    "QueryValueType",
    "binding_name",
    "byte_sorted",
    "sorted_unique",
    "validate_ordering_keys",
    "predicate_name",
    "subject_kind_name",
]
