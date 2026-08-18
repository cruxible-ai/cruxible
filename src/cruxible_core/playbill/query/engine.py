"""Direct evaluation of accepted QueryDefinitions over accepted Claim facts.

An evaluation is a pure function of the accepted definition digest, the caller
parameters, the accepted coordinate, and one explicit evaluation time. Nothing
here reads a wall clock, caches a result, or picks a winner among competing
accepted Claims: a one-cardinality read either surfaces its conflict set or
refuses with the exact conflicting statement digests.

Every Claim row becomes visible only through the existing evidence-relative
verdict machinery in :mod:`cruxible_core.playbill.claim_verdicts`; this module
never re-implements adjudication. Every budget that clips a result is named in
the result's truncation accounting, so a silently narrowed answer is
unrepresentable.

The PC-F backend slice replaces :class:`DirectClaimFactIndex` with a
materialized backend. Parity is measured against exactly five primitives:

``subjects(kinds, subject_id=...)``
    the canonically ordered accepted Subject paths of the declared kinds.
``subject(artifact_path)``
    one accepted Subject row by its exact ledger path.
``claims_on(artifact_path, predicate)``
    the visible Claim rows whose statement subject is that Subject.
``claims_to(artifact_path, predicate)``
    the visible Claim rows whose Subject-typed object is that Subject.
``visibility(row)``
    one Claim row's verdict and currency at the explicit evaluation time.

A backend that reproduces those five, in the same canonical order and under the
same verdict computation, reproduces this evaluator's results byte for byte.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import cmp_to_key
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_core.playbill.canonical import (
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_core.playbill.claim_attestations import VerifiedClaimAttestationV1
from cruxible_core.playbill.claim_verdicts import (
    CaptureVerdictEvidenceV1,
    ClaimAdjudicationRuleV1,
    EvidenceCurrency,
    EvidenceRelativeClaimVerdict,
    evaluate_claim_verdict,
)
from cruxible_core.playbill.claims import AcceptedClaim, SubjectClaimObject
from cruxible_core.playbill.errors import PlaybillError
from cruxible_core.playbill.projection import AcceptedProjectionCoordinate
from cruxible_core.playbill.providers import ProviderV1
from cruxible_core.playbill.query.definitions import (
    AcceptedQueryDefinitionV1,
    QueryDedupeV1,
    QueryDefinitionV1,
    QueryResultCardinalityV1,
    QueryResultShapeV1,
)
from cruxible_core.playbill.query.grammar import (
    QueryBudgetsV1,
    QueryClaimPresenceFilterV1,
    QueryComparisonFilterV1,
    QueryConjunctionFilterV1,
    QueryDisjunctionFilterV1,
    QueryEvaluationTimeRefV1,
    QueryFilterV1,
    QueryIncludeV1,
    QueryLiteralRefV1,
    QueryMembershipFilterV1,
    QueryNegationFilterV1,
    QueryOrderingV1,
    QueryParameterRefV1,
    QuerySubjectFieldRefV1,
    QueryTraversalDirectionV1,
    QueryValueRefV1,
    QueryValueTypeV1,
)
from cruxible_core.playbill.subjects import AcceptedSubject

QueryClippedBudgetV1 = Literal[
    "include_max_items",
    "max_paths",
    "max_paths_per_result",
    "max_results",
]
QueryValueStateV1 = Literal["absent", "conflict", "present"]

PARAMETER_DIGEST_DOMAIN = "playbill-query-parameters-v1"
RESULT_DIGEST_DOMAIN = "playbill-query-result-v1"

BUDGET_BELOW_DECLARED_DEPTH = "playbill.query.budget_below_declared_depth"
BUDGET_EXCEEDS_MAXIMUM = "playbill.query.budget_exceeds_maximum"
CLAIM_CONFLICT = "playbill.query.claim_conflict"
COORDINATE_MISMATCH = "playbill.query.coordinate_mismatch"
EVALUATION_TIME_NOT_ABSOLUTE = "playbill.query.evaluation_time_not_absolute"
PARAMETER_MISSING = "playbill.query.parameter_missing"
PARAMETER_TYPE_MISMATCH = "playbill.query.parameter_type_mismatch"
PARAMETER_UNDECLARED = "playbill.query.parameter_undeclared"
RESULT_CONFLICT = "playbill.query.result_conflict"
SUBJECT_FIELD_UNAVAILABLE = "playbill.query.subject_field_unavailable"
SUBJECT_UNRESOLVED = "playbill.query.subject_unresolved"
TRAVERSAL_OBJECT_NOT_SUBJECT = "playbill.query.traversal_object_not_subject"
VALUE_TYPE_MISMATCH = "playbill.query.value_type_mismatch"


class _StrictQueryEngineModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _byte_sorted(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(set(values), key=lambda item: item.encode("utf-8")))


# -- accepted evaluation facts -------------------------------------------


class ClaimFactRowV1(_StrictQueryEngineModel):
    """One accepted Claim with the exact inputs its verdict needs at any time."""

    tag: Literal["playbill-query-claim-fact-v1"] = "playbill-query-claim-fact-v1"
    accepted: AcceptedClaim
    rule: ClaimAdjudicationRuleV1
    captures: tuple[CaptureVerdictEvidenceV1, ...] = ()
    attestations: tuple[VerifiedClaimAttestationV1, ...] = ()
    referent_current: bool = True
    resolved_authority_basis: tuple[str, ...] = ()

    @field_validator("resolved_authority_basis")
    @classmethod
    def _authority_basis(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("Claim authority basis must be sorted and unique")
        return value


class ClaimQueryFactsV1(_StrictQueryEngineModel):
    """The accepted Subject/Claim facts one evaluation is permitted to read."""

    tag: Literal["playbill-query-facts-v1"] = "playbill-query-facts-v1"
    coordinate: AcceptedProjectionCoordinate
    subjects: tuple[AcceptedSubject, ...] = ()
    claims: tuple[ClaimFactRowV1, ...] = ()
    providers: tuple[ProviderV1, ...] = ()

    @field_validator("subjects")
    @classmethod
    def _subjects(cls, value: tuple[AcceptedSubject, ...]) -> tuple[AcceptedSubject, ...]:
        paths = tuple(item.path for item in value)
        if paths != _byte_sorted(paths):
            raise ValueError("accepted query Subject facts must be sorted and unique by path")
        return value

    @field_validator("claims")
    @classmethod
    def _claims(cls, value: tuple[ClaimFactRowV1, ...]) -> tuple[ClaimFactRowV1, ...]:
        paths = tuple(item.accepted.path for item in value)
        if paths != _byte_sorted(paths):
            raise ValueError("accepted query Claim facts must be sorted and unique by path")
        return value

    @field_validator("providers")
    @classmethod
    def _providers(cls, value: tuple[ProviderV1, ...]) -> tuple[ProviderV1, ...]:
        names = tuple(item.identity.qualified for item in value)
        if names != _byte_sorted(names):
            raise ValueError("accepted query Provider facts must be sorted and unique by identity")
        return value


# -- result surface -------------------------------------------------------


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

    tag: Literal["playbill-query-result-row-v1"] = "playbill-query-result-row-v1"
    bindings: tuple[QueryRowBindingV1, ...]
    result_subject_identity: str | None = None
    path: tuple[QueryClaimVisibilityV1, ...] = ()
    relation_claim: QueryClaimVisibilityV1 | None = None
    fields: tuple[QueryProjectedFieldV1, ...] = ()
    read_claims: tuple[QueryClaimVisibilityV1, ...] = ()
    includes: tuple[QueryIncludeResultV1, ...] = ()
    conflicts: tuple[QueryConflictV1, ...] = ()


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
        if value != _byte_sorted(value):
            raise ValueError("clipped query budgets must be sorted and unique")
        return value

    @field_validator("truncated_includes")
    @classmethod
    def _includes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != _byte_sorted(value):
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

    tag: Literal["playbill-query-result-v1"] = "playbill-query-result-v1"
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

    @field_validator("evaluated_at", "expires_at")
    @classmethod
    def _time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("query result times must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "ClaimQueryResultV1":
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


def query_parameter_digest(parameters: Sequence[QueryParameterBindingV1]) -> str:
    """Digest the exact resolved parameter binding of one evaluation."""

    return typed_digest(
        Sha256Value,
        PARAMETER_DIGEST_DOMAIN,
        {"parameters": [item.model_dump(mode="json") for item in parameters]},
    ).tagged


def claim_query_result_digest(result: ClaimQueryResultV1) -> str:
    """Digest one complete result, ordering and truncation accounting included."""

    payload = result.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(Sha256Value, RESULT_DIGEST_DOMAIN, payload).tagged


def query_execution_receipt(result: ClaimQueryResultV1) -> QueryExecutionReceiptV1:
    """Model the replay receipt of one evaluation without journalling it."""

    return QueryExecutionReceiptV1(
        definition_path=result.definition_path,
        definition_digest=result.definition_digest,
        parameter_digest=result.parameter_digest,
        coordinate=result.coordinate,
        evaluation_time=result.evaluated_at,
        budgets=result.budgets,
        truncation=result.truncation,
        verdict=result.verdict,
        refusal_code=None if result.refusal is None else result.refusal.code,
        result_digest=claim_query_result_digest(result),
    )


# -- typed value handling -------------------------------------------------


class _Typed(NamedTuple):
    ok: bool
    value: object


def _coerce_timestamp(value: object) -> _Typed:
    if not isinstance(value, str):
        return _Typed(False, None)
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return _Typed(False, None)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return _Typed(False, None)
    return _Typed(True, parsed)


def _coerce(value: object, value_type: QueryValueTypeV1) -> _Typed:
    """Return the declared type's comparable form, or a typed mismatch."""

    if value_type == "boolean":
        return _Typed(isinstance(value, bool), value)
    if value_type == "integer":
        return _Typed(isinstance(value, int) and not isinstance(value, bool), value)
    if value_type in {"string", "subject_reference"}:
        if not isinstance(value, str):
            return _Typed(False, None)
        return _Typed(True, value.encode("utf-8"))
    if value_type == "decimal":
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            return _Typed(False, None)
        try:
            return _Typed(True, Decimal(value))
        except (InvalidOperation, ValueError):
            return _Typed(False, None)
    return _coerce_timestamp(value)


def _compare(left: object, right: object, value_type: QueryValueTypeV1) -> int:
    first: Any = int(bool(left)) if value_type == "boolean" else left
    second: Any = int(bool(right)) if value_type == "boolean" else right
    if first == second:
        return 0
    return -1 if first < second else 1


def _render_time(value: datetime) -> str:
    utc = value.astimezone(UTC)
    timespec = "microseconds" if utc.microsecond else "seconds"
    return utc.isoformat(timespec=timespec).replace("+00:00", "Z")


class ClaimQueryError(PlaybillError):
    """An evaluation input has no representation in a canonical query result.

    A result always states the exact instant it was evaluated at, so an
    evaluation time that is not an absolute instant cannot be reported as a
    refused result; it is refused before a result exists.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class _RefusalSignal(Exception):
    """Internal fail-closed signal carrying one typed query refusal."""

    def __init__(self, refusal: QueryRefusalV1) -> None:
        super().__init__(refusal.code)
        self.refusal = refusal


def _refuse(
    code: str,
    message: str,
    *,
    statement_digests: tuple[str, ...] = (),
    subject_identities: tuple[str, ...] = (),
) -> _RefusalSignal:
    return _RefusalSignal(
        QueryRefusalV1(
            code=code,
            message=message,
            statement_digests=_byte_sorted(statement_digests),
            subject_identities=_byte_sorted(subject_identities),
        )
    )


# -- direct fact index ----------------------------------------------------


@dataclass(frozen=True)
class VisibleClaimRow:
    """One accepted Claim admitted by the owning query's verdict policy."""

    row: ClaimFactRowV1
    visibility: QueryClaimVisibilityV1

    @property
    def subject_path(self) -> str:
        return self.row.accepted.claim.statement.subject.artifact_path

    @property
    def object_subject_path(self) -> str | None:
        item = self.row.accepted.claim.statement.object
        return item.address.artifact_path if isinstance(item, SubjectClaimObject) else None


class DirectClaimFactIndex:
    """Direct accepted-projection reads under one query's verdict policy.

    This is the evaluation primitive surface a PC-F query backend must
    reproduce for parity: identical rows in identical canonical order, under an
    identical verdict computation at the same explicit evaluation time.
    """

    def __init__(
        self,
        facts: ClaimQueryFactsV1,
        *,
        definition: QueryDefinitionV1,
        evaluation_time: datetime,
    ) -> None:
        self._facts = facts
        self._definition = definition
        self._evaluation_time = evaluation_time
        self._providers: dict[str, ProviderV1] = {
            provider.identity.qualified: provider for provider in facts.providers
        }
        self._subjects: dict[str, AcceptedSubject] = {
            subject.path: subject for subject in facts.subjects
        }
        self._by_subject: dict[tuple[str, str], list[VisibleClaimRow]] = {}
        self._by_object: dict[tuple[str, str], list[VisibleClaimRow]] = {}
        for row in facts.claims:
            visible = self.visibility(row)
            if visible is None:
                continue
            statement = row.accepted.claim.statement
            key = (statement.subject.artifact_path, statement.predicate)
            self._by_subject.setdefault(key, []).append(visible)
            target = visible.object_subject_path
            if target is not None:
                self._by_object.setdefault((target, statement.predicate), []).append(visible)

    def visibility(self, row: ClaimFactRowV1) -> VisibleClaimRow | None:
        """Return the Claim row's visibility, or None when the policy hides it."""

        statement = row.accepted.claim.statement
        subject = self._subjects.get(statement.subject.artifact_path)
        if subject is None:
            return None
        verdict = evaluate_claim_verdict(
            claim_statement_digest=row.accepted.statement_digest,
            rule=row.rule,
            evaluation_time=self._evaluation_time,
            captures=row.captures,
            attestations=row.attestations,
            providers=self._providers,
            claim_effective_from=statement.effective_from,
            claim_effective_until=statement.effective_until,
            referent_current=row.referent_current,
            resolved_authority_basis=row.resolved_authority_basis,
        )
        policy = self._definition.evaluation_policy
        if verdict.verdict not in policy.visible_verdicts:
            return None
        if verdict.currency not in policy.visible_currency:
            return None
        return VisibleClaimRow(
            row=row,
            visibility=QueryClaimVisibilityV1(
                claim_path=row.accepted.path,
                statement_digest=row.accepted.statement_digest,
                artifact_digest=row.accepted.artifact_digest,
                predicate=statement.predicate,
                subject_identity=subject.shell.identity.qualified,
                verdict=verdict.verdict,
                currency=verdict.currency,
            ),
        )

    def subjects(self, kinds: tuple[str, ...], *, subject_id: str | None = None) -> tuple[str, ...]:
        """Return the canonically ordered Subject paths of the declared kinds."""

        admitted = set(kinds)
        return tuple(
            subject.path
            for subject in self._facts.subjects
            if subject.shell.subject_kind in admitted
            and (subject_id is None or subject.shell.subject_id == subject_id)
        )

    def subject(self, artifact_path: str) -> AcceptedSubject | None:
        """Return one accepted Subject row by its exact ledger path."""

        return self._subjects.get(artifact_path)

    def claims_on(self, artifact_path: str, predicate: str) -> tuple[VisibleClaimRow, ...]:
        """Return the visible Claims whose statement subject is that Subject."""

        return tuple(self._by_subject.get((artifact_path, predicate), ()))

    def claims_to(self, artifact_path: str, predicate: str) -> tuple[VisibleClaimRow, ...]:
        """Return the visible Claims whose Subject-typed object is that Subject."""

        return tuple(self._by_object.get((artifact_path, predicate), ()))

    def relations(
        self,
        artifact_path: str,
        predicate: str,
        direction: QueryTraversalDirectionV1,
    ) -> tuple[tuple[VisibleClaimRow, str], ...]:
        """Return the visible relation-Claim edges leaving one bound Subject."""

        edges: list[tuple[VisibleClaimRow, str]] = []
        if direction == "forward":
            for claim in self.claims_on(artifact_path, predicate):
                target = claim.object_subject_path
                if target is None:
                    raise _refuse(
                        TRAVERSAL_OBJECT_NOT_SUBJECT,
                        "A traversed Claim does not carry a Subject-typed object.",
                        statement_digests=(claim.visibility.statement_digest,),
                    )
                edges.append((claim, target))
        else:
            edges.extend(
                (claim, claim.subject_path) for claim in self.claims_to(artifact_path, predicate)
            )
        for claim, target in edges:
            if self.subject(target) is None:
                raise _refuse(
                    SUBJECT_UNRESOLVED,
                    "A traversed Claim names a Subject absent at the accepted coordinate.",
                    statement_digests=(claim.visibility.statement_digest,),
                )
        return tuple(edges)


# -- evaluation state -----------------------------------------------------


@dataclass
class _Row:
    bindings: dict[str, str | None]
    edges: tuple[tuple[str, QueryClaimVisibilityV1], ...] = ()
    reads: dict[str, QueryClaimVisibilityV1] = field(default_factory=dict)
    conflicts: dict[bytes, QueryConflictV1] = field(default_factory=dict)
    includes: tuple[QueryIncludeResultV1, ...] = ()

    def copy(self) -> "_Row":
        return _Row(
            bindings=dict(self.bindings),
            edges=self.edges,
            reads=dict(self.reads),
            conflicts=dict(self.conflicts),
        )

    def identity(self) -> bytes:
        ordered = sorted(self.bindings, key=lambda item: item.encode("utf-8"))
        return canonical_bytes(
            {
                "bindings": [[name, self.bindings[name]] for name in ordered],
                "edges": [[binding, item.claim_path] for binding, item in self.edges],
            }
        )

    def record(self, visibility: QueryClaimVisibilityV1) -> None:
        self.reads[visibility.claim_path] = visibility

    def record_conflict(self, conflict: QueryConflictV1) -> None:
        self.conflicts[canonical_bytes(conflict.model_dump(mode="json"))] = conflict

    def ordered_reads(self) -> tuple[QueryClaimVisibilityV1, ...]:
        keys = sorted(self.reads, key=lambda item: item.encode("utf-8"))
        return tuple(self.reads[key] for key in keys)

    def ordered_conflicts(self) -> tuple[QueryConflictV1, ...]:
        return tuple(self.conflicts[key] for key in sorted(self.conflicts))


@dataclass(frozen=True)
class _Value:
    state: QueryValueStateV1
    value: object = None


_ABSENT = _Value(state="absent")
_CONFLICT = _Value(state="conflict")

_OPERATORS: dict[str, Any] = {
    "eq": lambda order: order == 0,
    "ne": lambda order: order != 0,
    "gt": lambda order: order > 0,
    "gte": lambda order: order >= 0,
    "lt": lambda order: order < 0,
    "lte": lambda order: order <= 0,
}


class _Evaluator:
    """Filter, projection, and ordering semantics over bound Claim rows."""

    def __init__(
        self,
        definition: QueryDefinitionV1,
        *,
        index: DirectClaimFactIndex,
        parameters: Mapping[str, object],
        evaluation_time: datetime,
    ) -> None:
        self._definition = definition
        self._index = index
        self._parameters = parameters
        self._evaluation_time = evaluation_time

    def _claim_value(self, row: _Row, binding: str, predicate: str) -> _Value:
        subject_path = row.bindings.get(binding)
        if subject_path is None:
            return _ABSENT
        claims = self._index.claims_on(subject_path, predicate)
        if not claims:
            return _ABSENT
        distinct: dict[bytes, object] = {}
        for claim in claims:
            row.record(claim.visibility)
            item = claim.row.accepted.claim.statement.object
            if isinstance(item, SubjectClaimObject):
                target = self._index.subject(item.address.artifact_path)
                if target is None:
                    raise _refuse(
                        SUBJECT_UNRESOLVED,
                        "A read Claim names a Subject absent at the accepted coordinate.",
                        statement_digests=(claim.visibility.statement_digest,),
                    )
                value: object = target.shell.identity.qualified
            else:
                value = item.model_dump(mode="json").get("value")
            distinct[canonical_bytes(value)] = value
        if len(distinct) == 1:
            return _Value(state="present", value=next(iter(distinct.values())))
        subject = self._index.subject(subject_path)
        assert subject is not None
        digests = _byte_sorted(tuple(claim.visibility.statement_digest for claim in claims))
        if self._definition.evaluation_policy.conflict_behavior == "refuse_on_conflict":
            raise _refuse(
                CLAIM_CONFLICT,
                "Competing accepted Claims answer a one-cardinality read.",
                statement_digests=digests,
                subject_identities=(subject.shell.identity.qualified,),
            )
        row.record_conflict(
            QueryConflictV1(
                kind="claim_object",
                binding=binding,
                predicate=predicate,
                subject_identity=subject.shell.identity.qualified,
                statement_digests=digests,
            )
        )
        return _CONFLICT

    def resolve(self, row: _Row, ref: QueryValueRefV1) -> _Value:
        """Resolve one declared value reference against a bound row."""

        if isinstance(ref, QueryLiteralRefV1):
            return _Value(state="present", value=ref.value)
        if isinstance(ref, QueryParameterRefV1):
            if ref.parameter not in self._parameters:
                return _ABSENT
            return _Value(state="present", value=self._parameters[ref.parameter])
        if isinstance(ref, QueryEvaluationTimeRefV1):
            return _Value(state="present", value=_render_time(self._evaluation_time))
        if isinstance(ref, QuerySubjectFieldRefV1):
            subject_path = row.bindings.get(ref.binding)
            if subject_path is None:
                if ref.binding in row.bindings:
                    return _ABSENT
                raise _refuse(
                    SUBJECT_FIELD_UNAVAILABLE,
                    "A Subject field was read through a binding that carries no Subject.",
                )
            subject = self._index.subject(subject_path)
            assert subject is not None
            return _Value(
                state="present",
                value=(
                    subject.shell.subject_id
                    if ref.field == "subject_id"
                    else subject.shell.subject_kind
                ),
            )
        return self._claim_value(row, ref.binding, ref.predicate)

    def typed(
        self,
        row: _Row,
        ref: QueryValueRefV1,
        value_type: QueryValueTypeV1,
    ) -> _Typed | None:
        """Return the declared-type comparable, or None when the value is absent."""

        resolved = self.resolve(row, ref)
        if resolved.state != "present":
            return None
        coerced = _coerce(resolved.value, value_type)
        if not coerced.ok:
            raise _refuse(
                VALUE_TYPE_MISMATCH,
                f"A query value is not of its declared {value_type} type.",
            )
        return coerced

    def matches(self, row: _Row, filter_: QueryFilterV1) -> bool:
        """Evaluate one declared filter; an absent or conflicted value never matches."""

        if isinstance(filter_, QueryConjunctionFilterV1):
            return all(self.matches(row, item) for item in filter_.filters)
        if isinstance(filter_, QueryDisjunctionFilterV1):
            return any(self.matches(row, item) for item in filter_.filters)
        if isinstance(filter_, QueryNegationFilterV1):
            return not self.matches(row, filter_.operand)
        if isinstance(filter_, QueryClaimPresenceFilterV1):
            subject_path = row.bindings.get(filter_.binding)
            present = False
            if subject_path is not None:
                claims = self._index.claims_on(subject_path, filter_.predicate)
                for claim in claims:
                    row.record(claim.visibility)
                present = bool(claims)
            return present != filter_.negated
        if isinstance(filter_, QueryComparisonFilterV1):
            left = self.typed(row, filter_.left, filter_.value_type)
            right = self.typed(row, filter_.right, filter_.value_type)
            if left is None or right is None:
                return False
            return bool(
                _OPERATORS[filter_.operator](_compare(left.value, right.value, filter_.value_type))
            )
        return self._membership(row, filter_)

    def _membership(self, row: _Row, filter_: QueryMembershipFilterV1) -> bool:
        left = self.typed(row, filter_.left, filter_.value_type)
        if left is None:
            return filter_.negated
        found = False
        for candidate in filter_.values:
            right = self.typed(row, candidate, filter_.value_type)
            if right is not None and _compare(left.value, right.value, filter_.value_type) == 0:
                found = True
                break
        return found != filter_.negated

    def order(
        self,
        rows: Sequence[_Row],
        orderings: tuple[QueryOrderingV1, ...],
        *,
        tiebreaks: Sequence[bytes] | None = None,
    ) -> list[int]:
        """Return the declared row order with a canonical byte-order tiebreak.

        A row whose ordering key is absent or conflicted always sorts last,
        in both ascending and descending directions, so declared direction can
        never reorder unknowns against each other.
        """

        keys = [[self.typed(row, item.key, item.value_type) for item in orderings] for row in rows]
        identities = [
            row.identity() if tiebreaks is None else row.identity() + tiebreaks[index]
            for index, row in enumerate(rows)
        ]

        def compare(left: int, right: int) -> int:
            for index, ordering in enumerate(orderings):
                first, second = keys[left][index], keys[right][index]
                if first is None and second is None:
                    continue
                if first is None:
                    return 1
                if second is None:
                    return -1
                order = _compare(first.value, second.value, ordering.value_type)
                if order != 0:
                    return -order if ordering.direction == "descending" else order
            if identities[left] == identities[right]:
                return 0
            return -1 if identities[left] < identities[right] else 1

        return sorted(range(len(rows)), key=cmp_to_key(compare))


# -- the direct evaluator -------------------------------------------------


def resolve_query_parameters(
    definition: QueryDefinitionV1,
    parameters: Mapping[str, object] | None,
) -> tuple[QueryParameterBindingV1, ...]:
    """Bind caller parameters to their declarations or refuse fail-closed."""

    supplied = dict(parameters or {})
    declared = {item.name: item for item in definition.parameters}
    unknown = _byte_sorted(tuple(set(supplied) - set(declared)))
    if unknown:
        raise _refuse(
            PARAMETER_UNDECLARED,
            f"The query does not declare the parameter {unknown[0]!r}.",
        )
    bindings: list[QueryParameterBindingV1] = []
    for name in sorted(declared, key=lambda item: item.encode("utf-8")):
        declaration = declared[name]
        if name in supplied:
            value = supplied[name]
        elif declaration.required:
            raise _refuse(PARAMETER_MISSING, f"The query requires the parameter {name!r}.")
        else:
            value = declaration.default
        if value is not None and not _coerce(normalize_canonical(value), declaration.value_type).ok:
            raise _refuse(
                PARAMETER_TYPE_MISMATCH,
                f"Parameter {name!r} is not of its declared {declaration.value_type} type.",
            )
        bindings.append(
            QueryParameterBindingV1(name=name, value_type=declaration.value_type, value=value)
        )
    return tuple(bindings)


def _effective_budgets(
    definition: QueryDefinitionV1,
    budgets: QueryBudgetsV1 | None,
) -> QueryBudgetsV1:
    effective = budgets or definition.default_budgets
    if not effective.within(definition.maximum_budgets):
        raise _refuse(
            BUDGET_EXCEEDS_MAXIMUM,
            "The caller budget exceeds the QueryDefinition's declared ceiling.",
        )
    if len(definition.traversal) > effective.max_traversal_depth:
        raise _refuse(
            BUDGET_BELOW_DECLARED_DEPTH,
            "The caller budget cannot admit the declared traversal depth.",
        )
    return effective


def _expiry(definition: QueryDefinitionV1, evaluation_time: datetime) -> datetime | None:
    expiry = definition.evaluation_policy.result_expiry
    if expiry is None:
        return None
    return evaluation_time + timedelta(microseconds=expiry.microseconds)


def _binding_row(index: DirectClaimFactIndex, binding: str, path: str | None) -> QueryRowBindingV1:
    if path is None:
        return QueryRowBindingV1(binding=binding)
    subject = index.subject(path)
    assert subject is not None
    return QueryRowBindingV1(
        binding=binding,
        subject_identity=subject.shell.identity.qualified,
        subject_kind=subject.shell.subject_kind,
        subject_id=subject.shell.subject_id,
        subject_path=path,
    )


def _dedupe_key(row: _Row, definition: QueryDefinitionV1) -> bytes | None:
    if definition.dedupe == "none":
        return None
    if definition.dedupe == "subject":
        return canonical_bytes(row.bindings.get(definition.result_binding))
    return row.identity()


def evaluate_claim_query(
    definition: AcceptedQueryDefinitionV1,
    *,
    facts: ClaimQueryFactsV1,
    coordinate: AcceptedProjectionCoordinate,
    evaluation_time: datetime,
    parameters: Mapping[str, object] | None = None,
    budgets: QueryBudgetsV1 | None = None,
) -> ClaimQueryResultV1:
    """Evaluate one accepted QueryDefinition against accepted Claim facts.

    The result is a pure function of the definition digest, the resolved
    parameters, the accepted coordinate, and the explicit evaluation time.
    """

    query = definition.query
    if evaluation_time.tzinfo is None or evaluation_time.utcoffset() is None:
        raise ClaimQueryError(
            EVALUATION_TIME_NOT_ABSOLUTE,
            "a query evaluation time must be an explicit absolute instant",
        )
    try:
        if canonical_bytes(coordinate.model_dump(mode="json")) != canonical_bytes(
            facts.coordinate.model_dump(mode="json")
        ):
            raise _refuse(
                COORDINATE_MISMATCH,
                "The evaluation coordinate differs from the supplied accepted facts.",
            )
        bindings = resolve_query_parameters(query, parameters)
        return _evaluate(
            definition,
            facts=facts,
            coordinate=coordinate,
            evaluation_time=evaluation_time,
            bindings=bindings,
            budgets=_effective_budgets(query, budgets),
        )
    except _RefusalSignal as signal:
        return ClaimQueryResultV1(
            verdict="refused",
            definition_path=definition.path,
            definition_digest=definition.artifact_digest,
            parameter_digest=query_parameter_digest(()),
            coordinate=coordinate,
            evaluated_at=evaluation_time,
            expires_at=_expiry(query, evaluation_time),
            budgets=budgets or query.default_budgets,
            result_shape=query.result_shape,
            result_cardinality=query.result_cardinality,
            result_binding=query.result_binding,
            dedupe=query.dedupe,
            truncation=QueryTruncationV1(),
            refusal=signal.refusal,
        )


def _entry_rows(
    query: QueryDefinitionV1,
    index: DirectClaimFactIndex,
    parameters: Mapping[str, object],
) -> list[_Row]:
    subject_id: str | None = None
    if query.entry.subject_id is not None:
        supplied = parameters.get(query.entry.subject_id.parameter)
        if not isinstance(supplied, str):
            raise _refuse(
                PARAMETER_MISSING,
                "The query entry binds a Subject identifier parameter that is unbound.",
            )
        subject_id = supplied
    return [
        _Row(bindings={query.entry.binding: path})
        for path in index.subjects(query.entry.subject_kinds, subject_id=subject_id)
    ]


def _traverse(
    query: QueryDefinitionV1,
    evaluator: _Evaluator,
    index: DirectClaimFactIndex,
    rows: list[_Row],
    budgets: QueryBudgetsV1,
    clipped: set[QueryClippedBudgetV1],
) -> list[_Row]:
    for step in query.traversal:
        produced: list[_Row] = []
        for row in rows:
            source = row.bindings.get(step.from_binding)
            matched = False
            if source is not None:
                for claim, target in index.relations(source, step.predicate, step.direction):
                    subject = index.subject(target)
                    assert subject is not None
                    if (
                        step.target_subject_kinds
                        and subject.shell.subject_kind not in step.target_subject_kinds
                    ):
                        continue
                    candidate = row.copy()
                    candidate.bindings[step.binding] = target
                    candidate.edges = (*candidate.edges, (step.binding, claim.visibility))
                    candidate.record(claim.visibility)
                    if step.where is not None and not evaluator.matches(candidate, step.where):
                        continue
                    matched = True
                    produced.append(candidate)
            if not matched and not step.required:
                relaxed = row.copy()
                relaxed.bindings[step.binding] = None
                produced.append(relaxed)
        produced.sort(key=lambda item: item.identity())
        if budgets.max_paths is not None and len(produced) > budgets.max_paths:
            produced = produced[: budgets.max_paths]
            clipped.add("max_paths")
        rows = produced
    return rows


def _hydrate(
    evaluator: _Evaluator,
    index: DirectClaimFactIndex,
    row: _Row,
    include: QueryIncludeV1,
) -> QueryIncludeResultV1:
    source = row.bindings.get(include.from_binding)
    pairs: list[tuple[VisibleClaimRow, str | None]] = []
    if source is not None:
        if include.direction == "forward":
            pairs = [
                (claim, claim.object_subject_path)
                for claim in index.claims_on(source, include.predicate)
            ]
        else:
            pairs = [
                (claim, claim.subject_path) for claim in index.claims_to(source, include.predicate)
            ]
    scoped: list[_Row] = []
    candidates: list[tuple[VisibleClaimRow, str | None]] = []
    for claim, target in pairs:
        candidate = row.copy()
        candidate.bindings[include.binding] = target
        candidate.record(claim.visibility)
        if include.where is not None and not evaluator.matches(candidate, include.where):
            continue
        scoped.append(candidate)
        candidates.append((claim, target))
    order = evaluator.order(
        scoped,
        include.orderings,
        tiebreaks=[claim.visibility.claim_path.encode("utf-8") for claim, _ in candidates],
    )[: include.max_items]
    for position in order:
        for visibility in scoped[position].ordered_reads():
            row.record(visibility)
        for conflict in scoped[position].ordered_conflicts():
            row.record_conflict(conflict)
    return QueryIncludeResultV1(
        name=include.name,
        items=tuple(
            QueryIncludeItemV1(
                claim_object=candidates[position][0].row.accepted.claim.statement.object.model_dump(
                    mode="json"
                ),
                subject_identity=(
                    None
                    if candidates[position][1] is None
                    else _binding_row(
                        index, include.binding, candidates[position][1]
                    ).subject_identity
                ),
                visibility=candidates[position][0].visibility,
            )
            for position in order
        ),
        candidate_count=len(candidates),
        max_items=include.max_items,
        truncated=len(candidates) > len(order),
    )


def _result_subject_identities(
    index: DirectClaimFactIndex,
    query: QueryDefinitionV1,
    rows: Sequence[_Row],
) -> tuple[str, ...]:
    return _byte_sorted(
        tuple(
            _binding_row(
                index, query.result_binding, row.bindings.get(query.result_binding)
            ).subject_identity
            or ""
            for row in rows
        )
    )


def _evaluate(
    definition: AcceptedQueryDefinitionV1,
    *,
    facts: ClaimQueryFactsV1,
    coordinate: AcceptedProjectionCoordinate,
    evaluation_time: datetime,
    bindings: tuple[QueryParameterBindingV1, ...],
    budgets: QueryBudgetsV1,
) -> ClaimQueryResultV1:
    query = definition.query
    index = DirectClaimFactIndex(facts, definition=query, evaluation_time=evaluation_time)
    parameters = {item.name: item.value for item in bindings if item.value is not None}
    evaluator = _Evaluator(
        query,
        index=index,
        parameters=parameters,
        evaluation_time=evaluation_time,
    )
    clipped: set[QueryClippedBudgetV1] = set()

    rows = _traverse(
        query,
        evaluator,
        index,
        _entry_rows(query, index, parameters),
        budgets,
        clipped,
    )
    if query.where is not None:
        rows = [row for row in rows if evaluator.matches(row, query.where)]

    seen: set[bytes] = set()
    deduped: list[_Row] = []
    for row in sorted(rows, key=lambda item: item.identity()):
        key = _dedupe_key(row, query)
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        deduped.append(row)
    rows = deduped
    evaluated_path_count = len(rows) if budgets.max_paths is not None else None

    truncated_includes: set[str] = set()
    for row in rows:
        row.includes = tuple(_hydrate(evaluator, index, row, item) for item in query.includes)
        truncated_includes.update(item.name for item in row.includes if item.truncated)
    if truncated_includes:
        clipped.add("include_max_items")

    if budgets.max_paths_per_result is not None:
        counts: dict[str | None, int] = {}
        retained: list[_Row] = []
        for row in rows:
            key_path = row.bindings.get(query.result_binding)
            count = counts.get(key_path, 0)
            if count >= budgets.max_paths_per_result:
                clipped.add("max_paths_per_result")
                continue
            counts[key_path] = count + 1
            retained.append(row)
        rows = retained
    retained_path_count = len(rows) if budgets.max_paths is not None else None

    rows = [rows[position] for position in evaluator.order(rows, query.orderings)]
    candidate_result_count = len(rows)
    conflicts: dict[bytes, QueryConflictV1] = {}
    if query.result_cardinality == "one" and candidate_result_count > 1:
        identities = _result_subject_identities(index, query, rows)
        if query.evaluation_policy.conflict_behavior == "refuse_on_conflict":
            raise _refuse(
                RESULT_CONFLICT,
                "A one-cardinality query resolves to more than one accepted row.",
                subject_identities=identities,
            )
        conflict = QueryConflictV1(kind="result_cardinality", subject_identities=identities)
        conflicts[canonical_bytes(conflict.model_dump(mode="json"))] = conflict
    if candidate_result_count > budgets.max_results:
        rows = rows[: budgets.max_results]
        clipped.add("max_results")

    result_rows: list[QueryResultRowV1] = []
    for row in rows:
        fields: list[QueryProjectedFieldV1] = []
        for declared in () if query.projection is None else query.projection.fields:
            resolved = evaluator.resolve(row, declared.value)
            fields.append(
                QueryProjectedFieldV1(
                    name=declared.name,
                    state=resolved.state,
                    value=resolved.value if resolved.state == "present" else None,
                )
            )
        relation = next(
            (item for binding, item in row.edges if binding == query.result_binding),
            None,
        )
        for conflict in row.ordered_conflicts():
            conflicts[canonical_bytes(conflict.model_dump(mode="json"))] = conflict
        result_rows.append(
            QueryResultRowV1(
                bindings=tuple(
                    _binding_row(index, binding, row.bindings.get(binding))
                    for binding in query.row_bindings
                ),
                result_subject_identity=_binding_row(
                    index, query.result_binding, row.bindings.get(query.result_binding)
                ).subject_identity,
                path=tuple(item for _, item in row.edges),
                relation_claim=relation if query.result_shape == "relation_claim" else None,
                fields=tuple(fields),
                read_claims=row.ordered_reads(),
                includes=row.includes,
                conflicts=row.ordered_conflicts(),
            )
        )

    ordered_budgets: tuple[QueryClippedBudgetV1, ...] = tuple(
        item for item in _CLIPPED_BUDGET_ORDER if item in clipped
    )
    return ClaimQueryResultV1(
        verdict="completed",
        definition_path=definition.path,
        definition_digest=definition.artifact_digest,
        parameters=bindings,
        parameter_digest=query_parameter_digest(bindings),
        coordinate=coordinate,
        evaluated_at=evaluation_time,
        expires_at=_expiry(query, evaluation_time),
        budgets=budgets,
        result_shape=query.result_shape,
        result_cardinality=query.result_cardinality,
        result_binding=query.result_binding,
        dedupe=query.dedupe,
        rows=tuple(result_rows),
        conflicts=tuple(conflicts[key] for key in sorted(conflicts)),
        truncation=QueryTruncationV1(
            clipped_budgets=ordered_budgets,
            truncated_includes=_byte_sorted(tuple(truncated_includes)),
            candidate_result_count=candidate_result_count,
            returned_result_count=len(result_rows),
            evaluated_path_count=evaluated_path_count,
            retained_path_count=retained_path_count,
        ),
    )


_CLIPPED_BUDGET_ORDER: tuple[QueryClippedBudgetV1, ...] = (
    "include_max_items",
    "max_paths",
    "max_paths_per_result",
    "max_results",
)


__all__ = [
    "BUDGET_BELOW_DECLARED_DEPTH",
    "BUDGET_EXCEEDS_MAXIMUM",
    "CLAIM_CONFLICT",
    "COORDINATE_MISMATCH",
    "EVALUATION_TIME_NOT_ABSOLUTE",
    "PARAMETER_MISSING",
    "PARAMETER_TYPE_MISMATCH",
    "PARAMETER_UNDECLARED",
    "RESULT_CONFLICT",
    "SUBJECT_FIELD_UNAVAILABLE",
    "SUBJECT_UNRESOLVED",
    "TRAVERSAL_OBJECT_NOT_SUBJECT",
    "VALUE_TYPE_MISMATCH",
    "ClaimFactRowV1",
    "ClaimQueryError",
    "ClaimQueryFactsV1",
    "ClaimQueryResultV1",
    "DirectClaimFactIndex",
    "QueryClaimVisibilityV1",
    "QueryClippedBudgetV1",
    "QueryConflictV1",
    "QueryExecutionReceiptV1",
    "QueryIncludeItemV1",
    "QueryIncludeResultV1",
    "QueryParameterBindingV1",
    "QueryProjectedFieldV1",
    "QueryRefusalV1",
    "QueryResultRowV1",
    "QueryRowBindingV1",
    "QueryTruncationV1",
    "QueryValueStateV1",
    "VisibleClaimRow",
    "claim_query_result_digest",
    "evaluate_claim_query",
    "query_execution_receipt",
    "query_parameter_digest",
    "resolve_query_parameters",
]
