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
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_core.playbill.artifacts import (
    ArtifactAuthority,
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_core.playbill.canonical import ArtifactDigest, canonical_bytes, typed_digest
from cruxible_core.playbill.captures import CanonicalDurationV1
from cruxible_core.playbill.claim_verdicts import EvidenceCurrency, EvidenceRelativeClaimVerdict
from cruxible_core.playbill.diagnostics import CompilerDiagnostic
from cruxible_core.playbill.errors import CanonicalEncodingError, PlaybillFormatError
from cruxible_core.playbill.governance import PermissionTier
from cruxible_core.playbill.query.grammar import (
    QueryBudgetsV1,
    QueryEntryV1,
    QueryFilterV1,
    QueryIncludeV1,
    QueryOrderingV1,
    QueryParameterDeclarationV1,
    QueryProjectionV1,
    QueryReferenceInventoryV1,
    QueryTraversalStepV1,
    sorted_unique,
    validate_ordering_keys,
)
from cruxible_core.playbill.semantic import SemanticAddress

_QUERY_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_DESCRIPTION_MAX = 512

CLAIM_TYPE_PIN_ROLE = "claim-type"
PARAMETER_CONTRACT_PIN_ROLE = "parameter-contract"
RESULT_CONTRACT_PIN_ROLE = "result-contract"
_CONTRACT_PIN_ROLES = frozenset({PARAMETER_CONTRACT_PIN_ROLE, RESULT_CONTRACT_PIN_ROLE})

QueryResultShapeV1 = Literal["subject", "relation_claim", "path"]
QueryResultCardinalityV1 = Literal["one", "many"]
QueryDedupeV1 = Literal["subject", "path", "none"]
QueryConflictBehaviorV1 = Literal["surface_conflicts", "refuse_on_conflict"]


class QueryDefinitionFormatError(PlaybillFormatError):
    """A QueryDefinition envelope, path, or declaration grammar is invalid."""


class _StrictQueryDefinitionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class QueryEvaluationPolicyV1(_StrictQueryDefinitionModel):
    """The visibility, conflict, coordinate, and time law of one canonical query.

    Verdict and currency vocabulary is the accepted evidence-relative Claim
    vocabulary; this policy never introduces a parallel verdict scale.
    """

    tag: Literal["playbill-query-evaluation-policy-v1"] = "playbill-query-evaluation-policy-v1"
    visible_verdicts: tuple[EvidenceRelativeClaimVerdict, ...]
    visible_currency: tuple[EvidenceCurrency, ...]
    conflict_behavior: QueryConflictBehaviorV1
    requires_accepted_coordinate: Literal[True] = True
    requires_explicit_evaluation_time: Literal[True] = True
    result_expiry: CanonicalDurationV1 | None = None

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


class QueryDefinitionV1(_StrictQueryDefinitionModel):
    """One governed, digest-pinned, Claim-native canonical query declaration."""

    artifact_format: Literal["playbill-query-definition-v1"] = "playbill-query-definition-v1"
    identity: ArtifactIdentity
    description: str | None = None
    entry: QueryEntryV1
    traversal: tuple[QueryTraversalStepV1, ...] = ()
    where: QueryFilterV1 | None = None
    result_binding: str
    result_shape: QueryResultShapeV1
    result_cardinality: QueryResultCardinalityV1
    dedupe: QueryDedupeV1
    projection: QueryProjectionV1 | None = None
    orderings: tuple[QueryOrderingV1, ...] = ()
    includes: tuple[QueryIncludeV1, ...] = ()
    parameters: tuple[QueryParameterDeclarationV1, ...] = ()
    evaluation_policy: QueryEvaluationPolicyV1
    default_budgets: QueryBudgetsV1
    maximum_budgets: QueryBudgetsV1
    authority: ArtifactAuthority
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
        cls, value: tuple[QueryParameterDeclarationV1, ...]
    ) -> tuple[QueryParameterDeclarationV1, ...]:
        sorted_unique(tuple(item.name for item in value), label="QueryDefinition parameters")
        return value

    @field_validator("includes")
    @classmethod
    def _includes(cls, value: tuple[QueryIncludeV1, ...]) -> tuple[QueryIncludeV1, ...]:
        sorted_unique(tuple(item.name for item in value), label="QueryDefinition includes")
        return value

    @field_validator("pins")
    @classmethod
    def _pins(cls, value: tuple[ArtifactPin, ...]) -> tuple[ArtifactPin, ...]:
        if value != tuple(sorted(value, key=_pin_key)):
            raise ValueError("QueryDefinition pins must be canonically sorted")
        keys = tuple((pin.role, pin.target.qualified) for pin in value)
        if len(set(keys)) != len(keys):
            raise ValueError("QueryDefinition pins must be unique by role and target")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "QueryDefinitionV1":
        if self.identity.kind != "QueryDefinition" or not _QUERY_NAME_RE.fullmatch(
            self.identity.name
        ):
            raise ValueError(
                "QueryDefinition identity must be kind QueryDefinition and path-addressable"
            )
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

    def _inventory(self) -> QueryReferenceInventoryV1:
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


def query_definition_path(name: str) -> str:
    """Return the one canonical ledger path for a QueryDefinition identity name."""

    if not _QUERY_NAME_RE.fullmatch(name):
        raise QueryDefinitionFormatError("QueryDefinition identity is not path-addressable")
    return f"query-definitions/{name}.yaml"


def query_definition_address(path: str) -> SemanticAddress:
    """Return the whole-artifact semantic address of one QueryDefinition."""

    return SemanticAddress.whole_artifact(path)


def validate_query_definition_path(query: QueryDefinitionV1, path: str) -> str:
    expected = query_definition_path(query.identity.name)
    if path != expected:
        raise QueryDefinitionFormatError(
            f"QueryDefinition identity/path disagreement: {query.identity.qualified!r} "
            f"requires {expected!r}"
        )
    return path


def render_query_definition(query: QueryDefinitionV1) -> bytes:
    return canonical_bytes(query.model_dump(mode="json")) + b"\n"


def parse_query_definition(content: bytes, *, path: str) -> QueryDefinitionV1:
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise QueryDefinitionFormatError("QueryDefinition is not strict JSON") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("artifact_format") != "playbill-query-definition-v1"
    ):
        declared = payload.get("artifact_format") if isinstance(payload, dict) else None
        raise QueryDefinitionFormatError(
            f"unsupported QueryDefinition artifact format: {declared!r}"
        )
    try:
        query = QueryDefinitionV1.model_validate(payload)
    except (ValueError, CanonicalEncodingError) as exc:
        raise QueryDefinitionFormatError(
            "QueryDefinition failed strict playbill-query-definition-v1 validation"
        ) from exc
    if render_query_definition(query) != content:
        raise QueryDefinitionFormatError("QueryDefinition is not in canonical wire form")
    validate_query_definition_path(query, path)
    return query


def query_definition_digest(query: QueryDefinitionV1) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        query.model_dump(mode="json"),
    )


class AcceptedQueryDefinitionV1(_StrictQueryDefinitionModel):
    path: str
    query: QueryDefinitionV1
    artifact_digest: str

    @model_validator(mode="after")
    def _binding(self) -> "AcceptedQueryDefinitionV1":
        validate_query_definition_path(self.query, self.path)
        if self.artifact_digest != query_definition_digest(self.query).tagged:
            raise ValueError("accepted QueryDefinition digest differs from its exact envelope")
        return self


class QueryDefinitionLawResultV1(_StrictQueryDefinitionModel):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    required_tier: PermissionTier | None = None
    approval_scope: tuple[str, ...] = ()
    diagnostics: tuple[CompilerDiagnostic, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> "QueryDefinitionLawResultV1":
        if self.verdict == "accepted":
            if (
                self.artifact_digest is None
                or self.required_tier is None
                or not self.approval_scope
            ):
                raise ValueError("accepted QueryDefinition law result is incomplete")
            if self.diagnostics:
                raise ValueError("accepted QueryDefinition law result cannot carry diagnostics")
        elif self.artifact_digest is not None or self.required_tier is not None:
            raise ValueError("refused QueryDefinition law result cannot carry acceptance fields")
        return self


def _refusal(code: str, message: str, *, path: str) -> QueryDefinitionLawResultV1:
    return QueryDefinitionLawResultV1(
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


def evaluate_query_definition_law(
    query: QueryDefinitionV1,
    *,
    path: str,
    actor_roles: tuple[str, ...],
    predecessor: AcceptedQueryDefinitionV1 | None,
    accepted_artifacts: Mapping[str, tuple[ArtifactIdentity, str]] | None = None,
) -> QueryDefinitionLawResultV1:
    """Evaluate exact path, digest-pinned dependencies, lifecycle, and authority."""

    try:
        validate_query_definition_path(query, path)
    except QueryDefinitionFormatError as exc:
        return _refusal("playbill.query_definition.path_mismatch", str(exc), path=path)
    if accepted_artifacts is not None:
        for pin in query.pins:
            accepted = accepted_artifacts.get(pin.target.qualified)
            if accepted is None or accepted[1] != pin.artifact_digest:
                return _refusal(
                    "playbill.query_definition.pin_unresolved",
                    "A QueryDefinition pin does not resolve at the accepted parent coordinate.",
                    path=path,
                )
    roles = set(actor_roles)
    digest = query_definition_digest(query).tagged
    if predecessor is None:
        if query.lifecycle.state != "live" or query.lifecycle.predecessor_digest is not None:
            return _refusal(
                "playbill.query_definition.unexpected_predecessor",
                "A new QueryDefinition must begin live without a predecessor.",
                path=path,
            )
        if not roles.intersection(query.authority.propose_roles):
            return _refusal(
                "playbill.query_definition.actor_unauthorized",
                "The request actor lacks QueryDefinition proposal authority.",
                path=path,
            )
        approval_scope = query.authority.approve_roles
    else:
        previous = predecessor.query
        if previous.identity != query.identity or predecessor.path != path:
            return _refusal(
                "playbill.query_definition.predecessor_identity_mismatch",
                "The live predecessor has a different QueryDefinition identity.",
                path=path,
            )
        if digest == predecessor.artifact_digest:
            return _refusal(
                "playbill.query_definition.no_semantic_change",
                "QueryDefinition succession must produce a new artifact digest.",
                path=path,
            )
        if query.lifecycle.predecessor_digest != predecessor.artifact_digest:
            return _refusal(
                "playbill.query_definition.stale_predecessor",
                "The QueryDefinition does not name the exact live predecessor digest.",
                path=path,
            )
        if previous.lifecycle.state == "retired":
            return _refusal(
                "playbill.query_definition.lifecycle_invalid",
                "A retired QueryDefinition cannot be revived or revised.",
                path=path,
            )
        if query.authority != previous.authority:
            return _refusal(
                "playbill.query_definition.authority_change_unsupported",
                "QueryDefinition succession cannot rewrite accepted authority in v1.",
                path=path,
            )
        if not roles.intersection(previous.authority.propose_roles):
            return _refusal(
                "playbill.query_definition.actor_unauthorized",
                "The request actor lacks predecessor proposal authority.",
                path=path,
            )
        approval_scope = previous.authority.approve_roles
    return QueryDefinitionLawResultV1(
        verdict="accepted",
        artifact_digest=digest,
        required_tier="governed_write",
        approval_scope=approval_scope,
    )


__all__ = [
    "AcceptedQueryDefinitionV1",
    "CLAIM_TYPE_PIN_ROLE",
    "PARAMETER_CONTRACT_PIN_ROLE",
    "QueryConflictBehaviorV1",
    "QueryDedupeV1",
    "QueryDefinitionFormatError",
    "QueryDefinitionLawResultV1",
    "QueryDefinitionV1",
    "QueryEvaluationPolicyV1",
    "QueryResultCardinalityV1",
    "QueryResultShapeV1",
    "RESULT_CONTRACT_PIN_ROLE",
    "evaluate_query_definition_law",
    "parse_query_definition",
    "query_definition_address",
    "query_definition_digest",
    "query_definition_path",
    "render_query_definition",
    "validate_query_definition_path",
]
