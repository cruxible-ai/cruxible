"""Typed authored queries and the shared, deterministic filter expression grammar.

Parsing is local. Resolution is supplied by the candidate vocabulary; no parser runs
on the acceptance-law or query-evaluation paths.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, NoReturn, Protocol, TypeVar, cast

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.query.definitions import (
    QueryDefinition,
    QueryDefinitionSpec,
    QueryEvaluationPolicy,
)
from cruxible_client.contracts.query.grammar import (
    QueryBudgets,
    QueryClaimFieldRef,
    QueryClaimPresenceFilter,
    QueryClaimValueRef,
    QueryComparisonFilter,
    QueryComparisonOperator,
    QueryConjunctionFilter,
    QueryDisjunctionFilter,
    QueryEntry,
    QueryEvaluationTimeRef,
    QueryFilter,
    QueryInclude,
    QueryLiteralRef,
    QueryMembershipFilter,
    QueryNegationFilter,
    QueryOrdering,
    QueryParameterDeclaration,
    QueryParameterRef,
    QueryProjection,
    QueryProjectionField,
    QuerySubjectFieldRef,
    QueryTraversalStep,
    QueryValueRef,
    QueryValueType,
)
from cruxible_client.contracts.query.values import coerce_query_value


class _AuthoredModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, validate_by_name=True, serialize_by_alias=True
    )


class QueryMaximumInput(_AuthoredModel):
    results: int = Field(default=2000, ge=1)
    paths_per_result: int = Field(default=1000, ge=1)


class QueryOrderInput(_AuthoredModel):
    field: str
    type: QueryValueType


class QuerySpecInput(_AuthoredModel):
    """One QueryDefinition, expressed in binding names and predicate names."""

    query: str
    description: str | None = None
    params: dict[str, QueryValueType | Literal["subject"]] = Field(default_factory=dict)
    from_: dict[str, str] = Field(
        validation_alias=AliasChoices("from", "from_"), serialization_alias="from"
    )
    where: str | None = None
    hops: tuple[dict[str, str], ...] = ()
    fields: tuple[str | dict[str, str], ...]
    order: tuple[str | QueryOrderInput, ...] = ()
    include: dict[str, str] = Field(default_factory=dict)
    visible: Literal["current", "held"] = "current"
    limit: int = Field(default=200, ge=1)
    paths_per_result: int = Field(default=100, ge=1)
    max_: QueryMaximumInput = Field(
        default_factory=QueryMaximumInput,
        validation_alias=AliasChoices("max", "max_"),
        serialization_alias="max",
    )

    @model_validator(mode="after")
    def _single_bindings(self) -> QuerySpecInput:
        if len(self.from_) != 1:
            raise ValueError("from must declare exactly one entry binding")
        if any(len(item) != 1 for item in self.hops):
            raise ValueError("each hop must declare exactly one binding")
        if any(isinstance(item, dict) and len(item) != 1 for item in self.fields):
            raise ValueError("each renamed field must declare exactly one name")
        return self

    @property
    def identity(self) -> ArtifactIdentity:
        return ArtifactIdentity(kind="QueryDefinition", name=self.query)


class QuerySpecError(FormatError):
    """A local authored refusal, retaining the source path and token column."""

    def __init__(self, code: str, message: str, *, path: str, column: int | None = None):
        self.error_code = code
        self.field_path = path
        self.column = column
        super().__init__(f"{path}: " + (f"column {column}: " if column else "") + message)


@dataclass(frozen=True)
class FilterOperand:
    kind: Literal["reference", "parameter", "literal", "name", "decimal"]
    value: str | int | bool | None


@dataclass(frozen=True)
class FilterExpression:
    kind: Literal["comparison", "membership", "presence", "and", "or", "not"]
    operands: tuple[FilterOperand, ...] = ()
    operator: str = ""
    children: tuple[FilterExpression, ...] = ()


_RESERVED_BINDINGS = frozenset({"now", "and", "or", "not", "exists", "in", "true", "false", "null"})


_TOKEN = re.compile(
    r'\s*(?:("(?:[^"\\]|\\.)*")|(-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?)|'
    r"(\$?[a-zA-Z_][a-zA-Z0-9_.]*)|(==|!=|<=|>=|[=<>~(),]))"
)
_SYMBOLS = {
    "=": "eq",
    "==": "eq",
    "!=": "ne",
    "<": "lt",
    "<=": "lte",
    ">": "gt",
    ">=": "gte",
    "~": "contains",
}


class _FilterParser:
    def __init__(self, text: str, path: str, column_offset: int = 0):
        self.text, self.path, self.index = text, path, 0
        self.column_offset = column_offset
        self.tokens: list[tuple[str, int]] = []
        offset = 0
        while offset < len(text.rstrip()):
            match = _TOKEN.match(text, offset)
            if match is None:
                invalid = offset
                while invalid < len(text) and text[invalid].isspace():
                    invalid += 1
                self.fail("expected a reference, literal, comparison, or parenthesis", invalid + 1)
            token = next(group for group in match.groups() if group is not None)
            self.tokens.append((token, match.end() - len(token) + 1))
            offset = match.end()

    def fail(self, message: str, column: int | None = None) -> NoReturn:
        raise QuerySpecError(
            "cruxible.query.filter_invalid",
            message,
            path=self.path,
            column=self.column_offset
            + (
                column
                or (
                    self.tokens[self.index][1]
                    if self.index < len(self.tokens)
                    else len(self.text) + 1
                )
            ),
        )

    def peek(self) -> str:
        return self.tokens[self.index][0] if self.index < len(self.tokens) else ""

    def take(self, expected: str | None = None) -> str:
        token = self.peek()
        if not token or (expected is not None and token != expected):
            self.fail(f"expected {expected or 'an operand'}")
        self.index += 1
        return token

    def expression(self, level: int = 0, depth: int = 0) -> FilterExpression:
        if depth >= 64 and self.peek() in {"not", "("}:
            self.fail("expected at most 64 nested parentheses or not operators")
        if level < 2:
            word: Literal["or", "and"] = "or" if level == 0 else "and"
            children = [self.expression(level + 1, depth)]
            while self.peek() == word:
                self.take()
                children.append(self.expression(level + 1, depth))
            return (
                children[0]
                if len(children) == 1
                else FilterExpression(word, children=tuple(children))
            )
        if self.peek() == "not":
            self.take()
            return FilterExpression("not", children=(self.expression(2, depth + 1),))
        if self.peek() == "(":
            self.take()
            result = self.expression(depth=depth + 1)
            self.take(")")
            return result
        if self.peek() == "exists":
            self.take()
            return FilterExpression("presence", operands=(self.operand(reference=True),))
        left = self.operand(reference=True)
        operator = self.take()
        if operator == "in":
            self.take("(")
            values = [self.operand()]
            while self.peek() == ",":
                self.take()
                values.append(self.operand())
            self.take(")")
            return FilterExpression("membership", operands=(left, *values))
        if operator not in _SYMBOLS:
            self.fail("expected ==, !=, <, <=, >, >=, or in")
        return FilterExpression(
            "comparison", operands=(left, self.operand()), operator=_SYMBOLS[operator]
        )

    def operand(self, reference: bool = False) -> FilterOperand:
        token = self.take()
        if token in {"(", ")", ",", "and", "or", "not", "exists"} or token in _SYMBOLS:
            self.fail(
                "expected a reference, $parameter, or literal", self.tokens[self.index - 1][1]
            )
        if reference and (
            token.startswith(("$", '"'))
            or token in {"true", "false", "null"}
            or re.fullmatch(r"-?\d+(?:\.\d+)?", token)
        ):
            self.fail(
                "expected a binding, binding.predicate, or now", self.tokens[self.index - 1][1]
            )
        if token.startswith("$"):
            return FilterOperand("parameter", token[1:])
        if (
            token.startswith('"')
            or token in {"true", "false", "null"}
            or re.fullmatch(r"-?\d+(?:\.\d+)?", token)
        ):
            if re.fullmatch(r"-?\d+\.\d+", token):
                return FilterOperand("decimal", token)
            try:
                value = json.loads(token)
            except ValueError:
                self.fail("expected a valid JSON literal", self.tokens[self.index - 1][1])
            return FilterOperand("literal", value)
        return FilterOperand(
            "reference" if reference or "." in token or token == "now" else "name", token
        )


def parse_filter(
    expression: str, *, path: str = "$.where", column_offset: int = 0
) -> FilterExpression:
    parser = _FilterParser(expression, path, column_offset)
    result = parser.expression()
    if parser.peek():
        parser.fail("expected end of filter")
    return result


class QuerySpecResolver(Protocol):
    def require_kind(self, kind: str, *, path: str) -> str: ...
    def field(
        self, kinds: tuple[str, ...], name: str, *, path: str, scalar: bool = False
    ) -> tuple[str, QueryValueType]: ...
    def reverse_source(
        self, bindings: Mapping[str, tuple[str, ...]], name: str, *, path: str
    ) -> tuple[str, str, tuple[str, ...]]: ...

    def relation(
        self, kinds: tuple[str, ...], name: str, reverse: bool, *, path: str
    ) -> tuple[str, tuple[str, ...]]: ...
    def operator(self, predicate: str, operator: str, *, path: str) -> None: ...
    def literal(self, predicate: str, operator: str, value: object, *, path: str) -> object: ...


_Model = TypeVar("_Model", bound=BaseModel)


def _canonical(items: list[_Model]) -> tuple[_Model, ...]:
    by_bytes = {canonical_bytes(item.model_dump(mode="json")): item for item in items}
    return tuple(by_bytes[key] for key in sorted(by_bytes))


class _LowerQuery:
    def __init__(self, spec: QuerySpecInput, resolver: QuerySpecResolver):
        self.spec, self.resolver = spec, resolver
        self.bindings: dict[str, tuple[str, ...]] = {}

    @staticmethod
    def require_binding(name: str, path: str) -> None:
        if name in _RESERVED_BINDINGS:
            raise QuerySpecError(
                "cruxible.query.unknown_field",
                f"{name!r} is reserved by the filter grammar; rename the binding",
                path=path,
            )

    def ref(
        self, text: str, path: str, *, scalar: bool = False
    ) -> tuple[QueryValueRef, QueryValueType]:
        if text == "now":
            return QueryEvaluationTimeRef(), "timestamp"
        for field in ("role", "verdict"):
            if text.startswith(field + "(") and text.endswith(")"):
                ref, _ = self.ref(text[len(field) + 1 : -1], path)
                if not isinstance(ref, QueryClaimValueRef):
                    raise QuerySpecError(
                        "cruxible.query.unknown_field",
                        "role/verdict require a predicate",
                        path=path,
                    )
                return QueryClaimFieldRef(
                    binding=ref.binding,
                    predicate=ref.predicate,
                    field="role" if field == "role" else "verdict",
                ), "string"
        binding, dot, name = text.partition(".")
        if binding not in self.bindings:
            raise QuerySpecError(
                "cruxible.query.unknown_field",
                f"unknown binding {binding!r}; use {', '.join(self.bindings)}",
                path=path,
            )
        if not dot or name in {"subject_id", "subject_kind"}:
            return QuerySubjectFieldRef(
                binding=binding, field="subject_kind" if name == "subject_kind" else "subject_id"
            ), "string"
        predicate, value_type = self.resolver.field(
            self.bindings[binding], name, path=path, scalar=scalar
        )
        return QueryClaimValueRef(binding=binding, predicate=predicate), value_type

    def operand(
        self,
        operand: FilterOperand,
        path: str,
        *,
        left: QueryValueRef,
        value_type: QueryValueType,
        operator: str,
    ) -> QueryValueRef:
        if operand.kind == "reference" or (
            operand.kind == "name" and operand.value in self.bindings
        ):
            ref, actual = self.ref(str(operand.value), path, scalar=True)
            if actual != value_type:
                raise QuerySpecError(
                    "cruxible.query.value_type_mismatch",
                    f"cannot compare {value_type} with {actual}",
                    path=path,
                )
            return ref
        if operand.kind == "parameter":
            declared = self.spec.params.get(str(operand.value))
            if declared is None:
                raise QuerySpecError(
                    "cruxible.query.parameter_undeclared",
                    f"declare parameter {operand.value!r} in params",
                    path=path,
                )
            declared = "subject_reference" if declared == "subject" else declared
            if declared != value_type:
                raise QuerySpecError(
                    "cruxible.query.value_type_mismatch",
                    f"parameter is {declared}, predicate is {value_type}",
                    path=path,
                )
            return QueryParameterRef(parameter=str(operand.value))
        value: object = operand.value
        if operand.kind == "decimal" and value_type != "decimal":
            raise QuerySpecError(
                "cruxible.query.value_type_mismatch",
                f"decimal literal cannot compare with {value_type}",
                path=path,
            )
        if isinstance(left, QueryClaimValueRef):
            value = self.resolver.literal(left.predicate, operator, value, path=path)
        if not coerce_query_value(value, value_type).ok:
            raise QuerySpecError(
                "cruxible.query.value_type_mismatch", f"expected a {value_type} literal", path=path
            )
        if isinstance(left, QueryClaimFieldRef) and value not in left.vocabulary:
            raise QuerySpecError(
                "cruxible.query.unknown_member", f"use one of {left.vocabulary}", path=path
            )
        return QueryLiteralRef(value=value)

    def filter(self, text: str, path: str, *, column_offset: int = 0) -> QueryFilter:
        def lower(expr: FilterExpression) -> QueryFilter:
            if expr.kind in {"and", "or"}:
                filters = _canonical([lower(child) for child in expr.children])
                if len(filters) == 1:
                    return filters[0]
                model = QueryConjunctionFilter if expr.kind == "and" else QueryDisjunctionFilter
                return model(filters=filters)
            if expr.kind == "not":
                child = lower(expr.children[0])
                if isinstance(child, QueryMembershipFilter):
                    return child.model_copy(update={"negated": not child.negated})
                if isinstance(child, QueryClaimPresenceFilter):
                    return child.model_copy(update={"negated": not child.negated})
                return QueryNegationFilter(operand=child)
            left, value_type = self.ref(
                str(expr.operands[0].value), path, scalar=expr.kind != "presence"
            )
            if expr.kind == "presence":
                if not isinstance(left, QueryClaimValueRef):
                    raise QuerySpecError(
                        "cruxible.query.operator_not_applicable",
                        "exists requires a Claim predicate",
                        path=path,
                    )
                return QueryClaimPresenceFilter(binding=left.binding, predicate=left.predicate)
            if isinstance(left, QueryClaimValueRef):
                self.resolver.operator(
                    left.predicate, "in" if expr.kind == "membership" else expr.operator, path=path
                )
            values = [
                self.operand(
                    item,
                    path,
                    left=left,
                    value_type=value_type,
                    operator="in" if expr.kind == "membership" else expr.operator,
                )
                for item in expr.operands[1:]
            ]
            if expr.kind == "membership":
                return QueryMembershipFilter(
                    left=left, values=_canonical(values), value_type=value_type
                )
            if expr.operator == "contains":
                raise QuerySpecError(
                    "cruxible.query.operator_not_applicable",
                    "stored queries do not support contains",
                    path=path,
                )
            return QueryComparisonFilter(
                left=left,
                operator=cast(QueryComparisonOperator, expr.operator),
                right=values[0],
                value_type=value_type,
            )

        return lower(parse_filter(text, path=path, column_offset=column_offset))

    def lower(self) -> QueryDefinitionSpec:
        binding, entry_text = next(iter(self.spec.from_.items()))
        self.require_binding(binding, "$.from")
        matched_entry = re.fullmatch(
            r"\s*([a-z][a-z0-9_.]*)\s*(?:=\s*\$([a-z][a-z0-9_]*))?\s*", entry_text
        )
        if matched_entry is None:
            raise QuerySpecError(
                "cruxible.query.unknown_kind", "expected KIND or KIND = $parameter", path="$.from"
            )
        kind, parameter = matched_entry.groups()
        self.resolver.require_kind(kind, path="$.from")
        self.bindings[binding] = (kind,)
        subject_id = None
        if parameter is not None:
            declared = self.spec.params.get(parameter)
            if declared is None:
                raise QuerySpecError(
                    "cruxible.query.parameter_undeclared",
                    "declare the entry parameter in params",
                    path="$.from",
                )
            if declared not in {"string", "subject", "subject_reference"}:
                raise QuerySpecError(
                    "cruxible.query.value_type_mismatch",
                    "entry parameter must be string or subject",
                    path="$.from",
                )
            subject_id = QueryParameterRef(parameter=parameter)
        entry = QueryEntry(binding=binding, subject_kinds=(kind,), subject_id=subject_id)
        traversal = []
        for index, hop in enumerate(self.spec.hops):
            target, expression = next(iter(hop.items()))
            path = f"$.hops[{index}]"
            self.require_binding(target, path)
            filtered = re.split(r"\s+where(?:\s+|$)", expression, maxsplit=1)
            filter_separator = len(filtered) == 2
            condition = filtered[1] if filter_separator else ""
            relation_and_kind = re.split(r"\s*:\s*", filtered[0].strip(), maxsplit=1)
            relation = relation_and_kind[0]
            target_kind = relation_and_kind[1] if len(relation_and_kind) == 2 else ""
            reverse = relation.startswith("<")
            source, dot, predicate = relation.removeprefix("<").removesuffix("!").partition(".")
            inferred = None
            if reverse and source not in self.bindings:
                inferred = self.resolver.reverse_source(
                    self.bindings, source + "." + predicate, path=path
                )
                source = inferred[0]
            if not dot or source not in self.bindings or target in self.bindings:
                raise QuerySpecError(
                    "cruxible.query.unknown_field",
                    "a hop extends an earlier binding with a new binding",
                    path=path,
                )
            if inferred is not None:
                _, predicate, targets = inferred
            else:
                predicate, targets = self.resolver.relation(
                    self.bindings[source], predicate, reverse, path=path
                )
            if target_kind:
                selected = tuple(sorted(target_kind.split("|")))
                if not set(selected).issubset(targets):
                    raise QuerySpecError(
                        "cruxible.query.value_type_mismatch",
                        f"target must be one of {targets}",
                        path=path,
                    )
                targets = selected
            elif len(targets) != 1:
                raise QuerySpecError(
                    "cruxible.query.ambiguous_field",
                    f"state a target kind from {targets}",
                    path=path,
                )
            self.bindings[target] = targets
            traversal.append(
                QueryTraversalStep(
                    binding=target,
                    from_binding=source,
                    predicate=predicate,
                    direction="reverse" if reverse else "forward",
                    required=relation.endswith("!"),
                    target_subject_kinds=targets,
                    where=self.filter(
                        condition, path, column_offset=len(expression) - len(condition)
                    )
                    if filter_separator
                    else None,
                )
            )
        fields = []
        for index, item in enumerate(self.spec.fields):
            if isinstance(item, str):
                text = item
                bare = item
                for function in ("role", "verdict"):
                    if item.startswith(function + "(") and item.endswith(")"):
                        bare = item[len(function) + 1 : -1]
                name = bare.rsplit(".", 1)[-1]
                if item in self.bindings:
                    name = item
            else:
                name, text = next(iter(item.items()))
            ref, _ = self.ref(text, f"$.fields[{index}]")
            fields.append(QueryProjectionField(name=name, value=ref))
        orderings = []
        for index, ordering in enumerate(self.spec.order):
            override = ordering.type if isinstance(ordering, QueryOrderInput) else None
            text = ordering.field if isinstance(ordering, QueryOrderInput) else ordering
            ref, value_type = self.ref(text.removeprefix("-"), f"$.order[{index}]", scalar=True)
            if override is not None:
                if override != value_type and (value_type, override) != ("string", "decimal"):
                    raise QuerySpecError(
                        "cruxible.query.value_type_mismatch",
                        "order type must match the field or interpret a numeric string as decimal",
                        path=f"$.order[{index}]",
                    )
                value_type = override
            orderings.append(
                QueryOrdering(
                    key=ref,
                    direction="descending" if text.startswith("-") else "ascending",
                    value_type=value_type,
                )
            )
        includes = []
        for name, text in sorted(self.spec.include.items()):
            source, _, predicate = text.partition(".")
            if source not in self.bindings:
                raise QuerySpecError(
                    "cruxible.query.unknown_field",
                    "unknown include source",
                    path=f"$.include.{name}",
                )
            predicate, _ = self.resolver.field(
                self.bindings[source], predicate, path=f"$.include.{name}"
            )
            includes.append(
                QueryInclude(
                    name=name,
                    binding=f"included_{name}",
                    from_binding=source,
                    predicate=predicate,
                    direction="forward",
                    many=True,
                    max_items=500,
                )
            )
        path_shape = bool(traversal)
        return QueryDefinitionSpec(
            identity=self.spec.identity,
            description=self.spec.description,
            entry=entry,
            traversal=tuple(traversal),
            where=self.filter(self.spec.where, "$.where") if self.spec.where is not None else None,
            result_binding=binding,
            result_shape="path" if path_shape else "subject",
            result_cardinality="many",
            dedupe="path" if path_shape else "subject",
            projection=QueryProjection(fields=tuple(sorted(fields, key=lambda item: item.name)))
            if fields
            else None,
            parameters=tuple(
                QueryParameterDeclaration(
                    name=name,
                    value_type="subject_reference" if value_type == "subject" else value_type,
                )
                for name, value_type in sorted(self.spec.params.items())
            ),
            orderings=tuple(orderings),
            includes=tuple(includes),
            default_budgets=QueryBudgets(
                max_results=self.spec.limit,
                max_paths_per_result=self.spec.paths_per_result if path_shape else None,
                max_traversal_depth=10 if path_shape else 1,
                max_paths=20000 if path_shape else None,
            ),
            maximum_budgets=QueryBudgets(
                max_results=self.spec.max_.results,
                max_paths_per_result=self.spec.max_.paths_per_result if path_shape else None,
                max_traversal_depth=10 if path_shape else 1,
                max_paths=20000 if path_shape else None,
            ),
            evaluation_policy=QueryEvaluationPolicy(
                visible_verdicts=("stale", "supported")
                if self.spec.visible == "held"
                else ("supported",),
                visible_currency=("current", "stale")
                if self.spec.visible == "held"
                else ("current",),
                conflict_behavior="surface_conflicts",
            ),
        )


def lower_query_spec(spec: QuerySpecInput, resolver: QuerySpecResolver) -> QueryDefinitionSpec:
    """Resolve one authored query into the existing, law-checked typed tree."""
    return _LowerQuery(spec, resolver).lower()


def query_spec_from_definition(query: QueryDefinition) -> QuerySpecInput:
    """Render the authored subset, refusing rather than losing unrepresentable meaning."""

    def ref(value: QueryValueRef) -> str:
        if isinstance(value, QueryLiteralRef):
            return json.dumps(value.value, ensure_ascii=False)
        if isinstance(value, QueryParameterRef):
            return "$" + value.parameter
        if isinstance(value, QueryEvaluationTimeRef):
            return "now"
        if isinstance(value, QuerySubjectFieldRef):
            return (
                value.binding if value.field == "subject_id" else f"{value.binding}.{value.field}"
            )
        text = f"{value.binding}.{value.predicate}"
        return f"{value.field}({text})" if isinstance(value, QueryClaimFieldRef) else text

    def filter_operand(value: QueryValueRef, *, left: bool = False) -> None:
        if (
            isinstance(value, QueryClaimFieldRef)
            or (
                left
                and not isinstance(
                    value, (QueryClaimValueRef, QuerySubjectFieldRef, QueryEvaluationTimeRef)
                )
            )
            or (isinstance(value, QueryLiteralRef) and isinstance(value.value, (dict, list)))
        ):
            raise ValueError("filter operands require the explicit typed definition")

    def condition(value: QueryFilter) -> str:
        if isinstance(value, QueryComparisonFilter):
            filter_operand(value.left, left=True)
            filter_operand(value.right)
            symbols = {v: k for k, v in _SYMBOLS.items()}
            symbols["eq"] = "=="
            return f"{ref(value.left)} {symbols[value.operator]} {ref(value.right)}"
        if isinstance(value, QueryMembershipFilter):
            filter_operand(value.left, left=True)
            for item in value.values:
                filter_operand(item)
            text = f"{ref(value.left)} in ({', '.join(ref(item) for item in value.values)})"
            return f"not ({text})" if value.negated else text
        if isinstance(value, QueryClaimPresenceFilter):
            return ("not " if value.negated else "") + f"exists {value.binding}.{value.predicate}"
        if isinstance(value, QueryNegationFilter):
            if isinstance(value.operand, (QueryMembershipFilter, QueryClaimPresenceFilter)):
                raise ValueError("explicit filter wrappers require the typed definition")
            return f"not ({condition(value.operand)})"
        word = " and " if isinstance(value, QueryConjunctionFilter) else " or "
        return "(" + word.join(condition(item) for item in value.filters) + ")"

    if not isinstance(query.entry, QueryEntry):
        raise ValueError("artifact queries retain their typed definition form")
    if (
        query.result_binding != query.entry.binding
        or query.result_cardinality != "many"
        or query.result_shape != ("path" if query.traversal else "subject")
    ):
        raise ValueError("query result shape requires the explicit typed definition")
    expected_policy = QueryEvaluationPolicy(
        visible_verdicts=("stale", "supported")
        if "stale" in query.evaluation_policy.visible_verdicts
        else ("supported",),
        visible_currency=("current", "stale")
        if "stale" in query.evaluation_policy.visible_verdicts
        else ("current",),
        conflict_behavior="surface_conflicts",
    )
    path_shape = bool(query.traversal)
    if (
        query.dedupe != ("path" if path_shape else "subject")
        or query.evaluation_policy != expected_policy
    ):
        raise ValueError("query policy requires the explicit typed definition")
    if any(not item.required or item.default is not None for item in query.parameters):
        raise ValueError("parameter defaults require the explicit typed definition")
    if any(
        budget.max_traversal_depth != (10 if path_shape else 1)
        or budget.max_paths != (20000 if path_shape else None)
        for budget in (query.default_budgets, query.maximum_budgets)
    ):
        raise ValueError("query budgets require the explicit typed definition")
    if query.projection and any(
        not isinstance(item.value, (QuerySubjectFieldRef, QueryClaimValueRef, QueryClaimFieldRef))
        for item in query.projection.fields
    ):
        raise ValueError("projection constants require the explicit typed definition")
    entry = query.entry.subject_kinds
    if len(entry) != 1:
        raise ValueError("multiple entry kinds require the explicit typed definition")
    entry_text = entry[0] + (
        f" = ${query.entry.subject_id.parameter}" if query.entry.subject_id else ""
    )
    hops = []
    for step in query.traversal:
        if not step.target_subject_kinds:
            raise ValueError("unconstrained hop targets require the explicit typed definition")
        text = (
            ("<" if step.direction == "reverse" else "")
            + f"{step.from_binding}.{step.predicate}"
            + ("!" if step.required else "")
        )
        if step.target_subject_kinds:
            text += ": " + "|".join(step.target_subject_kinds)
        if step.where:
            text += " where " + condition(step.where)
        hops.append({step.binding: text})
    for item in query.includes:
        if (
            item.direction,
            item.many,
            item.max_items,
            item.binding,
            item.where,
            item.orderings,
        ) != ("forward", True, 500, "included_" + item.name, None, ()):
            raise ValueError("include requires the explicit typed definition")
    if any(
        not isinstance(
            item.key,
            (QueryClaimValueRef, QueryClaimFieldRef, QuerySubjectFieldRef, QueryEvaluationTimeRef),
        )
        for item in query.orderings
    ):
        raise ValueError("ordering constants and parameters require the explicit typed definition")
    return QuerySpecInput(
        query=query.identity.name,
        description=query.description,
        params={item.name: item.value_type for item in query.parameters},
        from_={query.entry.binding: entry_text},
        where=condition(query.where) if query.where else None,
        hops=tuple(hops),
        fields=tuple({item.name: ref(item.value)} for item in query.projection.fields)
        if query.projection
        else (),
        order=tuple(
            QueryOrderInput(
                field=("-" if item.direction == "descending" else "") + ref(item.key),
                type=item.value_type,
            )
            for item in query.orderings
        ),
        include={item.name: f"{item.from_binding}.{item.predicate}" for item in query.includes},
        visible="held" if "stale" in query.evaluation_policy.visible_verdicts else "current",
        limit=query.default_budgets.max_results,
        paths_per_result=query.default_budgets.max_paths_per_result or 100,
        max_=QueryMaximumInput(
            results=query.maximum_budgets.max_results,
            paths_per_result=query.maximum_budgets.max_paths_per_result or 1000,
        ),
    )
