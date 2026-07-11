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
    ├── gates: dict[str, GateSchema]
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
from typing import Annotated, Any, Literal, cast, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StrictBool,
    ValidationInfo,
    field_validator,
    model_validator,
)

from cruxible_core.config.auth_managed import AUTH_MANAGED_CREDENTIAL_PROPERTY_NAMES
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

# Tiers a type may declare as its direct-write requirement. Deliberately NOT the
# full permission ladder: ``read_only`` is not a write tier, and raising the
# requirement above ``graph_write`` is the job of ``write_policy``
# (proposal_only / mint_only are hard constraints, not tier checks).
_WRITE_TIER_VALUES = ("governed_write", "graph_write")


def _validate_write_tier_value(value: Any) -> Any:
    """Pre-validate ``write_tier`` with an actionable lint message."""
    if value is None or value in _WRITE_TIER_VALUES:
        return value
    allowed = ", ".join(f"'{tier}'" for tier in _WRITE_TIER_VALUES)
    msg = (
        f"write_tier must be one of {allowed} (got {value!r}). It declares the "
        "minimum permission tier allowed to direct-write this type; 'read_only' "
        "is not a write tier, and restricting writes harder than 'graph_write' "
        "is expressed with write_policy (proposal_only / mint_only), not a tier."
    )
    raise ValueError(msg)


def _reject_write_tier_on_non_direct_type(
    write_tier: str | None,
    write_policy: str | None,
) -> None:
    """Reject ``write_tier`` on types whose explicit policy refuses direct writes.

    A declared ``write_tier`` opens a direct-write surface; an explicit
    ``proposal_only``/``mint_only`` ``write_policy`` refuses that surface for
    every tier. Declaring both is contradictory config — fail at lint rather
    than ship a write_tier that can never take effect.
    """
    if write_tier is not None and write_policy in {"proposal_only", "mint_only"}:
        msg = (
            f"write_tier '{write_tier}' conflicts with write_policy "
            f"'{write_policy}': a type that refuses direct writes cannot "
            "declare a direct-write tier"
        )
        raise ValueError(msg)


class EntityTypeSchema(BaseModel):
    """Schema for an entity type definition."""

    description: str | None = None
    properties: dict[str, PropertySchema]
    constraints: list[str] = Field(default_factory=list)
    write_policy: Literal["direct", "proposal_only", "mint_only"] | None = None
    """Per-type direct-write governance.

    ``None`` inherits ``runtime.default_write_policy``; ``"direct"`` explicitly
    opts out of the instance default (but not the env kill-switch);
    ``"proposal_only"`` refuses direct entity adds for this type but still admits
    the governed verbs (``workflow_apply`` / ``group_resolve``); ``"mint_only"``
    is writable ONLY by the ``token_mint`` source and refuses ALL other sources,
    INCLUDING ``workflow_apply`` / ``group_resolve``, intended for auth-managed
    identity types. Resolved by ``service/direct_write_policy.py`` and enforced
    at the ``graph/operations.py`` chokepoint; ``mint_only`` config-declared
    write targets are additionally static-rejected at config load (see
    ``CoreConfig.validate_mint_only_entity_writes``).
    """
    auth_managed: bool = False
    """Mark this entity type as materialized from runtime credentials.

    Auth-managed is intentionally a general type-level marker, not an ``Actor``
    special case. Runtime credential mint/claim/rotation materializes one entity
    of every auth-managed type through the ``token_mint`` graph source. The
    marker must be paired with ``write_policy: mint_only`` so no config-declared
    write path can author credential identities.
    """
    write_tier: Literal["governed_write", "graph_write"] | None = None
    """Minimum permission tier whose holders may direct-write this type.

    ``None`` keeps the direct-write verbs at their default ``graph_write``
    requirement. ``"governed_write"`` opens this type's direct-write surface to
    ``governed_write`` actors (a kit-declared low-trust write surface);
    ``"graph_write"`` is an explicit restatement of the default. The tier can
    only be lowered below ``graph_write`` — restricting writes harder than the
    tier ladder is ``write_policy``'s job, so ``read_only``/``admin`` are
    rejected. Mutation guards and ``write_policy`` still apply after the tier
    check. Resolved by ``service/direct_write_policy.required_direct_write_tier``
    and enforced at the ``runtime/api.py`` direct-write facades.
    """

    @field_validator("write_tier", mode="before")
    @classmethod
    def validate_write_tier(cls, value: Any) -> Any:
        return _validate_write_tier_value(value)

    @model_validator(mode="after")
    def apply_graph_property_defaults(self) -> EntityTypeSchema:
        self.properties = _apply_graph_property_defaults(self.properties)
        _reject_write_tier_on_non_direct_type(self.write_tier, self.write_policy)
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
    """Per-signal-source guardrails for candidate group proposals.

    Unknown keys are refused: a typo'd enforcement flag (e.g. a misspelled
    ``require_evidence_on_support``) must be a config error, never a
    silently disabled guard.
    """

    model_config = ConfigDict(extra="forbid")

    role: Literal["blocking", "required", "advisory"] = "required"
    always_review_on_unsure: bool = False
    require_evidence_on_support: bool = False
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
    write_tier: Literal["governed_write", "graph_write"] | None = None
    """Minimum permission tier whose holders may direct-write this relationship.

    Same semantics as ``EntityTypeSchema.write_tier``: ``None`` keeps the
    default ``graph_write`` requirement; ``"governed_write"`` opens direct edge
    writes of this type to ``governed_write`` actors. Endpoint entity types are
    NOT part of the check — an edge write mutates only the edge, so a
    governed-tier relationship may attach to entities the caller could not
    write directly. Mutation guards and ``write_policy`` still apply.
    """

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @field_validator("write_tier", mode="before")
    @classmethod
    def validate_write_tier(cls, value: Any) -> Any:
        return _validate_write_tier_value(value)

    @model_validator(mode="after")
    def validate_proposal_identity(self) -> RelationshipSchema:
        self.properties = _apply_graph_property_defaults(self.properties)
        if self.proposal_identity == "relationship_tuple" and self.proposal_policy is None:
            msg = (
                "proposal_identity 'relationship_tuple' requires a governed proposal_policy section"
            )
            raise ValueError(msg)
        _reject_write_tier_on_non_direct_type(self.write_tier, self.write_policy)
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


def _result_property_refs(select: dict[str, Any]) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    for ref in _collect_query_refs(select):
        scope, path = _split_query_ref(ref)
        if scope != "result":
            continue
        parts = path.split(".")
        if len(parts) >= 2 and parts[0] == "properties":
            refs.append((ref, parts[1]))
    return refs


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
# Gate Schema
# ---------------------------------------------------------------------------


GATE_KINDS = frozenset({"git-pre-push"})
"""Source-adapter kinds a gate may declare. v1's only member is git-pre-push.

The kind names the SOURCE of candidate values (which adapter derives them),
never the evaluation: evaluation is always the declared condition against
state. Config lint rejects kinds outside this set; the CLI additionally fails
closed (exit 2) if it is ever asked to check a gate whose kind it has no
adapter for.
"""

_GATE_CONDITION_RESERVED_KEYS = frozenset({"query"})
"""Condition keys reserved for future variants.

``condition`` today is a property-equality predicate (every key is an entity
property that must equal its value). The key ``query`` is reserved — and
refused — so a future named-query condition variant
(``condition: {query: <named-query>}``) can be added WITHOUT a breaking
schema change: no existing declaration can already be using the spelling.
"""


class GitPrePushAdapterSchema(BaseModel):
    """Adapter config for gates of kind ``git-pre-push``.

    Branch scoping belongs to the SOURCE, not the gate: which pushed refs are
    in scope is a property of how candidates are derived from the pre-push
    protocol, so the pattern lives here rather than on the core gate.
    """

    branch_pattern: str
    """Glob over remote ref names (e.g. ``refs/heads/main``); only pushed
    refs matching it are gated."""

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_pattern(self) -> GitPrePushAdapterSchema:
        if not self.branch_pattern.strip():
            msg = "branch_pattern must be a non-empty ref pattern (e.g. refs/heads/main)"
            raise ValueError(msg)
        return self


class GateSchema(BaseModel):
    """Declared repo gate: state must agree before the world may act.

    Doctrine: a GUARD blocks a write INTO state (inbound); a GATE lets the
    world act only if state agrees (outbound). Gates are outbound
    exclusively.

    A gate is kind-based: ``kind`` names the source adapter that derives
    candidate values (v1: ``git-pre-push`` reads git's pre-push protocol and
    yields merged-in parent SHAs). A candidate is satisfied when at least one
    entity of ``entity_type`` carries the candidate in ``match_property`` AND
    matches the declared ``condition``. Core knows no ontology — the
    declaration supplies it; domain knowledge lives in the condition
    (declarative), never in the adapter (plumbing).
    """

    kind: str
    entity_type: str
    match_property: str
    condition: dict[str, str | int | float | bool]
    adapter: GitPrePushAdapterSchema | None = None
    description: str | None = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_gate_shape(self) -> GateSchema:
        if not self.kind.strip():
            msg = "kind must name a gate source adapter (e.g. git-pre-push)"
            raise ValueError(msg)
        if not self.condition:
            msg = "condition must declare at least one property=value pair"
            raise ValueError(msg)
        reserved = _GATE_CONDITION_RESERVED_KEYS.intersection(self.condition)
        if reserved:
            msg = (
                f"condition key(s) {sorted(reserved)} are reserved for a future "
                "named-query condition variant and cannot be used as property "
                "predicates"
            )
            raise ValueError(msg)
        if self.match_property in self.condition:
            msg = (
                "condition may not constrain match_property "
                f"'{self.match_property}'; the candidate value supplies that value"
            )
            raise ValueError(msg)
        if self.kind == "git-pre-push" and self.adapter is None:
            msg = (
                "gates of kind git-pre-push require an adapter config "
                "(adapter: {branch_pattern: refs/heads/main})"
            )
            raise ValueError(msg)
        return self


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
    """Actor identity allow-list condition for config-defined mutation guards.

    ``distinct_from_creation_actor`` additionally requires the acting actor to
    differ from the actor recorded in the target entity's CREATION provenance
    (its committed creation receipt) -- never a writable property, which agents
    could rewrite to launder self-approval. Strictly boolean: YAML ``true`` /
    ``false`` only, no string/int coercion.
    """

    type: Literal["actor"]
    allowed_actor_ids: list[str] = Field(min_length=1)
    distinct_from_creation_actor: StrictBool = False

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
                raise ValueError("relationship evidence guards do not support related-edge scoping")
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
    """Build a relationship candidate set from list-shaped workflow data.

    ``properties: auto`` maps every property declared on the relationship type
    from ``$item.<property_name>``, mirroring MakeRelationshipsSpec.
    """

    relationship_type: str
    items: Any
    from_type: Any
    from_id: Any
    to_type: Any
    to_id: Any
    properties: dict[str, Any] | Literal["auto"] = Field(default_factory=dict)
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
    """Build an entity set from list-shaped workflow data.

    ``properties: auto`` maps every property declared on the entity type from
    ``$item.<property_name>`` — the 1:1 boilerplate case. Rows must then carry
    every declared key (null for unset optional properties).
    """

    entity_type: str
    items: Any
    entity_id: Any
    properties: dict[str, Any] | Literal["auto"] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class MakeRelationshipsSpec(BaseModel):
    """Build a relationship set from list-shaped workflow data.

    ``properties: auto`` maps every property declared on the relationship type
    from ``$item.<property_name>``, mirroring MakeEntitiesSpec.
    """

    relationship_type: str
    items: Any
    from_type: Any
    from_id: Any
    to_type: Any
    to_id: Any
    properties: dict[str, Any] | Literal["auto"] = Field(default_factory=dict)
    evidence: CandidateEvidenceSpec | None = None

    model_config = {"extra": "forbid"}


class RegisterSourceArtifactsSpec(BaseModel):
    """Register source artifacts from already-loaded workflow row data."""

    items: Any
    artifact_id: Any
    content: Any
    kind: Literal["markdown"]
    label: Any | None = None
    original_uri: Any | None = None
    retention: Literal["manifest_only", "archive"] | None = Field(
        default=None,
        description=(
            "An existing artifact with identical content is a noop; differing "
            "label/original_uri/retention are NOT applied. Re-register under a "
            "new id to change retention."
        ),
    )

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
    "register_source_artifacts",
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

    Phase 4 — Write (mutate canonical state, only in ``apply`` mode):
        register_source_artifacts
                            Register source artifacts from workflow row data.
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
    register_source_artifacts: RegisterSourceArtifactsSpec | None = None
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
            "register_source_artifacts": self.register_source_artifacts,
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


def workflow_step_kind(step: WorkflowStepSchema) -> StepKind:
    """Return the active step kind for a validated workflow step.

    ``validate_step_shape`` guarantees exactly one kind field is set.
    """
    for kind in get_args(StepKind):
        field_name = "assert_spec" if kind == "assert" else kind
        if getattr(step, field_name) is not None:
            return cast("StepKind", kind)
    msg = f"workflow step '{step.id}' has no active step kind"
    raise ValueError(msg)


def workflow_step_wire(step: WorkflowStepSchema) -> dict[str, Any]:
    """Serialize one workflow step in the discriminated wire shape.

    Emits ``{id, kind, config}`` where ``config`` is the active kind's value
    (a string for ``provider`` and named-query references, an object
    otherwise). Structural fields (``as``, ``params``, ``input``,
    ``relationship_state``, ``include_source``) appear only when meaningful.
    """
    dumped = step.model_dump(mode="json", by_alias=True)
    kind = workflow_step_kind(step)
    wire: dict[str, Any] = {"id": step.id, "kind": kind, "config": dumped[kind]}
    if step.as_ is not None:
        wire["as"] = step.as_
    if step.params:
        wire["params"] = dumped["params"]
    if step.input:
        wire["input"] = dumped["input"]
    if step.relationship_state is not None:
        wire["relationship_state"] = dumped["relationship_state"]
    if step.include_source:
        wire["include_source"] = True
    return wire


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
    gates: dict[str, GateSchema] = Field(default_factory=dict)
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

    _compact_all_adjacent_queries: dict[str, dict[str, Any]] = PrivateAttr(default_factory=dict)

    model_config = {"extra": "forbid"}

    def _is_partial_layer(self, info: ValidationInfo) -> bool:
        """Return whether this config is one layer of a composition, not a full config.

        A partial layer may reference names owned by an earlier layer, so
        cross-reference checks are deferred to post-compose validation. A layer
        is partial when it declares ``extends`` or when the loader marks it as an
        overlay kit entry config (extends-less overlay kits resolve their base
        layer from the manifest's ``target_state``).
        """
        if self.extends is not None:
            return True
        context = info.context or {}
        return bool(context.get("partial_layer"))

    @model_validator(mode="after")
    def validate_root_config_minimums(self, info: ValidationInfo) -> CoreConfig:
        if not self._is_partial_layer(info) and not self.entity_types:
            raise ValueError(
                "entity_types must not be empty unless the config extends a base layer"
            )
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
    def validate_query_related_exclusions(self, info: ValidationInfo) -> CoreConfig:
        """Check that related-edge predicate specs use declared canonical relationships."""
        partial_layer = self._is_partial_layer(info)
        declared_relationships = {rel.name for rel in self.relationships}
        for query_name, query in self.named_queries.items():
            for include_alias, include in query.include.items():
                if self.resolve_relationship_reference(include.relationship) is None:
                    if partial_layer:
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
                            if partial_layer:
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
                        if partial_layer:
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
                            if partial_layer:
                                continue
                            msg = (
                                f"Named query '{query_name}' traversal step {step_index} "
                                f"references unknown relationship '{related.relationship}' "
                                f"in {field_name}"
                            )
                            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_mutation_guard_related_exclusions(self, info: ValidationInfo) -> CoreConfig:
        """Check that mutation-guard related-edge specs use declared relationships."""
        partial_layer = self._is_partial_layer(info)
        declared_relationships = {rel.name for rel in self.relationships}
        for guard in self.mutation_guards:
            for field_name, related_specs in (
                ("where_related", guard.where_related),
                ("where_not_related", guard.where_not_related),
            ):
                for related in related_specs:
                    if related.relationship not in declared_relationships:
                        if partial_layer:
                            continue
                        msg = (
                            f"Mutation guard '{guard.name}' references unknown "
                            f"relationship '{related.relationship}' in {field_name}"
                        )
                        raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_mint_only_entity_writes(self, info: ValidationInfo) -> CoreConfig:
        """Reject config-declared entity writes targeting a ``mint_only`` type.

        A ``mint_only`` entity type may be written ONLY by the ``token_mint``
        source. The runtime chokepoint enforces that for direct/batch/MCP/governed
        writes, but a workflow ``make_entities`` step builds entities that are
        later applied with the governed ``workflow_apply`` source — which BYPASSES
        the chokepoint refusal path. The only place to catch a config that wires a
        ``mint_only`` type into ``make_entities`` is here, fail-closed at load.

        Read the DECLARED ``write_policy`` directly (not the env-resolved policy):
        config verification is about the static config, not a runtime env state.
        ``make_entities`` is the complete config surface that creates entities —
        ``apply_entities`` / ``apply_all`` only re-reference a prior
        ``make_entities`` alias, and there is no seed-data config field.
        """
        mint_only_types = {
            name
            for name, schema in self.entity_types.items()
            if schema.write_policy == "mint_only" and not schema.auth_managed
        }
        if not mint_only_types:
            return self
        for wf, workflow in self.workflows.items():
            for i, step in enumerate(workflow.steps):
                if step.make_entities is None:
                    continue
                t = step.make_entities.entity_type
                if t in mint_only_types:
                    if self._is_partial_layer(info):
                        continue
                    msg = (
                        f"Workflow '{wf}' step {i} make_entities targets mint_only "
                        f"type '{t}', which may only be written by the token_mint source"
                    )
                    raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_auth_managed_entity_writes(self, info: ValidationInfo) -> CoreConfig:
        """Reject non-token config authorship for auth-managed entity types.

        ``auth_managed`` means the runtime credential store is the identity source
        of truth and the graph entity is materialized only by the ``token_mint``
        source. This validator is stricter than the generic ``mint_only`` check:
        auth-managed types must explicitly carry ``write_policy: mint_only`` and
        workflow aliases that would later be applied are rejected at declaration
        time. Provider output is not independently targetable in the current
        schema; provider-produced rows become entity writes only through
        ``make_entities``, which is tracked here.
        """
        auth_managed_types = {
            name for name, schema in self.entity_types.items() if schema.auth_managed
        }
        if not auth_managed_types:
            return self

        if not self._is_partial_layer(info):
            for entity_type in sorted(auth_managed_types):
                schema = self.entity_types[entity_type]
                if schema.write_policy != "mint_only":
                    msg = (
                        f"Auth-managed entity type '{entity_type}' must declare "
                        "write_policy: mint_only"
                    )
                    raise ValueError(msg)
                primary_key = schema.get_primary_key()
                unmaterializable = sorted(
                    prop_name
                    for prop_name, prop in schema.properties.items()
                    if prop_name != primary_key
                    and prop_name not in AUTH_MANAGED_CREDENTIAL_PROPERTY_NAMES
                    and not prop.optional
                    and prop.default is None
                )
                if unmaterializable:
                    joined = ", ".join(unmaterializable)
                    msg = (
                        f"Auth-managed entity type '{entity_type}' has required "
                        f"properties not materializable from runtime credentials: {joined}"
                    )
                    raise ValueError(msg)

        for workflow_name, workflow in self.workflows.items():
            for index, step in enumerate(workflow.steps):
                if step.make_entities is None:
                    continue
                entity_type = step.make_entities.entity_type
                if entity_type in auth_managed_types:
                    if self._is_partial_layer(info):
                        continue
                    msg = (
                        f"Workflow '{workflow_name}' step {index} make_entities "
                        f"targets auth-managed type '{entity_type}', which may only "
                        "be written by the token_mint source"
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
    def validate_query_select_property_refs(self, info: ValidationInfo) -> CoreConfig:
        """Check selected result-property refs against declared return identity."""
        partial_layer = self._is_partial_layer(info)
        for query_name, query in self.named_queries.items():
            if query.select is None or query.result_shape == "relationship":
                continue
            return_type = _normalize_query_entity_returns(query.returns)
            result_property_refs = _result_property_refs(query.select)
            if return_type == "AnyEntity":
                entry_type = query.entry_point
                if entry_type is None:
                    continue
                entry_schema = self.entity_types.get(entry_type)
                if entry_schema is None:
                    if partial_layer:
                        continue
                    raise ValueError(
                        f"Named query '{query_name}' select references entry type "
                        f"'{entry_type}', which is not a declared entity type"
                    )
                entry_pk = entry_schema.get_primary_key()
                if entry_pk is None:
                    continue
                for ref, prop_name in result_property_refs:
                    if prop_name == entry_pk:
                        raise ValueError(
                            f"Named query '{query_name}' select reference '{ref}' uses "
                            f"entry type '{entry_type}' primary key '{entry_pk}'; identity "
                            "columns are auto-emitted for AnyEntity returns"
                        )
                continue

            entity_schema = self.entity_types.get(return_type)
            if entity_schema is None:
                if partial_layer:
                    continue
                raise ValueError(
                    f"Named query '{query_name}' select references return type "
                    f"'{return_type}', which is not a declared entity type"
                )
            allowed_properties = set(entity_schema.properties)
            primary_key = entity_schema.get_primary_key()
            if primary_key is not None:
                allowed_properties.add(primary_key)
            for ref, prop_name in result_property_refs:
                if prop_name in allowed_properties:
                    continue
                raise ValueError(
                    f"Named query '{query_name}' select reference '{ref}' is not a property "
                    f"or primary key of return type '{return_type}'"
                )
        return self

    @model_validator(mode="after")
    def validate_relationship_query_returns(self, info: ValidationInfo) -> CoreConfig:
        """Check collection/traversal query return declarations."""
        partial_layer = self._is_partial_layer(info)
        for query_name, query in self.named_queries.items():
            if query.mode == "collection" and query.result_shape == "entity":
                entity_type = _normalize_query_entity_returns(query.returns)
                if entity_type not in self.entity_types:
                    if partial_layer:
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
                    if partial_layer:
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


def schema_wire_payload(config: CoreConfig) -> dict[str, Any]:
    """Dump a config for the schema wire surface (HTTP /schema, MCP, CLI).

    Identical to ``model_dump(mode="json")`` except each workflow step uses
    the discriminated ``{id, kind, config, ...}`` shape instead of one
    nullable field per step kind.
    """
    payload = config.model_dump(mode="json")
    for name, workflow in config.workflows.items():
        payload["workflows"][name]["steps"] = [workflow_step_wire(step) for step in workflow.steps]
    return payload
