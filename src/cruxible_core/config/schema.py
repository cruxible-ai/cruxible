"""Pydantic models for Cruxible Core config YAML validation.

The config defines a decision domain: entity types, relationships,
named queries, constraints, and optional execution artifacts such as
contracts, providers, workflows, and workflow tests.

Hierarchy:
    CoreConfig
    ├── entity_types: dict[str, EntityTypeSchema]
    │   └── properties: dict[str, PropertySchema]
    ├── relationships: list[RelationshipSchema]
    │   ├── properties: dict[str, PropertySchema]
    │   └── proposal_policy: ProposalPolicySchema
    │       └── signals: dict[str, SignalPolicySchema]
    ├── named_queries: dict[str, NamedQuerySchema]
    │   └── traversal: list[TraversalStep]
    ├── constraints: list[ConstraintSchema]
    ├── feedback_profiles: dict[str, FeedbackProfileSchema]
    ├── outcome_profiles: dict[str, OutcomeProfileSchema]
    ├── quality_checks: list[QualityCheckSchema]
    ├── mutation_guards: list[MutationGuardSchema]
    ├── decision_policies: list[DecisionPolicySchema]
    ├── contracts: dict[str, ContractSchema]
    ├── artifacts: dict[str, ProviderArtifactSchema]
    ├── providers: dict[str, ProviderSchema]
    ├── workflows: dict[str, WorkflowSchema]
    ├── runtime: RuntimeConfigSchema
    └── tests: list[WorkflowTestSchema]
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, Field, field_validator, model_validator

from cruxible_core.config.predicates import StructuredPredicateSpec
from cruxible_core.predicate import PredicateValueType
from cruxible_core.primitives import canonical_json
from cruxible_core.query.enums import QueryDedupe, QueryResultShape, QueryVisibilityState

_PATH_TOKEN = r"[\w-]+"
_PATH_TOKEN_RE = re.compile(rf"^{_PATH_TOKEN}$")
_FEEDBACK_PATH_PATTERN = rf"^(FROM|TO|EDGE)\.({_PATH_TOKEN})$"
_OUTCOME_PATH_PATTERN = (
    rf"^(RESOLUTION|GROUP|WORKFLOW|THESIS|RECEIPT|SURFACE|TRACESET)\.({_PATH_TOKEN})$"
)

WorkflowType = Literal["utility", "canonical", "decision_support", "proposal"]
QueryMode = Literal["collection", "traversal"]
TracePayloadRetention = Literal["full", "preview", "metadata"]
MutationPayloadRetention = Literal["full", "preview", "metadata"]
PropertyType = Literal[
    "string",
    "int",
    "integer",
    "float",
    "number",
    "bool",
    "date",
    "datetime",
    "json",
]


def _normalize_query_entity_returns(returns: str) -> str:
    stripped = returns.strip()
    if stripped.startswith("list[") and stripped.endswith("]"):
        return stripped[5:-1].strip()
    return stripped


# ---------------------------------------------------------------------------
# Property Schema (shared between entity types and relationships)
# ---------------------------------------------------------------------------


class EnumSchema(BaseModel):
    """Shared enum vocabulary referenced by property schemas."""

    values: list[str]
    ordered: Literal["low_to_high"] | None = None
    description: str | None = None

    @model_validator(mode="after")
    def validate_values(self) -> EnumSchema:
        if not self.values:
            msg = "enum values must not be empty"
            raise ValueError(msg)
        if any(not isinstance(value, str) or not value.strip() for value in self.values):
            msg = "enum values must be non-empty strings"
            raise ValueError(msg)
        if len(set(self.values)) != len(self.values):
            msg = "enum values must be unique"
            raise ValueError(msg)
        return self


class PropertySchema(BaseModel):
    """Schema for graph properties and contract fields.

    Graph properties may use terse defaults when nested under entity or
    relationship schemas. Contract fields remain explicit through
    ``ContractSchema`` validation.
    """

    type: PropertyType = "string"
    primary_key: bool = False
    indexed: bool = False
    optional: bool = False
    required: bool | None = Field(default=None, exclude=True)
    default: Any | None = None
    enum: list[Any] | None = None
    enum_ref: str | None = None
    description: str | None = None
    json_schema: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_schema_usage(self) -> PropertySchema:
        if self.required is not None:
            required_optional = not self.required
            if "optional" in self.model_fields_set and self.optional != required_optional:
                msg = "required and optional are conflicting aliases"
                raise ValueError(msg)
            self.optional = required_optional
        if self.primary_key and self.optional:
            msg = "primary_key properties may not be optional"
            raise ValueError(msg)
        if self.enum is not None and self.enum_ref is not None:
            msg = "enum and enum_ref are mutually exclusive"
            raise ValueError(msg)
        if self.enum_ref is not None and self.type != "string":
            msg = "enum_ref is only allowed on properties with type 'string'"
            raise ValueError(msg)
        if self.enum is not None:
            if not self.enum:
                msg = "enum values must not be empty"
                raise ValueError(msg)
            seen: set[str] = set()
            for index, value in enumerate(self.enum):
                try:
                    key = canonical_json(value)
                except (TypeError, ValueError) as exc:
                    msg = f"enum value at index {index} must be JSON-serializable"
                    raise ValueError(msg) from exc
                if key in seen:
                    msg = "enum values must be unique"
                    raise ValueError(msg)
                seen.add(key)
            if self.default is not None and self.default not in self.enum:
                allowed = ", ".join(str(value) for value in self.enum)
                msg = f"default must be one of enum values: {allowed}"
                raise ValueError(msg)
        if self.json_schema is None:
            return self
        if self.type != "json":
            msg = "json_schema is only allowed on properties with type 'json'"
            raise ValueError(msg)
        try:
            canonical_json(self.json_schema)
        except (TypeError, ValueError) as exc:
            msg = f"json_schema must be JSON-serializable: {exc}"
            raise ValueError(msg) from exc
        return self


def _apply_graph_property_defaults(
    properties: dict[str, PropertySchema],
) -> dict[str, PropertySchema]:
    """Apply graph-only defaults for entity and relationship properties."""
    normalized: dict[str, PropertySchema] = {}
    for name, prop in properties.items():
        if (
            not prop.primary_key
            and "optional" not in prop.model_fields_set
            and "required" not in prop.model_fields_set
        ):
            prop = prop.model_copy(update={"optional": True})
        normalized[name] = prop
    return normalized


# ---------------------------------------------------------------------------
# Entity Type Schema
# ---------------------------------------------------------------------------


class EntityTypeSchema(BaseModel):
    """Schema for an entity type definition."""

    description: str | None = None
    properties: dict[str, PropertySchema]
    constraints: list[str] = Field(default_factory=list)
    write_policy: Literal["direct", "proposal_only"] | None = None
    """Per-type direct-write governance.

    ``None`` inherits ``runtime.default_write_policy``; ``"direct"`` explicitly
    opts out of the instance default (but not the env kill-switch);
    ``"proposal_only"`` refuses direct entity adds for this type. Resolved by
    ``service/direct_write_policy.py`` and enforced at the
    ``graph/operations.py`` chokepoint.
    """

    @model_validator(mode="after")
    def apply_graph_property_defaults(self) -> EntityTypeSchema:
        self.properties = _apply_graph_property_defaults(self.properties)
        return self

    def get_primary_key(self) -> str | None:
        """Return the primary key property name, if any."""
        for name, prop in self.properties.items():
            if prop.primary_key:
                return name
        return None


# ---------------------------------------------------------------------------
# Proposal Policy Config (for candidate group resolve)
# ---------------------------------------------------------------------------


class SignalPolicySchema(BaseModel):
    """Per-signal-source guardrails for candidate group proposals."""

    role: Literal["blocking", "required", "advisory"] = "required"
    always_review_on_unsure: bool = False
    note: str = ""


class ProposalPolicySchema(BaseModel):
    """Guardrails for candidate group proposals on a relationship type."""

    signals: dict[str, SignalPolicySchema] = Field(default_factory=dict)
    auto_resolve_when: Literal["all_support", "no_contradict"] = "all_support"
    auto_resolve_requires_prior_trust: Literal["trusted_only", "trusted_or_watch"] = "trusted_only"
    max_group_size: int = 1000


# ---------------------------------------------------------------------------
# Relationship Schema
# ---------------------------------------------------------------------------


class RelationshipSchema(BaseModel):
    """Schema for a relationship type definition."""

    name: str
    from_entity: str = Field(alias="from")
    to_entity: str = Field(alias="to")
    cardinality: str = "many_to_many"
    properties: dict[str, PropertySchema] = Field(default_factory=dict)
    description: str | None = None
    reverse_name: str | None = Field(default=None, validation_alias="inverse")
    proposal_policy: ProposalPolicySchema | None = None
    proposal_identity: Literal["thesis_signature", "relationship_tuple"] = "thesis_signature"
    write_policy: Literal["direct", "proposal_only"] | None = None
    """Per-type direct-write governance.

    ``None`` inherits ``runtime.default_write_policy``; ``"direct"`` explicitly
    opts out of the instance default (but not the env kill-switch);
    ``"proposal_only"`` refuses direct (non-pending) edge writes for this type —
    edges may only enter through the governed proposal/``group_resolve`` or
    ``workflow_apply`` path, or be staged with ``pending=true``. Resolved by
    ``service/direct_write_policy.py`` and enforced at the
    ``graph/operations.py`` chokepoint.
    """

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @model_validator(mode="after")
    def validate_proposal_identity(self) -> RelationshipSchema:
        self.properties = _apply_graph_property_defaults(self.properties)
        if self.proposal_identity == "relationship_tuple" and self.proposal_policy is None:
            msg = (
                "proposal_identity 'relationship_tuple' requires a governed proposal_policy section"
            )
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# Named Query Schema (declarative traversal)
# ---------------------------------------------------------------------------


class RelatedExclusionSpec(BaseModel):
    """Exclude a candidate when a second relationship exists for the same pair."""

    relationship: str
    direction: Literal["outgoing", "incoming", "both"] = "outgoing"

    @field_validator("relationship")
    @classmethod
    def validate_relationship_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("relationship must be a non-empty string")
        return value


class QueryPredicateSpec(StructuredPredicateSpec):
    """Structured predicate map used by named-query traversal steps."""


def _validate_top_level_query_predicate_scopes(
    predicates: QueryPredicateSpec | None,
    *,
    field_name: str,
    allow_result: bool = False,
) -> None:
    from cruxible_core.query.predicates import QUERY_PREDICATE_SCOPES

    if predicates is None:
        return
    for path in predicates.root:
        scope = path.split(".", 1)[0]
        if scope == "result" and not allow_result:
            msg = f"result predicates are not supported in {field_name}"
            raise ValueError(msg)
        if scope not in QUERY_PREDICATE_SCOPES:
            allowed = ", ".join(sorted(QUERY_PREDICATE_SCOPES))
            msg = (
                f"top-level {field_name} predicate path '{path}' must start with one of: {allowed}"
            )
            raise ValueError(msg)


class RelatedPredicateSpec(BaseModel):
    """Predicate-backed related-edge existence check for a traversal candidate."""

    relationship: str
    direction: Literal["outgoing", "incoming", "both"] = "outgoing"
    edge: QueryPredicateSpec | None = None
    source: QueryPredicateSpec | None = None
    target: QueryPredicateSpec | None = None
    current: QueryPredicateSpec | None = None
    candidate: QueryPredicateSpec | None = None
    entry: QueryPredicateSpec | None = None

    @field_validator("relationship")
    @classmethod
    def validate_relationship_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("relationship must be a non-empty string")
        return value


class QueryOrderSpec(BaseModel):
    """Deterministic ordering rule for named query result rows."""

    by: str
    direction: Literal["asc", "desc"] = "asc"
    value_type: PredicateValueType | None = None
    enum_ref: str | None = None

    @field_validator("by")
    @classmethod
    def validate_order_ref(cls, value: str) -> str:
        if not value.startswith("$") or len(value) == 1:
            raise ValueError("order_by.by must be a query reference like $result.entity_id")
        return value

    @field_validator("enum_ref")
    @classmethod
    def validate_enum_ref(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("order_by.enum_ref must be a non-empty string")
        return value

    @model_validator(mode="after")
    def validate_order_value_mode(self) -> QueryOrderSpec:
        if self.value_type is not None and self.enum_ref is not None:
            raise ValueError("order_by.value_type and order_by.enum_ref are mutually exclusive")
        return self


class QueryIncludeSpec(BaseModel):
    """One-hop side context attached to each primary named-query row."""

    from_: str = Field(alias="from")
    relationship: str
    direction: Literal["incoming", "outgoing"] = "outgoing"
    many: bool = False
    required: bool = False
    limit: int | None = Field(default=None, ge=0)
    where: QueryPredicateSpec | None = None
    where_related: list[RelatedPredicateSpec] = Field(default_factory=list)
    where_not_related: list[RelatedPredicateSpec] = Field(default_factory=list)
    order_by: list[QueryOrderSpec] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @field_validator("from_")
    @classmethod
    def validate_from_ref(cls, value: str) -> str:
        if not value.startswith("$") or len(value) == 1:
            raise ValueError("include from must be an entity reference like $result")
        return value

    @field_validator("relationship")
    @classmethod
    def validate_relationship_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("relationship must be a non-empty string")
        return value

    @model_validator(mode="after")
    def validate_include_shape(self) -> QueryIncludeSpec:
        _validate_top_level_query_predicate_scopes(self.where, field_name="include where")
        for order in self.order_by:
            scope, _path = _split_query_ref(order.by)
            if scope not in {"edge", "source", "target", "input"}:
                msg = (
                    f"include order_by reference '{order.by}' must use "
                    "$edge, $source, $target, or $input"
                )
                raise ValueError(msg)
        return self


def _collect_query_refs(value: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(value, str):
        if value.startswith("$"):
            refs.append(value)
        return refs
    if isinstance(value, list):
        for item in value:
            refs.extend(_collect_query_refs(item))
        return refs
    if isinstance(value, dict):
        for item in value.values():
            refs.extend(_collect_query_refs(item))
    return refs


def _split_query_ref(ref: str) -> tuple[str, str]:
    if not ref.startswith("$"):
        raise ValueError(f"query reference '{ref}' must start with '$'")
    scope, sep, path = ref[1:].partition(".")
    if not scope or not sep or not path:
        raise ValueError(f"invalid query reference '{ref}'")
    return scope, path


class TraversalStep(BaseModel):
    """A single step in a named query's traversal path.

    Each step follows one or more relationships in a direction, optionally
    filtering on edge and/or target entity properties, applying constraints,
    and excluding candidates when related edges already exist. When multiple
    relationships are listed, the engine traverses all of them from the
    current entities and merges results (fan-out).
    """

    relationship: str | list[str]
    direction: Literal["outgoing", "incoming", "both"] = "outgoing"
    filter: dict[str, Any] | None = None
    target_filter: dict[str, Any] | None = None
    where: QueryPredicateSpec | None = None
    where_related: list[RelatedPredicateSpec] = Field(default_factory=list)
    where_not_related: list[RelatedPredicateSpec] = Field(default_factory=list)
    constraint: str | None = None
    constraint_value_type: PredicateValueType | None = None
    exclude_if_related: list[RelatedExclusionSpec] = Field(default_factory=list)
    max_depth: int = Field(default=1, ge=1)
    required: bool = True
    alias: str | None = Field(default=None, alias="as")

    model_config = {"populate_by_name": True}

    @field_validator("relationship")
    @classmethod
    def validate_relationship(cls, v: str | list[str]) -> str | list[str]:
        if isinstance(v, list):
            if len(v) == 0:
                msg = "relationship list must not be empty"
                raise ValueError(msg)
            for item in v:
                if not isinstance(item, str) or not item.strip():
                    msg = "relationship list items must be non-empty strings"
                    raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def validate_constraint_type(self) -> TraversalStep:
        if self.constraint is None and self.constraint_value_type is not None:
            msg = "constraint_value_type requires constraint"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_where_scope(self) -> TraversalStep:
        _validate_top_level_query_predicate_scopes(self.where, field_name="where")
        return self

    @field_validator("alias")
    @classmethod
    def validate_alias(cls, value: str | None) -> str | None:
        if value is not None and _PATH_TOKEN_RE.fullmatch(value) is None:
            msg = "as must match [\\w-]+"
            raise ValueError(msg)
        return value

    @property
    def relationship_types(self) -> list[str]:
        """Normalize relationship to a deduplicated list."""
        if isinstance(self.relationship, str):
            return [self.relationship]
        return list(dict.fromkeys(self.relationship))


class NamedQuerySchema(BaseModel):
    """Schema for a declarative named query.

    Queries declare whether they collect one entity/relationship type or start
    from an entry entity and traverse relationship steps.
    """

    mode: QueryMode
    description: str | None = None
    entry_point: str | None = None
    traversal: list[TraversalStep] = Field(default_factory=list)
    returns: str
    result_shape: QueryResultShape = "path"
    dedupe: QueryDedupe = "path"
    relationship_state: QueryVisibilityState = "live"
    allow_relationship_state_override: bool = False
    where: QueryPredicateSpec | None = None
    select: dict[str, Any] | None = None
    order_by: list[QueryOrderSpec] = Field(default_factory=list)
    include: dict[str, QueryIncludeSpec] = Field(default_factory=dict)
    limit: int | None = Field(default=None, ge=0)
    max_paths: int | None = Field(default=None, gt=0)
    max_paths_per_result: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_result_shape(self) -> NamedQuerySchema:
        aliases = [step.alias for step in self.traversal if step.alias is not None]
        duplicate_aliases = sorted({alias for alias in aliases if aliases.count(alias) > 1})
        if duplicate_aliases:
            duplicate_str = ", ".join(duplicate_aliases)
            msg = f"duplicate traversal aliases: {duplicate_str}"
            raise ValueError(msg)
        alias_set = set(aliases)
        include_aliases = set(self.include)
        invalid_include_aliases = sorted(
            alias for alias in include_aliases if _PATH_TOKEN_RE.fullmatch(alias) is None
        )
        if invalid_include_aliases:
            invalid = ", ".join(invalid_include_aliases)
            raise ValueError(f"include aliases must match [\\w-]+: {invalid}")
        collisions = sorted(alias_set & include_aliases)
        if collisions:
            collision_str = ", ".join(collisions)
            raise ValueError(
                f"include aliases must not collide with traversal aliases: {collision_str}"
            )
        self._validate_include_refs(alias_set)
        self._validate_projection_and_order_refs(alias_set)
        _validate_top_level_query_predicate_scopes(
            self.where,
            field_name="query where",
            allow_result=True,
        )
        is_collection = self.mode == "collection"
        if is_collection:
            if self.entry_point is not None:
                msg = "mode 'collection' queries must not define entry_point"
                raise ValueError(msg)
            if self.result_shape == "path":
                msg = "mode 'collection' queries do not support result_shape 'path'"
                raise ValueError(msg)
            if self.traversal:
                msg = "mode 'collection' queries must not define traversal"
                raise ValueError(msg)
            if self.include and self.result_shape == "entity" and self.select is None:
                msg = "mode 'collection' entity queries with include must define select"
                raise ValueError(msg)
            if self.max_paths is not None or self.max_paths_per_result is not None:
                msg = "mode 'collection' queries do not support path budgets"
                raise ValueError(msg)
            if self.where is not None:
                allowed_where_scopes = (
                    {"result"}
                    if self.result_shape == "entity"
                    else {"edge", "source", "target", "result"}
                )
                for predicate_path in self.where.root:
                    scope = predicate_path.split(".", 1)[0]
                    if scope not in allowed_where_scopes:
                        allowed = ", ".join(sorted(allowed_where_scopes))
                        raise ValueError(
                            "mode 'collection' query where predicate "
                            f"'{predicate_path}' must start with one of: {allowed}"
                        )
        else:
            if self.entry_point is None:
                msg = "mode 'traversal' queries require entry_point"
                raise ValueError(msg)
            if not self.traversal:
                msg = "mode 'traversal' queries require at least one traversal step"
                raise ValueError(msg)
            if self.where is not None:
                msg = "mode 'traversal' queries do not support top-level where"
                raise ValueError(msg)
        if "dedupe" not in self.model_fields_set:
            self.dedupe = "entity" if self.result_shape == "entity" else "path"
        has_non_required_step = any(not step.required for step in self.traversal)
        if self.result_shape == "entity" and self.dedupe != "entity":
            msg = "result_shape 'entity' requires dedupe 'entity'"
            raise ValueError(msg)
        if self.result_shape == "entity" and has_non_required_step:
            msg = "required false traversal steps require result_shape 'path' or 'relationship'"
            raise ValueError(msg)
        if self.result_shape == "entity" and (
            self.max_paths is not None or self.max_paths_per_result is not None
        ):
            msg = "path budgets require result_shape 'path' or 'relationship'"
            raise ValueError(msg)
        if (
            self.result_shape == "entity"
            and self.include
            and not is_collection
            and self.select is None
        ):
            msg = "traversal entity queries with include must define select"
            raise ValueError(msg)
        if self.result_shape == "relationship":
            if not is_collection and not self.traversal:
                msg = "result_shape 'relationship' requires at least one traversal step"
                raise ValueError(msg)
            if self.dedupe == "entity":
                msg = "result_shape 'relationship' requires dedupe 'path' or 'none'"
                raise ValueError(msg)
            if has_non_required_step and not self.traversal[-1].required:
                msg = (
                    "result_shape 'relationship' requires the final traversal step "
                    "to be required when using required false"
                )
                raise ValueError(msg)
        if self.relationship_state == "pending":
            if self.result_shape not in {"path", "relationship"} and not (
                is_collection and self.include
            ):
                msg = "relationship_state 'pending' requires result_shape 'path' or 'relationship'"
                raise ValueError(msg)
            if self.dedupe == "entity" and not (is_collection and self.include):
                msg = "relationship_state 'pending' requires dedupe 'path' or 'none'"
                raise ValueError(msg)
        if self.relationship_state == "reviewable":
            if self.result_shape != "path" and not (
                (is_collection and self.result_shape == "relationship")
                or (is_collection and self.include)
            ):
                msg = "relationship_state 'reviewable' requires result_shape 'path'"
                raise ValueError(msg)
            if self.dedupe == "entity" and not (is_collection and self.include):
                msg = "relationship_state 'reviewable' requires dedupe 'path' or 'none'"
                raise ValueError(msg)
        return self

    def _validate_include_refs(self, aliases: set[str]) -> None:
        for alias, include in self.include.items():
            ref = include.from_
            if not ref.startswith("$"):
                raise ValueError(f"include '{alias}' from reference '{ref}' must start with '$'")
            scope, sep, path = ref[1:].partition(".")
            if scope in {"entry", "result"}:
                if sep or path:
                    raise ValueError(
                        f"include '{alias}' from reference '{ref}' must be exactly ${scope}"
                    )
                continue
            if not sep or not path:
                raise ValueError(f"invalid include from reference '{ref}'")
            if scope != "path":
                raise ValueError(
                    f"include '{alias}' from reference '{ref}' must use $entry, "
                    "$result, or $path.<alias>.source|target"
                )
            parts = path.split(".")
            if len(parts) != 2 or parts[1] not in {"source", "target"}:
                raise ValueError(
                    f"include '{alias}' from reference '{ref}' must use "
                    "$path.<alias>.source or $path.<alias>.target"
                )
            path_alias = parts[0]
            if path_alias not in aliases:
                raise ValueError(
                    f"include '{alias}' from reference '{ref}' uses unknown "
                    f"traversal alias '{path_alias}'"
                )

    def _validate_projection_and_order_refs(self, aliases: set[str]) -> None:
        allowed_scopes = {
            "input",
            "entry",
            "result",
            "path",
            "include",
            "relationship",
            "from_entity",
            "to_entity",
        }
        refs: list[str] = []
        if self.select is not None:
            for value in self.select.values():
                refs.extend(_collect_query_refs(value))
        refs.extend(order.by for order in self.order_by)
        for ref in refs:
            scope, path = _split_query_ref(ref)
            if scope not in allowed_scopes:
                allowed = ", ".join(sorted(allowed_scopes))
                msg = (
                    f"unsupported query reference scope '${scope}' in '{ref}'; "
                    f"use one of: {allowed}"
                )
                raise ValueError(msg)
            if scope == "path":
                alias = path.split(".", 1)[0] if path else ""
                if not alias:
                    raise ValueError(f"path reference '{ref}' must include a traversal alias")
                if alias not in aliases:
                    raise ValueError(
                        f"path reference '{ref}' uses unknown traversal alias '{alias}'"
                    )
            if scope == "include":
                include_alias, _, include_path = path.partition(".")
                if not include_alias:
                    raise ValueError(f"include reference '{ref}' must include an include alias")
                include = self.include.get(include_alias)
                if include is None:
                    raise ValueError(
                        f"include reference '{ref}' uses unknown include alias '{include_alias}'"
                    )
                if include.many and include_path.split(".", 1)[0] in {
                    "edge",
                    "source",
                    "target",
                }:
                    raise ValueError(
                        f"include reference '{ref}' targets many include '{include_alias}'; "
                        f"use $include.{include_alias}.items, count, or existence"
                    )
            if self.result_shape == "entity" and scope in {
                "path",
                "relationship",
                "from_entity",
                "to_entity",
            }:
                raise ValueError(
                    f"query reference '{ref}' is not available for result_shape 'entity'"
                )
            if self.result_shape == "relationship" and scope == "path":
                raise ValueError(
                    f"query reference '{ref}' is not available for result_shape 'relationship'"
                )
            if self.result_shape == "path" and scope in {
                "relationship",
                "from_entity",
                "to_entity",
            }:
                raise ValueError(
                    f"query reference '{ref}' is not available for result_shape 'path'"
                )


# ---------------------------------------------------------------------------
# Constraint Schema
# ---------------------------------------------------------------------------


class ConstraintSchema(BaseModel):
    """Schema for a constraint rule.

    Constraints are evaluated during graph mutation or query time.
    Severity determines whether violations are warnings or errors.
    """

    name: str
    rule: str
    value_type: PredicateValueType | None = None
    severity: Literal["warning", "error"] = "warning"
    description: str | None = None


# ---------------------------------------------------------------------------
# Feedback Profile Schema
# ---------------------------------------------------------------------------


FeedbackPathRef = Annotated[str, Field(pattern=_FEEDBACK_PATH_PATTERN)]
OutcomePathRef = Annotated[str, Field(pattern=_OUTCOME_PATH_PATTERN)]


FeedbackRemediationHint = Literal[
    "constraint",
    "decision_policy",
    "quality_check",
    "provider_fix",
    "unknown",
]
"""Bounded remediation lane assigned to a feedback reason code."""


class FeedbackReasonCodeSchema(BaseModel):
    """Structured feedback code used by agents and analysis."""

    description: str
    remediation_hint: FeedbackRemediationHint = "unknown"
    required_scope_keys: list[str] = Field(default_factory=list)


class FeedbackProfileSchema(BaseModel):
    """Relationship-scoped feedback vocabulary and grouping metadata."""

    version: int = 1
    reason_codes: dict[str, FeedbackReasonCodeSchema] = Field(default_factory=dict)
    scope_keys: dict[str, FeedbackPathRef] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_required_scope_keys(self) -> FeedbackProfileSchema:
        declared = set(self.scope_keys.keys())
        for code, schema in self.reason_codes.items():
            missing = [key for key in schema.required_scope_keys if key not in declared]
            if missing:
                missing_str = ", ".join(sorted(missing))
                msg = (
                    f"Feedback reason code '{code}' references undeclared "
                    f"required_scope_keys: {missing_str}"
                )
                raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# Outcome Profile Schema
# ---------------------------------------------------------------------------


SurfaceType = Literal["query", "workflow", "operation"]
"""Surface a decision is scoped to: a named query, a workflow, or a graph operation."""


OutcomeAnchorType = Literal["resolution", "receipt"]
"""What an outcome is anchored to: a group resolution or a receipt."""


OutcomeLabel = Literal["correct", "incorrect", "partial", "unknown"]
"""Coarse outcome label captured on every OutcomeRecord."""


OutcomeRemediationHint = Literal[
    "trust_adjustment",
    "require_review",
    "decision_policy",
    "provider_fix",
    "workflow_fix",
    "graph_fix",
    "unknown",
]
"""Bounded remediation lane assigned to an outcome code."""


class OutcomeCodeSchema(BaseModel):
    """Structured outcome code used by agents and outcome analysis."""

    description: str
    remediation_hint: OutcomeRemediationHint = "unknown"
    required_scope_keys: list[str] = Field(default_factory=list)


class OutcomeProfileSchema(BaseModel):
    """Anchor-scoped outcome vocabulary and grouping metadata."""

    anchor_type: OutcomeAnchorType
    version: int = 1
    relationship_type: str | None = None
    workflow_name: str | None = None
    surface_type: SurfaceType | None = None
    surface_name: str | None = None
    outcome_codes: dict[str, OutcomeCodeSchema] = Field(default_factory=dict)
    scope_keys: dict[str, OutcomePathRef] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_shape(self) -> OutcomeProfileSchema:
        declared = set(self.scope_keys.keys())
        for code, schema in self.outcome_codes.items():
            missing = [key for key in schema.required_scope_keys if key not in declared]
            if missing:
                missing_str = ", ".join(sorted(missing))
                msg = (
                    f"Outcome code '{code}' references undeclared required_scope_keys: "
                    f"{missing_str}"
                )
                raise ValueError(msg)

        if self.anchor_type == "resolution":
            if self.relationship_type is None:
                msg = "Resolution outcome profiles require relationship_type"
                raise ValueError(msg)
            if self.surface_type is not None or self.surface_name is not None:
                msg = "Resolution outcome profiles may not define surface_type or surface_name"
                raise ValueError(msg)
            allowed_prefixes = {"RESOLUTION", "GROUP", "WORKFLOW", "THESIS"}
        else:
            if self.surface_type is None or self.surface_name is None:
                msg = "Receipt outcome profiles require surface_type and surface_name"
                raise ValueError(msg)
            if self.relationship_type is not None or self.workflow_name is not None:
                msg = "Receipt outcome profiles may not define relationship_type or workflow_name"
                raise ValueError(msg)
            allowed_prefixes = {"RECEIPT", "SURFACE", "TRACESET"}

        for scope_key, path in self.scope_keys.items():
            prefix, _, _ = path.partition(".")
            if prefix not in allowed_prefixes:
                allowed_str = ", ".join(sorted(allowed_prefixes))
                msg = (
                    f"Outcome profile scope key '{scope_key}' uses unsupported path '{path}'. "
                    f"Allowed prefixes: {allowed_str}"
                )
                raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# Decision Policy Schema
# ---------------------------------------------------------------------------


class DecisionPolicyMatch(BaseModel):
    """Structured exact-match selectors for action-side decision policies."""

    from_match: dict[str, Any] = Field(default_factory=dict, alias="from")
    to: dict[str, Any] = Field(default_factory=dict)
    edge: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class DecisionPolicySchema(BaseModel):
    """Consumer-specific action rule applied during query or proposal execution."""

    name: str
    description: str | None = None
    rationale: str = ""
    applies_to: Literal["query", "workflow"]
    query_name: str | None = None
    workflow_name: str | None = None
    relationship_type: str
    effect: Literal["suppress", "require_review"]
    match: DecisionPolicyMatch = Field(default_factory=DecisionPolicyMatch)
    expires_at: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> DecisionPolicySchema:
        if self.applies_to == "query":
            if self.query_name is None or self.workflow_name is not None:
                msg = "Query decision policies require query_name only"
                raise ValueError(msg)
            if self.effect != "suppress":
                msg = "Query decision policies only support effect 'suppress'"
                raise ValueError(msg)
        else:
            if self.workflow_name is None or self.query_name is not None:
                msg = "Workflow decision policies require workflow_name only"
                raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# Quality Check Schema
# ---------------------------------------------------------------------------


class QualityCheckBase(BaseModel):
    """Base schema for evaluate-time graph quality checks."""

    name: str
    description: str | None = None
    severity: Literal["warning", "error"] = "warning"

    model_config = {"extra": "forbid"}


class PropertyQualityCheck(QualityCheckBase):
    """Check a top-level property on entities or relationships."""

    kind: Literal["property"] = "property"
    target: Literal["entity", "relationship"]
    entity_type: str | None = None
    relationship_type: str | None = None
    property: str
    rule: Literal["required", "non_empty", "type", "pattern"]
    expected_type: str | None = None
    pattern: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> PropertyQualityCheck:
        if self.target == "entity":
            if self.entity_type is None or self.relationship_type is not None:
                msg = "Property quality checks targeting entities require entity_type only"
                raise ValueError(msg)
        else:
            if self.relationship_type is None or self.entity_type is not None:
                msg = (
                    "Property quality checks targeting relationships require relationship_type only"
                )
                raise ValueError(msg)

        if self.rule == "type" and not self.expected_type:
            msg = "Property quality checks with rule 'type' require expected_type"
            raise ValueError(msg)
        if self.rule != "type" and self.expected_type is not None:
            msg = "expected_type is only allowed when rule is 'type'"
            raise ValueError(msg)

        if self.rule == "pattern" and not self.pattern:
            msg = "Property quality checks with rule 'pattern' require pattern"
            raise ValueError(msg)
        if self.rule != "pattern" and self.pattern is not None:
            msg = "pattern is only allowed when rule is 'pattern'"
            raise ValueError(msg)

        return self


class JsonContentQualityCheck(QualityCheckBase):
    """Check JSON array-of-object content on entities or relationships."""

    kind: Literal["json_content"] = "json_content"
    target: Literal["entity", "relationship"]
    entity_type: str | None = None
    relationship_type: str | None = None
    property: str
    rule: Literal["no_empty_objects_in_array", "required_nested_keys"]
    keys: list[str] = Field(default_factory=list)
    match: Literal["any", "all"] | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> JsonContentQualityCheck:
        if self.target == "entity":
            if self.entity_type is None or self.relationship_type is not None:
                msg = "JSON content checks targeting entities require entity_type only"
                raise ValueError(msg)
        else:
            if self.relationship_type is None or self.entity_type is not None:
                msg = "JSON content checks targeting relationships require relationship_type only"
                raise ValueError(msg)

        if self.rule == "required_nested_keys":
            if not self.keys:
                msg = "JSON content checks with rule 'required_nested_keys' require keys"
                raise ValueError(msg)
            if self.match is None:
                msg = "JSON content checks with rule 'required_nested_keys' require match"
                raise ValueError(msg)
        else:
            if self.keys:
                msg = "keys is only allowed when rule is 'required_nested_keys'"
                raise ValueError(msg)
            if self.match is not None:
                msg = "match is only allowed when rule is 'required_nested_keys'"
                raise ValueError(msg)

        return self


class UniquenessQualityCheck(QualityCheckBase):
    """Check entity-property uniqueness, optionally across compound keys."""

    kind: Literal["uniqueness"] = "uniqueness"
    entity_type: str
    properties: list[str]

    @model_validator(mode="after")
    def validate_shape(self) -> UniquenessQualityCheck:
        if not self.properties:
            msg = "Uniqueness quality checks require at least one property"
            raise ValueError(msg)
        return self


class BoundsQualityCheck(QualityCheckBase):
    """Check entity or relationship counts against a numeric range."""

    kind: Literal["bounds"] = "bounds"
    target: Literal["entity_count", "relationship_count"]
    entity_type: str | None = None
    relationship_type: str | None = None
    min_count: int | None = None
    max_count: int | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> BoundsQualityCheck:
        if self.target == "entity_count":
            if self.entity_type is None or self.relationship_type is not None:
                msg = "Bounds checks on entity_count require entity_type only"
                raise ValueError(msg)
        else:
            if self.relationship_type is None or self.entity_type is not None:
                msg = "Bounds checks on relationship_count require relationship_type only"
                raise ValueError(msg)

        if self.min_count is None and self.max_count is None:
            msg = "Bounds quality checks require min_count, max_count, or both"
            raise ValueError(msg)
        return self


class CardinalityQualityCheck(QualityCheckBase):
    """Check per-entity relationship counts in one direction."""

    kind: Literal["cardinality"] = "cardinality"
    entity_type: str
    relationship_type: str
    direction: Literal["incoming", "outgoing"]
    min_count: int | None = None
    max_count: int | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> CardinalityQualityCheck:
        if self.min_count is None and self.max_count is None:
            msg = "Cardinality quality checks require min_count, max_count, or both"
            raise ValueError(msg)
        return self


class RelationshipPropertyConsistencyQualityCheck(QualityCheckBase):
    """Check an entity property against a related entity property or id."""

    kind: Literal["relationship_property_consistency"] = "relationship_property_consistency"
    entity_type: str
    relationship_type: str
    direction: Literal["incoming", "outgoing"]
    source_property: str
    target_property: str | None = None
    allow_missing_source: bool = False


class NamedQueryResultCountQualityCheck(QualityCheckBase):
    """Check that a named query returns a count within expected bounds."""

    kind: Literal["named_query_result_count"] = "named_query_result_count"
    query_name: str
    params: dict[str, Any] = Field(default_factory=dict)
    min_count: int | None = None
    max_count: int | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> NamedQueryResultCountQualityCheck:
        if self.min_count is None and self.max_count is None:
            msg = "Named-query result count checks require min_count, max_count, or both"
            raise ValueError(msg)
        return self


QualityCheckSchema = Annotated[
    (
        PropertyQualityCheck
        | JsonContentQualityCheck
        | UniquenessQualityCheck
        | BoundsQualityCheck
        | CardinalityQualityCheck
        | RelationshipPropertyConsistencyQualityCheck
        | NamedQueryResultCountQualityCheck
    ),
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Mutation Guard Schema
# ---------------------------------------------------------------------------


class NamedQueryResultCountGuardCondition(BaseModel):
    """Named-query result count condition for config-defined mutation guards."""

    type: Literal["query"]
    query_name: str
    params: dict[str, Any] = Field(default_factory=dict)
    min_count: int | None = None
    max_count: int | None = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_shape(self) -> NamedQueryResultCountGuardCondition:
        if self.min_count is None and self.max_count is None:
            msg = "Mutation guard named-query conditions require min_count, max_count, or both"
            raise ValueError(msg)
        return self


class ActorIdentityGuardCondition(BaseModel):
    """Actor identity allow-list condition for config-defined mutation guards."""

    type: Literal["actor"]
    allowed_actor_ids: list[str] = Field(min_length=1)

    model_config = {"extra": "forbid"}

    @field_validator("allowed_actor_ids")
    @classmethod
    def _validate_allowed_actor_ids(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            actor_id = value.strip()
            if not actor_id:
                raise ValueError("allowed_actor_ids entries must be non-empty strings")
            if actor_id in seen:
                raise ValueError(f"duplicate allowed_actor_ids entry: '{actor_id}'")
            seen.add(actor_id)
            normalized.append(actor_id)
        return normalized


class CoWriteRequirement(BaseModel):
    """Required co-written entity for a ``co_write`` mutation guard condition."""

    entity_type: str
    via_relationship: str
    kind: str | None = None

    model_config = {"extra": "forbid"}

    @field_validator("entity_type", "via_relationship", "kind")
    @classmethod
    def _non_empty_string(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not value.strip():
            raise ValueError("must be a non-empty string")
        return value


class CoWriteGuardCondition(BaseModel):
    """Same-write co-creation condition for config-defined mutation guards.

    The guarded transition is rejected unless THIS write also creates an entity
    of ``requires.entity_type`` (optionally filtered by the ``kind`` property)
    linked to the guarded ``$entity`` via ``requires.via_relationship``. The
    required entity and the linking edge must both be created in the same write
    delta; a stale pre-existing linked entity does not satisfy the condition.
    """

    type: Literal["co_write"]
    requires: CoWriteRequirement

    model_config = {"extra": "forbid"}


class EvidenceRequirementGuardCondition(BaseModel):
    """Evidence floor condition for config-defined relationship mutation guards."""

    type: Literal["evidence"]
    require_evidence: Literal["source_evidence"]
    min_count: int = Field(default=1, ge=1)

    model_config = {"extra": "forbid"}


MutationGuardConditionSchema = Annotated[
    (
        NamedQueryResultCountGuardCondition
        | ActorIdentityGuardCondition
        | CoWriteGuardCondition
        | EvidenceRequirementGuardCondition
    ),
    Field(discriminator="type"),
]


class MutationGuardSchema(BaseModel):
    """Reject direct writes when configured guard conditions are not met.

    Entity-property guards fire on creates and updates alike; updates that
    re-assert the existing value are not transitions and do not fire.
    Relationship evidence guards fire on writes to the configured relationship
    type and require the resulting relationship evidence to meet the floor.
    """

    name: str
    entity_type: str | None = None
    property: str | None = None
    new_value: Any = None
    relationship_type: str | None = None
    condition: MutationGuardConditionSchema
    message: str | None = None
    where: QueryPredicateSpec | None = None
    where_related: list[RelatedPredicateSpec] = Field(default_factory=list)
    where_not_related: list[RelatedPredicateSpec] = Field(default_factory=list)

    model_config = {"extra": "forbid"}

    @field_validator("name", "entity_type", "property", "relationship_type")
    @classmethod
    def _non_empty_string(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not value.strip():
            raise ValueError("must be a non-empty string")
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> MutationGuardSchema:
        if isinstance(self.condition, EvidenceRequirementGuardCondition):
            if self.relationship_type is None:
                raise ValueError("relationship evidence guards require relationship_type")
            if self.where is not None:
                raise ValueError("relationship evidence guards do not support 'where' scoping")
            if self.where_related or self.where_not_related:
                raise ValueError(
                    "relationship evidence guards do not support related-edge scoping"
                )
            forbidden_fields = [
                field
                for field in ("entity_type", "property", "new_value")
                if field in self.model_fields_set
            ]
            if forbidden_fields:
                joined = ", ".join(forbidden_fields)
                raise ValueError(
                    f"relationship evidence guards may not define entity-property fields: {joined}"
                )
            return self

        missing_fields: list[str] = []
        for field in ("entity_type", "property"):
            if field not in self.model_fields_set or getattr(self, field) is None:
                missing_fields.append(field)
        if "new_value" not in self.model_fields_set:
            missing_fields.append("new_value")
        if missing_fields:
            joined = ", ".join(missing_fields)
            raise ValueError(f"entity mutation guards require: {joined}")
        if self.relationship_type is not None:
            raise ValueError("entity mutation guards may not define relationship_type")
        return self

    @model_validator(mode="after")
    def validate_where_scope(self) -> MutationGuardSchema:
        if self.where is None:
            return self
        for path, operators in self.where.root.items():
            scope = path.split(".", 1)[0]
            if scope != "candidate":
                raise ValueError(
                    f"guard where predicate path '{path}' must use the 'candidate' scope"
                )
            # Operand values may also be scope-bearing refs ($scope.path); they
            # resolve against the same context, so they must be candidate-scoped
            # too -- otherwise a $current/$edge/etc. operand escapes the contract.
            for ref in _collect_query_refs(operators):
                ref_scope = ref[1:].split(".", 1)[0]
                if ref_scope != "candidate":
                    raise ValueError(
                        f"guard where predicate operand '{ref}' must use the 'candidate' scope"
                    )
        return self


# ---------------------------------------------------------------------------
# Workflow / Provider Contracts
# ---------------------------------------------------------------------------


class ContractSchema(BaseModel):
    """Typed payload contract for provider or workflow inputs."""

    description: str | None = None
    fields: dict[str, PropertySchema]
    allow_extra: bool = False

    @model_validator(mode="after")
    def validate_explicit_field_types(self) -> ContractSchema:
        for name, prop in self.fields.items():
            if "type" not in prop.model_fields_set:
                msg = f"Contract field '{name}' must define type explicitly"
                raise ValueError(msg)
        return self


BUILTIN_CONTRACTS: dict[str, ContractSchema] = {
    "cruxible.EmptyInput": ContractSchema(fields={}),
    "cruxible.JsonObject": ContractSchema(fields={}, allow_extra=True),
    "cruxible.JsonItems": ContractSchema(fields={"items": PropertySchema(type="json")}),
    "cruxible.ParsedTabularBundle": ContractSchema(
        fields={
            "artifact": PropertySchema(type="json"),
            "tables": PropertySchema(type="json"),
            "files": PropertySchema(type="json"),
            "diagnostics": PropertySchema(type="json"),
        }
    ),
}


ContractReference = str | ContractSchema


class ProviderArtifactSchema(BaseModel):
    """Pinned external artifact referenced by a provider."""

    kind: str
    uri: str
    digest: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderSchema(BaseModel):
    """Versioned executable leaf used by workflow provider steps."""

    kind: Literal["function", "model", "tool"] = "function"
    description: str | None = None
    contract_in: ContractReference
    contract_out: ContractReference
    ref: str
    version: str
    deterministic: bool = True
    artifact: str | None = None
    runtime: Literal["python", "http_json", "command"] = "python"
    side_effects: bool = False
    config: dict[str, Any] = Field(default_factory=dict)


ShapeCastType = Literal["str", "int", "float", "bool", "json"]
MissingRequiredAction = Literal["error", "drop"]
FilterComparisonOp = Literal[
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "==",
    "!=",
    ">",
    ">=",
    "<",
    "<=",
    "before",
    "on_or_before",
    "after",
    "on_or_after",
]
DeduplicationStrategy = Literal["first", "last", "max", "min"]
CountSelector = Literal["returned_results", "total_results", "items", "results"]
CountComparisonOp = Literal["eq", "ne", "lt", "lte", "gt", "gte", "==", "!=", "<", "<=", ">", ">="]


class AssertSpec(BaseModel):
    """Structured workflow guard condition."""

    left: Any
    op: FilterComparisonOp
    right: Any
    message: str
    value_type: PredicateValueType | None = None


class AssertNotTruncatedSpec(BaseModel):
    """Guard that a prior read-derived step output was not truncated."""

    step: str

    model_config = {"extra": "forbid"}


class AssertCountSpec(BaseModel):
    """Guard that a prior step output count satisfies a comparison."""

    step: str
    count: CountSelector
    op: CountComparisonOp
    value: Any
    message: str | None = None

    model_config = {"extra": "forbid"}


class AssertExistsSpec(BaseModel):
    """Guard that one workflow reference resolves to a present value."""

    ref: str
    message: str | None = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_reference(self) -> AssertExistsSpec:
        if self.ref != "$input" and not (
            self.ref.startswith("$input.") or self.ref.startswith("$steps.")
        ):
            msg = "assert_exists.ref must be a workflow reference"
            raise ValueError(msg)
        if self.ref in {"$input.", "$steps."}:
            msg = "assert_exists.ref must not have an empty path"
            raise ValueError(msg)
        return self


class ShapeItemsSpec(BaseModel):
    """Project, rename, and explicitly cast list-shaped workflow data."""

    items: Any
    include_input: bool = False
    rename: dict[str, str] = Field(default_factory=dict)
    fields: dict[str, Any] = Field(default_factory=dict)
    casts: dict[str, ShapeCastType] = Field(default_factory=dict)
    required: list[str] = Field(default_factory=list)
    on_missing_required: MissingRequiredAction = "error"

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_projection(self) -> ShapeItemsSpec:
        if not self.include_input and not self.rename and not self.fields:
            msg = "shape_items with include_input: false must define fields or rename"
            raise ValueError(msg)
        for source, target in self.rename.items():
            if "." in source or "." in target:
                msg = "shape_items rename keys are top-level only"
                raise ValueError(msg)
            if not source or not target:
                msg = "shape_items rename keys must be non-empty strings"
                raise ValueError(msg)
        rename_targets = list(self.rename.values())
        if len(set(rename_targets)) != len(rename_targets):
            msg = "shape_items rename targets must be unique"
            raise ValueError(msg)
        return self


class JoinItemsSpec(BaseModel):
    """Join two list-shaped workflow payloads by resolved item keys."""

    left_items: Any
    right_items: Any
    left_key: Any
    right_key: Any
    join_type: Literal["inner"] = "inner"
    fields: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class FilterComparisonSpec(BaseModel):
    """Single predicate comparison used by filter_items."""

    left: Any
    op: FilterComparisonOp
    right: Any
    value_type: PredicateValueType | None = None

    model_config = {"extra": "forbid"}


class FilterItemsSpec(BaseModel):
    """Filter list-shaped workflow data using shared exact filters and comparisons."""

    items: Any
    where: dict[str, Any] = Field(default_factory=dict)
    comparisons: list[FilterComparisonSpec] = Field(default_factory=list)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_where_keys(self) -> FilterItemsSpec:
        for key in self.where:
            if "." in key:
                msg = "filter_items where keys are top-level only"
                raise ValueError(msg)
        return self


class AggregateValueSpec(BaseModel):
    """Value expression used by aggregate_items rollup measures."""

    value: Any
    value_type: PredicateValueType | None = None

    model_config = {"extra": "forbid"}


class AggregateDistinctSpec(BaseModel):
    """Value expression used by aggregate_items count_distinct."""

    value: Any

    model_config = {"extra": "forbid"}


class AggregateMeasureSpec(BaseModel):
    """One aggregate_items measure operation."""

    count: bool | None = None
    count_where: FilterComparisonSpec | None = None
    count_distinct: AggregateDistinctSpec | None = None
    sum: AggregateValueSpec | None = None
    min: AggregateValueSpec | None = None
    max: AggregateValueSpec | None = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_one_operation(self) -> AggregateMeasureSpec:
        operations = {
            "count": self.count,
            "count_where": self.count_where,
            "count_distinct": self.count_distinct,
            "sum": self.sum,
            "min": self.min,
            "max": self.max,
        }
        active = [name for name, value in operations.items() if value is not None]
        if len(active) != 1:
            msg = "aggregate measure must define exactly one operation"
            raise ValueError(msg)
        if self.count is not None and self.count is not True:
            msg = "aggregate measure count must be true"
            raise ValueError(msg)
        return self

    @property
    def operation(self) -> str:
        """Return the single configured aggregate operation name."""
        if self.count is not None:
            return "count"
        if self.count_where is not None:
            return "count_where"
        if self.count_distinct is not None:
            return "count_distinct"
        if self.sum is not None:
            return "sum"
        if self.min is not None:
            return "min"
        return "max"


class AggregateItemsSpec(BaseModel):
    """Aggregate list-shaped workflow rows into deterministic summary rows."""

    items: Any
    group_by: dict[str, Any] = Field(default_factory=dict)
    measures: dict[str, AggregateMeasureSpec]

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_fields(self) -> AggregateItemsSpec:
        if not self.measures:
            msg = "aggregate_items measures must not be empty"
            raise ValueError(msg)
        collisions = set(self.group_by).intersection(self.measures)
        if collisions:
            names = ", ".join(sorted(collisions))
            msg = f"aggregate_items group_by and measures field(s) collide: {names}"
            raise ValueError(msg)
        return self


class DedupeItemsSpec(BaseModel):
    """Deduplicate list-shaped workflow data by one or more resolved keys."""

    items: Any
    keys: list[Any]
    strategy: DeduplicationStrategy = "first"
    rank: Any | None = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_dedupe(self) -> DedupeItemsSpec:
        if not self.keys:
            msg = "dedupe_items keys must not be empty"
            raise ValueError(msg)
        if self.strategy in {"max", "min"} and self.rank is None:
            msg = f"dedupe_items strategy '{self.strategy}' requires rank"
            raise ValueError(msg)
        return self


class CandidateEvidenceSpec(BaseModel):
    """Evidence metadata mapping for generated candidate members."""

    refs: Any | None = None
    rationale: Any | None = None

    model_config = {"extra": "forbid"}


class MakeCandidatesSpec(BaseModel):
    """Build a relationship candidate set from list-shaped workflow data."""

    relationship_type: str
    items: Any
    from_type: Any
    from_id: Any
    to_type: Any
    to_id: Any
    properties: dict[str, Any] = Field(default_factory=dict)
    evidence: CandidateEvidenceSpec | None = None

    model_config = {"extra": "forbid"}


class ScoreSignalMappingSpec(BaseModel):
    """Map numeric scores to tri-state candidate signals."""

    path: str
    support_gte: float
    unsure_gte: float

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_thresholds(self) -> ScoreSignalMappingSpec:
        if self.support_gte < self.unsure_gte:
            msg = "score.support_gte must be greater than or equal to score.unsure_gte"
            raise ValueError(msg)
        return self


class EnumSignalMappingSpec(BaseModel):
    """Map enum-like values to tri-state candidate signals."""

    path: str
    map: dict[str, Literal["support", "unsure", "contradict"]]

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_non_empty_map(self) -> EnumSignalMappingSpec:
        if not self.map:
            msg = "enum.map must not be empty"
            raise ValueError(msg)
        return self


class MapSignalsSpec(BaseModel):
    """Convert raw provider output into a governed signal batch."""

    signal_source: str
    items: Any
    from_id: Any
    to_id: Any
    evidence: Any | None = None
    evidence_refs: Any | None = None
    score: ScoreSignalMappingSpec | None = None
    enum: EnumSignalMappingSpec | None = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_mapping_mode(self) -> MapSignalsSpec:
        mapping_modes = sum(mode is not None for mode in (self.score, self.enum))
        if mapping_modes != 1:
            msg = "map_signals must define exactly one of 'score' or 'enum'"
            raise ValueError(msg)
        return self


class ProposeRelationshipGroupSpec(BaseModel):
    """Assemble a governed relationship-group proposal from built-in artifacts."""

    relationship_type: str
    candidates_from: str
    signals_from: list[str]
    on_empty: Literal["complete"] | None = None
    thesis_text: Any = ""
    pending_refresh_mode: Literal["replace", "retain_missing"] = "replace"
    analysis_state: dict[str, Any] = Field(default_factory=dict)
    suggested_priority: Any | None = None
    proposed_by: Literal["human", "agent"] = "agent"

    model_config = {"extra": "forbid"}


class MakeEntitiesSpec(BaseModel):
    """Build an entity set from list-shaped workflow data."""

    entity_type: str
    items: Any
    entity_id: Any
    properties: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class MakeRelationshipsSpec(BaseModel):
    """Build a relationship set from list-shaped workflow data."""

    relationship_type: str
    items: Any
    from_type: Any
    from_id: Any
    to_type: Any
    to_id: Any
    properties: dict[str, Any] = Field(default_factory=dict)
    evidence: CandidateEvidenceSpec | None = None

    model_config = {"extra": "forbid"}


class ApplyEntitiesSpec(BaseModel):
    """Apply an entity set to staged canonical state."""

    entities_from: str

    model_config = {"extra": "forbid"}


class ApplyRelationshipsSpec(BaseModel):
    """Apply a relationship set to staged canonical state."""

    relationships_from: str

    model_config = {"extra": "forbid"}


class ApplyAllSpec(BaseModel):
    """Apply multiple entity and relationship sets in explicit order."""

    entities_from: list[str] = Field(default_factory=list)
    relationships_from: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_inputs(self) -> ApplyAllSpec:
        if not self.entities_from and not self.relationships_from:
            msg = "apply_all requires entities_from, relationships_from, or both"
            raise ValueError(msg)
        return self


StepKind = Literal[
    "query",
    "provider",
    "assert",
    "assert_not_truncated",
    "assert_count",
    "assert_exists",
    "shape_items",
    "join_items",
    "filter_items",
    "aggregate_items",
    "dedupe_items",
    "make_candidates",
    "map_signals",
    "propose_relationship_group",
    "make_entities",
    "make_relationships",
    "apply_entities",
    "apply_relationships",
    "apply_all",
]
"""The workflow step kinds, grouped into Read/Compute/Build/Write phases."""


class WorkflowStepSchema(BaseModel):
    """Single step in a declarative workflow.

    Exactly one step kind must be set per step. The kinds fall into four
    logical phases:

    Phase 1 — Read (pull data in):
        query               Run a named or inline query against the graph.

    Phase 2 — Compute (transform data):
        provider            Call an external provider (function/model/tool).
        assert              Guard condition; fails the workflow if false.
        assert_not_truncated
                            Guard that read-derived context is complete.
        assert_count        Guard a read/result collection count.
        assert_exists       Guard one required intermediate reference.
        shape_items         Project, rename, and cast list-shaped data.
        join_items          Indexed inner join over two item sets.
        filter_items        Filter list-shaped data with shared predicates.
        aggregate_items     Group list-shaped data into summary rows.
        dedupe_items        Deterministically deduplicate list-shaped data.

    Phase 3 — Build (structure results for the graph):
        make_candidates     Build relationship candidate pairs from list data.
        map_signals         Convert provider scores/enums into tri-state signals.
        propose_relationship_group
                            Assemble candidates + signals into a governed
                            group proposal.
        make_entities       Build entity objects from list data.
        make_relationships  Build relationship objects from list data.

    Phase 4 — Write (mutate the graph, only in ``apply`` mode):
        apply_entities      Write built entities into the graph.
        apply_relationships Write built relationships into the graph.
        apply_all           Write explicitly listed entity and relationship sets.

    Steps reference earlier outputs via ``$steps.<id>`` or ``$item``
    (in list contexts). Typical flows::

        query → provider → make_candidates → propose_relationship_group
                         → map_signals    ↗

        query → provider → make_relationships → apply_relationships
    """

    id: str
    query: str | NamedQuerySchema | None = None
    provider: str | None = None
    assert_spec: AssertSpec | None = Field(alias="assert", default=None)
    assert_not_truncated: AssertNotTruncatedSpec | None = None
    assert_count: AssertCountSpec | None = None
    assert_exists: AssertExistsSpec | None = None
    shape_items: ShapeItemsSpec | None = None
    join_items: JoinItemsSpec | None = None
    filter_items: FilterItemsSpec | None = None
    aggregate_items: AggregateItemsSpec | None = None
    dedupe_items: DedupeItemsSpec | None = None
    make_candidates: MakeCandidatesSpec | None = None
    map_signals: MapSignalsSpec | None = None
    propose_relationship_group: ProposeRelationshipGroupSpec | None = None
    make_entities: MakeEntitiesSpec | None = None
    make_relationships: MakeRelationshipsSpec | None = None
    apply_entities: ApplyEntitiesSpec | None = None
    apply_relationships: ApplyRelationshipsSpec | None = None
    apply_all: ApplyAllSpec | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    relationship_state: Any | None = None
    include_source: bool = False
    input: dict[str, Any] = Field(default_factory=dict)
    as_: str | None = Field(alias="as", default=None)

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @model_validator(mode="after")
    def validate_step_shape(self) -> WorkflowStepSchema:
        step_candidates = {
            "query": self.query,
            "provider": self.provider,
            "assert": self.assert_spec,
            "assert_not_truncated": self.assert_not_truncated,
            "assert_count": self.assert_count,
            "assert_exists": self.assert_exists,
            "shape_items": self.shape_items,
            "join_items": self.join_items,
            "filter_items": self.filter_items,
            "aggregate_items": self.aggregate_items,
            "dedupe_items": self.dedupe_items,
            "make_candidates": self.make_candidates,
            "map_signals": self.map_signals,
            "propose_relationship_group": self.propose_relationship_group,
            "make_entities": self.make_entities,
            "make_relationships": self.make_relationships,
            "apply_entities": self.apply_entities,
            "apply_relationships": self.apply_relationships,
            "apply_all": self.apply_all,
        }
        active_step_kinds = [
            name for name, candidate in step_candidates.items() if candidate is not None
        ]
        if len(active_step_kinds) != 1:
            valid = ", ".join(f"'{k}'" for k in get_args(StepKind))
            raise ValueError(f"Workflow step must define exactly one of {valid}")

        step_kind = active_step_kinds[0]
        step_policies = {
            "query": {"require_as": True, "allow_params": True, "allow_input": False},
            "provider": {"require_as": True, "allow_params": False, "allow_input": True},
            "assert": {"require_as": False, "allow_params": False, "allow_input": False},
            "assert_not_truncated": {
                "require_as": False,
                "allow_params": False,
                "allow_input": False,
            },
            "assert_count": {"require_as": False, "allow_params": False, "allow_input": False},
            "assert_exists": {"require_as": False, "allow_params": False, "allow_input": False},
        }
        policy = step_policies.get(
            step_kind,
            {"require_as": True, "allow_params": False, "allow_input": False},
        )
        step_label = "Assert" if step_kind.startswith("assert") else step_kind

        if policy["require_as"]:
            if self.as_ is None:
                msg = f"{step_kind} workflow steps require 'as'"
                raise ValueError(msg)
        elif self.as_ is not None:
            msg = f"{step_label} workflow steps may not define 'as'"
            raise ValueError(msg)

        if not policy["allow_params"] and self.params:
            msg = f"{step_label} workflow steps may not define 'params'"
            raise ValueError(msg)

        if step_kind != "query" and self.relationship_state is not None:
            msg = f"{step_label} workflow steps may not define 'relationship_state'"
            raise ValueError(msg)

        if step_kind != "query" and self.include_source:
            msg = f"{step_label} workflow steps may not define 'include_source'"
            raise ValueError(msg)

        if not policy["allow_input"] and self.input:
            msg = f"{step_label} workflow steps may not define 'input'"
            raise ValueError(msg)

        return self


class WorkflowSchema(BaseModel):
    """Declarative composition of query and provider steps."""

    description: str | None = None
    type: WorkflowType = "utility"
    contract_in: ContractReference = "cruxible.EmptyInput"
    contract_out: ContractReference | None = None
    steps: list[WorkflowStepSchema]
    returns: str

    model_config = {"extra": "forbid"}


class WorkflowTestExpectSchema(BaseModel):
    """Minimal assertions for config-defined workflow tests."""

    output_equals: Any | None = None
    output_contains: dict[str, Any] | None = None
    receipt_contains_provider: str | list[str] | None = None
    error_contains: str | None = None

    @property
    def required_providers(self) -> list[str]:
        if self.receipt_contains_provider is None:
            return []
        if isinstance(self.receipt_contains_provider, str):
            return [self.receipt_contains_provider]
        return self.receipt_contains_provider


class WorkflowTestSchema(BaseModel):
    """Fixture for exercising a workflow with expected outputs/evidence."""

    name: str
    workflow: str
    input: dict[str, Any] = Field(default_factory=dict)
    expect: WorkflowTestExpectSchema = Field(default_factory=WorkflowTestExpectSchema)


class RuntimeConfigSchema(BaseModel):
    """Runtime behavior options for local execution and audit capture."""

    trace_payloads: TracePayloadRetention = "preview"
    mutation_payloads: MutationPayloadRetention = "metadata"
    default_write_policy: Literal["direct", "proposal_only"] = "direct"
    """Instance-wide default direct-write governance.

    Applies to entity/relationship types whose own ``write_policy`` is unset
    (``None``). ``"proposal_only"`` makes the whole instance proposal-only unless
    a type opts out with an explicit ``write_policy: direct``. The
    ``CRUXIBLE_REFUSE_DIRECT_WRITES`` env kill-switch overrides this and every
    per-type opt-out. See ``service/direct_write_policy.py``.
    """

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Top-Level Config
# ---------------------------------------------------------------------------


class CoreConfig(BaseModel):
    """Top-level Cruxible Core configuration.

    Parsed from YAML. Defines the complete decision domain: entity types,
    relationships, queries, constraints, providers, and workflows.
    """

    version: str = "1.0"
    name: str
    description: str | None = None
    cruxible_version: str | None = None
    extends: str | None = None

    entity_types: dict[str, EntityTypeSchema] = Field(default_factory=dict)
    relationships: list[RelationshipSchema] = Field(default_factory=list)
    named_queries: dict[str, NamedQuerySchema] = Field(default_factory=dict)
    enums: dict[str, EnumSchema] = Field(default_factory=dict)
    constraints: list[ConstraintSchema] = Field(default_factory=list)
    feedback_profiles: dict[str, FeedbackProfileSchema] = Field(default_factory=dict)
    outcome_profiles: dict[str, OutcomeProfileSchema] = Field(default_factory=dict)
    quality_checks: list[QualityCheckSchema] = Field(default_factory=list)
    mutation_guards: list[MutationGuardSchema] = Field(default_factory=list)
    decision_policies: list[DecisionPolicySchema] = Field(default_factory=list)
    contracts: dict[str, ContractSchema] = Field(default_factory=dict)
    artifacts: dict[str, ProviderArtifactSchema] = Field(default_factory=dict)
    providers: dict[str, ProviderSchema] = Field(default_factory=dict)
    workflows: dict[str, WorkflowSchema] = Field(default_factory=dict)
    runtime: RuntimeConfigSchema = Field(default_factory=RuntimeConfigSchema)
    tests: list[WorkflowTestSchema] = Field(default_factory=list)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_root_config_minimums(self) -> CoreConfig:
        if self.extends is None and not self.entity_types:
            raise ValueError("entity_types must not be empty unless extends is set")
        return self

    @model_validator(mode="after")
    def validate_enum_refs(self) -> CoreConfig:
        """Check enum_ref usage and defaults across every PropertySchema."""
        for location, prop in _iter_config_properties(self):
            if prop.enum_ref is None:
                continue
            enum_schema = self.enums.get(prop.enum_ref)
            if enum_schema is None:
                msg = f"{location}: enum_ref '{prop.enum_ref}' is not defined in enums"
                raise ValueError(msg)
            if prop.default is not None and prop.default not in enum_schema.values:
                allowed = ", ".join(enum_schema.values)
                msg = f"{location}: default must be one of enum_ref '{prop.enum_ref}': {allowed}"
                raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_json_schemas(self) -> CoreConfig:
        """Check supported json_schema shape and nested enum_ref usage."""
        from cruxible_core.config.json_schema_validation import validate_json_schema_shape

        for location, prop in _iter_config_properties(self):
            if prop.json_schema is None:
                continue
            validate_json_schema_shape(
                prop.json_schema,
                self.enums,
                f"{location}.json_schema",
            )
        return self

    @model_validator(mode="after")
    def validate_query_related_exclusions(self) -> CoreConfig:
        """Check that related-edge predicate specs use declared canonical relationships."""
        declared_relationships = {rel.name for rel in self.relationships}
        for query_name, query in self.named_queries.items():
            for include_alias, include in query.include.items():
                if self.resolve_relationship_reference(include.relationship) is None:
                    if self.extends is not None:
                        continue
                    msg = (
                        f"Named query '{query_name}' include '{include_alias}' "
                        f"references unknown relationship '{include.relationship}'"
                    )
                    raise ValueError(msg)
                for field_name, related_specs in (
                    ("where_related", include.where_related),
                    ("where_not_related", include.where_not_related),
                ):
                    for related in related_specs:
                        if related.relationship not in declared_relationships:
                            if self.extends is not None:
                                continue
                            msg = (
                                f"Named query '{query_name}' include '{include_alias}' "
                                f"references unknown relationship '{related.relationship}' "
                                f"in {field_name}"
                            )
                            raise ValueError(msg)
            for step_index, step in enumerate(query.traversal):
                for exclusion in step.exclude_if_related:
                    if exclusion.relationship not in declared_relationships:
                        if self.extends is not None:
                            continue
                        msg = (
                            f"Named query '{query_name}' traversal step {step_index} "
                            f"references unknown relationship '{exclusion.relationship}' "
                            "in exclude_if_related"
                        )
                        raise ValueError(msg)
                for field_name, related_specs in (
                    ("where_related", step.where_related),
                    ("where_not_related", step.where_not_related),
                ):
                    for related in related_specs:
                        if related.relationship not in declared_relationships:
                            if self.extends is not None:
                                continue
                            msg = (
                                f"Named query '{query_name}' traversal step {step_index} "
                                f"references unknown relationship '{related.relationship}' "
                                f"in {field_name}"
                            )
                            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_mutation_guard_related_exclusions(self) -> CoreConfig:
        """Check that mutation-guard related-edge specs use declared relationships."""
        declared_relationships = {rel.name for rel in self.relationships}
        for guard in self.mutation_guards:
            for field_name, related_specs in (
                ("where_related", guard.where_related),
                ("where_not_related", guard.where_not_related),
            ):
                for related in related_specs:
                    if related.relationship not in declared_relationships:
                        if self.extends is not None:
                            continue
                        msg = (
                            f"Mutation guard '{guard.name}' references unknown "
                            f"relationship '{related.relationship}' in {field_name}"
                        )
                        raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_query_order_enum_refs(self) -> CoreConfig:
        """Check that query ordering enum refs target declared ordered enums."""
        for query_name, query in self.named_queries.items():
            for index, order in enumerate(query.order_by):
                self._validate_order_enum_ref(
                    order,
                    f"Named query '{query_name}' order_by[{index}]",
                )
            for include_alias, include in query.include.items():
                for index, order in enumerate(include.order_by):
                    self._validate_order_enum_ref(
                        order,
                        (f"Named query '{query_name}' include '{include_alias}' order_by[{index}]"),
                    )
        return self

    def _validate_order_enum_ref(self, order: QueryOrderSpec, location: str) -> None:
        if order.enum_ref is None:
            return
        enum_schema = self.enums.get(order.enum_ref)
        if enum_schema is None:
            msg = f"{location} references unknown enum_ref '{order.enum_ref}'"
            raise ValueError(msg)
        if enum_schema.ordered != "low_to_high":
            msg = f"{location} references enum_ref '{order.enum_ref}', but that enum is not ordered"
            raise ValueError(msg)

    @model_validator(mode="after")
    def validate_relationship_query_returns(self) -> CoreConfig:
        """Check collection/traversal query return declarations."""
        for query_name, query in self.named_queries.items():
            if query.mode == "collection" and query.result_shape == "entity":
                entity_type = _normalize_query_entity_returns(query.returns)
                if entity_type not in self.entity_types:
                    if self.extends is not None:
                        continue
                    msg = (
                        f"Named query '{query_name}' with collection entity "
                        f"collection returns unknown entity '{query.returns}'"
                    )
                    raise ValueError(msg)
            if query.result_shape != "relationship":
                continue
            if query.mode == "collection":
                resolved = self.resolve_relationship_reference(query.returns)
                if resolved is None:
                    if self.extends is not None:
                        continue
                    msg = (
                        f"Named query '{query_name}' with collection relationship "
                        f"collection returns unknown relationship '{query.returns}'"
                    )
                    raise ValueError(msg)
                rel_schema, is_reverse = resolved
                if is_reverse:
                    msg = (
                        f"Named query '{query_name}' with collection relationship "
                        f"collection must return canonical relationship '{rel_schema.name}', "
                        f"not reverse alias '{query.returns}'"
                    )
                    raise ValueError(msg)
                continue
            if not query.traversal:
                msg = (
                    f"Named query '{query_name}' with result_shape 'relationship' "
                    "requires traversal"
                )
                raise ValueError(msg)
            final_step = query.traversal[-1]
            final_relationships: list[str] = []
            for rel_ref in final_step.relationship_types:
                resolved = self.resolve_relationship_reference(rel_ref)
                if resolved is None:
                    continue
                rel_schema, _is_reverse = resolved
                final_relationships.append(rel_schema.name)
            final_relationships = list(dict.fromkeys(final_relationships))
            if len(final_relationships) != 1 or query.returns != final_relationships[0]:
                expected = ", ".join(final_relationships) if final_relationships else "<unknown>"
                msg = (
                    f"Named query '{query_name}' with result_shape 'relationship' must set "
                    f"returns to its final relationship type ({expected})"
                )
                raise ValueError(msg)
        return self

    def get_relationship(self, name: str) -> RelationshipSchema | None:
        """Find a relationship schema by name."""
        for rel in self.relationships:
            if rel.name == name:
                return rel
        return None

    def resolve_relationship_reference(
        self,
        name: str,
    ) -> tuple[RelationshipSchema, bool] | None:
        """Resolve a canonical relationship name or reverse-name alias.

        Returns the canonical relationship schema plus a boolean indicating
        whether the reference used the reverse-facing alias.
        """
        for rel in self.relationships:
            if rel.name == name:
                return rel, False
        for rel in self.relationships:
            if rel.reverse_name == name:
                return rel, True
        return None

    def get_entity_type(self, name: str) -> EntityTypeSchema | None:
        """Find an entity type schema by name."""
        return self.entity_types.get(name)

    def get_feedback_profile(self, relationship_type: str) -> FeedbackProfileSchema | None:
        """Find a feedback profile by relationship type."""
        return self.feedback_profiles.get(relationship_type)

    def get_outcome_profile(self, profile_key: str) -> OutcomeProfileSchema | None:
        """Find an outcome profile by key."""
        return self.outcome_profiles.get(profile_key)


def _iter_config_properties(config: CoreConfig) -> list[tuple[str, PropertySchema]]:
    """Return every property schema with a human-readable config path."""
    properties: list[tuple[str, PropertySchema]] = []
    for entity_name, entity in config.entity_types.items():
        for prop_name, prop in entity.properties.items():
            properties.append((f"entity_types.{entity_name}.properties.{prop_name}", prop))
    for relationship in config.relationships:
        for prop_name, prop in relationship.properties.items():
            properties.append((f"relationships.{relationship.name}.properties.{prop_name}", prop))
    for contract_name, contract_schema in config.contracts.items():
        for field_name, prop in contract_schema.fields.items():
            properties.append((f"contracts.{contract_name}.fields.{field_name}", prop))
    for provider_name, provider in config.providers.items():
        for direction, provider_contract_ref in (
            ("contract_in", provider.contract_in),
            ("contract_out", provider.contract_out),
        ):
            if isinstance(provider_contract_ref, ContractSchema):
                for field_name, prop in provider_contract_ref.fields.items():
                    properties.append(
                        (f"providers.{provider_name}.{direction}.fields.{field_name}", prop)
                    )
    for workflow_name, workflow in config.workflows.items():
        for direction, workflow_contract_ref in (
            ("contract_in", workflow.contract_in),
            ("contract_out", workflow.contract_out),
        ):
            if workflow_contract_ref is None:
                continue
            if isinstance(workflow_contract_ref, ContractSchema):
                for field_name, prop in workflow_contract_ref.fields.items():
                    properties.append(
                        (f"workflows.{workflow_name}.{direction}.fields.{field_name}", prop)
                    )
    return properties
