"""Governed QueryDefinition v1 artifact, identity/path law, and acceptance law.

A QueryDefinition is the only governed way to name a canonical read of accepted
Claim state. Every accepted definition declares which verdicts and currencies it
may see, what it does when a one-cardinality read finds competing Claims, and
that execution must bind an explicit accepted coordinate and evaluation time.
There is no last-write-wins disposition anywhere in the grammar.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Mapping
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny, field_validator, model_validator

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import (
    CURRENT_ARTIFACT_CODEC,
    ArtifactCodec,
    ArtifactDigest,
    artifact_bytes_for_path,
    artifact_path_for_codec,
    artifact_path_matches,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.claim_verdicts import (
    EvidenceCurrency,
    EvidenceRelativeClaimVerdictV1,
)
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import CanonicalEncodingError, FormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.query.grammar import (
    QueryArtifactsEntry,
    QueryBudgets,
    QueryClaimFieldRef,
    QueryEntry,
    QueryEvaluationTimeRef,
    QueryFilter,
    QueryInclude,
    QueryLiteralRef,
    QueryOrdering,
    QueryParameterDeclaration,
    QueryParameterRef,
    QueryProjection,
    QueryReferenceInventory,
    QuerySubjectFieldRef,
    QueryTraversalStep,
    require_union_discriminators,
    sorted_unique,
    validate_ordering_keys,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.shapes import (
    Contract,
    ContractField,
    ContractRef,
    DecimalType,
    FieldPresent,
    FieldType,
    IntType,
    StringType,
    assignable,
    check_value,
    contract_digest,
    parse_contract_ref,
)

_QUERY_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_DESCRIPTION_MAX = 512

CLAIM_TYPE_PIN_ROLE = "claim-type"
PARAMETER_CONTRACT_PIN_ROLE = "parameter-contract"
RESULT_CONTRACT_PIN_ROLE = "result-contract"
_CONTRACT_PIN_ROLES = frozenset({PARAMETER_CONTRACT_PIN_ROLE, RESULT_CONTRACT_PIN_ROLE})

QueryResultShape = Literal["subject", "relation_claim", "path", "artifact_definition"]
QueryResultCardinality = Literal["one", "many"]
QueryDedupe = Literal["subject", "path", "none", "artifact"]
QueryConflictBehavior = Literal["surface_conflicts", "refuse_on_conflict"]


class QueryDefinitionFormatError(FormatError):
    """A QueryDefinition envelope, path, or declaration grammar is invalid."""


class _StrictQueryDefinitionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")

    @model_validator(mode="before")
    @classmethod
    def _require_discriminators(cls, data: Any) -> Any:
        require_union_discriminators(cls, data)
        return data


class QueryEvaluationPolicy(_StrictQueryDefinitionModel):
    """The visibility, conflict, coordinate, and time law of one canonical query.

    Verdict and currency vocabulary is the accepted evidence-relative Claim
    vocabulary; this policy never introduces a parallel verdict scale.
    """

    tag: Literal["playbill-query-evaluation-policy-v1"] = "playbill-query-evaluation-policy-v1"
    visible_verdicts: tuple[EvidenceRelativeClaimVerdictV1, ...]
    visible_currency: tuple[EvidenceCurrency, ...]
    conflict_behavior: QueryConflictBehavior
    requires_accepted_coordinate: Literal[True] = True
    requires_explicit_evaluation_time: Literal[True] = True
    result_expiry: CanonicalDuration | None = None

    @field_validator("visible_verdicts")
    @classmethod
    def _verdicts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("a query verdict policy must admit at least one Claim verdict")
        return sorted_unique(value, label="query visible verdicts")

    @field_validator("visible_currency")
    @classmethod
    def _currency(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("a query verdict policy must admit at least one Claim currency")
        return sorted_unique(value, label="query visible currency")


def _pin_key(pin: ArtifactPin) -> tuple[bytes, bytes, bytes]:
    return (
        pin.role.encode("utf-8"),
        pin.target.qualified.encode("utf-8"),
        pin.artifact_digest.encode("ascii"),
    )


class QueryDefinition(_StrictQueryDefinitionModel):
    """One governed, digest-pinned, Claim-native canonical query declaration."""

    artifact_format: Literal[
        "playbill-query-definition-v1",
        "playbill-query-definition-v2",
    ] = "playbill-query-definition-v1"
    identity: ArtifactIdentity
    description: str | None = None
    entry: QueryEntry | QueryArtifactsEntry
    traversal: tuple[QueryTraversalStep, ...] = ()
    where: QueryFilter | None = None
    result_binding: str
    result_shape: QueryResultShape
    result_cardinality: QueryResultCardinality
    dedupe: QueryDedupe
    projection: QueryProjection | None = None
    orderings: tuple[QueryOrdering, ...] = ()
    includes: tuple[QueryInclude, ...] = ()
    parameters: tuple[QueryParameterDeclaration, ...] = ()
    #: The Contract the parameters a caller supplies must satisfy (``name@N``).
    parameter_contract: ContractRef | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    #: The Contract every projected row satisfies (``name@N``); checked at acceptance.
    row_contract: ContractRef | None = Field(default=None, exclude_if=lambda value: value is None)
    evaluation_policy: QueryEvaluationPolicy
    default_budgets: QueryBudgets
    maximum_budgets: QueryBudgets
    pins: tuple[ArtifactPin, ...] = ()
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @field_validator("description")
    @classmethod
    def _description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if unicodedata.normalize("NFC", value) != value:
            raise ValueError("QueryDefinition description must already be NFC-normalized")
        if not value or len(value) > _DESCRIPTION_MAX:
            raise ValueError("QueryDefinition description is empty or exceeds its byte budget")
        if any(character.isspace() and character != " " for character in value):
            raise ValueError("QueryDefinition description must be one single-spaced line")
        return value

    @field_validator("parameters")
    @classmethod
    def _parameters(
        cls, value: tuple[QueryParameterDeclaration, ...]
    ) -> tuple[QueryParameterDeclaration, ...]:
        sorted_unique(tuple(item.name for item in value), label="QueryDefinition parameters")
        return value

    @field_validator("parameter_contract", "row_contract", mode="before")
    @classmethod
    def _contract_spelling(cls, value: object) -> object:
        return parse_contract_ref(value) if isinstance(value, str) else value

    @field_validator("includes")
    @classmethod
    def _includes(cls, value: tuple[QueryInclude, ...]) -> tuple[QueryInclude, ...]:
        sorted_unique(tuple(item.name for item in value), label="QueryDefinition includes")
        return value

    @field_validator("pins", check_fields=False)
    @classmethod
    def _pins(cls, value: tuple[ArtifactPin, ...]) -> tuple[ArtifactPin, ...]:
        if value != tuple(sorted(value, key=_pin_key)):
            raise ValueError("QueryDefinition pins must be canonically sorted")
        keys = tuple((pin.role, pin.target.qualified) for pin in value)
        if len(set(keys)) != len(keys):
            raise ValueError("QueryDefinition pins must be unique by role and target")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "QueryDefinition":
        if self.identity.kind != "QueryDefinition" or not _QUERY_NAME_RE.fullmatch(
            self.identity.name
        ):
            raise ValueError(
                "QueryDefinition identity must be kind QueryDefinition and path-addressable"
            )
        if isinstance(self.entry, QueryArtifactsEntry):
            if self.artifact_format not in {
                "playbill-query-definition-v2",
                "cruxible-query-definition-v3",
            }:
                raise ValueError("Artifact selection requires query definition v2")
            if (self.result_shape, self.result_cardinality, self.dedupe) != (
                "artifact_definition",
                "many",
                "artifact",
            ) or self.result_binding != self.entry.binding:
                raise ValueError("Artifact queries return many artifact-deduplicated definitions")
            if (
                self.traversal
                or self.where
                or self.projection
                or self.orderings
                or self.includes
                or self.parameters
                or self.pins
                or self.parameter_contract
                or self.row_contract
            ):
                raise ValueError(
                    "Artifact queries only admit definition selectors and result budgets"
                )
            if any(
                b.max_paths is not None or b.max_traversal_depth != 0
                for b in (self.default_budgets, self.maximum_budgets)
            ):
                raise ValueError("Artifact queries do not traverse paths")
            if not self.default_budgets.within(self.maximum_budgets):
                raise ValueError("default budgets exceed maximum budgets")
            return self
        if self.artifact_format not in {
            "playbill-query-definition-v1",
            "cruxible-query-definition-v3",
        } or (self.result_shape == "artifact_definition" or self.dedupe == "artifact"):
            raise ValueError("Claim traversal requires the frozen v1 query grammar")
        if self.row_contract is not None and self.projection is None:
            raise ValueError("a row Contract requires a declared projection to produce it")
        if self.parameter_contract is not None and not self.parameters:
            raise ValueError("a parameter Contract requires declared parameters")
        self._validate_bindings()
        self._validate_reference_scopes()
        self._validate_shape_rules()
        self._validate_budgets()
        self._validate_pins()
        return self

    # -- structural laws -------------------------------------------------

    def _step_bindings(self) -> tuple[str, ...]:
        return tuple(step.binding for step in self.traversal)

    @property
    def row_bindings(self) -> tuple[str, ...]:
        """Return the entry and traversal bindings a result row may name."""

        return (self.entry.binding, *self._step_bindings())

    @property
    def subject_kinds(self) -> tuple[str, ...]:
        """Return the complete declared Subject-kind vocabulary of this query."""

        kinds = set(self.entry.subject_kinds)
        for step in self.traversal:
            kinds.update(step.target_subject_kinds)
        return tuple(sorted(kinds, key=lambda item: item.encode("utf-8")))

    @property
    def referenced_predicates(self) -> tuple[str, ...]:
        """Return every ClaimType predicate this declaration traverses or reads."""

        inventory = self._inventory()
        return tuple(sorted(inventory.predicates, key=lambda item: item.encode("utf-8")))

    def _inventory(self) -> QueryReferenceInventory:
        inventory = self.entry.references
        for step in self.traversal:
            inventory = inventory.merged(step.references)
        if self.where is not None:
            inventory = inventory.merged(self.where.references)
        if self.projection is not None:
            inventory = inventory.merged(self.projection.references)
        for ordering in self.orderings:
            inventory = inventory.merged(ordering.references)
        for include in self.includes:
            inventory = inventory.merged(include.references)
        return inventory

    def _validate_bindings(self) -> None:
        seen = [self.entry.binding]
        for step in self.traversal:
            if step.from_binding not in seen:
                raise ValueError(
                    "a QueryDefinition traversal step must extend an earlier declared binding"
                )
            if step.binding in seen:
                raise ValueError("QueryDefinition bindings must be unique")
            seen.append(step.binding)
        for include in self.includes:
            if include.from_binding not in seen:
                raise ValueError("a QueryDefinition include must attach to a declared row binding")
            if include.binding in seen:
                raise ValueError("QueryDefinition include bindings must be unique")
            seen.append(include.binding)
        if self.result_binding not in self.row_bindings:
            raise ValueError("QueryDefinition result_binding must name a declared row binding")

    def _validate_reference_scopes(self) -> None:
        declared = {item.name for item in self.parameters}
        inventory = self._inventory()
        unknown = inventory.parameters - declared
        if unknown:
            raise ValueError("QueryDefinition references an undeclared parameter")
        in_scope = [self.entry.binding]
        for step in self.traversal:
            in_scope.append(step.binding)
            if step.where is not None and not step.where.references.bindings.issubset(
                set(in_scope)
            ):
                raise ValueError(
                    "a QueryDefinition traversal filter may only reference bindings already bound"
                )
        row_bindings = set(self.row_bindings)
        if self.where is not None and not self.where.references.bindings.issubset(row_bindings):
            raise ValueError("a QueryDefinition where filter may only reference row bindings")
        if self.projection is not None and not self.projection.references.bindings.issubset(
            row_bindings
        ):
            raise ValueError("a QueryDefinition projection may only reference row bindings")
        for ordering in self.orderings:
            if not ordering.references.bindings.issubset(row_bindings):
                raise ValueError("QueryDefinition orderings may only reference row bindings")
        validate_ordering_keys(self.orderings, label="QueryDefinition orderings")

    def _validate_shape_rules(self) -> None:
        optional_steps = any(not step.required for step in self.traversal)
        if self.result_shape == "subject":
            if self.dedupe != "subject":
                raise ValueError("a Subject-shaped query requires Subject dedupe")
            if optional_steps:
                raise ValueError(
                    "optional traversal steps require a path or relation_claim result shape"
                )
        else:
            if not self.traversal:
                raise ValueError(
                    "path and relation_claim result shapes require at least one traversal step"
                )
            if self.dedupe == "subject":
                raise ValueError(
                    "path and relation_claim result shapes require path or none dedupe"
                )
        if self.result_shape == "relation_claim":
            if self.result_binding == self.entry.binding:
                raise ValueError(
                    "a relation_claim result shape must resolve to a traversal binding"
                )
            if optional_steps and not self.traversal[-1].required:
                raise ValueError(
                    "a relation_claim result shape requires its final traversal step to be required"
                )
        if self.result_cardinality == "one":
            if self.default_budgets.max_results != 1 or self.maximum_budgets.max_results != 1:
                raise ValueError("a one-cardinality query must bound both budgets to one result")
        elif self.evaluation_policy.conflict_behavior != "surface_conflicts":
            raise ValueError("a many-cardinality query must surface conflicts rather than refuse")

    def _validate_budgets(self) -> None:
        if not self.default_budgets.within(self.maximum_budgets):
            raise ValueError("QueryDefinition default budgets exceed their declared ceiling")
        if len(self.traversal) > self.default_budgets.max_traversal_depth:
            raise ValueError("QueryDefinition traversal depth exceeds its declared depth budget")
        path_budgets_declared = self.default_budgets.max_paths is not None
        if (self.result_shape == "subject") == path_budgets_declared:
            raise ValueError(
                "path budgets are required for path and relation_claim shapes "
                "and prohibited for Subject shapes"
            )

    def _validate_pins(self) -> None:
        pinned = tuple(pin for pin in self.pins if pin.role == CLAIM_TYPE_PIN_ROLE)
        if any(pin.target.kind != "ClaimType" for pin in pinned):
            raise ValueError("QueryDefinition claim-type pins must target ClaimType identities")
        pinned_predicates = tuple(sorted(pin.target.name for pin in pinned))
        if pinned_predicates != tuple(sorted(self.referenced_predicates)):
            raise ValueError(
                "QueryDefinition must pin exactly the ClaimTypes whose predicates it references"
            )
        for pin in self.pins:
            if pin.role in _CONTRACT_PIN_ROLES and pin.target.kind != "Contract":
                raise ValueError("QueryDefinition contract pins must target Contract identities")


class NamedQueryDefinition(QueryDefinition):
    artifact_format: Literal["cruxible-query-definition-v3"] = "cruxible-query-definition-v3"  # type: ignore[assignment]
    pins: ClassVar[tuple[ArtifactPin, ...]] = ()  # type: ignore[misc]

    @model_validator(mode="before")
    @classmethod
    def _v3_defaults(cls, data: Any) -> Any:
        return _query_v3_defaults(data)

    def _validate_pins(self) -> None:
        pass


#: Spec fields an author may leave out; the defaults are stated in the stored definition.
SPEC_DEFAULTED_FIELDS = (
    "result_shape",
    "result_cardinality",
    "dedupe",
    "evaluation_policy",
    "default_budgets",
    "maximum_budgets",
)

_ARTIFACTS_ENTRY_TAG = "playbill-query-artifacts-entry-v2"


def _spec_schema(schema: dict[str, Any]) -> None:
    required = schema.get("required")
    if isinstance(required, list):
        schema["required"] = [name for name in required if name not in SPEC_DEFAULTED_FIELDS]


def _query_v3_defaults(data: Any) -> Any:
    if (
        not isinstance(data, Mapping)
        or data.get("artifact_format") != "cruxible-query-definition-v3"
    ):
        return data
    stated = dict(data)
    entry = stated.get("entry")
    if isinstance(entry, Mapping):
        stated.setdefault(
            "result_binding",
            entry.get("binding", "definition" if "artifact_kind" in entry else None),
        )
    stated.setdefault("result_cardinality", "many")
    if "default_budgets" not in stated and "maximum_budgets" in stated:
        stated["default_budgets"] = stated["maximum_budgets"]
    return stated


def query_spec_defaults(data: Mapping[str, Any]) -> dict[str, Any]:
    """The spec with each omitted defaulted field stated.

    - ``result_shape``: ``subject`` for a Subject entry;
      ``artifact_definition`` for an artifacts entry (v2).
    - ``result_cardinality``: ``many``.
    - ``dedupe``: ``subject`` or ``artifact`` for those shapes; other shapes state it.
    - ``evaluation_policy``: supported, current Claims; conflicts surfaced.
    - budgets: 100 results by default, 1000 at most, the traversal's own depth.

    A minimal spec and its spelled-out equivalent are one definition, one digest.
    """

    stated = dict(data)
    entry = stated.get("entry")
    artifacts = isinstance(entry, QueryArtifactsEntry) or (
        isinstance(entry, Mapping) and entry.get("tag") == _ARTIFACTS_ENTRY_TAG
    )
    if artifacts:
        stated.setdefault("artifact_format", "playbill-query-definition-v2")
    shape = stated.setdefault("result_shape", "artifact_definition" if artifacts else "subject")
    stated.setdefault("result_cardinality", "many")
    if shape in ("subject", "artifact_definition"):
        stated.setdefault("dedupe", "artifact" if shape == "artifact_definition" else "subject")
    stated.setdefault(
        "evaluation_policy",
        {
            "visible_verdicts": ["supported"],
            "visible_currency": ["current"],
            "conflict_behavior": "surface_conflicts",
        },
    )
    traversal = stated.get("traversal")
    depth = len(traversal) if isinstance(traversal, list | tuple) else 0
    path_fields = (
        {"max_paths": 20000, "max_paths_per_result": 100}
        if shape in {"path", "relation_claim"}
        else {}
    )
    stated.setdefault(
        "default_budgets", {"max_results": 100, "max_traversal_depth": depth, **path_fields}
    )
    stated.setdefault(
        "maximum_budgets", {"max_results": 1000, "max_traversal_depth": depth, **path_fields}
    )
    return stated


class QueryDefinitionSpec(QueryDefinition):
    """Authoring grammar: omitted ClaimType pins resolve at the intent base.

    Explicit pins remain assertions, never silently rebound. Accepted artifacts
    always pass the stricter QueryDefinition validation after lowering. The
    shape, cardinality, dedupe, evaluation policy and budgets may be left out
    (``query_spec_defaults``); the stored definition states them.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_mode_override="validation",
        json_schema_extra=_spec_schema,
    )

    @model_validator(mode="before")
    @classmethod
    def _defaults(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            data = _query_v3_defaults(data)
            return query_spec_defaults(data)
        return data

    def _validate_pins(self) -> None:
        referenced = set(self.referenced_predicates)
        for pin in self.pins:
            if pin.role == CLAIM_TYPE_PIN_ROLE and (
                pin.target.kind != "ClaimType" or pin.target.name not in referenced
            ):
                raise ValueError("query ClaimType pins must reference a used predicate")
            if pin.role in _CONTRACT_PIN_ROLES and pin.target.kind != "Contract":
                raise ValueError("QueryDefinition contract pins must target Contract identities")


class NamedQueryDefinitionSpec(NamedQueryDefinition):
    """The pin-less typed authoring form with the same stated defaults."""

    model_config = QueryDefinitionSpec.model_config

    @model_validator(mode="before")
    @classmethod
    def _defaults(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            return query_spec_defaults(_query_v3_defaults(data))
        return data


def query_definition_path(name: str) -> str:
    """Return the one canonical ledger path for a QueryDefinition identity name."""

    if not _QUERY_NAME_RE.fullmatch(name):
        raise QueryDefinitionFormatError("QueryDefinition identity is not path-addressable")
    return f"query-definitions/{name}.json"


def query_definition_address(path: str) -> SemanticAddress:
    """Return the whole-artifact semantic address of one QueryDefinition."""

    return SemanticAddress.whole_artifact(path)


def validate_query_definition_path(
    query: QueryDefinition,
    path: str,
    *,
    codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC,
) -> str:
    expected = query_definition_path(query.identity.name)
    if not artifact_path_matches(expected, path, codec=codec):
        raise QueryDefinitionFormatError(
            f"QueryDefinition identity/path disagreement: {query.identity.qualified!r} "
            f"requires {artifact_path_for_codec(expected, codec)!r}"
        )
    return path


def query_definition_wire(query: QueryDefinition) -> dict[str, Any]:
    if getattr(query, "artifact_format") != "cruxible-query-definition-v3":
        return query.model_dump(mode="json")

    def compact(model: BaseModel, output: dict[str, Any]) -> None:
        # Tags belong to declared model positions, never to opaque JSON data.
        output.pop("tag", None)
        if "kind" in type(model).model_fields:
            output["kind"] = getattr(model, "kind")
        for name in type(model).model_fields:
            child = getattr(model, name)
            if name not in output:
                continue
            if isinstance(child, BaseModel) and isinstance(output[name], dict):
                compact(child, output[name])
            elif isinstance(child, tuple) and isinstance(output[name], list):
                for source, target in zip(child, output[name], strict=True):
                    if isinstance(source, BaseModel) and isinstance(target, dict):
                        compact(source, target)

    body = query.model_dump(mode="json", exclude_defaults=True)
    compact(query, body)
    body["artifact_format"] = query.artifact_format
    if query.result_binding == query.entry.binding:
        body.pop("result_binding", None)
    if query.result_cardinality == "many":
        body.pop("result_cardinality", None)
    if query.default_budgets == query.maximum_budgets:
        body.pop("default_budgets", None)
    return body


def render_query_definition(query: QueryDefinition) -> bytes:
    return pretty_canonical_bytes(query_definition_wire(query))


def validate_query_definition(payload: Any) -> QueryDefinition:
    """Strictly select the stored owner model without changing the payload."""
    if (
        type(payload) is NamedQueryDefinition
        and payload.artifact_format == "cruxible-query-definition-v3"
    ) or (
        type(payload) is QueryDefinition
        and payload.artifact_format
        in {"playbill-query-definition-v1", "playbill-query-definition-v2"}
    ):
        # Owner parsers already validated immutable objects; readers must not
        # parse the same accepted artifact again just to put it in a carrier.
        return payload
    if isinstance(payload, QueryDefinition):
        payload = payload.model_dump(mode="json")
    model = (
        NamedQueryDefinition
        if isinstance(payload, Mapping)
        and payload.get("artifact_format") == "cruxible-query-definition-v3"
        else QueryDefinition
    )
    return model.model_validate(payload)


def parse_query_definition(
    content: bytes,
    *,
    path: str,
    codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC,
) -> QueryDefinition:
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise QueryDefinitionFormatError("QueryDefinition is not strict JSON") from exc
    if not isinstance(payload, dict) or payload.get("artifact_format") not in {
        "playbill-query-definition-v1",
        "playbill-query-definition-v2",
        "cruxible-query-definition-v3",
    }:
        declared = payload.get("artifact_format") if isinstance(payload, dict) else None
        raise QueryDefinitionFormatError(
            f"unsupported QueryDefinition artifact format: {declared!r}"
        )
    try:
        query = validate_query_definition(payload)
    except (ValueError, CanonicalEncodingError) as exc:
        raise QueryDefinitionFormatError(
            "QueryDefinition failed strict playbill-query-definition-v1 validation"
        ) from exc
    validate_query_definition_path(query, path, codec=codec)
    if artifact_bytes_for_path(render_query_definition(query), path, codec=codec) != content:
        raise QueryDefinitionFormatError("QueryDefinition is not in canonical wire form")
    return query


def query_definition_digest(query: QueryDefinition) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        query_definition_wire(query),
    )


class AcceptedQueryDefinition(_StrictQueryDefinitionModel):
    path: str
    query: Annotated[
        SerializeAsAny[QueryDefinition | NamedQueryDefinition],
        Field(discriminator="artifact_format"),
    ]
    artifact_digest: str

    @field_validator("query", mode="before")
    @classmethod
    def _query_owner(cls, value: Any) -> QueryDefinition:
        return validate_query_definition(value)

    @model_validator(mode="after")
    def _binding(self) -> "AcceptedQueryDefinition":
        validate_query_definition_path(self.query, self.path)
        if self.artifact_digest != query_definition_digest(self.query).tagged:
            raise ValueError("accepted QueryDefinition digest differs from its exact envelope")
        return self


class QueryDefinitionLawResult(_StrictQueryDefinitionModel):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    required_tier: PermissionTier | None = None
    approval_scope: tuple[str, ...] = ()
    diagnostics: tuple[CompilerDiagnostic, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> "QueryDefinitionLawResult":
        if self.verdict == "accepted":
            if self.artifact_digest is None or self.required_tier is None:
                raise ValueError("accepted QueryDefinition law result is incomplete")
            if self.diagnostics:
                raise ValueError("accepted QueryDefinition law result cannot carry diagnostics")
        elif self.artifact_digest is not None or self.required_tier is not None:
            raise ValueError("refused QueryDefinition law result cannot carry acceptance fields")
        return self


def _refusal(code: str, message: str, *, path: str) -> QueryDefinitionLawResult:
    return QueryDefinitionLawResult(
        verdict="refused",
        diagnostics=(
            CompilerDiagnostic(
                code=code,
                severity="error",
                message=message,
                subject=SemanticAddress.whole_artifact(path),
            ),
        ),
    )


#: Which Contract field kinds each query value type agrees with.
_VALUE_TYPE_KINDS: Mapping[str, frozenset[str]] = {
    "string": frozenset({"string", "enum", "enum_of", "key", "subject"}),
    "integer": frozenset({"int"}),
    "boolean": frozenset({"bool"}),
    "decimal": frozenset({"decimal"}),
    "timestamp": frozenset({"datetime"}),
    "subject_reference": frozenset({"subject", "key"}),
}


def query_contract_problems(
    query: QueryDefinition,
    *,
    resolve: Callable[[ContractRef], Contract | None],
) -> tuple[tuple[str, str], ...]:
    """Where the query disagrees with its parameter and row Contracts, as (code, message).

    Parameters: the declared names are exactly the Contract's fields, a required
    parameter is a required field, and the value types agree. Rows: the
    projection's field names are exactly the Contract's fields, and each
    projected value whose type is fixed by the query (a parameter, a literal, a
    Subject field, the evaluation time, a Claim's role or verdict) agrees with
    its field. A Claim value's type is the ClaimType's, judged where Claims are.
    """

    problems: list[tuple[str, str]] = []

    def contract(ref: ContractRef, label: str) -> Contract | None:
        found = resolve(ref)
        if found is None:
            problems.append(
                (
                    "cruxible.query_definition.contract_unresolved",
                    f"The {label} Contract {ref.spelling} is not accepted.",
                )
            )
            return None
        if ref.digest is not None and contract_digest(found).tagged != ref.digest:
            problems.append(
                (
                    "cruxible.query_definition.contract_unresolved",
                    f"The {label} Contract {ref.spelling} is accepted at another digest.",
                )
            )
            return None
        return found

    declared = {item.name: item for item in query.parameters}
    parameter_shape: Contract | None = None
    if query.parameter_contract is not None:
        shape = contract(query.parameter_contract, "parameter")
        parameter_shape = shape
        if shape is not None:
            if set(declared) != set(shape.fields):
                problems.append(
                    (
                        "cruxible.query_definition.parameters_disagree",
                        f"The parameters {sorted(declared)} are not the fields of "
                        f"{query.parameter_contract.spelling}: {sorted(shape.fields)}.",
                    )
                )
            for name, item in declared.items():
                field = shape.fields.get(name)
                if field is None:
                    continue
                if item.required == field.optional or (
                    field.type.kind not in _VALUE_TYPE_KINDS[item.value_type]
                ):
                    problems.append(
                        (
                            "cruxible.query_definition.parameters_disagree",
                            f"Parameter {name!r} ({item.value_type}, "
                            f"{'required' if item.required else 'optional'}) disagrees with "
                            f"its field {field.spelling}.",
                        )
                    )
    if query.row_contract is not None and query.projection is not None:
        shape = contract(query.row_contract, "row")
        if shape is not None:
            projected = {item.name: item.value for item in query.projection.fields}
            if set(projected) != set(shape.fields):
                problems.append(
                    (
                        "cruxible.query_definition.row_disagrees",
                        f"The projection produces {sorted(projected)}, but "
                        f"{query.row_contract.spelling} declares {sorted(shape.fields)}.",
                    )
                )
            for name, value in projected.items():
                field = shape.fields.get(name)
                if field is None:
                    continue
                problem = _projected_value_problem(value, field, declared, parameter_shape)
                if problem is not None:
                    problems.append(
                        (
                            "cruxible.query_definition.row_disagrees",
                            f"Projected field {name!r} {problem}; its Contract field is "
                            f"{field.spelling}.",
                        )
                    )
    return tuple(problems)


def _has_refinement(field_type: FieldType) -> bool:
    """Whether ``field_type`` narrows its kind beyond what a bare value type guarantees."""

    if isinstance(field_type, StringType):
        return (
            field_type.pattern is not None
            or field_type.min_len is not None
            or field_type.max_len is not None
        )
    if isinstance(field_type, IntType | DecimalType):
        return field_type.min is not None or field_type.max is not None
    return False


def _projected_value_problem(
    value: object,
    field: ContractField,
    declared: Mapping[str, QueryParameterDeclaration],
    parameter_shape: Contract | None,
) -> str | None:
    kind = field.type.kind
    if isinstance(value, QueryParameterRef):
        parameter = declared.get(value.parameter)
        if parameter is None:
            return None
        # A declared parameter_contract field is fully typed, refinements
        # included: judge it with the same edge rule any other flow uses.
        param_field = (
            None if parameter_shape is None else parameter_shape.fields.get(value.parameter)
        )
        if param_field is not None:
            return (
                None
                if assignable(param_field.type, field.type)
                else f"is parameter {value.parameter!r}, typed {param_field.spelling}"
            )
        allowed = _VALUE_TYPE_KINDS[parameter.value_type]
        if kind not in allowed:
            return f"is a {parameter.value_type} parameter"
        if _has_refinement(field.type):
            # No parameter_contract exists to bound this parameter's value, so
            # a refined row field (a range, a pattern, a bounded length) can
            # never be verified against it. Fail closed rather than let an
            # out-of-bound value through unchecked at run time.
            return (
                f"is parameter {value.parameter!r}, declared only as "
                f"{parameter.value_type} with no Contract bounding it; its field "
                f"{field.spelling} cannot be verified"
            )
        return None
    if isinstance(value, QueryLiteralRef):
        if value.value is None:
            # T? means the key is absent, never a null value: a projected
            # literal null can never satisfy a Contract field, optional or not.
            return "is a null literal; a Contract field is absent, never null"
        checked = check_value(value.value, field.type, at="/literal")
        return None if isinstance(checked, FieldPresent) else "is a literal outside its type"
    if isinstance(value, QuerySubjectFieldRef):
        return None if kind in _VALUE_TYPE_KINDS["string"] else "is a Subject identity string"
    if isinstance(value, QueryEvaluationTimeRef):
        return None if kind == "datetime" else "is the evaluation time (a datetime)"
    if isinstance(value, QueryClaimFieldRef):
        return None if kind in ("string", "enum") else "is a Claim role or verdict (a string)"
    return None


def evaluate_query_definition_law(
    query: QueryDefinition,
    *,
    path: str,
    predecessor: AcceptedQueryDefinition | None,
    accepted_artifacts: Mapping[str, tuple[ArtifactIdentity, str]] | None = None,
    by_name: bool = False,
    contracts: Callable[[ContractRef], Contract | None] | None = None,
) -> QueryDefinitionLawResult:
    """Evaluate exact path, digest-pinned dependencies, Contract agreement, and lifecycle."""

    try:
        validate_query_definition_path(query, path)
    except QueryDefinitionFormatError as exc:
        return _refusal("cruxible.query_definition.path_mismatch", str(exc), path=path)
    if contracts is not None:
        problems = query_contract_problems(query, resolve=contracts)
        if problems:
            return _refusal(problems[0][0], " ".join(item[1] for item in problems), path=path)
    elif query.parameter_contract is not None or query.row_contract is not None:
        return _refusal(
            "cruxible.query_definition.contract_unresolved",
            "A QueryDefinition with Contract pins is judged against its accepted Contracts.",
            path=path,
        )
    if accepted_artifacts is not None:
        for pin in query.pins:
            accepted = accepted_artifacts.get(pin.target.qualified)
            if accepted is None or (not by_name and accepted[1] != pin.artifact_digest):
                return _refusal(
                    "cruxible.query_definition.pin_unresolved",
                    "A QueryDefinition pin does not resolve at the accepted parent coordinate.",
                    path=path,
                )
    if accepted_artifacts is not None and isinstance(query, NamedQueryDefinition):
        for predicate in query.referenced_predicates:
            if accepted_artifacts.get(f"ClaimType:{predicate}") is None:
                return _refusal(
                    "cruxible.query_definition.pin_unresolved",
                    f"Query predicate {predicate!r} names no accepted ClaimType.",
                    path=path,
                )
    digest = query_definition_digest(query).tagged
    if predecessor is None:
        if query.lifecycle.state != "live" or query.lifecycle.predecessor_digest is not None:
            return _refusal(
                "cruxible.query_definition.unexpected_predecessor",
                "A new QueryDefinition must begin live without a predecessor.",
                path=path,
            )
    else:
        previous = predecessor.query
        if previous.identity != query.identity or predecessor.path != path:
            return _refusal(
                "cruxible.query_definition.predecessor_identity_mismatch",
                "The live predecessor has a different QueryDefinition identity.",
                path=path,
            )
        if digest == predecessor.artifact_digest:
            return _refusal(
                "cruxible.query_definition.no_semantic_change",
                "QueryDefinition succession must produce a new artifact digest.",
                path=path,
            )
        if query.lifecycle.predecessor_digest != predecessor.artifact_digest:
            return _refusal(
                "cruxible.query_definition.stale_predecessor",
                "The QueryDefinition does not name the exact live predecessor digest.",
                path=path,
            )
        if previous.lifecycle.state == "retired":
            return _refusal(
                "cruxible.query_definition.lifecycle_invalid",
                "A retired QueryDefinition cannot be revived or revised.",
                path=path,
            )
    return QueryDefinitionLawResult(
        verdict="accepted",
        artifact_digest=digest,
        required_tier="governed_write",
        approval_scope=(),
    )


__all__ = [
    "AcceptedQueryDefinition",
    "CLAIM_TYPE_PIN_ROLE",
    "PARAMETER_CONTRACT_PIN_ROLE",
    "QueryConflictBehavior",
    "QueryDedupe",
    "QueryDefinitionFormatError",
    "QueryDefinitionLawResult",
    "QueryDefinition",
    "QueryDefinitionSpec",
    "SPEC_DEFAULTED_FIELDS",
    "query_spec_defaults",
    "QueryEvaluationPolicy",
    "QueryResultCardinality",
    "QueryResultShape",
    "RESULT_CONTRACT_PIN_ROLE",
    "evaluate_query_definition_law",
    "query_contract_problems",
    "parse_query_definition",
    "query_definition_address",
    "query_definition_digest",
    "query_definition_path",
    "render_query_definition",
    "validate_query_definition_path",
]
