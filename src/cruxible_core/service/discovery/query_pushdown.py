"""Conservative entry selection before accepted Claim facts are assembled.

Selections are supersets: every Claim of each selected Subject is retained,
including competitors. Unsupported filters keep the original full evaluation.
Loops here grow with the declaration, supplied values, or matching index rows;
only an unfiltered kind entry enumerates its kind (the requested result set).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, get_args

from cruxible_client.contracts.canonical_forms import (
    CanonicalFormDidNotConverge,
    canonicalize_literal,
)
from cruxible_client.contracts.claim_types import (
    claim_type_path,
    effective_canonical_form,
    parse_claim_type,
)
from cruxible_client.contracts.claim_verdicts import EvidenceRelativeClaimVerdictV1
from cruxible_client.contracts.query.definitions import QueryDefinition
from cruxible_client.contracts.query.grammar import (
    QueryArtifactsEntry,
    QueryBudgets,
    QueryClaimValueRef,
    QueryComparisonFilter,
    QueryConjunctionFilter,
    QueryDisjunctionFilter,
    QueryFilter,
    QueryLiteralRef,
    QueryMembershipFilter,
    QueryParameterRef,
    QuerySubjectFieldRef,
    QueryValueRef,
)
from cruxible_core.indexes.sql_batch import select_in
from cruxible_core.query.engine import _RefusalSignal, resolve_query_parameters


@dataclass(frozen=True)
class QueryFactsSelection:
    paths: tuple[str, ...]
    # Present only when LIMIT was pushed past a plain entry's result budget.
    candidate_count: int | None = None


def select_query_facts(
    connection: Any,
    query: QueryDefinition,
    *,
    parameters: Mapping[str, object] | None,
    budgets: QueryBudgets | None,
    source: Any,
    live_subjects_only: bool = False,
) -> QueryFactsSelection | None:
    """Select only proven entry candidates, or return None for the full-load oracle.

    Traversals/includes retain full facts: pruning an intermediate path before
    max_paths, a reverse edge, or an optional binding requires a separate proof.
    Negation/absence and unsupported compositions likewise keep the full load.
    """
    entry = query.entry
    if isinstance(entry, QueryArtifactsEntry) or query.traversal or query.includes:
        return None
    try:
        bound = {
            item.name: item.value
            for item in resolve_query_parameters(query, parameters)
            if item.value is not None
        }
    except _RefusalSignal:
        # Parameter refusal belongs to the evaluator, with its original bytes.
        return None

    def constant(ref: QueryValueRef) -> object:
        if isinstance(ref, QueryLiteralRef):
            return ref.value
        if isinstance(ref, QueryParameterRef):
            return bound.get(ref.parameter)
        return None

    def paths_of_kinds(kinds: tuple[str, ...]) -> set[str]:
        return {
            str(row[0])
            for kind in kinds
            for row in connection.execute(
                "SELECT path FROM subjects WHERE subject_kind=? ORDER BY path", (kind,)
            )
        }

    def leaf(filter_: QueryFilter) -> set[str] | None:
        if isinstance(filter_, QueryConjunctionFilter | QueryDisjunctionFilter):
            selections = [leaf(item) for item in filter_.filters]
            if any(item is None for item in selections):
                return None
            complete = [item for item in selections if item is not None]
            return (
                set.intersection(*complete)
                if isinstance(filter_, QueryConjunctionFilter)
                else set.union(*complete)
            )
        if isinstance(filter_, QueryComparisonFilter):
            ref, other = filter_.left, filter_.right
            if isinstance(ref, QueryLiteralRef | QueryParameterRef):
                ref, other = other, ref
            if not isinstance(other, QueryLiteralRef | QueryParameterRef):
                return None
            values = [constant(other)]
            operator: str = filter_.operator
            if ref is filter_.right:
                operator = {"gt": "lt", "gte": "lte", "lt": "gt", "lte": "gte"}.get(
                    operator, operator
                )
        elif isinstance(filter_, QueryMembershipFilter) and not filter_.negated:
            ref = filter_.left
            if not all(
                isinstance(item, QueryLiteralRef | QueryParameterRef) for item in filter_.values
            ):
                return None
            values = [constant(item) for item in filter_.values]
            operator = "eq"
        else:
            return None
        if (
            not isinstance(ref, QuerySubjectFieldRef | QueryClaimValueRef)
            or ref.binding != entry.binding
        ):
            return None
        if isinstance(ref, QuerySubjectFieldRef):
            if filter_.value_type != "string" or not all(
                isinstance(value, str) for value in values
            ):
                return None
            if ref.field == "subject_kind":

                def matches(kind: str) -> bool:
                    a = kind.encode("utf-8")
                    for value in values:
                        assert isinstance(value, str)
                        b = value.encode("utf-8")
                        if {
                            "eq": a == b,
                            "gt": a > b,
                            "gte": a >= b,
                            "lt": a < b,
                            "lte": a <= b,
                        }.get(operator, False):
                            return True
                    return False

                if operator == "ne":
                    return None
                return paths_of_kinds(tuple(kind for kind in entry.subject_kinds if matches(kind)))
            if operator != "eq":
                return None
            return {
                str(row[0])
                for kind in entry.subject_kinds
                for row in select_in(
                    connection,
                    "SELECT path FROM subjects WHERE subject_kind=? AND subject_id IN ({})",
                    values,
                    prefix=(kind,),
                )
            }
        if not isinstance(ref, QueryClaimValueRef) or operator != "eq":
            return None
        # Eliminated subjects must not contribute a refusal or hidden-verdict
        # advisory. Scalar schemas prove comparisons cannot refuse for type;
        # surface_conflicts preserves competing values without a global refusal.
        policy = query.evaluation_policy
        if policy.conflict_behavior != "surface_conflicts" or set(policy.visible_verdicts) != set(
            get_args(EvidenceRelativeClaimVerdictV1)
        ):
            return None
        type_path = claim_type_path(ref.predicate)
        claim_type = parse_claim_type(source[type_path], path=type_path)
        scalar = filter_.value_type
        schema = claim_type.literal_schema or {}
        if (
            claim_type.object_kind != "literal"
            or scalar not in {"string", "integer", "boolean"}
            or schema.get("type") != scalar
        ):
            return None
        try:
            values = [
                canonicalize_literal(value, effective_canonical_form(claim_type))
                for value in values
            ]
        except CanonicalFormDidNotConverge:
            return None
        expected = {"string": str, "integer": int, "boolean": bool}[scalar]
        if not all(type(value) is expected for value in values):
            return None
        column = {
            "string": "literal_text",
            "integer": "literal_integer_text",
            "boolean": "literal_boolean",
        }[scalar]
        indexed = [
            str(value)
            if scalar == "integer"
            else int(bool(value))
            if scalar == "boolean"
            else value
            for value in values
        ]
        return {
            str(row[0])
            for row in select_in(
                connection,
                "SELECT subject_path FROM claims WHERE predicate=? AND literal_type=? "
                f"AND {column} IN ({{}}) AND lifecycle='live'",
                indexed,
                prefix=(ref.predicate, scalar),
            )
            if str(row[0]).split("/", 2)[1] in entry.subject_kinds
        }

    selected: set[str] | None = None
    if entry.subject_id is not None:
        identifier = bound.get(entry.subject_id.parameter)
        if not isinstance(identifier, str):
            return None
        selected = {
            str(row[0])
            for kind in entry.subject_kinds
            for row in connection.execute(
                "SELECT path FROM subjects WHERE subject_kind=? AND subject_id=?",
                (kind, identifier),
            )
        }
    if query.where is not None:
        narrowed = leaf(query.where)
        if narrowed is not None:
            selected = narrowed if selected is None else selected & narrowed
    effective = budgets or query.default_budgets
    plain_budget = (
        query.where is None
        and not query.orderings
        and query.result_cardinality == "many"
        and effective.max_paths is None
        and effective.max_paths_per_result is None
    )
    if selected is not None:
        paths = tuple(sorted(selected))
        if live_subjects_only:
            paths = tuple(
                str(row[0])
                for row in select_in(
                    connection,
                    "SELECT path FROM subjects WHERE lifecycle='live' AND path IN ({})",
                    paths,
                )
            )
            paths = tuple(sorted(paths))
        return QueryFactsSelection(
            paths[: effective.max_results] if plain_budget else paths,
            len(paths) if plain_budget else None,
        )
    if not plain_budget:
        return None
    lifecycle = " AND lifecycle='live'" if live_subjects_only else ""
    count = sum(
        int(row[0])
        for kind in entry.subject_kinds
        for row in connection.execute(
            "SELECT amount FROM query_subject_counts WHERE subject_kind=?" + lifecycle, (kind,)
        )
    )
    budget_paths = sorted(
        str(row[0])
        for kind in entry.subject_kinds
        for row in connection.execute(
            "SELECT path FROM subjects WHERE subject_kind=?" + lifecycle + " ORDER BY path LIMIT ?",
            (kind, effective.max_results),
        )
    )[: effective.max_results]
    return QueryFactsSelection(tuple(budget_paths), count)
