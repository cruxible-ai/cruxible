"""Typed exact definitions returned by artifact queries."""

from datetime import datetime
from typing import Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from cruxible_client.contracts.canonical import normalize_canonical
from cruxible_client.contracts.claim_types import ClaimType, claim_type_digest, claim_type_path
from cruxible_client.contracts.claim_verdicts import EvidenceCurrency, EvidenceRelativeClaimVerdict
from cruxible_client.contracts.procedures.artifacts import (
    ProcedureArtifactAny,
    procedure_artifact_digest,
    procedure_path,
)
from cruxible_client.contracts.projection import AcceptedProjectionCoordinate
from cruxible_client.contracts.query.definitions import (
    QueryDedupeV1,
    QueryResultCardinalityV1,
    QueryResultShapeV1,
)
from cruxible_client.contracts.query.grammar import QueryBudgetsV1, QueryValueTypeV1, byte_sorted


class QueryArtifactDefinitionV2(BaseModel):
    """The full definition and its exact accepted identity/path/version binding."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    identity: str
    path: str
    artifact_digest: str
    definition: ClaimType | ProcedureArtifactAny

    @model_validator(mode="after")
    def _binding(self) -> "QueryArtifactDefinitionV2":
        source = self.definition
        if isinstance(source, ClaimType):
            path, digest = claim_type_path(source.predicate), claim_type_digest(source).tagged
        else:
            path, digest = (
                procedure_path(source.identity.name),
                procedure_artifact_digest(source).tagged,
            )
        if (self.identity, self.path, self.artifact_digest) != (
            source.identity.qualified,
            path,
            digest,
        ):
            raise ValueError("query definition row does not reproduce its identity/path/digest")
        return self


QueryClippedBudgetV1 = Literal[
    "include_max_items",
    "max_paths",
    "max_paths_per_result",
    "max_results",
]
QueryValueStateV1 = Literal["absent", "conflict", "present"]


class _StrictQueryEngineModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class QueryClaimVisibilityV1(_StrictQueryEngineModel):
    """Why one Claim row is present: its verdict and currency at the read time."""

    tag: Literal["playbill-query-claim-visibility-v1"] = "playbill-query-claim-visibility-v1"
    claim_path: str
    statement_digest: str
    artifact_digest: str
    predicate: str
    subject_identity: str
    verdict: EvidenceRelativeClaimVerdict
    currency: EvidenceCurrency


class QueryConflictV1(_StrictQueryEngineModel):
    """Competing accepted Claims surfaced instead of silently resolved."""

    tag: Literal["playbill-query-conflict-v1"] = "playbill-query-conflict-v1"
    kind: Literal["claim_object", "result_cardinality"]
    binding: str | None = None
    predicate: str | None = None
    subject_identity: str | None = None
    statement_digests: tuple[str, ...] = ()
    subject_identities: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> "QueryConflictV1":
        if self.kind == "claim_object":
            if self.binding is None or self.predicate is None or self.subject_identity is None:
                raise ValueError("a Claim-object conflict names its binding, predicate, subject")
            if len(self.statement_digests) < 2 or self.subject_identities:
                raise ValueError("a Claim-object conflict names two or more statement digests")
        else:
            if self.binding is not None or self.predicate is not None:
                raise ValueError("a result-cardinality conflict names no binding or predicate")
            if self.subject_identity is not None or self.statement_digests:
                raise ValueError("a result-cardinality conflict names only competing row subjects")
            if len(self.subject_identities) < 2:
                raise ValueError("a result-cardinality conflict names two or more row subjects")
        return self


class QueryRefusalV1(_StrictQueryEngineModel):
    """One typed, dot-namespaced refusal; a refused query returns no rows."""

    tag: Literal["playbill-query-refusal-v1"] = "playbill-query-refusal-v1"
    code: str
    message: str
    statement_digests: tuple[str, ...] = ()
    subject_identities: tuple[str, ...] = ()

    @field_validator("code")
    @classmethod
    def _code(cls, value: str) -> str:
        if not value.startswith("playbill.query."):
            raise ValueError("a query refusal code must be playbill.query dot-namespaced")
        return value


class QueryRowBindingV1(_StrictQueryEngineModel):
    """One declared row binding and the accepted Subject it resolved to."""

    tag: Literal["playbill-query-row-binding-v1"] = "playbill-query-row-binding-v1"
    binding: str
    subject_identity: str | None = None
    subject_kind: str | None = None
    subject_id: str | None = None
    subject_path: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> "QueryRowBindingV1":
        bound = (self.subject_identity, self.subject_kind, self.subject_id, self.subject_path)
        if any(item is None for item in bound) and any(item is not None for item in bound):
            raise ValueError("a query row binding is either fully bound or fully unbound")
        return self


class QueryProjectedFieldV1(_StrictQueryEngineModel):
    """One projected field; absence and conflict are stated, never rendered null."""

    tag: Literal["playbill-query-projected-field-v1"] = "playbill-query-projected-field-v1"
    name: str
    state: QueryValueStateV1
    value: object = None

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object) -> object:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _shape(self) -> "QueryProjectedFieldV1":
        if self.state != "present" and self.value is not None:
            raise ValueError("an absent or conflicted projected field carries no value")
        return self


class QueryProjectedFields(tuple[QueryProjectedFieldV1, ...]):
    """Named access to the existing projected-field envelopes; wire stays an array.

    A field still exposes its explicit presence/conflict state. Attribute access
    must never turn an absent projection into a seemingly present null value.
    """

    def __getattribute__(self, name: str) -> Any:
        if not name.startswith("_"):
            for field in self:
                if field.name == name:
                    return field
            raise AttributeError(f"Query has no projected field {name!r}")
        return super().__getattribute__(name)

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> Any:
        from pydantic_core import core_schema

        return core_schema.no_info_after_validator_function(
            cls, handler.generate_schema(tuple[QueryProjectedFieldV1, ...])
        )


class QueryIncludeItemV1(_StrictQueryEngineModel):
    """One hydrated side-context Claim attached to a primary row."""

    tag: Literal["playbill-query-include-item-v1"] = "playbill-query-include-item-v1"
    claim_object: object
    subject_identity: str | None = None
    visibility: QueryClaimVisibilityV1

    @field_validator("claim_object", mode="before")
    @classmethod
    def _claim_object(cls, value: object) -> object:
        return normalize_canonical(value)


class QueryIncludeResultV1(_StrictQueryEngineModel):
    """One include's hydrated items with its own explicit item accounting."""

    tag: Literal["playbill-query-include-result-v1"] = "playbill-query-include-result-v1"
    name: str
    items: tuple[QueryIncludeItemV1, ...] = ()
    candidate_count: int
    max_items: int
    truncated: bool

    @model_validator(mode="after")
    def _shape(self) -> "QueryIncludeResultV1":
        if len(self.items) > self.max_items:
            raise ValueError("a query include cannot retain more items than its declared budget")
        if self.truncated != (self.candidate_count > len(self.items)):
            raise ValueError("query include truncation must agree with its retained item count")
        return self


class QueryResultRowV1(_StrictQueryEngineModel):
    """One result row together with every Claim it was read through."""

    tag: Literal["playbill-query-result-row-v1", "playbill-query-result-row-v2"] = (
        "playbill-query-result-row-v1"
    )
    bindings: tuple[QueryRowBindingV1, ...]
    artifact: QueryArtifactDefinitionV2 | None = None
    result_subject_identity: str | None = None
    path: tuple[QueryClaimVisibilityV1, ...] = ()
    relation_claim: QueryClaimVisibilityV1 | None = None
    fields: QueryProjectedFields = Field(default_factory=QueryProjectedFields)
    read_claims: tuple[QueryClaimVisibilityV1, ...] = ()
    includes: tuple[QueryIncludeResultV1, ...] = ()
    conflicts: tuple[QueryConflictV1, ...] = ()

    @model_serializer(mode="wrap")
    def _wire(self, handler: Any) -> dict[str, Any]:
        payload = handler(self)
        if self.tag == "playbill-query-result-row-v1":
            payload.pop("artifact", None)
        return cast(dict[str, Any], payload)

    @model_validator(mode="after")
    def _artifact_shape(self) -> "QueryResultRowV1":
        if (self.tag == "playbill-query-result-row-v2") != (self.artifact is not None):
            raise ValueError("only v2 definition rows carry an artifact")
        return self


class QueryTruncationV1(_StrictQueryEngineModel):
    """Explicit clipping accounting; a silently narrowed result is unrepresentable."""

    tag: Literal["playbill-query-truncation-v1"] = "playbill-query-truncation-v1"
    clipped_budgets: tuple[QueryClippedBudgetV1, ...] = ()
    truncated_includes: tuple[str, ...] = ()
    candidate_result_count: int = 0
    returned_result_count: int = 0
    evaluated_path_count: int | None = None
    retained_path_count: int | None = None

    @field_validator("clipped_budgets")
    @classmethod
    def _clipped(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("clipped query budgets must be sorted and unique")
        return value

    @field_validator("truncated_includes")
    @classmethod
    def _includes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("truncated query includes must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "QueryTruncationV1":
        if ("include_max_items" in self.clipped_budgets) != bool(self.truncated_includes):
            raise ValueError("include truncation must name the exact includes that clipped")
        if ("max_results" in self.clipped_budgets) != (
            self.candidate_result_count > self.returned_result_count
        ):
            raise ValueError("result truncation must agree with the returned row count")
        return self

    @property
    def truncated(self) -> bool:
        """Return whether any declared budget clipped this result."""

        return bool(self.clipped_budgets)


class QueryVerdictExclusionV1(_StrictQueryEngineModel):
    """How many Claims one excluded verdict hid from this evaluation."""

    tag: Literal["playbill-query-verdict-exclusion-v1"] = "playbill-query-verdict-exclusion-v1"
    verdict: EvidenceRelativeClaimVerdict
    excluded_claim_count: int = Field(ge=1)


class QueryVerdictVisibilityV1(_StrictQueryEngineModel):
    """Advisory accounting of the Claims the evaluation policy did not show.

    Not part of the digest preimage: this reports what the read declined to look
    at, never what it read, so two evaluations that commit to the same rows stay
    byte-identical whether or not either hid anything.
    """

    tag: Literal["playbill-query-verdict-visibility-v1"] = "playbill-query-verdict-visibility-v1"
    excluded_claim_count: int = Field(ge=1)
    excluded_by_verdict: tuple[QueryVerdictExclusionV1, ...] = ()
    visible_verdicts: tuple[EvidenceRelativeClaimVerdict, ...] = ()

    @field_validator("excluded_by_verdict")
    @classmethod
    def _exclusions(
        cls, value: tuple[QueryVerdictExclusionV1, ...]
    ) -> tuple[QueryVerdictExclusionV1, ...]:
        verdicts = tuple(item.verdict for item in value)
        if verdicts != byte_sorted(verdicts):
            raise ValueError("excluded query verdicts must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "QueryVerdictVisibilityV1":
        counted = sum(item.excluded_claim_count for item in self.excluded_by_verdict)
        if counted != self.excluded_claim_count:
            raise ValueError("excluded claim count must equal its per-verdict accounting")
        return self


class QueryParameterBindingV1(_StrictQueryEngineModel):
    """One resolved caller parameter exactly as the evaluation bound it."""

    tag: Literal["playbill-query-parameter-binding-v1"] = "playbill-query-parameter-binding-v1"
    name: str
    value_type: QueryValueTypeV1
    value: object = None

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object) -> object:
        return normalize_canonical(value)


class ClaimQueryResultV1(_StrictQueryEngineModel):
    """One replayable canonical read of accepted Claim state."""

    tag: Literal["playbill-query-result-v1", "playbill-query-result-v2"] = (
        "playbill-query-result-v1"
    )
    verdict: Literal["completed", "refused"]
    definition_path: str
    definition_digest: str
    parameters: tuple[QueryParameterBindingV1, ...] = ()
    parameter_digest: str
    coordinate: AcceptedProjectionCoordinate
    evaluated_at: datetime
    expires_at: datetime | None = None
    budgets: QueryBudgetsV1
    result_shape: QueryResultShapeV1
    result_cardinality: QueryResultCardinalityV1
    result_binding: str
    dedupe: QueryDedupeV1
    rows: tuple[QueryResultRowV1, ...] = ()
    conflicts: tuple[QueryConflictV1, ...] = ()
    truncation: QueryTruncationV1
    refusal: QueryRefusalV1 | None = None
    # Advisory sidecar, deliberately OUTSIDE the digest preimage (see
    # `claim_query_result_digest`). A Claim the evaluation policy hides is
    # indistinguishable from a Claim that does not exist -- a projected field
    # comes back "absent" either way -- so a definition whose visible_verdicts
    # omit the verdicts its Claims actually carry answers every run with silence
    # and looks healthy doing it. This says how many rows that silence covers.
    # It reports what was NOT read, so it cannot change what the read committed
    # to: keeping it out of the preimage means no result digest and no receipt
    # re-pins, and a caller who ignores it still replays byte-identically.
    verdict_visibility: QueryVerdictVisibilityV1 | None = None

    @field_validator("evaluated_at", "expires_at")
    @classmethod
    def _time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("query result times must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "ClaimQueryResultV1":
        artifact_query = self.result_shape == "artifact_definition"
        if (self.tag == "playbill-query-result-v2") != artifact_query:
            raise ValueError("artifact results use v2; Claim results retain v1")
        if any((row.artifact is not None) != artifact_query for row in self.rows):
            raise ValueError("result rows disagree with the declared shape")
        if (self.verdict == "refused") != (self.refusal is not None):
            raise ValueError("a query result is refused exactly when it carries a refusal")
        if self.verdict == "refused" and (self.rows or self.conflicts):
            raise ValueError("a refused query result carries neither rows nor conflicts")
        if self.expires_at is not None and self.expires_at < self.evaluated_at:
            raise ValueError("query result expiry cannot precede its evaluation time")
        return self


class QueryExecutionReceiptV1(_StrictQueryEngineModel):
    """The exact replay coordinates of one query execution.

    The query-receipt journal wiring lands in the PC-F discovery slice; this
    model is the payload that wiring will record.
    """

    tag: Literal["playbill-query-execution-receipt-v1"] = "playbill-query-execution-receipt-v1"
    definition_path: str
    definition_digest: str
    parameter_digest: str
    coordinate: AcceptedProjectionCoordinate
    evaluation_time: datetime
    budgets: QueryBudgetsV1
    truncation: QueryTruncationV1
    verdict: Literal["completed", "refused"]
    refusal_code: str | None = None
    result_digest: str

    @field_validator("evaluation_time")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("query receipt evaluation time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "QueryExecutionReceiptV1":
        if (self.verdict == "refused") != (self.refusal_code is not None):
            raise ValueError("a query receipt names a refusal code exactly when it refused")
        return self
