"""Direct evaluation of accepted QueryDefinitions over accepted Claim facts.

An evaluation is a pure function of the accepted definition digest, the caller
parameters, the accepted coordinate, and one explicit evaluation time. Nothing
here reads a wall clock, caches a result, or picks a winner among competing
accepted Claims: a one-cardinality read either surfaces its conflict set or
refuses with the exact conflicting statement digests.

Every Claim row becomes visible only through the existing evidence-relative
verdict machinery in :mod:`cruxible_client.contracts.claim_verdicts`; this module
never re-implements adjudication. Every budget that clips a result is named in
the result's truncation accounting, so a silently narrowed answer is
unrepresentable.

The evaluator reads accepted state only through the five primitives of
:class:`~cruxible_core.query.backends.ClaimQueryBackendV1`, and it
binds the coordinate the backend was materialized at rather than trusting the
one the caller names. Relation traversal is derived here from those primitives,
so a backend can never contribute an edge the Claim facts do not carry, and a
backend that reproduces the five in the same canonical order under the same
verdict computation reproduces these results byte for byte.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cmp_to_key
from typing import Any, cast

from cruxible_client.contracts.canonical import (
    Sha256Value,
    canonical_bytes,
    canonical_equal,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.canonical_forms import (
    CanonicalFormDidNotConverge,
    canonicalize_literal,
)
from cruxible_client.contracts.claim_verdicts import EvidenceRelativeClaimVerdictV1
from cruxible_client.contracts.claims import (
    ExactContentClaimObject,
    NoneClaimObject,
    SubjectClaimObject,
)
from cruxible_client.contracts.content_memo import request_derive
from cruxible_client.contracts.errors import CanonicalEncodingError, CruxibleError
from cruxible_client.contracts.query.definitions import (
    AcceptedQueryDefinition,
    QueryDefinition,
    QueryEvaluationPolicy,
)
from cruxible_client.contracts.query.grammar import (
    QueryArtifactsEntry,
    QueryBudgets,
    QueryClaimFieldRef,
    QueryClaimPresenceFilter,
    QueryClaimValueRef,
    QueryComparisonFilter,
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
    QueryParameterRef,
    QuerySubjectFieldRef,
    QueryTraversalDirection,
    QueryValueRef,
    QueryValueType,
    byte_sorted,
)
from cruxible_client.contracts.query.results import (
    ClaimQueryResult,
    QueryArtifactDefinition,
    QueryClaimVisibility,
    QueryClippedBudget,
    QueryConflict,
    QueryExecutionReceipt,
    QueryIncludeItem,
    QueryIncludeResult,
    QueryParameterBinding,
    QueryProjectedField,
    QueryProjectedFields,
    QueryRefusal,
    QueryResultRow,
    QueryRowBinding,
    QueryTruncation,
    QueryValueState,
    QueryVerdictExclusion,
    QueryVerdictVisibility,
)
from cruxible_client.contracts.query.values import QueryTypedValue, coerce_query_value
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.indexes.subject_aliases import (
    SubjectReferenceAmbiguous,
    SubjectReferenceResolver,
)
from cruxible_core.query.backends import (
    ClaimFactRowV1,
    ClaimQueryBackendFactoryV1,
    ClaimQueryBackendV1,
    ClaimQueryFactsV1,
    DirectClaimFactIndex,
    VisibleClaimRow,
)

PARAMETER_DIGEST_DOMAIN = "playbill-query-parameters-v1"
ATTEMPTED_PARAMETER_DIGEST_DOMAIN = "playbill-query-attempted-parameters-v1"

BUDGET_BELOW_DECLARED_DEPTH = "cruxible.query.budget_below_declared_depth"
BUDGET_EXCEEDS_MAXIMUM = "cruxible.query.budget_exceeds_maximum"
CLAIM_CONFLICT = "cruxible.query.claim_conflict"
COORDINATE_MISMATCH = "cruxible.query.coordinate_mismatch"
EVALUATION_TIME_NOT_ABSOLUTE = "cruxible.query.evaluation_time_not_absolute"
PARAMETER_MISSING = "cruxible.query.parameter_missing"
PARAMETER_TYPE_MISMATCH = "cruxible.query.parameter_type_mismatch"
PARAMETER_UNDECLARED = "cruxible.query.parameter_undeclared"
RESULT_CONFLICT = "cruxible.query.result_conflict"
SUBJECT_FIELD_UNAVAILABLE = "cruxible.query.subject_field_unavailable"
SUBJECT_UNRESOLVED = "cruxible.query.subject_unresolved"
TRAVERSAL_OBJECT_NOT_SUBJECT = "cruxible.query.traversal_object_not_subject"
VALUE_TYPE_MISMATCH = "cruxible.query.value_type_mismatch"
ALIAS_AMBIGUOUS = "cruxible.query.alias_ambiguous"


def query_parameter_digest(parameters: Sequence[QueryParameterBinding]) -> str:
    """Digest the exact resolved parameter binding of one evaluation."""

    return typed_digest(
        Sha256Value,
        PARAMETER_DIGEST_DOMAIN,
        {"parameters": [item.model_dump(mode="json") for item in parameters]},
    ).tagged


def query_attempted_parameter_digest(parameters: Mapping[str, object] | None) -> str:
    """Digest the attempted caller input of an evaluation refused before binding.

    A refusal raised before the declared parameters resolved has no resolved
    binding to digest, and must never borrow the digest of the empty binding:
    that spelling belongs to an evaluation that legitimately declared no
    parameters. The attempted input is committed under its own domain instead,
    so an unbound input can never collide with any resolved one. A value the
    canonical value set refuses is committed by the type it was rejected as,
    never by a canonical value it never had.
    """

    attempted: dict[str, object] = {}
    for name, value in dict(parameters or {}).items():
        try:
            attempted[name] = {"supplied": normalize_canonical(value)}
        except CanonicalEncodingError:
            attempted[name] = {"uncanonical": type(value).__name__}
    return typed_digest(
        Sha256Value,
        ATTEMPTED_PARAMETER_DIGEST_DOMAIN,
        {"parameters": attempted},
    ).tagged


def claim_query_result_digest(result: ClaimQueryResult) -> str:
    """Digest one complete result, ordering and truncation accounting included.

    `verdict_visibility` sits outside the preimage. It is advisory accounting of
    the Claims the evaluation policy declined to show, so it describes what was
    not read rather than what the result commits to; keeping it out means adding
    it re-pins no existing digest and no receipt, and a result replays
    byte-identically whether or not the advisory is present.
    """

    payload = result.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("verdict_visibility")
    return typed_digest(Sha256Value, result.tag, payload).tagged


def query_execution_receipt(result: ClaimQueryResult) -> QueryExecutionReceipt:
    """Model the replay receipt of one evaluation without journalling it."""

    return QueryExecutionReceipt(
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


def _compare(left: object, right: object, value_type: QueryValueType) -> int:
    first: Any = int(bool(left)) if value_type == "boolean" else left
    second: Any = int(bool(right)) if value_type == "boolean" else right
    if first == second:
        return 0
    return -1 if first < second else 1


def _render_time(value: datetime) -> str:
    utc = value.astimezone(UTC)
    timespec = "microseconds" if utc.microsecond else "seconds"
    return utc.isoformat(timespec=timespec).replace("+00:00", "Z")


class ClaimQueryError(CruxibleError):
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

    def __init__(self, refusal: QueryRefusal) -> None:
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
        QueryRefusal(
            code=code,
            message=message,
            statement_digests=byte_sorted(statement_digests),
            subject_identities=byte_sorted(subject_identities),
        )
    )


# -- derived relation traversal -------------------------------------------


def _is_none(claim: VisibleClaimRow) -> bool:
    return isinstance(claim.row.accepted.claim.statement.object, NoneClaimObject)


def relation_edges(
    backend: ClaimQueryBackendV1,
    artifact_path: str,
    predicate: str,
    direction: QueryTraversalDirection,
) -> tuple[tuple[VisibleClaimRow, str], ...]:
    """Return the visible relation-Claim edges leaving one bound Subject.

    Traversal is derived from the backend primitives rather than stored, so no
    backend can answer with an edge its Claim rows do not carry. An inverse
    Claim (a ``none`` object) is not an edge in either direction: it states the
    unit holds nothing, and the Subject its ``of`` names is not a target.
    """

    edges: list[tuple[VisibleClaimRow, str]] = []
    if direction == "forward":
        for claim in backend.claims_on(artifact_path, predicate):
            if _is_none(claim):
                continue
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
            (claim, claim.subject_path)
            for claim in backend.claims_to(artifact_path, predicate)
            if not _is_none(claim)
        )
    for claim, target in edges:
        if backend.subject(target) is None:
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
    edges: tuple[tuple[str, QueryClaimVisibility], ...] = ()
    reads: dict[str, QueryClaimVisibility] = field(default_factory=dict)
    conflicts: dict[bytes, QueryConflict] = field(default_factory=dict)
    includes: tuple[QueryIncludeResult, ...] = ()

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

    def record(self, visibility: QueryClaimVisibility) -> None:
        self.reads[visibility.claim_path] = visibility

    def record_conflict(self, conflict: QueryConflict) -> None:
        self.conflicts[canonical_bytes(conflict.model_dump(mode="json"))] = conflict

    def ordered_reads(self) -> tuple[QueryClaimVisibility, ...]:
        keys = sorted(self.reads, key=lambda item: item.encode("utf-8"))
        return tuple(self.reads[key] for key in keys)

    def ordered_conflicts(self) -> tuple[QueryConflict, ...]:
        return tuple(self.conflicts[key] for key in sorted(self.conflicts))


@dataclass(frozen=True)
class _Value:
    state: QueryValueState
    value: object = None
    #: A many-valued field's values, in canonical order; ``value`` is their list.
    many: tuple[object, ...] | None = None


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
        definition: QueryDefinition,
        *,
        backend: ClaimQueryBackendV1,
        parameters: Mapping[str, object],
        evaluation_time: datetime,
        canonical_forms: Mapping[str, tuple[str, ...]] | None = None,
        resolve_reference: SubjectReferenceResolver | None = None,
    ) -> None:
        self._definition = definition
        self._backend = backend
        self._parameters = parameters
        self._evaluation_time = evaluation_time
        self._canonical_forms = canonical_forms or {}
        self._resolve_reference = resolve_reference

    def _form(self, ref: QueryValueRef) -> tuple[str, ...]:
        """The canonical form of the predicate a Claim-value reference reads, if any."""

        if isinstance(ref, QueryClaimValueRef):
            return tuple(self._canonical_forms.get(ref.predicate, ()))
        return ()

    def _operand_form(self, ref: QueryValueRef, *others: QueryValueRef) -> tuple[str, ...]:
        """The form an operand compared with stored Claim values is canonicalized by.

        A Claim value is already canonical (the Claim law keeps it so); a literal,
        a parameter or a Subject field compared with one takes that predicate's
        form first, so two spellings equal under it compare equal.
        """

        if isinstance(ref, QueryClaimValueRef):
            return ()
        for other in others:
            form = self._form(other)
            if form:
                return form
        return ()

    @staticmethod
    def _operand_vocabulary(ref: QueryValueRef, *others: QueryValueRef) -> tuple[str, ...]:
        """The closed value set a caller parameter compared with a Claim field must be in.

        A literal is checked when the definition is declared; a parameter only
        has its value here, and a role or verdict outside the field's set is a
        type mismatch, never a filter that silently matches nothing.
        """

        if not isinstance(ref, QueryParameterRef):
            return ()
        for other in others:
            if isinstance(other, QueryClaimFieldRef):
                return other.vocabulary
        return ()

    def _claim_value(self, row: _Row, binding: str, predicate: str) -> _Value:
        subject_path = row.bindings.get(binding)
        if subject_path is None:
            return _ABSENT
        claims = self._backend.claims_on(subject_path, predicate)
        if not claims:
            return _ABSENT
        distinct: dict[bytes, object] = {}
        for claim in claims:
            row.record(claim.visibility)
            item = claim.row.accepted.claim.statement.object
            if isinstance(item, SubjectClaimObject):
                target = self._backend.subject(item.address.artifact_path)
                if target is None:
                    raise _refuse(
                        SUBJECT_UNRESOLVED,
                        "A read Claim names a Subject absent at the accepted coordinate.",
                        statement_digests=(claim.visibility.statement_digest,),
                    )
                value: object = target.shell.identity.qualified
            else:
                value = item.model_dump(mode="json").get("value")
            # An exact-content object has no literal value to project, but two
            # of them are distinct by digest and span, so they compete exactly
            # as two literals do under the query's conflict_behavior.
            identity = (
                item.model_dump(mode="json") if isinstance(item, ExactContentClaimObject) else value
            )
            distinct[canonical_bytes(identity)] = value
        if any(claim.row.cardinality == "many" for claim in claims):
            # A many-valued field's Claims are its values: the field projects
            # as their list, in canonical order, and never conflicts.
            values = tuple(distinct[key] for key in sorted(distinct))
            return _Value(state="present", value=list(values), many=values)
        if len(distinct) == 1:
            return _Value(state="present", value=next(iter(distinct.values())))
        subject = self._backend.subject(subject_path)
        assert subject is not None
        digests = byte_sorted(tuple(claim.visibility.statement_digest for claim in claims))
        if self._definition.evaluation_policy.conflict_behavior == "refuse_on_conflict":
            raise _refuse(
                CLAIM_CONFLICT,
                "Competing accepted Claims answer a one-cardinality read.",
                statement_digests=digests,
                subject_identities=(subject.shell.identity.qualified,),
            )
        row.record_conflict(
            QueryConflict(
                kind="claim_object",
                binding=binding,
                predicate=predicate,
                subject_identity=subject.shell.identity.qualified,
                statement_digests=digests,
            )
        )
        return _CONFLICT

    def _claim_field(self, row: _Row, ref: QueryClaimFieldRef) -> _Value:
        """One field of the Claims a ``claim_value`` read of the predicate reads.

        The field comes from what the evaluation already holds for each read
        Claim: the role from its statement, the verdict from the visibility the
        shared verdict path admitted it under. Nothing else is read, so the
        field costs what the value read costs. A conflicted value is a
        conflicted field; Claims that agree on a one-cardinality value but not
        on the field state no single field value either.
        """

        subject_path = row.bindings.get(ref.binding)
        if subject_path is None:
            return _ABSENT
        claims = self._backend.claims_on(subject_path, ref.predicate)
        if not claims:
            return _ABSENT
        if self._claim_value(row, ref.binding, ref.predicate).state == "conflict":
            return _CONFLICT
        values = byte_sorted(
            tuple(
                claim.row.accepted.claim.statement.role
                if ref.field == "role"
                else claim.visibility.verdict
                for claim in claims
            )
        )
        if any(claim.row.cardinality == "many" for claim in claims):
            return _Value(state="present", value=list(values), many=values)
        if len(values) == 1:
            return _Value(state="present", value=values[0])
        if self._definition.evaluation_policy.conflict_behavior == "refuse_on_conflict":
            subject = self._backend.subject(subject_path)
            assert subject is not None
            raise _refuse(
                CLAIM_CONFLICT,
                f"Accepted Claims of one value differ in their {ref.field}.",
                statement_digests=tuple(claim.visibility.statement_digest for claim in claims),
                subject_identities=(subject.shell.identity.qualified,),
            )
        return _CONFLICT

    def resolve(self, row: _Row, ref: QueryValueRef) -> _Value:
        """Resolve one declared value reference against a bound row."""

        if isinstance(ref, QueryLiteralRef):
            return _Value(state="present", value=ref.value)
        if isinstance(ref, QueryParameterRef):
            if ref.parameter not in self._parameters:
                return _ABSENT
            return _Value(state="present", value=self._parameters[ref.parameter])
        if isinstance(ref, QueryEvaluationTimeRef):
            return _Value(state="present", value=_render_time(self._evaluation_time))
        if isinstance(ref, QuerySubjectFieldRef):
            subject_path = row.bindings.get(ref.binding)
            if subject_path is None:
                if ref.binding in row.bindings:
                    return _ABSENT
                raise _refuse(
                    SUBJECT_FIELD_UNAVAILABLE,
                    "A Subject field was read through a binding that carries no Subject.",
                )
            subject = self._backend.subject(subject_path)
            assert subject is not None
            return _Value(
                state="present",
                value=(
                    subject.shell.subject_id
                    if ref.field == "subject_id"
                    else subject.shell.subject_kind
                ),
            )
        if isinstance(ref, QueryClaimFieldRef):
            return self._claim_field(row, ref)
        return self._claim_value(row, ref.binding, ref.predicate)

    def typed(
        self,
        row: _Row,
        ref: QueryValueRef,
        value_type: QueryValueType,
        *,
        form: tuple[str, ...] = (),
        vocabulary: tuple[str, ...] = (),
    ) -> QueryTypedValue | None:
        """Return the one declared-type comparable, or None when there is no one value.

        Ordering reads one value per row: a many-valued field holding exactly one
        orders as it; several have no single value to order by. ``form`` is the
        canonical form of the stored values this operand is compared with; the
        operand is canonicalized by it first. ``vocabulary`` is the closed value
        set of a Claim field it is compared with.
        """

        resolved = self.resolve(row, ref)
        if resolved.state != "present":
            return None
        value = resolved.value
        if resolved.many is not None:
            if len(resolved.many) != 1:
                return None
            value = resolved.many[0]
        return self._coerce(ref, value, value_type, form=form, vocabulary=vocabulary)

    def typed_values(
        self,
        row: _Row,
        ref: QueryValueRef,
        value_type: QueryValueType,
        *,
        form: tuple[str, ...] = (),
        vocabulary: tuple[str, ...] = (),
    ) -> tuple[QueryTypedValue, ...] | None:
        """Every declared-type comparable an operand holds, or None when it is absent.

        A filter compares each value of a many-valued field: it holds when any
        value satisfies it (``ne`` and ``not in``: when no value is excluded),
        as the compact ``--where`` reads the same field.
        """

        resolved = self.resolve(row, ref)
        if resolved.state != "present":
            return None
        values = resolved.many if resolved.many is not None else (resolved.value,)
        if not values:
            return None
        return tuple(
            self._coerce(ref, value, value_type, form=form, vocabulary=vocabulary)
            for value in values
        )

    def _coerce(
        self,
        ref: QueryValueRef,
        value: object,
        value_type: QueryValueType,
        *,
        form: tuple[str, ...],
        vocabulary: tuple[str, ...],
    ) -> QueryTypedValue:
        if form:
            try:
                value = canonicalize_literal(value, form)
            except CanonicalFormDidNotConverge as exc:
                raise _refuse(VALUE_TYPE_MISMATCH, str(exc)) from exc
        if (
            value_type == "subject_reference"
            and self._resolve_reference is not None
            and isinstance(ref, QueryLiteralRef | QueryParameterRef)
            and isinstance(value, str)
        ):
            # A Subject reference the definition or caller wrote resolves
            # exactly as at every other entry point: the exact Subject, else
            # one alias of its kind. The stored definition is never rewritten.
            try:
                value = self._resolve_reference(value)
            except SubjectReferenceAmbiguous as exc:
                raise _refuse(ALIAS_AMBIGUOUS, str(exc)) from exc
        coerced = coerce_query_value(value, value_type)
        if not coerced.ok:
            raise _refuse(
                VALUE_TYPE_MISMATCH,
                f"A query value is not of its declared {value_type} type.",
            )
        if vocabulary and value not in vocabulary:
            raise _refuse(
                VALUE_TYPE_MISMATCH,
                f"A query value compared with a Claim field is not one of {', '.join(vocabulary)}.",
            )
        return coerced

    def matches(self, row: _Row, filter_: QueryFilter) -> bool:
        """Evaluate one declared filter; an absent or conflicted value never matches.

        A many-valued operand matches when any of its values does; ``ne``
        matches when none of them equals the other side.
        """

        if isinstance(filter_, QueryConjunctionFilter):
            return all(self.matches(row, item) for item in filter_.filters)
        if isinstance(filter_, QueryDisjunctionFilter):
            return any(self.matches(row, item) for item in filter_.filters)
        if isinstance(filter_, QueryNegationFilter):
            return not self.matches(row, filter_.operand)
        if isinstance(filter_, QueryClaimPresenceFilter):
            subject_path = row.bindings.get(filter_.binding)
            present = False
            if subject_path is not None:
                claims = self._backend.claims_on(subject_path, filter_.predicate)
                for claim in claims:
                    row.record(claim.visibility)
                present = bool(claims)
            return present != filter_.negated
        if isinstance(filter_, QueryComparisonFilter):
            left = self.typed_values(
                row,
                filter_.left,
                filter_.value_type,
                form=self._operand_form(filter_.left, filter_.right),
                vocabulary=self._operand_vocabulary(filter_.left, filter_.right),
            )
            right = self.typed_values(
                row,
                filter_.right,
                filter_.value_type,
                form=self._operand_form(filter_.right, filter_.left),
                vocabulary=self._operand_vocabulary(filter_.right, filter_.left),
            )
            if left is None or right is None:
                return False
            orders = (
                _compare(one.value, other.value, filter_.value_type)
                for one in left
                for other in right
            )
            if filter_.operator == "ne":
                return not any(order == 0 for order in orders)
            return any(_OPERATORS[filter_.operator](order) for order in orders)
        return self._membership(row, filter_)

    def _membership(self, row: _Row, filter_: QueryMembershipFilter) -> bool:
        left = self.typed_values(
            row,
            filter_.left,
            filter_.value_type,
            form=self._operand_form(filter_.left, *filter_.values),
            vocabulary=self._operand_vocabulary(filter_.left, *filter_.values),
        )
        if left is None:
            return filter_.negated
        found = False
        for candidate in filter_.values:
            right = self.typed_values(
                row,
                candidate,
                filter_.value_type,
                form=self._operand_form(candidate, filter_.left),
                vocabulary=self._operand_vocabulary(candidate, filter_.left),
            )
            if right is not None and any(
                _compare(one.value, other.value, filter_.value_type) == 0
                for one in left
                for other in right
            ):
                found = True
                break
        return found != filter_.negated

    def order(
        self,
        rows: Sequence[_Row],
        orderings: tuple[QueryOrdering, ...],
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
    definition: QueryDefinition,
    parameters: Mapping[str, object] | None,
) -> tuple[QueryParameterBinding, ...]:
    """Bind caller parameters to their declarations or refuse fail-closed."""

    supplied = dict(parameters or {})
    declared = {item.name: item for item in definition.parameters}
    unknown = byte_sorted(tuple(set(supplied) - set(declared)))
    if unknown:
        raise _refuse(
            PARAMETER_UNDECLARED,
            f"The query does not declare the parameter {unknown[0]!r}.",
        )
    bindings: list[QueryParameterBinding] = []
    for name in sorted(declared, key=lambda item: item.encode("utf-8")):
        declaration = declared[name]
        if name in supplied:
            value = supplied[name]
        elif declaration.required:
            raise _refuse(PARAMETER_MISSING, f"The query requires the parameter {name!r}.")
        else:
            value = declaration.default
        if (
            value is not None
            and not coerce_query_value(normalize_canonical(value), declaration.value_type).ok
        ):
            raise _refuse(
                PARAMETER_TYPE_MISMATCH,
                f"Parameter {name!r} is not of its declared {declaration.value_type} type.",
            )
        bindings.append(
            QueryParameterBinding(name=name, value_type=declaration.value_type, value=value)
        )
    return tuple(bindings)


def _effective_budgets(
    definition: QueryDefinition,
    budgets: QueryBudgets | None,
) -> QueryBudgets:
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


def _expiry(definition: QueryDefinition, evaluation_time: datetime) -> datetime | None:
    expiry = definition.evaluation_policy.result_expiry
    if expiry is None:
        return None
    return evaluation_time + timedelta(microseconds=expiry.microseconds)


def _binding_row(
    backend: ClaimQueryBackendV1,
    binding: str,
    path: str | None,
) -> QueryRowBinding:
    if path is None:
        return QueryRowBinding(binding=binding)
    subject = backend.subject(path)
    assert subject is not None
    # A query can use the same subject binding in its result and truncation
    # witness. These are the complete scalar inputs of the served contract.
    identity = subject.shell.identity.qualified
    kind, identifier = subject.shell.subject_kind, subject.shell.subject_id
    return request_derive(
        ("query.row_binding", binding, identity, kind, identifier, path),
        lambda: QueryRowBinding(
            binding=binding,
            subject_identity=identity,
            subject_kind=kind,
            subject_id=identifier,
            subject_path=path,
        ),
    )


def _dedupe_key(row: _Row, definition: QueryDefinition) -> bytes | None:
    if definition.dedupe == "none":
        return None
    if definition.dedupe == "subject":
        return canonical_bytes(row.bindings.get(definition.result_binding))
    return row.identity()


def evaluate_claim_query(
    definition: AcceptedQueryDefinition,
    *,
    facts: ClaimQueryFactsV1,
    coordinate: AcceptedProjectionCoordinate,
    evaluation_time: datetime,
    parameters: Mapping[str, object] | None = None,
    budgets: QueryBudgets | None = None,
    backend_factory: ClaimQueryBackendFactoryV1 = DirectClaimFactIndex,
    resolve_reference: SubjectReferenceResolver | None = None,
    entry_paths: tuple[str, ...] | None = None,
    entry_candidate_count: int | None = None,
    canonical_forms: Mapping[str, tuple[str, ...]] | None = None,
) -> ClaimQueryResult:
    """Evaluate one accepted QueryDefinition against accepted Claim facts.

    The result is a pure function of the definition digest, the resolved
    parameters, the accepted coordinate, and the explicit evaluation time. The
    backend is materialized from those accepted facts inside the evaluation, so
    a caller can choose how state is stored but never what it says.
    ``resolve_reference`` resolves the Subject references a literal or a
    parameter compares, at the same coordinate (``indexes.subject_aliases``).
    """

    query = definition.query
    if isinstance(query.entry, QueryArtifactsEntry):
        raise ValueError("artifact definition queries require the indexed artifact reader")
    if evaluation_time.tzinfo is None or evaluation_time.utcoffset() is None:
        raise ClaimQueryError(
            EVALUATION_TIME_NOT_ABSOLUTE,
            "a query evaluation time must be an explicit absolute instant",
        )
    bindings: tuple[QueryParameterBinding, ...] | None = None
    try:
        if not canonical_equal(
            coordinate.model_dump(mode="json"), facts.coordinate.model_dump(mode="json")
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
            backend_factory=backend_factory,
            resolve_reference=resolve_reference,
            entry_paths=entry_paths,
            entry_candidate_count=entry_candidate_count,
            canonical_forms=canonical_forms,
        )
    except _RefusalSignal as signal:
        return ClaimQueryResult(
            verdict="refused",
            definition_path=definition.path,
            definition_digest=definition.artifact_digest,
            parameter_digest=(
                query_attempted_parameter_digest(parameters)
                if bindings is None
                else query_parameter_digest(bindings)
            ),
            coordinate=coordinate,
            evaluated_at=evaluation_time,
            expires_at=_expiry(query, evaluation_time),
            budgets=budgets or query.default_budgets,
            result_shape=query.result_shape,
            result_cardinality=query.result_cardinality,
            result_binding=query.result_binding,
            dedupe=query.dedupe,
            truncation=QueryTruncation(),
            refusal=signal.refusal,
        )


def evaluate_artifact_query(
    definition: AcceptedQueryDefinition,
    *,
    read: Callable[[QueryArtifactsEntry, int], tuple[int, tuple[QueryArtifactDefinition, ...]]],
    coordinate: AcceptedProjectionCoordinate,
    evaluation_time: datetime,
    parameters: Mapping[str, object] | None = None,
    budgets: QueryBudgets | None = None,
) -> ClaimQueryResult:
    """Evaluate definition membership using the coordinate-bound indexed reader."""
    query = definition.query
    assert isinstance(query.entry, QueryArtifactsEntry)
    if evaluation_time.tzinfo is None or evaluation_time.utcoffset() is None:
        raise ClaimQueryError(EVALUATION_TIME_NOT_ABSOLUTE, "query time must be absolute")
    bindings = None
    refusal = None
    rows: tuple[QueryResultRow, ...] = ()
    truncation = QueryTruncation()
    effective = budgets or query.default_budgets
    try:
        bindings = resolve_query_parameters(query, parameters)
        effective = _effective_budgets(query, budgets)
        count, definitions = read(query.entry, effective.max_results)
        rows = tuple(
            QueryResultRow(tag="playbill-query-result-row-v2", bindings=(), artifact=item)
            for item in definitions
        )
        truncation = QueryTruncation(
            clipped_budgets=("max_results",) if count > len(rows) else (),
            candidate_result_count=count,
            returned_result_count=len(rows),
        )
    except _RefusalSignal as signal:
        refusal = signal.refusal
    return ClaimQueryResult(
        tag="playbill-query-result-v2",
        verdict="refused" if refusal else "completed",
        definition_path=definition.path,
        definition_digest=definition.artifact_digest,
        parameters=bindings or (),
        parameter_digest=query_attempted_parameter_digest(parameters)
        if bindings is None
        else query_parameter_digest(bindings),
        coordinate=coordinate,
        evaluated_at=evaluation_time,
        expires_at=_expiry(query, evaluation_time),
        budgets=effective,
        result_shape=query.result_shape,
        result_cardinality=query.result_cardinality,
        result_binding=query.result_binding,
        dedupe=query.dedupe,
        rows=rows,
        truncation=truncation,
        refusal=refusal,
    )


def _entry_rows(
    query: QueryDefinition,
    backend: ClaimQueryBackendV1,
    parameters: Mapping[str, object],
) -> list[_Row]:
    assert isinstance(query.entry, QueryEntry)
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
        for path in backend.subjects(query.entry.subject_kinds, subject_id=subject_id)
    ]


def _traverse(
    query: QueryDefinition,
    evaluator: _Evaluator,
    backend: ClaimQueryBackendV1,
    rows: list[_Row],
    budgets: QueryBudgets,
    clipped: set[QueryClippedBudget],
) -> list[_Row]:
    for step in query.traversal:
        produced: list[_Row] = []
        for row in rows:
            source = row.bindings.get(step.from_binding)
            matched = False
            if source is not None:
                for claim, target in relation_edges(
                    backend, source, step.predicate, step.direction
                ):
                    subject = backend.subject(target)
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
    backend: ClaimQueryBackendV1,
    row: _Row,
    include: QueryInclude,
) -> QueryIncludeResult:
    source = row.bindings.get(include.from_binding)
    pairs: list[tuple[VisibleClaimRow, str | None]] = []
    if source is not None:
        if include.direction == "forward":
            pairs = [
                (claim, claim.object_subject_path)
                for claim in backend.claims_on(source, include.predicate)
            ]
        else:
            pairs = [
                (claim, claim.subject_path)
                for claim in backend.claims_to(source, include.predicate)
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
    return QueryIncludeResult(
        name=include.name,
        items=tuple(
            QueryIncludeItem(
                claim_object=candidates[position][0].row.accepted.claim.statement.object.model_dump(
                    mode="json"
                ),
                subject_identity=(
                    None
                    if candidates[position][1] is None
                    else _binding_row(
                        backend, include.binding, candidates[position][1]
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
    backend: ClaimQueryBackendV1,
    query: QueryDefinition,
    rows: Sequence[_Row],
) -> tuple[str, ...]:
    return byte_sorted(
        tuple(
            _binding_row(
                backend, query.result_binding, row.bindings.get(query.result_binding)
            ).subject_identity
            or ""
            for row in rows
        )
    )


def _evaluate(
    definition: AcceptedQueryDefinition,
    *,
    facts: ClaimQueryFactsV1,
    coordinate: AcceptedProjectionCoordinate,
    evaluation_time: datetime,
    bindings: tuple[QueryParameterBinding, ...],
    budgets: QueryBudgets,
    backend_factory: ClaimQueryBackendFactoryV1,
    resolve_reference: SubjectReferenceResolver | None = None,
    entry_paths: tuple[str, ...] | None = None,
    entry_candidate_count: int | None = None,
    canonical_forms: Mapping[str, tuple[str, ...]] | None = None,
) -> ClaimQueryResult:
    query = definition.query
    backend = backend_factory(facts, definition=query, evaluation_time=evaluation_time)
    if not canonical_equal(
        backend.coordinate.model_dump(mode="json"), coordinate.model_dump(mode="json")
    ):
        raise _refuse(
            COORDINATE_MISMATCH,
            "The backend was materialized at a different accepted coordinate.",
        )
    parameters = {item.name: item.value for item in bindings if item.value is not None}
    evaluator = _Evaluator(
        query,
        backend=backend,
        parameters=parameters,
        evaluation_time=evaluation_time,
        canonical_forms=canonical_forms
        if canonical_forms is not None
        else {
            row.accepted.claim.statement.predicate: row.canonical_form
            for row in facts.claims
            if row.canonical_form
        },
        resolve_reference=resolve_reference,
    )
    clipped: set[QueryClippedBudget] = set()

    rows = _traverse(
        query,
        evaluator,
        backend,
        _entry_rows(query, backend, parameters)
        if entry_paths is None
        else [_Row(bindings={query.entry.binding: path}) for path in entry_paths],
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
        row.includes = tuple(_hydrate(evaluator, backend, row, item) for item in query.includes)
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
    candidate_result_count = len(rows) if entry_candidate_count is None else entry_candidate_count
    conflicts: dict[bytes, QueryConflict] = {}
    if query.result_cardinality == "one" and candidate_result_count > 1:
        identities = _result_subject_identities(backend, query, rows)
        if query.evaluation_policy.conflict_behavior == "refuse_on_conflict":
            raise _refuse(
                RESULT_CONFLICT,
                "A one-cardinality query resolves to more than one accepted row.",
                subject_identities=identities,
            )
        conflict = QueryConflict(kind="result_cardinality", subject_identities=identities)
        conflicts[canonical_bytes(conflict.model_dump(mode="json"))] = conflict
    if candidate_result_count > budgets.max_results:
        rows = rows[: budgets.max_results]
        clipped.add("max_results")

    result_rows: list[QueryResultRow] = []
    for row in rows:
        fields: list[QueryProjectedField] = []
        for declared in () if query.projection is None else query.projection.fields:
            resolved = evaluator.resolve(row, declared.value)
            fields.append(
                QueryProjectedField(
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
            QueryResultRow(
                bindings=tuple(
                    _binding_row(backend, binding, row.bindings.get(binding))
                    for binding in query.row_bindings
                ),
                result_subject_identity=_binding_row(
                    backend, query.result_binding, row.bindings.get(query.result_binding)
                ).subject_identity,
                path=tuple(item for _, item in row.edges),
                relation_claim=relation if query.result_shape == "relation_claim" else None,
                fields=QueryProjectedFields(fields),
                read_claims=row.ordered_reads(),
                includes=row.includes,
                conflicts=row.ordered_conflicts(),
            )
        )

    ordered_budgets: tuple[QueryClippedBudget, ...] = tuple(
        item for item in _CLIPPED_BUDGET_ORDER if item in clipped
    )
    return ClaimQueryResult(
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
        truncation=QueryTruncation(
            clipped_budgets=ordered_budgets,
            truncated_includes=byte_sorted(tuple(truncated_includes)),
            candidate_result_count=candidate_result_count,
            returned_result_count=len(result_rows),
            evaluated_path_count=evaluated_path_count,
            retained_path_count=retained_path_count,
        ),
        verdict_visibility=_verdict_visibility(backend, policy=query.evaluation_policy),
    )


def _verdict_visibility(
    backend: ClaimQueryBackendV1,
    *,
    policy: QueryEvaluationPolicy,
) -> QueryVerdictVisibility | None:
    """Account for the Claims this evaluation's policy declined to show.

    None when the policy hid nothing, so a definition that shows everything it
    reads carries no advisory at all.
    """

    excluded = tuple(backend.excluded_by_verdict)
    if not excluded:
        return None
    return QueryVerdictVisibility(
        excluded_claim_count=sum(count for _verdict, count in excluded),
        excluded_by_verdict=tuple(
            QueryVerdictExclusion(
                verdict=cast(EvidenceRelativeClaimVerdictV1, verdict),
                excluded_claim_count=count,
            )
            for verdict, count in excluded
        ),
        visible_verdicts=policy.visible_verdicts,
    )


_CLIPPED_BUDGET_ORDER: tuple[QueryClippedBudget, ...] = (
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
    "ClaimQueryBackendFactoryV1",
    "ClaimQueryBackendV1",
    "ClaimQueryError",
    "ClaimQueryFactsV1",
    "DirectClaimFactIndex",
    "VisibleClaimRow",
    "claim_query_result_digest",
    "evaluate_claim_query",
    "relation_edges",
    "query_attempted_parameter_digest",
    "query_execution_receipt",
    "query_parameter_digest",
    "resolve_query_parameters",
]
