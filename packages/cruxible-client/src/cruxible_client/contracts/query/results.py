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
from cruxible_client.contracts.claim_verdicts import (
    EvidenceCurrency,
    EvidenceRelativeClaimVerdictV1,
)
from cruxible_client.contracts.diagnostics import normalize_code
from cruxible_client.contracts.procedures.artifacts import (
    ProcedureArtifact,
    procedure_artifact_digest,
    procedure_path,
)
from cruxible_client.contracts.projection import AcceptedProjectionCoordinate
from cruxible_client.contracts.query.definitions import (
    QueryDedupe,
    QueryResultCardinality,
    QueryResultShape,
)
from cruxible_client.contracts.query.grammar import QueryBudgets, QueryValueType, byte_sorted


class QueryArtifactDefinition(BaseModel):
    """The full definition and its exact accepted identity/path/version binding."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    identity: str
    path: str
    artifact_digest: str
    definition: ClaimType | ProcedureArtifact

    @model_validator(mode="after")
    def _binding(self) -> "QueryArtifactDefinition":
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


QueryClippedBudget = Literal[
    "include_max_items",
    "max_paths",
    "max_paths_per_result",
    "max_results",
]
QueryValueState = Literal["absent", "conflict", "present"]


class _StrictQueryEngineModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class QueryClaimVisibility(_StrictQueryEngineModel):
    """Why one Claim row is present: its verdict and currency at the read time."""

    tag: Literal["playbill-query-claim-visibility-v1"] = "playbill-query-claim-visibility-v1"
    claim_path: str
    statement_digest: str
    artifact_digest: str
    predicate: str
    subject_identity: str
    verdict: EvidenceRelativeClaimVerdictV1
    currency: EvidenceCurrency


class QueryConflict(_StrictQueryEngineModel):
    """Competing accepted Claims surfaced instead of silently resolved."""

    tag: Literal["playbill-query-conflict-v1"] = "playbill-query-conflict-v1"
    kind: Literal["claim_object", "result_cardinality"]
    binding: str | None = None
    predicate: str | None = None
    subject_identity: str | None = None
    statement_digests: tuple[str, ...] = ()
    subject_identities: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> "QueryConflict":
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


class QueryRefusal(_StrictQueryEngineModel):
    """One typed, dot-namespaced refusal; a refused query returns no rows."""

    tag: Literal["playbill-query-refusal-v1"] = "playbill-query-refusal-v1"
    code: str
    message: str
    statement_digests: tuple[str, ...] = ()
    subject_identities: tuple[str, ...] = ()

    @field_validator("code")
    @classmethod
    def _code(cls, value: str) -> str:
        if not normalize_code(value).startswith("cruxible.query."):
            raise ValueError("a query refusal code must be cruxible.query dot-namespaced")
        return value


class QueryRowBinding(_StrictQueryEngineModel):
    """One declared row binding and the accepted Subject it resolved to."""

    tag: Literal["playbill-query-row-binding-v1"] = "playbill-query-row-binding-v1"
    binding: str
    subject_identity: str | None = None
    subject_kind: str | None = None
    subject_id: str | None = None
    subject_path: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> "QueryRowBinding":
        bound = (self.subject_identity, self.subject_kind, self.subject_id, self.subject_path)
        if any(item is None for item in bound) and any(item is not None for item in bound):
            raise ValueError("a query row binding is either fully bound or fully unbound")
        return self


class QueryProjectedField(_StrictQueryEngineModel):
    """One projected field; absence and conflict are stated, never rendered null."""

    tag: Literal["playbill-query-projected-field-v1"] = "playbill-query-projected-field-v1"
    name: str
    state: QueryValueState
    value: object = None

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object) -> object:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _shape(self) -> "QueryProjectedField":
        if self.state != "present" and self.value is not None:
            raise ValueError("an absent or conflicted projected field carries no value")
        return self


class QueryProjectedFields(tuple[QueryProjectedField, ...]):
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
            cls, handler.generate_schema(tuple[QueryProjectedField, ...])
        )


class QueryIncludeItem(_StrictQueryEngineModel):
    """One hydrated side-context Claim attached to a primary row."""

    tag: Literal["playbill-query-include-item-v1"] = "playbill-query-include-item-v1"
    claim_object: object
    subject_identity: str | None = None
    visibility: QueryClaimVisibility

    @field_validator("claim_object", mode="before")
    @classmethod
    def _claim_object(cls, value: object) -> object:
        return normalize_canonical(value)


class QueryIncludeResult(_StrictQueryEngineModel):
    """One include's hydrated items with its own explicit item accounting."""

    tag: Literal["playbill-query-include-result-v1"] = "playbill-query-include-result-v1"
    name: str
    items: tuple[QueryIncludeItem, ...] = ()
    candidate_count: int
    max_items: int
    truncated: bool

    @model_validator(mode="after")
    def _shape(self) -> "QueryIncludeResult":
        if len(self.items) > self.max_items:
            raise ValueError("a query include cannot retain more items than its declared budget")
        if self.truncated != (self.candidate_count > len(self.items)):
            raise ValueError("query include truncation must agree with its retained item count")
        return self


class QueryResultRow(_StrictQueryEngineModel):
    """One result row together with every Claim it was read through."""

    tag: Literal["playbill-query-result-row-v1", "playbill-query-result-row-v2"] = (
        "playbill-query-result-row-v1"
    )
    bindings: tuple[QueryRowBinding, ...]
    artifact: QueryArtifactDefinition | None = None
    result_subject_identity: str | None = None
    path: tuple[QueryClaimVisibility, ...] = ()
    relation_claim: QueryClaimVisibility | None = None
    fields: QueryProjectedFields = Field(default_factory=QueryProjectedFields)
    read_claims: tuple[QueryClaimVisibility, ...] = ()
    includes: tuple[QueryIncludeResult, ...] = ()
    conflicts: tuple[QueryConflict, ...] = ()

    @model_serializer(mode="wrap")
    def _wire(self, handler: Any) -> dict[str, Any]:
        payload = handler(self)
        if self.tag == "playbill-query-result-row-v1":
            payload.pop("artifact", None)
        return cast(dict[str, Any], payload)

    @model_validator(mode="after")
    def _artifact_shape(self) -> "QueryResultRow":
        if (self.tag == "playbill-query-result-row-v2") != (self.artifact is not None):
            raise ValueError("only v2 definition rows carry an artifact")
        return self


class QueryTruncation(_StrictQueryEngineModel):
    """Explicit clipping accounting; a silently narrowed result is unrepresentable."""

    tag: Literal["playbill-query-truncation-v1"] = "playbill-query-truncation-v1"
    clipped_budgets: tuple[QueryClippedBudget, ...] = ()
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
    def _shape(self) -> "QueryTruncation":
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


class QueryVerdictExclusion(_StrictQueryEngineModel):
    """How many Claims one excluded verdict hid from this evaluation."""

    tag: Literal["playbill-query-verdict-exclusion-v1"] = "playbill-query-verdict-exclusion-v1"
    verdict: EvidenceRelativeClaimVerdictV1
    excluded_claim_count: int = Field(ge=1)


class QueryVerdictVisibility(_StrictQueryEngineModel):
    """Advisory accounting of the Claims the evaluation policy did not show.

    Not part of the digest preimage: this reports what the read declined to look
    at, never what it read, so two evaluations that commit to the same rows stay
    byte-identical whether or not either hid anything.
    """

    tag: Literal["playbill-query-verdict-visibility-v1"] = "playbill-query-verdict-visibility-v1"
    excluded_claim_count: int = Field(ge=1)
    excluded_by_verdict: tuple[QueryVerdictExclusion, ...] = ()
    visible_verdicts: tuple[EvidenceRelativeClaimVerdictV1, ...] = ()

    @field_validator("excluded_by_verdict")
    @classmethod
    def _exclusions(
        cls, value: tuple[QueryVerdictExclusion, ...]
    ) -> tuple[QueryVerdictExclusion, ...]:
        verdicts = tuple(item.verdict for item in value)
        if verdicts != byte_sorted(verdicts):
            raise ValueError("excluded query verdicts must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "QueryVerdictVisibility":
        counted = sum(item.excluded_claim_count for item in self.excluded_by_verdict)
        if counted != self.excluded_claim_count:
            raise ValueError("excluded claim count must equal its per-verdict accounting")
        return self


class QueryParameterBinding(_StrictQueryEngineModel):
    """One resolved caller parameter exactly as the evaluation bound it."""

    tag: Literal["playbill-query-parameter-binding-v1"] = "playbill-query-parameter-binding-v1"
    name: str
    value_type: QueryValueType
    value: object = None

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object) -> object:
        return normalize_canonical(value)


class ClaimQueryResult(_StrictQueryEngineModel):
    """One replayable canonical read of accepted Claim state."""

    tag: Literal["playbill-query-result-v1", "playbill-query-result-v2"] = (
        "playbill-query-result-v1"
    )
    verdict: Literal["completed", "refused"]
    definition_path: str
    definition_digest: str
    parameters: tuple[QueryParameterBinding, ...] = ()
    parameter_digest: str
    coordinate: AcceptedProjectionCoordinate
    evaluated_at: datetime
    expires_at: datetime | None = None
    budgets: QueryBudgets
    result_shape: QueryResultShape
    result_cardinality: QueryResultCardinality
    result_binding: str
    dedupe: QueryDedupe
    rows: tuple[QueryResultRow, ...] = ()
    conflicts: tuple[QueryConflict, ...] = ()
    truncation: QueryTruncation
    refusal: QueryRefusal | None = None
    # Advisory sidecar, deliberately OUTSIDE the digest preimage (see
    # `claim_query_result_digest`). A Claim the evaluation policy hides is
    # indistinguishable from a Claim that does not exist -- a projected field
    # comes back "absent" either way -- so a definition whose visible_verdicts
    # omit the verdicts its Claims actually carry answers every run with silence
    # and looks healthy doing it. This says how many rows that silence covers.
    # It reports what was NOT read, so it cannot change what the read committed
    # to: keeping it out of the preimage means no result digest and no receipt
    # re-pins, and a caller who ignores it still replays byte-identically.
    verdict_visibility: QueryVerdictVisibility | None = None

    @field_validator("evaluated_at", "expires_at")
    @classmethod
    def _time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("query result times must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "ClaimQueryResult":
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


class QueryExecutionReceipt(_StrictQueryEngineModel):
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
    budgets: QueryBudgets
    truncation: QueryTruncation
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
    def _shape(self) -> "QueryExecutionReceipt":
        if (self.verdict == "refused") != (self.refusal_code is not None):
            raise ValueError("a query receipt names a refusal code exactly when it refused")
        return self
