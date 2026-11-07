"""Conservative entry selection and reach before accepted Claim facts are assembled.

Selections are supersets: every Claim of each selected Subject is retained,
including competitors. Unsupported filters keep the original full evaluation.
Loops here grow with the declaration, supplied values, or admitted index rows.
Existing entry restrictions flow through each filter; conjunctions start with
identity seeks, then value seeks, then kind selections.

A conjunction seeks with the parts an index can answer and leaves the rest to
the evaluator (superset-safe): that is admitted only for left-out parts on a
strict allowlist of shapes proven never to refuse on any row (``allowed``), so a
Subject the seek eliminated changes nothing the result commits to. (The verdict
advisory, which sits outside the result digest, then counts hidden Claims among
the seek's candidates: a Subject the seek eliminated cannot match whatever its
Claims' verdicts.)

A query with hops or includes reads only what it can reach (``query_reach``):
from its entry rows, each hop expands through the forward (subject) or reverse
(object) Claim index of the Subjects already bound, and each binding's read
predicates load only for the Subjects that binding can hold. Every (Subject,
predicate) bucket the evaluator can touch is loaded whole, so the evaluation,
its refusals and its advisory are those of the full load.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from cruxible_client.contracts.canonical_forms import (
    CanonicalFormDidNotConverge,
    canonicalize_literal,
)
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_path,
    effective_canonical_form,
    parse_claim_type,
)
from cruxible_client.contracts.identity_normalizers import (
    KeyNormalizationRefused,
    canonical_key_part,
)
from cruxible_client.contracts.query.definitions import QueryDefinition
from cruxible_client.contracts.query.grammar import (
    QueryArtifactsEntry,
    QueryBudgets,
    QueryClaimFieldRef,
    QueryClaimPresenceFilter,
    QueryClaimValueRef,
    QueryComparisonFilter,
    QueryConjunctionFilter,
    QueryDisjunctionFilter,
    QueryEvaluationTimeRef,
    QueryFilter,
    QueryLiteralRef,
    QueryMembershipFilter,
    QueryNegationFilter,
    QueryPairStep,
    QueryParameterRef,
    QuerySubjectFieldRef,
    QueryTraversalStep,
    QueryValueRef,
)
from cruxible_client.contracts.shapes import claim_value_kind, value_type_name
from cruxible_client.contracts.subject_kinds import SubjectEnd, live_subject_kind
from cruxible_client.contracts.subjects import subject_path
from cruxible_core.indexes.sql_batch import select_in
from cruxible_core.indexes.subject_identity import (
    key_normalizer_of,
    subjects_with_key,
    tree_subject_kind_reader,
)
from cruxible_core.query.engine import OperandProbe, _RefusalSignal, resolve_query_parameters

_SUBJECTS = "subjects/"


@dataclass(frozen=True)
class QueryFactsSelection:
    #: The entry rows the evaluator starts from; None: every Subject of the
    #: entry kinds (the evaluator enumerates them itself).
    paths: tuple[str, ...] | None
    # Present only when LIMIT was pushed past a plain entry's result budget.
    candidate_count: int | None = None
    #: When set, exactly the live Claims the evaluation can reach; otherwise
    #: every live Claim of the referenced predicates about ``paths``.
    claim_paths: tuple[str, ...] | None = None


@dataclass(frozen=True)
class _FilterSelection:
    apply: Callable[[set[str] | None], set[str]]
    # Identity seeks precede scalar seeks and population-wide kind selections.
    priority: int


class _Residual:
    """A filter no index answers, safe to evaluate on the seek's candidates only."""


_RESIDUAL = _Residual()


def _kind_of(path: str) -> str:
    return path.split("/", 2)[1]


def _paths_of_kinds(connection: Any, kinds: Iterable[str]) -> set[str]:
    found: set[str] = set()
    for kind in sorted(set(kinds)):
        prefix = f"{_SUBJECTS}{kind}/"
        found.update(
            str(row[0])
            for row in connection.execute(
                "SELECT path FROM subjects WHERE path>=? AND path<?",
                (prefix, prefix[:-1] + chr(ord("/") + 1)),
            )
        )
    return found


def select_query_facts(
    connection: Any,
    query: QueryDefinition,
    *,
    parameters: Mapping[str, object] | None,
    budgets: QueryBudgets | None,
    source: Any,
    live_subjects_only: bool = False,
    operands: OperandProbe | None = None,
) -> QueryFactsSelection | None:
    """Select only proven entry candidates and reach, or return None for the full-load oracle.

    A query with hops keeps its exact entry rows (pruning an entry before
    ``max_paths`` or a traversal refusal requires a separate proof) and reads
    only what its hops reach. ``operands`` is the evaluator's own coercion of
    literal and parameter operands: without it no conjunction leaves a part to
    the evaluator and no Subject-valued equality seeks.
    """
    entry = query.entry
    if isinstance(entry, QueryArtifactsEntry):
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

    claim_types: dict[str, ClaimType | None] = {}

    def claim_type_of(predicate: str) -> ClaimType | None:
        if predicate not in claim_types:
            path = claim_type_path(predicate)
            content = source.get(path)
            claim_types[predicate] = (
                None if content is None else parse_claim_type(content, path=path)
            )
        return claim_types[predicate]

    policy = query.evaluation_policy
    surfaces = policy.conflict_behavior == "surface_conflicts"

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

    def constant_ref(ref: QueryValueRef) -> bool:
        return isinstance(ref, QueryLiteralRef | QueryParameterRef)

    def allowed(filter_: QueryFilter) -> bool:
        """Whether ``filter_`` may stay in memory beside a seek: a strict allowlist.

        The evaluator runs a left-out part on every row of the full load,
        including the rows the seek eliminated, so the part must provably never
        refuse on any row. Only these shapes are proven:

        - a presence test (``exists x.p``): it reads Claims, compares nothing;
        - a comparison or membership each of whose operand pairs is
          - two literals or parameters (their coercion is probed by
            ``OperandProbe`` over the whole filter), or
          - an entry Subject field (``subject_id``, ``subject_kind``) compared
            as ``string`` or ``subject_reference`` with a literal or parameter:
            a field is always a string and takes no Claim's form or normalizer,
            or
          - ``now`` compared as ``string`` or ``timestamp`` with a literal, or
          - a scalar Claim value of the entry (``scalar_claim``: one-valued,
            literal, not a key part, declared type equal to the comparison
            type, conflicts surfaced) with a literal or parameter;
        - an and/or/not of allowed parts.

        Every other shape keeps the full evaluation: a Claim role or verdict,
        any other Claim value (many-valued, a key, Subject-valued, another
        type), ``now`` against anything but a literal, or two row-dependent
        operands.
        """

        if isinstance(filter_, QueryConjunctionFilter | QueryDisjunctionFilter):
            return all(allowed(item) for item in filter_.filters)
        if isinstance(filter_, QueryNegationFilter):
            return allowed(filter_.operand)
        if isinstance(filter_, QueryClaimPresenceFilter):
            return filter_.binding == entry.binding
        if isinstance(filter_, QueryComparisonFilter):
            pairs = [(filter_.left, filter_.right)]
        else:
            pairs = [(filter_.left, value) for value in filter_.values]
        value_type = filter_.value_type
        for one, other in pairs:
            if constant_ref(one) and constant_ref(other):
                continue
            if constant_ref(one):
                one, other = other, one
            if not constant_ref(other):
                return False
            if isinstance(one, QuerySubjectFieldRef):
                if one.binding == entry.binding and value_type in {"string", "subject_reference"}:
                    continue
                return False
            if (
                isinstance(one, QueryEvaluationTimeRef)
                and isinstance(other, QueryLiteralRef)
                and value_type in {"string", "timestamp"}
            ):
                continue
            if isinstance(one, QueryClaimValueRef) and scalar_claim(one, value_type):
                continue
            return False
        return True

    def scalar_claim(ref: QueryClaimValueRef, value_type: str) -> bool:
        """A one-valued, literal, non-key Claim value of the filter's own scalar type.

        Its stored value takes no form and no normalizer (only the compared
        literal or parameter is transformed, and ``OperandProbe`` checks that
        under this predicate's form and normalizer), it coerces to its declared
        type, and conflicts surface on its row (a refusing policy is excluded).
        """

        if not surfaces or ref.binding != entry.binding:
            return False
        claim_type = claim_type_of(ref.predicate)
        return (
            claim_type is not None
            # A none (inverse) object holds no value of the declared type.
            and claim_type.inverse is None
            and claim_type.cardinality == "one"
            and claim_value_kind(claim_type.value) == "literal"
            and value_type in {"string", "integer", "boolean"}
            and value_type_name(claim_type.value) == value_type
            and key_normalizer_of(claim_type, tree_subject_kind_reader(source)) is None
        )

    absorbed = False

    def plan_filter(filter_: QueryFilter) -> _FilterSelection | _Residual | None:
        nonlocal absorbed
        if isinstance(filter_, QueryConjunctionFilter | QueryDisjunctionFilter):
            plans = [plan_filter(item) for item in filter_.filters]
            if any(item is None for item in plans):
                return None
            complete = [item for item in plans if isinstance(item, _FilterSelection)]
            if isinstance(filter_, QueryConjunctionFilter):
                if not complete:
                    return _RESIDUAL if allowed(filter_) else None
                if len(complete) < len(plans):
                    # The left-out parts are evaluated on the candidates only.
                    if operands is None:
                        return None
                    absorbed = True
                complete.sort(key=lambda item: item.priority)

                def conjunction(admissible: set[str] | None) -> set[str]:
                    selected = admissible
                    for plan in complete:
                        selected = plan.apply(selected)
                        if not selected:
                            break
                    assert selected is not None
                    return selected

                return _FilterSelection(conjunction, min(item.priority for item in complete))
            if len(complete) < len(plans):
                # A seekable sibling is left out too: the whole part must be allowed.
                return _RESIDUAL if allowed(filter_) else None

            def disjunction(admissible: set[str] | None) -> set[str]:
                # Siblings share the incoming restriction, never each other's
                # matches: otherwise OR would incorrectly become AND.
                return set.union(*(item.apply(admissible) for item in complete))

            return _FilterSelection(disjunction, max(item.priority for item in complete))
        if isinstance(filter_, QueryNegationFilter | QueryClaimPresenceFilter):
            return _RESIDUAL if allowed(filter_) else None
        # A seek's own part runs on eliminated rows too; each seek proves it
        # cannot refuse there (surfaced conflicts, typed scalar or Subject reads).
        seek = seek_filter(filter_)
        if seek is not None:
            return seek
        return _RESIDUAL if allowed(filter_) else None

    def seek_filter(
        filter_: QueryComparisonFilter | QueryMembershipFilter,
    ) -> _FilterSelection | None:
        if isinstance(filter_, QueryComparisonFilter):
            ref, other = filter_.left, filter_.right
            if isinstance(ref, QueryLiteralRef | QueryParameterRef):
                ref, other = other, ref
            if not isinstance(other, QueryLiteralRef | QueryParameterRef):
                return None
            operand_refs: tuple[QueryValueRef, ...] = (other,)
            operator: str = filter_.operator
            if ref is filter_.right:
                operator = {"gt": "lt", "gte": "lte", "lt": "gt", "lte": "gte"}.get(
                    operator, operator
                )
        elif not filter_.negated:
            ref = filter_.left
            if not all(
                isinstance(item, QueryLiteralRef | QueryParameterRef) for item in filter_.values
            ):
                return None
            operand_refs = filter_.values
            operator = "eq"
        else:
            return None
        values = [constant(item) for item in operand_refs]
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
                kinds = tuple(kind for kind in entry.subject_kinds if matches(kind))

                def kind_paths(admissible: set[str] | None) -> set[str]:
                    if admissible is None:
                        return paths_of_kinds(kinds)
                    return {path for path in admissible if _kind_of(path) in kinds}

                return _FilterSelection(kind_paths, 2)
            if operator != "eq":
                return None

            def identity_paths(admissible: set[str] | None) -> set[str]:
                if admissible is not None:
                    identifiers = set(values)
                    return {
                        str(row[0])
                        for row in select_in(
                            connection,
                            "SELECT path,subject_id FROM subjects WHERE path IN ({})",
                            tuple(admissible),
                        )
                        if row[1] in identifiers
                    }
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

            return _FilterSelection(identity_paths, 0)
        if operator != "eq" or not surfaces:
            return None
        claim_type = claim_type_of(ref.predicate)
        if claim_type is None or claim_type.inverse is not None:
            # A seek's own part runs on eliminated rows: an inverse-capable
            # type can hold a visible none, whose value coerces to no type.
            return None
        if claim_value_kind(claim_type.value) == "subject":
            if filter_.value_type != "subject_reference":
                return None
            return subject_value_seek(ref, operand_refs)
        # Eliminated subjects must not contribute a refusal: scalar schemas
        # prove comparisons cannot refuse for type; surface_conflicts preserves
        # competing values without a global refusal.
        scalar = filter_.value_type
        declared_type = value_type_name(claim_type.value)
        if scalar not in {"string", "integer", "boolean"} or declared_type != scalar:
            return None
        rule = key_normalizer_of(claim_type, tree_subject_kind_reader(source))
        form = effective_canonical_form(claim_type)
        try:
            # A key predicate's operand takes the shared key canonicalization
            # (R12). One it refuses is not pushed down: the full evaluation
            # refuses it (value_type_mismatch).
            values = [
                canonical_key_part(value, form, rule)
                if rule is not None and isinstance(value, str)
                else canonicalize_literal(value, form)
                for value in values
            ]
        except (CanonicalFormDidNotConverge, KeyNormalizationRefused):
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
        predicate = ref.predicate

        def value_paths(admissible: set[str] | None) -> set[str]:
            if admissible is not None:
                allowed = set(indexed)
                # Force the Subject/predicate seek: the unconstrained literal
                # index would walk every matching Subject before intersecting.
                return {
                    str(row[0])
                    for row in select_in(
                        connection,
                        f"SELECT subject_path,{column} FROM claims "
                        "INDEXED BY claims_by_subject_predicate "
                        "WHERE predicate=? AND literal_type=? AND lifecycle='live' "
                        "AND subject_path IN ({})",
                        tuple(admissible),
                        prefix=(predicate, scalar),
                    )
                    if row[1] in allowed
                }
            return {
                str(row[0])
                for row in select_in(
                    connection,
                    "SELECT subject_path FROM claims WHERE predicate=? AND literal_type=? "
                    f"AND {column} IN ({{}}) AND lifecycle='live'",
                    indexed,
                    prefix=(predicate, scalar),
                )
                if _kind_of(str(row[0])) in entry.subject_kinds
            }

        return _FilterSelection(value_paths, 1)

    def subject_value_seek(
        ref: QueryClaimValueRef, operand_refs: tuple[QueryValueRef, ...]
    ) -> _FilterSelection | None:
        """``x.p == $s`` over a Subject-valued ``p``: one object-index seek per named Subject.

        The operand compares as the evaluator spells it (``Subject:kind/id``,
        after alias resolution); a stored value is its target's identity, so
        only Subjects holding a live ``p`` Claim naming that target can match.
        """

        if operands is None or live_subjects_only:
            return None
        targets: set[str] = set()
        for item in operand_refs:
            try:
                typed = operands.values(item, "subject_reference", ref)
            except _RefusalSignal:
                return None
            for value in typed or ():
                text = value.value.decode("utf-8") if isinstance(value.value, bytes) else None
                if text is None or not text.startswith("Subject:"):
                    continue
                kind, slash, identifier = text.removeprefix("Subject:").partition("/")
                if not slash:
                    continue
                try:
                    targets.add(subject_path(kind, identifier))
                except ValueError:
                    continue
        predicate = ref.predicate

        def object_paths(admissible: set[str] | None) -> set[str]:
            if admissible is not None:
                return {
                    str(row[0])
                    for row in select_in(
                        connection,
                        "SELECT subject_path,object_path FROM claims "
                        "INDEXED BY claims_by_subject_predicate "
                        "WHERE predicate=? AND lifecycle='live' AND subject_path IN ({})",
                        tuple(admissible),
                        prefix=(predicate,),
                    )
                    if row[1] in targets
                }
            return {
                str(row[0])
                for row in select_in(
                    connection,
                    "SELECT subject_path FROM claims INDEXED BY claims_by_object_subject "
                    "WHERE object_kind='subject' AND predicate=? AND lifecycle='live' "
                    "AND object_path IN ({})",
                    sorted(targets),
                    prefix=(predicate,),
                )
                if _kind_of(str(row[0])) in entry.subject_kinds
            }

        return _FilterSelection(object_paths, 1)

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
    if query.traversal:
        # Hops keep the exact entry rows: only the entry parameter narrows them.
        entries = None if selected is None else _live(connection, selected, live_subjects_only)
        return QueryFactsSelection(
            entries,
            None,
            query_reach(connection, query, entries=entries, source=source),
        )
    if query.where is not None:
        plan = plan_filter(query.where)
        if isinstance(plan, _FilterSelection) and (
            not absorbed or (operands is not None and not operands.refuses(query.where))
        ):
            selected = plan.apply(selected)
    effective = budgets or query.default_budgets
    plain_budget = (
        query.where is None
        and not query.orderings
        and not query.includes
        and query.result_cardinality == "many"
        and effective.max_paths is None
        and effective.max_paths_per_result is None
    )
    if selected is not None:
        paths = _live(connection, selected, live_subjects_only)
        paths = paths[: effective.max_results] if plain_budget else paths
        return QueryFactsSelection(
            paths,
            len(_live(connection, selected, live_subjects_only)) if plain_budget else None,
            query_reach(connection, query, entries=paths, source=source)
            if query.includes
            else None,
        )
    if query.includes:
        return QueryFactsSelection(
            None, None, query_reach(connection, query, entries=None, source=source)
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


def _live(connection: Any, selected: set[str], live_only: bool) -> tuple[str, ...]:
    paths = tuple(sorted(selected))
    if not live_only:
        return paths
    return tuple(
        sorted(
            str(row[0])
            for row in select_in(
                connection,
                "SELECT path FROM subjects WHERE lifecycle='live' AND path IN ({})",
                paths,
            )
        )
    )


def _filter_reads(filter_: QueryFilter | None) -> dict[str, set[str]]:
    """Each binding's predicates one filter reads."""

    reads: dict[str, set[str]] = {}
    _walk_reads(filter_, reads)
    return reads


def _walk_reads(filter_: QueryFilter | None, reads: dict[str, set[str]]) -> None:
    def ref(value: QueryValueRef) -> None:
        if isinstance(value, QueryClaimValueRef | QueryClaimFieldRef):
            reads.setdefault(value.binding, set()).add(value.predicate)

    if filter_ is None:
        return
    if isinstance(filter_, QueryConjunctionFilter | QueryDisjunctionFilter):
        for item in filter_.filters:
            _walk_reads(item, reads)
    elif isinstance(filter_, QueryNegationFilter):
        _walk_reads(filter_.operand, reads)
    elif isinstance(filter_, QueryClaimPresenceFilter):
        reads.setdefault(filter_.binding, set()).add(filter_.predicate)
    elif isinstance(filter_, QueryComparisonFilter):
        ref(filter_.left)
        ref(filter_.right)
    else:
        ref(filter_.left)
        for value in filter_.values:
            ref(value)


def _binding_reads(query: QueryDefinition) -> dict[str, set[str]]:
    """Each binding's predicates read on the final rows (where, fields, orders, includes).

    A hop's own filter is read where that hop evaluates (``query_reach``).
    """

    reads: dict[str, set[str]] = {}

    def ref(value: QueryValueRef) -> None:
        if isinstance(value, QueryClaimValueRef | QueryClaimFieldRef):
            reads.setdefault(value.binding, set()).add(value.predicate)

    def walk(filter_: QueryFilter | None) -> None:
        _walk_reads(filter_, reads)

    walk(query.where)
    for field in () if query.projection is None else query.projection.fields:
        ref(field.value)
    for ordering in query.orderings:
        ref(ordering.key)
    for include in query.includes:
        walk(include.where)
        for ordering in include.orderings:
            ref(ordering.key)
    return reads


_FORWARD_SQL = (
    "SELECT path, object_kind, object_path, subject_path FROM claims "
    "INDEXED BY claims_by_subject_predicate "
    "WHERE predicate=? AND lifecycle='live' AND subject_path IN ({})"
)
_REVERSE_SQL = (
    "SELECT path, subject_path, object_path FROM claims INDEXED BY claims_by_object_subject "
    "WHERE object_kind='subject' AND predicate=? AND lifecycle='live' AND object_path IN ({})"
)


def query_reach(
    connection: Any,
    query: QueryDefinition,
    *,
    entries: tuple[str, ...] | None,
    source: Mapping[str, bytes],
) -> tuple[str, ...]:
    """The live Claims an evaluation from ``entries`` can read (None: the entry kinds).

    The rows are walked structurally, a superset of the evaluator's (no
    visibility, no filters; an optional step also keeps its unbound row), and
    every bucket a row can touch is loaded whole: the forward bucket
    ``(Subject, predicate)`` for a hop, a forward include and a read; the
    reverse bucket (Claims whose Subject object is that Subject) for a reverse
    hop or include. A pair hop's candidates come from the Subject key index
    (whole key) or the pair index (one end with a fact), and its pairs' key and
    fact buckets load. Cost: the reached Claims and rows.
    """

    assert not isinstance(query.entry, QueryArtifactsEntry)
    entry = query.entry.binding
    start = (
        entries
        if entries is not None
        else sorted(_paths_of_kinds(connection, query.entry.subject_kinds))
    )
    on_rows: dict[tuple[str, str], list[tuple[str, str | None]]] = {}
    to_rows: dict[tuple[str, str], list[tuple[str, str]]] = {}

    def forward(subjects: Iterable[str], predicate: str) -> None:
        wanted = sorted({path for path in subjects if (path, predicate) not in on_rows})
        for path in wanted:
            on_rows[(path, predicate)] = []
        for claim, kind, target, holder in select_in(
            connection, _FORWARD_SQL, wanted, prefix=(predicate,)
        ):
            on_rows[(str(holder), predicate)].append(
                (str(claim), str(target) if kind == "subject" and target is not None else None)
            )

    def reverse(subjects: Iterable[str], predicate: str) -> None:
        wanted = sorted({path for path in subjects if (path, predicate) not in to_rows})
        for path in wanted:
            to_rows[(path, predicate)] = []
        for claim, holder, target in select_in(
            connection, _REVERSE_SQL, wanted, prefix=(predicate,)
        ):
            to_rows[(str(target), predicate)].append((str(claim), str(holder)))

    def targets(path: str, predicate: str, direction: str) -> list[str]:
        if direction == "forward":
            return [target for _claim, target in on_rows[(path, predicate)] if target is not None]
        return [holder for _claim, holder in to_rows[(path, predicate)]]

    def names(pair: str, part: str, end: str) -> bool:
        return any(target == end for _claim, target in on_rows[(pair, part)])

    Row = tuple[tuple[str, str | None], ...]
    rows: set[Row] = {((entry, path),) for path in start}

    def bound(row: Row, binding: str) -> str | None:
        return dict(row).get(binding)

    def extend(row: Row, binding: str, value: str | None) -> Row:
        return (*row, (binding, value))

    def step_reads(where: QueryFilter | None, candidates: set[Row]) -> None:
        # A hop's filter runs on every candidate row of that hop, including
        # rows a later required hop removes: its buckets load for those rows.
        for binding, predicates in _filter_reads(where).items():
            subjects = {path for row in candidates if (path := bound(row, binding)) is not None}
            for predicate in sorted(predicates):
                forward(subjects, predicate)

    for step in query.traversal:
        produced: set[Row] = set()
        if isinstance(step, QueryTraversalStep):
            sources = {bound(row, step.from_binding) for row in rows} - {None}
            (forward if step.direction == "forward" else reverse)(
                (path for path in sources if path is not None), step.predicate
            )
            for row in rows:
                origin = bound(row, step.from_binding)
                for target in (
                    [] if origin is None else targets(origin, step.predicate, step.direction)
                ):
                    if (
                        not step.target_subject_kinds
                        or _kind_of(target) in step.target_subject_kinds
                    ):
                        produced.add(extend(row, step.binding, target))
                if not step.required:
                    produced.add(extend(row, step.binding, None))
            step_reads(step.where, produced)
            rows = produced
            continue
        assert isinstance(step, QueryPairStep)
        combos: dict[tuple[str, ...], set[str]] = {}
        for row in rows:
            ends = tuple(bound(row, part.binding) for part in step.parts)
            if all(end is not None for end in ends):
                combos.setdefault(tuple(str(end) for end in ends), set())
        _pair_candidates(connection, step, combos, source, forward, reverse, to_rows, names)
        for row in rows:
            ends = tuple(bound(row, part.binding) for part in step.parts)
            for pair in combos.get(tuple(str(end) for end in ends), ()) if None not in ends else ():
                produced.add(extend(row, step.binding, pair))
            if not step.required:
                produced.add(extend(row, step.binding, None))
        step_reads(step.where, produced)
        rows = produced
    reached: dict[str, set[str]] = {}
    for row in rows:
        for binding, path in row:
            if path is not None:
                reached.setdefault(binding, set()).add(path)
    for include in query.includes:
        holders = reached.get(include.from_binding, set())
        (forward if include.direction == "forward" else reverse)(holders, include.predicate)
        reached[include.binding] = {
            target
            for path in holders
            for target in targets(path, include.predicate, include.direction)
        }
    for binding, predicates in _binding_reads(query).items():
        for predicate in sorted(predicates):
            forward(reached.get(binding, set()), predicate)
    claims = {claim for bucket in on_rows.values() for claim, _target in bucket}
    claims.update(claim for bucket in to_rows.values() for claim, _holder in bucket)
    return tuple(sorted(claims, key=lambda item: item.encode("utf-8")))


def _pair_candidates(
    connection: Any,
    step: QueryPairStep,
    combos: dict[tuple[str, ...], set[str]],
    source: Mapping[str, bytes],
    forward: Callable[[Iterable[str], str], None],
    reverse: Callable[[Iterable[str], str], None],
    to_rows: Mapping[tuple[str, str], list[tuple[str, str]]],
    names: Callable[[str, str, str], bool],
) -> None:
    """Fill each end combination's candidate pairs and load their key and fact buckets.

    - one end with a fact: one ``pair_units`` seek per end (current units only;
      historical shells have no row);
    - a whole key of a live keyed kind: one ``subject_keys`` seek per combination;
    - a whole key otherwise (a retired, grandfathered pair kind): the first end's
      reverse bucket of its part, filtered by every part's loaded key Claims.
    """

    parts = tuple(item.part for item in step.parts)
    prefix = f"{_SUBJECTS}{step.subject_kind}/"
    if step.fact is not None:
        ends = sorted({combo[0] for combo in combos})
        found: dict[str, set[str]] = {}
        for end, pair in select_in(
            connection,
            "SELECT end_path, pair_path FROM pair_units WHERE part=? AND predicate=? "
            "AND end_path IN ({})",
            ends,
            prefix=(parts[0], step.fact),
        ):
            if str(pair).startswith(prefix):
                found.setdefault(str(end), set()).add(str(pair))
        for combo in combos:
            combos[combo] = found.get(combo[0], set())
    else:
        declared = live_subject_kind(source, step.subject_kind)
        keys = None if declared is None else tuple(declared.keys)
        if keys is not None and set(keys) == set(parts) and len(keys) == len(parts):
            order = {part: index for index, part in enumerate(parts)}
            for combo in combos:
                key = tuple((part, SubjectEnd(path=combo[order[part]])) for part in keys)
                combos[combo] = set(subjects_with_key(connection, step.subject_kind, key))
        else:
            reverse({combo[0] for combo in combos}, parts[0])
            for combo in combos:
                combos[combo] = {
                    holder
                    for _claim, holder in to_rows[(combo[0], parts[0])]
                    if holder.startswith(prefix)
                }
    pairs = {pair for found_pairs in combos.values() for pair in found_pairs}
    for part in parts:
        forward(pairs, part)
    if step.fact is not None:
        forward(pairs, step.fact)
    else:
        for combo, candidates in combos.items():
            combos[combo] = {
                pair
                for pair in candidates
                if all(names(pair, part, end) for part, end in zip(parts, combo, strict=True))
            }


__all__ = ["QueryFactsSelection", "query_reach", "select_query_facts"]
