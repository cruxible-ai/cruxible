"""The ``query`` read verb: any question over accepted state, one call.

Three modes, exactly one per call:

- **compact** -- ``kind`` and/or ``contains`` with optional ``where``, ``select``,
  ``follow`` and ``order_by``. A Subject-kind query lowers to a
  ``QueryDefinitionSpecV1`` wrapped as an inline definition (its own digest, no
  accepted path) and runs through the same evaluator as a governed
  QueryDefinition. ``kind: ClaimType`` / ``kind: Procedure`` select definitions
  through the artifact entry. ``contains`` with no kind searches the values of
  every live Claim.
- **spec** -- a full ``QueryDefinitionSpecV1``, pinned at the coordinate.
- **name** -- an accepted QueryDefinition with its ``params``, run exactly as
  ``run_query`` runs it.

Lowered filters are the ones the accepted grammar states exactly: a
one-cardinality predicate compared, matched against a set or tested for
presence, and ``subject_id``. ``contains`` and value filters on many-valued
predicates are evaluated inline over the evaluator's rows with "any value"
semantics; they are never part of an accepted QueryDefinition. ``ne`` means no
value equals, so a Subject without the value matches.

Answers lead with values: typed columns, rows of values with verdict flags, and
list-page paging whose cursor binds the selection, the coordinate and the
listing it was cut from.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal, cast

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.claim_verdicts import EvidenceCurrency, EvidenceRelativeClaimVerdict
from cruxible_client.contracts.compact_query import (
    PlaybillQueryColumnV1,
    PlaybillQueryReceiptV1,
    PlaybillQueryRequestV1,
    PlaybillQueryResult,
    QueryFilterOperator,
    QueryFilterV1,
    QueryFlag,
    QueryFollowDirection,
    QueryMode,
)
from cruxible_client.contracts.get_reads import summary_value
from cruxible_client.contracts.primitives import canonical_json
from cruxible_client.contracts.procedures.artifacts import (
    ProcedureArtifactV1,
    ProcedureArtifactV2,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.query.definitions import (
    CLAIM_TYPE_PIN_ROLE,
    AcceptedQueryDefinitionV1,
    QueryDefinitionSpecV1,
    QueryDefinitionV1,
    QueryEvaluationPolicyV1,
    query_definition_digest,
    query_definition_path,
)
from cruxible_client.contracts.query.grammar import (
    QueryArtifactsEntryV2,
    QueryBudgetsV1,
    QueryClaimPresenceFilterV1,
    QueryClaimValueRefV1,
    QueryComparisonFilterV1,
    QueryConjunctionFilterV1,
    QueryDisjunctionFilterV1,
    QueryEntryV1,
    QueryEvaluationTimeRefV1,
    QueryLiteralRefV1,
    QueryMembershipFilterV1,
    QueryNegationFilterV1,
    QueryOrderingV1,
    QuerySubjectFieldRefV1,
    QueryTraversalStepV1,
    QueryValueRefV1,
    QueryValueTypeV1,
    binding_name,
)
from cruxible_client.contracts.query.grammar import (
    QueryFilterV1 as GrammarFilter,
)
from cruxible_client.contracts.query.results import ClaimQueryResultV1
from cruxible_client.contracts.temporal import utc_now
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.discovery.contract_names import CaptureContractNames
from cruxible_core.service.discovery.exact_content import ExactContentReader
from cruxible_core.service.discovery.query import evaluate_accepted_query
from cruxible_core.service.discovery.query_values import (
    LiveValue,
    ValueIndex,
    distinct,
    ensure_values,
    read_live_values,
    subject_labels,
)
from cruxible_core.service.discovery.query_vocabulary import (
    ORDERABLE_TYPES,
    SUBJECT_ID_FIELD,
    PredicateInfo,
    QueryVocabulary,
    check_operator,
    check_value,
    claim_type_row,
    load_query_vocabulary,
    object_label,
    query_not_found,
    query_refusal,
    value_type_of,
)
from cruxible_core.service.discovery.read_flags import (
    answer_flags,
    claim_flags,
    ordered_flags,
    verdict_flags,
)
from cruxible_core.service.list_pages import (
    PlaybillListCursorMismatch,
    PlaybillListCursorStale,
    decode_list_cursor,
    encode_list_cursor,
    list_snapshot,
    page_after_boundary,
)
from cruxible_core.service.read_refusals import (
    ReadRefusalError,
    nearest,
    resolve_read_coordinate,
)

LIST_NAME = "query"
COMPACT_QUERY_MAX_RESULTS = 5000
ARTIFACT_QUERY_MAX_RESULTS = 2000
DEFAULT_COLUMN_CAP = 12
INLINE_DEFINITION_NAME = "inline"
ROOT = "subject"
ARTIFACT_KINDS = ("ClaimType", "Procedure")
_ALL_VERDICTS: tuple[EvidenceRelativeClaimVerdict, ...] = (
    "contradicted",
    "stale",
    "supported",
    "uncovered",
    "unresolved",
)
_ALL_CURRENCY: tuple[EvidenceCurrency, ...] = ("current", "not_applicable", "stale")
_CONTAINS_DIGEST_DOMAIN = "playbill-compact-contains-v1"
_ENGINE_TYPES: dict[str, QueryValueTypeV1] = {
    "string": "string",
    "enum": "string",
    "date": "string",
    "integer": "integer",
    "decimal": "decimal",
    "boolean": "boolean",
    "timestamp": "timestamp",
    "subject": "subject_reference",
}
_LOWERED_OPERATORS = frozenset({"eq", "ne", "lt", "lte", "gt", "gte", "in"})


# -- mode and coordinate ------------------------------------------------------


def _mode(request: PlaybillQueryRequestV1) -> QueryMode:
    compact = request.kind is not None or request.contains is not None
    shaping = bool(request.where or request.select or request.follow or request.order_by)
    chosen = [
        mode
        for mode, present in (
            ("inline", compact),
            ("spec", request.spec is not None),
            ("named", request.name is not None),
        )
        if present
    ]
    if len(chosen) != 1:
        raise query_refusal(
            "playbill.query.mode_invalid",
            "a query takes exactly one mode: kind and/or contains (compact), spec, or name",
            repair=(
                'pass kind (e.g. kind="dev.roadmap_item"), contains, a spec, or a name; not several'
            ),
        )
    mode = cast(QueryMode, chosen[0])
    if mode != "inline" and shaping:
        raise query_refusal(
            "playbill.query.mode_invalid",
            "where, select, follow and order_by shape a compact query only",
            repair="drop them, or pass kind instead of spec/name",
        )
    if request.params is not None and mode != "named":
        raise query_refusal(
            "playbill.query.mode_invalid",
            "params bind a named query only",
            repair="pass name with params, or drop params",
        )
    return mode


# -- answers ------------------------------------------------------------------


@dataclass
class _Answer:
    """Every candidate row in order, and how to render one page of them."""

    mode: QueryMode
    kind: str | None
    spec_digest: str
    columns: tuple[PlaybillQueryColumnV1, ...]
    candidates: Sequence[Any]
    keys: Sequence[tuple[str, ...]]
    render: Callable[[Sequence[Any]], list[dict[str, Any]]]
    capped: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Field:
    """One resolved field: which binding it reads and what it reads there."""

    name: str
    binding: str
    info: PredicateInfo | Literal["subject_id"]
    label: str

    @property
    def predicate(self) -> str | None:
        return None if isinstance(self.info, str) else self.info.predicate


@dataclass(frozen=True)
class _Follow:
    """One follow hop: ``forward`` along the kind's predicate, or ``reverse`` into it."""

    alias: str
    info: PredicateInfo
    target_kinds: tuple[str, ...]
    direction: QueryFollowDirection = "forward"

    @property
    def lowered_targets(self) -> tuple[str, ...]:
        """The Subject kinds the traversal step admits at the far end."""

        kinds = self.info.object_kinds if self.direction == "forward" else self.info.subject_kinds
        return tuple(sorted(kinds))


@dataclass(frozen=True)
class _Column:
    """A rendered column: a field, or a followed Subject reference."""

    name: str
    binding: str
    field: _Field | None


@dataclass
class _InlineFilter:
    field: _Field
    operator: QueryFilterOperator
    value: object


ROW_METADATA = frozenset({"subject", "subject_id", "flags"})


def _out(name: str) -> str:
    """The row key a column's values are served under.

    ``subject``, ``subject_id`` and ``flags`` are row metadata on every Subject
    row. A column named like one of them keeps its values under
    ``value.<name>`` instead of overwriting the metadata or losing its values.
    ``_column_keys`` then makes the keys unique across the whole column set; a
    projection needs no more, since its field names are distinct identifiers.
    """

    return f"value.{name}" if name in ROW_METADATA else name


@dataclass(frozen=True)
class _Wanted:
    """One column asking for a row key: its identity and its names, best first."""

    owner: tuple[str, ...]
    names: tuple[str, ...]
    item: _Field | _Follow


def _column_keys(wanted: Sequence[_Wanted]) -> list[str]:
    """Allocate every column's row key so no two distinct columns share one.

    The whole key set is built first (read-model spec, Addendum 3): row
    metadata is reserved, a column named like metadata moves under
    ``value.<name>``, and a key two columns both want goes to neither; each
    falls back to its next name (the full predicate). A column with no fallback
    keeps the contested key if it is the only one without a fallback; any key
    still taken refuses, so two distinct columns never read as one.
    """

    candidates = [tuple(dict.fromkeys(_out(name) for name in item.names)) for item in wanted]
    wants = Counter(names[0] for names in candidates)
    taken = set(ROW_METADATA)
    keys: list[str | None] = [None] * len(wanted)
    for index, names in enumerate(candidates):
        if wants[names[0]] == 1:
            keys[index] = names[0]
            taken.add(names[0])
    pending = [index for index, key in enumerate(keys) if key is None]
    fixed = [index for index in pending if len(candidates[index]) == 1]
    flexible = [index for index in pending if len(candidates[index]) > 1]
    for index in fixed:
        key = candidates[index][0]
        if key in taken:
            raise query_refusal(
                "playbill.query.column_collision",
                f"two columns would both be served as {key!r}",
                repair="rename the follow alias, or select one of the two fields",
                field_path="select",
            )
        keys[index] = key
        taken.add(key)
    for index in flexible:
        fallback = next((name for name in candidates[index][1:] if name not in taken), None)
        if fallback is None:
            raise query_refusal(
                "playbill.query.column_collision",
                f"two columns would both be served as {candidates[index][0]!r}",
                repair="rename the follow alias, or select one of the two fields",
                field_path="select",
            )
        keys[index] = fallback
        taken.add(fallback)
    return [key for key in keys if key is not None]


def _column(item: _Field | _Follow, *, name: str) -> PlaybillQueryColumnV1:
    if isinstance(item, _Follow):
        return PlaybillQueryColumnV1(
            name=name,
            predicate=item.info.predicate,
            type="subject",
            cardinality="one",
        )
    if isinstance(item.info, str):
        return PlaybillQueryColumnV1(name=name, type="string", cardinality="one")
    info = item.info
    return PlaybillQueryColumnV1(
        name=name,
        predicate=info.predicate,
        type=object_label(info),
        members=info.members or None,
        cardinality=info.cardinality,
    )


# -- the compact Subject-kind query -------------------------------------------


class _CompactPlan:
    """A validated compact request against one Subject kind."""

    def __init__(self, vocabulary: QueryVocabulary, request: PlaybillQueryRequestV1) -> None:
        assert request.kind is not None
        self.vocabulary = vocabulary
        self.kind = vocabulary.require_kind(request.kind)
        self.follows: dict[str, _Follow] = {}
        roots = {name.split(".", 1)[0] for name in (*vocabulary.predicates, *vocabulary.kinds)}
        for index, follow in enumerate(request.follow):
            path = f"follow[{index}]"
            if follow.direction == "reverse":
                resolved = self._incoming(follow.field, field_path=f"{path}.field")
                targets = resolved.subject_kinds
            else:
                resolved = self._outgoing(follow.field, field_path=f"{path}.field")
                targets = resolved.object_kinds or vocabulary.kinds
            alias = follow.as_
            try:
                binding_name(alias)
            except ValueError:
                alias = ""
            if (
                not alias
                or alias in self.follows
                or alias in {ROOT, SUBJECT_ID_FIELD, "value"}
                or alias in roots
            ):
                raise query_refusal(
                    "playbill.query.alias_invalid",
                    f"alias {follow.as_!r} must be a new lower-case identifier that is not "
                    "subject, subject_id, value, another alias, or a predicate namespace",
                    repair='pick a short alias such as "parent"',
                    field_path=f"{path}.as",
                )
            self.follows[alias] = _Follow(
                alias=alias, info=resolved, target_kinds=targets, direction=follow.direction
            )

    def _outgoing(self, name: str, *, field_path: str) -> PredicateInfo:
        """A forward follow: a Subject-valued predicate of the kind itself."""

        relations = tuple(
            self.vocabulary.field_name(info, (self.kind,))
            for info in self.vocabulary.predicates_of(self.kind)
            if info.value_type == "subject"
        )
        incoming = self.vocabulary.resolve_incoming(self.kind, name)
        try:
            resolved = self.vocabulary.resolve_field(
                (self.kind,), name, field_path=field_path, owner=self.kind
            )
        except ReadRefusalError:
            if len(incoming) != 1:
                raise
            resolved = incoming[0]
        if (
            isinstance(resolved, str)
            or resolved.value_type != "subject"
            or self.kind not in resolved.subject_kinds
        ):
            repair = "follow a predicate whose values are Subjects"
            if len(incoming) == 1:
                repair = (
                    f"{incoming[0].predicate} points at {self.kind}; follow it backwards "
                    'with direction "reverse"'
                )
            raise query_refusal(
                "playbill.query.follow_not_relation",
                f"{name!r} is not a Subject-valued predicate of {self.kind}",
                nearest=relations,
                repair=repair,
                field_path=field_path,
            )
        return resolved

    def _incoming(self, name: str, *, field_path: str) -> PredicateInfo:
        """A reverse follow: another kind's Subject-valued predicate naming this kind."""

        found = self.vocabulary.resolve_incoming(self.kind, name)
        if len(found) == 1:
            return found[0]
        if found:
            raise query_refusal(
                "playbill.query.ambiguous_field",
                f"{name!r} names more than one predicate that points at {self.kind}",
                nearest=tuple(info.predicate for info in found),
                repair="name the predicate in full",
                field_path=field_path,
            )
        incoming = tuple(info.predicate for info in self.vocabulary.incoming(self.kind))
        if not incoming:
            message = f"no Subject-valued predicate points at {self.kind}"
        elif name in self.vocabulary.predicates:
            message = f"predicate {name!r} does not name {self.kind} Subjects"
        else:
            message = f"no predicate named {name!r} points at {self.kind}"
        raise query_refusal(
            "playbill.query.follow_not_incoming",
            message,
            nearest=nearest(name, incoming) or incoming,
            repair=(
                f"follow backwards along a predicate whose values are {self.kind} Subjects "
                f"(orient kind={self.kind} lists them as incoming)"
                if incoming
                else "follow forward instead, or query the other kind"
            ),
            field_path=field_path,
        )

    def field(self, name: str, *, field_path: str) -> _Field:
        head, _, rest = name.partition(".")
        follow = self.follows.get(head) if rest else None
        if follow is not None:
            info = self.vocabulary.resolve_field(
                follow.target_kinds, rest, field_path=field_path, owner=f"{head} ({follow.alias})"
            )
            label = rest if isinstance(info, str) else f"{head}.{info.predicate}"
            return _Field(name=name, binding=follow.alias, info=info, label=label)
        info = self.vocabulary.resolve_field(
            (self.kind,), name, field_path=field_path, owner=self.kind
        )
        label = SUBJECT_ID_FIELD if isinstance(info, str) else info.predicate
        return _Field(name=name, binding=ROOT, info=info, label=label)

    def column_name(self, item: _Field) -> str:
        """The column a selected field is served as: its shared short name."""

        if isinstance(item.info, str):
            return item.name
        if item.binding == ROOT:
            return self.vocabulary.field_name(item.info, (self.kind,))
        follow = self.follows[item.binding]
        return f"{follow.alias}.{self.vocabulary.field_name(item.info, follow.target_kinds)}"


def _literal(value: object) -> QueryLiteralRefV1:
    return QueryLiteralRefV1(value=value)


def _engine_literal(info: PredicateInfo | Literal["subject_id"], value: object) -> object:
    if not isinstance(info, str) and info.value_type == "subject":
        return f"Subject:{value}"
    return value


def _value_ref(item: _Field) -> QueryValueRefV1:
    if isinstance(item.info, str):
        return QuerySubjectFieldRefV1(binding=item.binding, field="subject_id")
    return QueryClaimValueRefV1(binding=item.binding, predicate=item.info.predicate)


def _engine_type(item: _Field) -> QueryValueTypeV1:
    if isinstance(item.info, str):
        return "string"
    return _ENGINE_TYPES[item.info.value_type]


def _lowerable(item: _Field, operator: str) -> bool:
    if operator == "exists":
        return not isinstance(item.info, str)
    if operator not in _LOWERED_OPERATORS:
        return False
    if isinstance(item.info, str):
        return True
    return item.info.cardinality == "one" and item.info.value_type in _ENGINE_TYPES


def _lower_filter(item: _Field, operator: str, value: object) -> GrammarFilter:
    if operator == "exists":
        assert not isinstance(item.info, str)
        return QueryClaimPresenceFilterV1(
            binding=item.binding, predicate=item.info.predicate, negated=not value
        )
    left = _value_ref(item)
    value_type = _engine_type(item)
    if operator == "in":
        assert isinstance(value, tuple)
        literals = {
            canonical_bytes(ref.model_dump(mode="json")): ref
            for ref in (_literal(_engine_literal(item.info, entry)) for entry in value)
        }
        return QueryMembershipFilterV1(
            left=left,
            values=tuple(literals[key] for key in sorted(literals)),
            value_type=value_type,
        )
    right = _literal(_engine_literal(item.info, value))
    if operator == "ne":
        # No value equals: a Subject without the value matches, and a contested
        # slot matches no value filter (its comparison is a conflict, never true).
        differs = QueryComparisonFilterV1(
            left=left, operator="ne", right=right, value_type=value_type
        )
        if isinstance(item.info, str):
            return differs
        absent = QueryNegationFilterV1(
            operand=QueryClaimPresenceFilterV1(binding=item.binding, predicate=item.info.predicate)
        )
        operands: list[GrammarFilter] = [differs, absent]
        operands.sort(key=lambda entry: canonical_bytes(entry.model_dump(mode="json")))
        return QueryDisjunctionFilterV1(filters=tuple(operands))
    return QueryComparisonFilterV1(
        left=left,
        operator=cast(Literal["eq", "gt", "gte", "lt", "lte"], operator),
        right=right,
        value_type=value_type,
    )


def _all_of(filters: Sequence[GrammarFilter]) -> GrammarFilter | None:
    unique = {canonical_bytes(item.model_dump(mode="json")): item for item in filters}
    ordered = [unique[key] for key in sorted(unique)]
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    return QueryConjunctionFilterV1(filters=tuple(ordered))


def _checked_filters(
    plan: _CompactPlan, where: Sequence[QueryFilterV1]
) -> list[tuple[_Field, QueryFilterOperator, object]]:
    checked: list[tuple[_Field, QueryFilterOperator, object]] = []
    for index, item in enumerate(where):
        path = f"where[{index}]"
        resolved = plan.field(item.field, field_path=f"{path}.field")
        check_operator(resolved.info, item.operator, field_path=path, label=resolved.label)
        if item.operator == "in":
            raw = cast(tuple[object, ...], item.value)
            value: object = tuple(
                check_value(
                    resolved.info,
                    "eq",
                    entry,
                    field_path=f"{path}.in[{position}]",
                    label=resolved.label,
                )
                for position, entry in enumerate(raw)
            )
        else:
            value = check_value(
                resolved.info,
                item.operator,
                item.value,
                field_path=f"{path}.{item.operator}",
                label=resolved.label,
            )
        checked.append((resolved, item.operator, value))
    return checked


def _require_refs(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    plan: _CompactPlan,
    checked: Sequence[tuple[_Field, QueryFilterOperator, object]],
) -> None:
    """A Subject a filter names by reference or by id must exist; a wrong one refuses."""

    wanted: list[tuple[str, tuple[str, ...], str, int]] = []
    for index, (item, operator, value) in enumerate(checked):
        if operator not in {"eq", "ne", "in"}:
            continue
        values = cast(tuple[object, ...], value) if operator == "in" else (value,)
        if isinstance(item.info, str):
            kinds = (
                (plan.kind,) if item.binding == ROOT else plan.follows[item.binding].target_kinds
            )
            wanted.extend((str(entry), kinds, "id", index) for entry in values)
        elif item.info.value_type == "subject":
            wanted.extend(
                (str(entry), (str(entry).split("/", 1)[0],), "ref", index) for entry in values
            )
    if not wanted:
        return
    with instance.bind_accepted_projection(coordinate) as projection:
        connection = projection.typed.connection
        for value, kinds, form, index in wanted:
            marks = ",".join("?" for _ in kinds)
            known = {
                f"{kind}/{subject_id}"
                for kind, subject_id in connection.execute(
                    "SELECT subject_kind, subject_id FROM subjects "
                    f"WHERE subject_kind IN ({marks})",
                    kinds,
                )
            }
            spelled = value if form == "ref" else None
            if spelled is None:
                if any(f"{kind}/{value}" in known for kind in kinds):
                    continue
                names = [name.split("/", 1)[1] for name in known]
            else:
                if spelled in known:
                    continue
                names = sorted(known)
            raise query_refusal(
                "playbill.query.unknown_ref",
                f"no accepted Subject {value!r} of {' / '.join(kinds)} exists",
                nearest=nearest(value, names),
                repair="name an existing Subject (query the kind without where to list them)",
                field_path=f"where[{index}]",
            )


def _comparable(info: PredicateInfo | Literal["subject_id"], value: object) -> object:
    if isinstance(info, str):
        return value
    if info.value_type == "decimal":
        from decimal import Decimal

        try:
            return Decimal(str(value))
        except ArithmeticError:  # pragma: no cover - checked values are numeric
            return value
    if info.value_type == "timestamp" and isinstance(value, str):
        from cruxible_core.service.discovery.query_vocabulary import _parse_instant

        return _parse_instant(value) or value
    return value


def _contested(cardinality: str, values: Sequence[object]) -> bool:
    """A one-value slot holding more than one live value: no value filter matches it."""

    return cardinality == "one" and len(distinct(values)) > 1


def _inline_matches(item: _InlineFilter, values: Sequence[object]) -> bool:
    operator = item.operator
    if operator == "contains":
        needle = str(item.value).casefold()
        return any(isinstance(value, str) and needle in value.casefold() for value in values)
    info = item.field.info
    typed = [_comparable(info, value) for value in values]
    if operator == "in":
        wanted = {
            _member_key(_comparable(info, entry)) for entry in cast(tuple[object, ...], item.value)
        }
        return any(_member_key(value) in wanted for value in typed)
    target = _comparable(info, item.value)
    if operator == "eq":
        return any(value == target for value in typed)
    if operator == "ne":
        return not any(value == target for value in typed)
    return any(_ordered_match(operator, value, target) for value in typed)


def _ordered_match(operator: str, left: Any, right: Any) -> bool:
    try:
        if operator == "lt":
            return bool(left < right)
        if operator == "lte":
            return bool(left <= right)
        if operator == "gt":
            return bool(left > right)
        return bool(left >= right)
    except TypeError:
        return False


def _member_key(value: object) -> tuple[str, object]:
    """A hashable membership key over comparable forms: instants, decimals, text.

    The key is (type tag, form), so no two types meet: a boolean stays apart from
    the integers it would otherwise equal, and a JSON array or object is keyed by
    its sorted JSON text under its own tag, never equal to a string spelling it.
    """

    if isinstance(value, bool):
        return ("boolean", value)
    try:
        hash(value)
    except TypeError:
        return ("json", canonical_json(value, default=repr))
    return ("scalar", value)


def _pins(vocabulary: QueryVocabulary, predicates: Sequence[str]) -> tuple[ArtifactPin, ...]:
    pins = [
        ArtifactPin(
            role=CLAIM_TYPE_PIN_ROLE,
            target=ArtifactIdentity(kind="ClaimType", name=predicate),
            artifact_digest=vocabulary.predicates[predicate].claim_type_digest,
        )
        for predicate in predicates
    ]
    return tuple(
        sorted(
            pins,
            key=lambda pin: (
                pin.role.encode("utf-8"),
                pin.target.qualified.encode("utf-8"),
                pin.artifact_digest.encode("ascii"),
            ),
        )
    )


def _accepted(query: QueryDefinitionV1) -> AcceptedQueryDefinitionV1:
    strict = QueryDefinitionV1.model_validate(query.model_dump(mode="json"))
    return AcceptedQueryDefinitionV1(
        path=query_definition_path(strict.identity.name),
        query=strict,
        artifact_digest=query_definition_digest(strict).tagged,
    )


def _refuse_engine(result: ClaimQueryResultV1, *, declared: Sequence[str] = ()) -> None:
    if result.refusal is None:
        return
    code = result.refusal.code
    repair = None
    if code in {
        "playbill.query.parameter_undeclared",
        "playbill.query.parameter_missing",
        "playbill.query.parameter_type_mismatch",
    }:
        repair = f"pass params named {', '.join(declared)}" if declared else "pass no params"
    raise query_refusal(code, result.refusal.message, nearest=declared, repair=repair)


def _server_budgets(budgets: QueryBudgetsV1, ceiling: int) -> QueryBudgetsV1:
    """A definition's own budgets held under the query surface's server ceiling."""

    return budgets.model_copy(
        update={
            "max_results": min(budgets.max_results, ceiling),
            "max_paths": None if budgets.max_paths is None else min(budgets.max_paths, ceiling),
            "max_paths_per_result": (
                None
                if budgets.max_paths_per_result is None
                else min(budgets.max_paths_per_result, ceiling)
            ),
        }
    )


def _capped(result: ClaimQueryResultV1) -> tuple[tuple[str, ...], tuple[str, ...]]:
    clipped = tuple(
        item for item in result.truncation.clipped_budgets if item != "include_max_items"
    )
    if not clipped:
        return (), ()
    limits = {
        "max_results": result.budgets.max_results,
        "max_paths": result.budgets.max_paths,
        "max_paths_per_result": result.budgets.max_paths_per_result,
    }
    capped = tuple(f"{name}={limits.get(name)}" for name in clipped)
    note = (
        f"the answer hit the server cap {', '.join(capped)}; rows past it are not listed; "
        "narrow the query with where"
    )
    return capped, (note,)


def _default_columns(
    vocabulary: QueryVocabulary, kind: str
) -> tuple[list[PredicateInfo], tuple[str, ...]]:
    def shown_as(info: PredicateInfo) -> str:
        return vocabulary.field_name(info, (kind,))

    predicates = sorted(
        vocabulary.predicates_of(kind), key=lambda info: (shown_as(info), info.predicate)
    )
    shown = predicates[:DEFAULT_COLUMN_CAP]
    left_out = tuple(shown_as(info) for info in predicates[DEFAULT_COLUMN_CAP:])
    notes: tuple[str, ...] = ()
    if left_out:
        notes = (
            f"showing {DEFAULT_COLUMN_CAP} of {len(predicates)} predicates; left out: "
            f"{', '.join(left_out)} (name them in select)",
        )
    return shown, notes


@dataclass
class _RowRenderer:
    """Render rows of bound Subjects into values and flags, reading only one page."""

    instance: PlaybillInstance
    coordinate: AcceptedProjectionCoordinate
    vocabulary: QueryVocabulary
    evaluation_time: datetime
    columns: Sequence[_Column]
    content: ExactContentReader
    values: ValueIndex = field(default_factory=ValueIndex)

    def render(
        self,
        rows: Sequence[dict[str, str | None]],
        *,
        extra_flags: Sequence[set[QueryFlag]] | None = None,
    ) -> list[dict[str, Any]]:
        by_binding: dict[str, set[str]] = {}
        predicates: dict[str, set[str]] = {}
        for column in self.columns:
            if column.field is not None and column.field.predicate is not None:
                predicates.setdefault(column.binding, set()).add(column.field.predicate)
        for row in rows:
            for binding, path in row.items():
                if path is not None:
                    by_binding.setdefault(binding, set()).add(path)
        paths = {path for bound in by_binding.values() for path in bound}
        for binding, wanted in predicates.items():
            ensure_values(
                self.values,
                self.instance,
                self.coordinate,
                paths=by_binding.get(binding, set()),
                predicates=wanted,
            )
        with self.instance.bind_accepted_projection(self.coordinate) as projection:
            labels = subject_labels(projection.typed.connection, paths)
        shown: list[LiveValue] = []
        for row in rows:
            for column in self.columns:
                path = row.get(column.binding)
                if path is None or column.field is None or column.field.predicate is None:
                    continue
                shown.extend(self.values.slot(path, column.field.predicate))
        flags = claim_flags(
            self.instance,
            self.coordinate,
            identities=[item.identity for item in shown],
            evaluation_time=self.evaluation_time,
        )
        rendered: list[dict[str, Any]] = []
        for position, row in enumerate(rows):
            root = row.get(ROOT)
            label = labels.get(root or "", root or "")
            out: dict[str, Any] = {
                "subject": label,
                "subject_id": label.split("/", 1)[1] if "/" in label else label,
            }
            row_flags: set[QueryFlag] = set(extra_flags[position]) if extra_flags else set()
            for column in self.columns:
                path = row.get(column.binding)
                if column.field is None:
                    out[column.name] = None if path is None else labels.get(path, path)
                    continue
                if isinstance(column.field.info, str):
                    bound = None if path is None else labels.get(path, path)
                    out[column.name] = None if bound is None else bound.split("/", 1)[-1]
                    continue
                info = column.field.info
                slot = [] if path is None else self.values.slot(path, info.predicate)
                values = distinct(_value_identity(item) for item in slot)
                for item in slot:
                    row_flags.update(flags.get(item.identity, ()))
                row_flags.update(answer_flags(info.cardinality, len(values)))
                listed = info.cardinality == "many" or len(values) > 1
                if info.value_type == "exact_content":
                    values = _exact_values(self.content, values)
                if listed:
                    out[column.name] = values
                else:
                    out[column.name] = values[0] if values else None
            out["flags"] = ordered_flags(row_flags)
            rendered.append(out)
        return rendered


def _value_identity(item: LiveValue) -> object:
    """What makes two live values one value.

    A literal or Subject value is itself. An exact-content value is its digest
    AND the span it states: two Claims selecting different spans of one body are
    two values, through deduplication, cardinality, contest and rendering.
    """

    if item.exact:
        return {
            "content_digest": str(item.value),
            "span": None if item.span is None else list(item.span),
        }
    return item.value


def _exact_values(content: ExactContentReader, identities: Sequence[object]) -> list[object]:
    """Distinct exact-content identities as their text, or the marker in its place."""

    shown: list[object] = []
    for identity in identities:
        assert isinstance(identity, Mapping)
        span = identity["span"]
        shown.append(
            content.value(
                str(identity["content_digest"]), None if span is None else (span[0], span[1])
            )
        )
    return shown


def _searchable(item: LiveValue, content: ExactContentReader) -> str | None:
    """The text ``contains`` matches in a value: its string, or an exact value's text."""

    if item.exact:
        return content.text(str(item.value), item.span)
    return item.value if isinstance(item.value, str) else None


def _compact_columns(
    plan: _CompactPlan, request: PlaybillQueryRequestV1
) -> tuple[list[_Column], list[PlaybillQueryColumnV1], tuple[str, ...]]:
    """The columns a compact query serves, each under its own row key."""

    wanted: dict[tuple[str, ...], _Wanted] = {}

    def want(item: _Field | _Follow, names: tuple[str, ...]) -> None:
        if isinstance(item, _Follow):
            owner: tuple[str, ...] = ("follow", item.alias)
        else:
            owner = ("field", item.binding, item.predicate or SUBJECT_ID_FIELD)
        wanted.setdefault(owner, _Wanted(owner=owner, names=names, item=item))

    notes: tuple[str, ...] = ()
    if request.select:
        for index, name in enumerate(request.select):
            if name in plan.follows:
                want(plan.follows[name], (name,))
                continue
            resolved = plan.field(name, field_path=f"select[{index}]")
            if resolved.binding == ROOT and isinstance(resolved.info, str):
                continue
            if isinstance(resolved.info, str):
                want(resolved, (plan.column_name(resolved),))
                continue
            full = resolved.info.predicate
            if resolved.binding != ROOT:
                full = f"{resolved.binding}.{full}"
            want(resolved, (plan.column_name(resolved), full))
    else:
        shown, notes = _default_columns(plan.vocabulary, plan.kind)
        for info in shown:
            name = plan.vocabulary.field_name(info, (plan.kind,))
            want(
                _Field(name=name, binding=ROOT, info=info, label=info.predicate),
                (name, info.predicate),
            )
        for follow in plan.follows.values():
            want(follow, (follow.alias,))
    items = list(wanted.values())
    columns: list[_Column] = []
    output: list[PlaybillQueryColumnV1] = []
    for item, key in zip(items, _column_keys(items), strict=True):
        if isinstance(item.item, _Follow):
            columns.append(_Column(name=key, binding=item.item.alias, field=None))
        else:
            columns.append(_Column(name=key, binding=item.item.binding, field=item.item))
        output.append(_column(item.item, name=key))
    return columns, output, notes


def _compact_subject_query(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    vocabulary: QueryVocabulary,
    request: PlaybillQueryRequestV1,
    evaluation_time: datetime,
    *,
    content: ExactContentReader,
) -> _Answer:
    plan = _CompactPlan(vocabulary, request)
    checked = _checked_filters(plan, request.where)
    _require_refs(instance, coordinate, plan, checked)
    lowered: list[GrammarFilter] = []
    inline: list[_InlineFilter] = []
    for resolved, operator, value in checked:
        if _lowerable(resolved, operator):
            lowered.append(_lower_filter(resolved, operator, value))
        else:
            inline.append(_InlineFilter(field=resolved, operator=operator, value=value))
    orderings: list[QueryOrderingV1] = []
    for index, raw in enumerate(request.order_by):
        path = f"order_by[{index}]"
        descending = raw.startswith("-")
        resolved = plan.field(raw.removeprefix("-").removeprefix("+"), field_path=path)
        if not isinstance(resolved.info, str) and (
            resolved.info.cardinality != "one" or resolved.info.value_type not in ORDERABLE_TYPES
        ):
            raise query_refusal(
                "playbill.query.order_not_applicable",
                f"cannot order by {resolved.label} "
                f"({resolved.info.cardinality}-valued {resolved.info.value_type})",
                repair="order by a one-valued scalar predicate or subject_id",
                field_path=path,
            )
        orderings.append(
            QueryOrderingV1(
                key=_value_ref(resolved),
                direction="descending" if descending else "ascending",
                value_type=_engine_type(resolved),
            )
        )
    kind_predicates = vocabulary.predicates_of(plan.kind)
    columns, output, notes = _compact_columns(plan, request)

    follows = tuple(plan.follows.values())
    path_shape = bool(follows)
    budgets = QueryBudgetsV1(
        max_results=COMPACT_QUERY_MAX_RESULTS,
        max_traversal_depth=len(follows),
        max_paths=COMPACT_QUERY_MAX_RESULTS if path_shape else None,
        max_paths_per_result=COMPACT_QUERY_MAX_RESULTS if path_shape else None,
    )
    ordering_keys = {canonical_bytes(item.key.model_dump(mode="json")) for item in orderings}
    if len(ordering_keys) != len(orderings):
        raise query_refusal(
            "playbill.query.order_repeated",
            "order_by names the same field twice",
            repair="name each field once",
            field_path="order_by",
        )
    draft: dict[str, Any] = {
        "artifact_format": "playbill-query-definition-v1",
        "identity": ArtifactIdentity(kind="QueryDefinition", name=INLINE_DEFINITION_NAME),
        "entry": QueryEntryV1(binding=ROOT, subject_kinds=(plan.kind,)),
        "traversal": tuple(
            QueryTraversalStepV1(
                binding=follow.alias,
                from_binding=ROOT,
                predicate=follow.info.predicate,
                direction=follow.direction,
                required=False,
                target_subject_kinds=follow.lowered_targets,
            )
            for follow in follows
        ),
        "where": _all_of(lowered),
        "result_binding": ROOT,
        "result_shape": "path" if path_shape else "subject",
        "result_cardinality": "many",
        "dedupe": "path" if path_shape else "subject",
        "orderings": tuple(orderings),
        "evaluation_policy": QueryEvaluationPolicyV1(
            visible_verdicts=_ALL_VERDICTS,
            visible_currency=_ALL_CURRENCY,
            conflict_behavior="surface_conflicts",
        ),
        "default_budgets": budgets,
        "maximum_budgets": budgets,
    }
    unpinned = QueryDefinitionSpecV1(**draft)
    spec = QueryDefinitionSpecV1(
        **{**draft, "pins": _pins(vocabulary, unpinned.referenced_predicates)}
    )
    definition = _accepted(spec)
    result = evaluate_accepted_query(
        instance,
        definition,
        coordinate=coordinate,
        evaluation_time=evaluation_time,
    )
    _refuse_engine(result)
    capped, cap_notes = _capped(result)
    bindings = (ROOT, *(follow.alias for follow in follows))
    candidates, _keys = _bound_rows(result.rows, bindings)
    renderer = _RowRenderer(
        instance=instance,
        coordinate=coordinate,
        vocabulary=vocabulary,
        evaluation_time=evaluation_time,
        columns=columns,
        content=content,
    )
    if inline or request.contains is not None:
        candidates = _apply_inline(
            instance,
            coordinate,
            renderer.values,
            candidates,
            inline,
            content=content,
            contains=request.contains,
            kind_predicates=tuple(info.predicate for info in kind_predicates),
            cardinality_of={
                predicate: info.cardinality for predicate, info in vocabulary.predicates.items()
            },
        )
    keys = [tuple(row.get(binding) or "" for binding in bindings) for row in candidates]
    if follows and not orderings:
        # Rows about one Subject belong together: with no order_by, a follow
        # answer sorts by the queried Subject, then each follow alias in request
        # order (an unbound alias first). The key is the page key, so cursors
        # continue this same total order.
        order = sorted(range(len(keys)), key=keys.__getitem__)
        candidates = [candidates[index] for index in order]
        keys = [keys[index] for index in order]
    return _Answer(
        mode="inline",
        kind=plan.kind,
        spec_digest=definition.artifact_digest,
        columns=tuple(output),
        candidates=candidates,
        keys=keys,
        render=lambda page: renderer.render(cast(Sequence[dict[str, str | None]], page)),
        capped=capped,
        notes=(*notes, *cap_notes),
    )


def _bound_rows(
    rows: Sequence[Any], bindings: tuple[str, ...]
) -> tuple[list[dict[str, str | None]], list[tuple[str, ...]]]:
    """One row per bound Subject and followed target, in the evaluator's order.

    Two relation Claims can bind the same (Subject, target) pair, which the
    evaluator reports as two paths. A row shows the bound Subjects, not the
    relation Claims, so the pair is one row; its bindings are its page key.
    """

    candidates: list[dict[str, str | None]] = []
    keys: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for row in rows:
        bound = {binding.binding: binding.subject_path for binding in row.bindings}
        key = tuple(bound.get(binding) or "" for binding in bindings)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(bound)
        keys.append(key)
    return candidates, keys


def _apply_inline(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    values: ValueIndex,
    candidates: list[dict[str, str | None]],
    inline: Sequence[_InlineFilter],
    *,
    content: ExactContentReader,
    contains: str | None,
    kind_predicates: tuple[str, ...],
    cardinality_of: Mapping[str, str],
) -> list[dict[str, str | None]]:
    wanted: dict[str, set[str]] = {}
    for item in inline:
        if item.field.predicate is not None:
            wanted.setdefault(item.field.binding, set()).add(item.field.predicate)
    if contains is not None:
        wanted.setdefault(ROOT, set()).update(kind_predicates)
    for binding, predicates in wanted.items():
        ensure_values(
            values,
            instance,
            coordinate,
            paths={row[binding] for row in candidates if row.get(binding) is not None},  # type: ignore[misc]
            predicates=predicates,
        )
    labels: dict[str, str] = {}
    if any(isinstance(item.field.info, str) for item in inline):
        with instance.bind_accepted_projection(coordinate) as projection:
            labels = subject_labels(
                projection.typed.connection,
                {path for row in candidates for path in row.values() if path is not None},
            )
    needle = None if contains is None else contains.casefold()
    kept: list[dict[str, str | None]] = []
    for row in candidates:
        matched = True
        for item in inline:
            path = row.get(item.field.binding)
            if isinstance(item.field.info, str):
                label = None if path is None else labels.get(path)
                cell: list[object] = [] if label is None else [label.split("/", 1)[1]]
            else:
                cell = (
                    []
                    if path is None
                    else [value.value for value in values.slot(path, item.field.info.predicate)]
                )
                identities = (
                    []
                    if path is None
                    else [
                        _value_identity(value)
                        for value in values.slot(path, item.field.info.predicate)
                    ]
                )
                if _contested(item.field.info.cardinality, identities):
                    matched = False
                    break
            if not _inline_matches(item, cell):
                matched = False
                break
        if matched and needle is not None:
            root = row.get(ROOT)
            matched = root is not None and any(
                (text := _searchable(value, content)) is not None
                and needle in text.casefold()
                and not _contested(
                    cardinality_of.get(value.predicate, "many"),
                    [_value_identity(mate) for mate in values.slot(root, value.predicate)],
                )
                for value in values.subject(root)
            )
        if matched:
            kept.append(row)
    return kept


# -- contains across every kind ------------------------------------------------


def _contains_everywhere(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    vocabulary: QueryVocabulary,
    request: PlaybillQueryRequestV1,
    evaluation_time: datetime,
    *,
    content: ExactContentReader,
) -> _Answer:
    assert request.contains is not None
    needle = request.contains.casefold()
    matches = [
        item
        for item in read_live_values(instance, coordinate, subject_paths=None, predicates=None)
        if (text := _searchable(item, content)) is not None and needle in text.casefold()
    ]
    matches.sort(key=lambda item: (item.subject_path, item.predicate, item.identity))
    spec_digest = typed_digest(
        Sha256Value, _CONTAINS_DIGEST_DOMAIN, {"contains": request.contains}
    ).tagged

    def render(page: Sequence[Any]) -> list[dict[str, Any]]:
        items = cast(Sequence[LiveValue], page)
        slots = sorted({(item.subject_path, item.predicate) for item in items})
        slot_values = ValueIndex()
        for path, predicate in slots:
            ensure_values(slot_values, instance, coordinate, paths=(path,), predicates=(predicate,))
        mates = [value for path, predicate in slots for value in slot_values.slot(path, predicate)]
        flags = claim_flags(
            instance,
            coordinate,
            identities=[item.identity for item in mates],
            evaluation_time=evaluation_time,
        )
        with instance.bind_accepted_projection(coordinate) as projection:
            labels = subject_labels(
                projection.typed.connection, {item.subject_path for item in items}
            )
        rows: list[dict[str, Any]] = []
        for item in items:
            label = labels.get(item.subject_path, item.subject_path)
            marks = set(flags.get(item.identity, ()))
            info = vocabulary.predicates.get(item.predicate)
            slot = slot_values.slot(item.subject_path, item.predicate)
            if info is not None:
                marks.update(
                    answer_flags(
                        info.cardinality, len(distinct(_value_identity(value) for value in slot))
                    )
                )
            row: dict[str, Any] = {
                "subject": label,
                "subject_id": label.split("/", 1)[-1],
                "kind": label.split("/", 1)[0],
                "predicate": item.predicate,
                "value": item.value,
                "claim": item.identity.removeprefix("Claim:"),
            }
            if item.exact:
                row["value"] = content.value(str(item.value), item.span)
            rows.append({**row, "flags": ordered_flags(marks)})
        return rows

    return _Answer(
        mode="inline",
        kind=None,
        spec_digest=spec_digest,
        columns=(
            PlaybillQueryColumnV1(name="kind", type="string"),
            PlaybillQueryColumnV1(name="predicate", type="string"),
            PlaybillQueryColumnV1(name="value", type="string"),
            PlaybillQueryColumnV1(name="claim", type="string"),
        ),
        candidates=matches,
        keys=[(item.identity,) for item in matches],
        render=render,
    )


# -- ClaimType and Procedure definitions ---------------------------------------

_ARTIFACT_FIELDS: dict[str, tuple[str, ...]] = {
    "ClaimType": ("namespace", "name", "subject_kind"),
    "Procedure": ("namespace", "name"),
}
_ARTIFACT_COLUMNS: dict[str, tuple[PlaybillQueryColumnV1, ...]] = {
    "ClaimType": (
        PlaybillQueryColumnV1(name="predicate", type="string"),
        PlaybillQueryColumnV1(name="subject_kinds", type="string", cardinality="many"),
        PlaybillQueryColumnV1(name="object", type="string"),
        PlaybillQueryColumnV1(
            name="cardinality", type="enum", members=("many", "one"), cardinality="one"
        ),
        PlaybillQueryColumnV1(name="members", type="string", cardinality="many"),
        PlaybillQueryColumnV1(name="description", type="string"),
        PlaybillQueryColumnV1(name="evidence", type="string", cardinality="many"),
    ),
    "Procedure": (
        PlaybillQueryColumnV1(name="name", type="string"),
        PlaybillQueryColumnV1(
            name="runnable",
            type="enum",
            members=("binding_required", "directly_runnable"),
        ),
    ),
}


def _namespace(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name else ""


def _artifact_facets(kind: str, row: dict[str, Any]) -> dict[str, list[object]]:
    name = str(row["predicate"] if kind == "ClaimType" else row["name"])
    facets: dict[str, list[object]] = {"name": [name], "namespace": [_namespace(name)]}
    if kind == "ClaimType":
        facets["subject_kind"] = list(row.get("subject_kinds", ()))
    return facets


def _artifact_answer(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    vocabulary: QueryVocabulary,
    *,
    kind: str,
    definition: AcceptedQueryDefinitionV1,
    evaluation_time: datetime,
    mode: QueryMode,
    request: PlaybillQueryRequestV1 | None,
    budgets: QueryBudgetsV1 | None = None,
) -> _Answer:
    result = evaluate_accepted_query(
        instance,
        definition,
        coordinate=coordinate,
        evaluation_time=evaluation_time,
        budgets=budgets,
    )
    _refuse_engine(result)
    capped, cap_notes = _capped(result)
    contracts = CaptureContractNames(instance, coordinate)
    runnable: dict[str, bool] = {}
    if kind == "Procedure":
        with instance.bind_accepted_projection(coordinate) as projection:
            runnable = {
                row.identity: row.directly_runnable
                for row in projection.typed.procedure_inventory()
            }
    rows: list[dict[str, Any]] = []
    identities: list[str] = []
    for item in result.rows:
        assert item.artifact is not None
        source = item.artifact.definition
        if isinstance(source, ClaimType):
            info = vocabulary.predicates.get(source.predicate)
            if info is None:
                value_type, members = value_type_of(source)
                info = PredicateInfo(
                    predicate=source.predicate,
                    claim_type=source,
                    claim_type_digest=item.artifact.artifact_digest,
                    value_type=value_type,
                    members=members,
                    cardinality=source.cardinality,
                    subject_kinds=tuple(source.allowed_subject_kinds),
                    object_kinds=tuple(source.allowed_object_subject_kinds),
                )
            rows.append(claim_type_row(info, contracts))
        else:
            assert isinstance(source, (ProcedureArtifactV1, ProcedureArtifactV2))
            rows.append(
                {
                    "name": source.identity.name,
                    "runnable": (
                        "directly_runnable"
                        if runnable.get(source.identity.qualified)
                        else "binding_required"
                    ),
                }
            )
        identities.append(item.artifact.identity)
    columns = _ARTIFACT_COLUMNS[kind]
    if request is not None:
        rows, identities, columns = _shape_artifact_rows(kind, rows, identities, request)
    return _Answer(
        mode=mode,
        kind=kind,
        spec_digest=definition.artifact_digest,
        columns=columns,
        candidates=rows,
        keys=[(identity,) for identity in identities],
        render=lambda page: [dict(row) for row in page],
        capped=capped,
        notes=cap_notes,
    )


def _shape_artifact_rows(
    kind: str,
    rows: list[dict[str, Any]],
    identities: list[str],
    request: PlaybillQueryRequestV1,
) -> tuple[list[dict[str, Any]], list[str], tuple[PlaybillQueryColumnV1, ...]]:
    fields = _ARTIFACT_FIELDS[kind]
    checks: list[tuple[str, QueryFilterOperator, object]] = []
    for index, item in enumerate(request.where):
        path = f"where[{index}]"
        if item.field not in fields:
            raise query_refusal(
                "playbill.query.unknown_field",
                f"{kind} definitions have no field {item.field!r}",
                nearest=nearest(item.field, fields) or fields,
                repair=f"filter {kind} on {', '.join(fields)}",
                field_path=f"{path}.field",
            )
        if item.operator not in {"eq", "ne", "in", "contains"}:
            raise query_refusal(
                "playbill.query.operator_not_applicable",
                f"{item.operator!r} does not apply to {kind} {item.field}",
                nearest=("contains", "eq", "in", "ne"),
                repair="use eq, ne, in or contains",
                field_path=path,
            )
        value = item.value
        if item.operator == "in":
            if not all(isinstance(entry, str) for entry in cast(tuple[object, ...], value)):
                raise query_refusal(
                    "playbill.query.value_type_mismatch",
                    f"{kind} {item.field} values are strings",
                    repair='pass strings, for example ["dev"]',
                    field_path=f"{path}.in",
                )
        elif not isinstance(value, str):
            raise query_refusal(
                "playbill.query.value_type_mismatch",
                f"{kind} {item.field} values are strings",
                repair='pass a string, for example "dev"',
                field_path=f"{path}.{item.operator}",
            )
        checks.append((item.field, item.operator, value))
    needle = None if request.contains is None else request.contains.casefold()
    kept_rows: list[dict[str, Any]] = []
    kept_ids: list[str] = []
    for row, identity in zip(rows, identities, strict=True):
        facets = _artifact_facets(kind, row)
        matched = True
        for name, operator, value in checks:
            cell = facets.get(name, [])
            if operator == "eq":
                matched = value in cell
            elif operator == "ne":
                matched = value not in cell
            elif operator == "in":
                matched = any(entry in cell for entry in cast(tuple[object, ...], value))
            else:
                matched = any(str(value).casefold() in str(entry).casefold() for entry in cell)
            if not matched:
                break
        if matched and needle is not None:
            texts: list[str] = []
            for cell in row.values():
                texts.extend(str(entry) for entry in (cell if isinstance(cell, list) else [cell]))
            matched = any(needle in text.casefold() for text in texts)
        if matched:
            kept_rows.append(row)
            kept_ids.append(identity)
    columns = _ARTIFACT_COLUMNS[kind]
    names = [column.name for column in columns]
    for index, raw in enumerate(request.order_by):
        name = raw.removeprefix("-").removeprefix("+")
        if name not in names:
            raise query_refusal(
                "playbill.query.unknown_field",
                f"{kind} rows have no column {name!r}",
                nearest=nearest(name, names) or tuple(names),
                repair=f"order by one of {', '.join(names)}",
                field_path=f"order_by[{index}]",
            )
    for raw in reversed(request.order_by):
        name = raw.removeprefix("-").removeprefix("+")
        order = sorted(
            range(len(kept_rows)),
            key=lambda position: str(kept_rows[position].get(name, "")),
            reverse=raw.startswith("-"),
        )
        kept_rows = [kept_rows[position] for position in order]
        kept_ids = [kept_ids[position] for position in order]
    if request.select:
        chosen: list[PlaybillQueryColumnV1] = []
        for index, name in enumerate(request.select):
            match = next((column for column in columns if column.name == name), None)
            if match is None:
                raise query_refusal(
                    "playbill.query.unknown_field",
                    f"{kind} rows have no column {name!r}",
                    nearest=nearest(name, names) or tuple(names),
                    repair=f"select from {', '.join(names)}",
                    field_path=f"select[{index}]",
                )
            chosen.append(match)
        key = "predicate" if kind == "ClaimType" else "name"
        selected = {column.name for column in chosen} | {key}
        kept_rows = [
            {name: value for name, value in row.items() if name in selected} for row in kept_rows
        ]
        columns = tuple(chosen)
    return kept_rows, kept_ids, columns


def _require_artifact_names(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    vocabulary: QueryVocabulary,
    request: PlaybillQueryRequestV1,
) -> None:
    """A definition filter naming a namespace, name or kind that is not accepted refuses."""

    if request.kind == "ClaimType":
        names = set(vocabulary.predicates)
        known = {
            "name": names,
            "namespace": {_namespace(name) for name in names},
            "subject_kind": set(vocabulary.kinds),
        }
    else:
        with instance.bind_accepted_projection(coordinate) as projection:
            names = {
                row.identity.removeprefix("Procedure:")
                for row in projection.typed.procedure_inventory()
                if row.lifecycle == "live"
            }
        known = {"name": names, "namespace": {_namespace(name) for name in names}}
    for index, item in enumerate(request.where):
        if item.operator not in {"eq", "in"} or item.field not in known:
            continue
        values = cast(tuple[object, ...], item.value) if item.operator == "in" else (item.value,)
        for value in values:
            if isinstance(value, str) and value not in known[item.field]:
                raise query_refusal(
                    "playbill.query.unknown_ref",
                    f"no accepted {request.kind} has {item.field} {value!r}",
                    nearest=nearest(value, known[item.field]),
                    repair=f"use an accepted {item.field}; query kind={request.kind} lists them",
                    field_path=f"where[{index}]",
                )


def _artifact_definition(
    vocabulary: QueryVocabulary, request: PlaybillQueryRequestV1
) -> AcceptedQueryDefinitionV1:
    kind = cast(Literal["ClaimType", "Procedure"], request.kind)
    selection: Literal["all", "namespaces", "name_prefixes"] = "all"
    namespaces: tuple[str, ...] = ()
    prefixes: tuple[str, ...] = ()
    for item in request.where:
        if item.field != "namespace" or item.operator not in {"eq", "in"}:
            continue
        raw = cast(tuple[object, ...], item.value) if item.operator == "in" else (item.value,)
        names = tuple(sorted({str(entry) for entry in raw}, key=lambda value: value.encode()))
        try:
            if kind == "ClaimType":
                QueryArtifactsEntryV2(artifact_kind=kind, selection="namespaces", namespaces=names)
                selection, namespaces = "namespaces", names
            else:
                candidate = tuple(sorted(f"{name}." for name in names))
                QueryArtifactsEntryV2(
                    artifact_kind=kind, selection="name_prefixes", name_prefixes=candidate
                )
                selection, prefixes = "name_prefixes", candidate
        except ValueError:
            known = {_namespace(name) for name in vocabulary.predicates}
            raise query_refusal(
                "playbill.query.value_type_mismatch",
                f"{names!r} is not a {kind} namespace",
                nearest=nearest(names[0], known) if names else (),
                repair="pass a dotted lower-case namespace such as dev.roadmap_item",
                field_path="where",
            ) from None
        break
    budgets = QueryBudgetsV1(max_results=ARTIFACT_QUERY_MAX_RESULTS, max_traversal_depth=0)
    query = QueryDefinitionSpecV1(
        artifact_format="playbill-query-definition-v2",
        identity=ArtifactIdentity(kind="QueryDefinition", name=INLINE_DEFINITION_NAME),
        entry=QueryArtifactsEntryV2(
            artifact_kind=kind,
            selection=selection,
            namespaces=namespaces,
            name_prefixes=prefixes,
        ),
        result_binding="definition",
        result_shape="artifact_definition",
        result_cardinality="many",
        dedupe="artifact",
        evaluation_policy=QueryEvaluationPolicyV1(
            visible_verdicts=_ALL_VERDICTS,
            visible_currency=_ALL_CURRENCY,
            conflict_behavior="surface_conflicts",
        ),
        default_budgets=budgets,
        maximum_budgets=budgets,
    )
    return _accepted(query)


# -- spec and named queries -------------------------------------------------------


def _pinned_spec(vocabulary: QueryVocabulary, spec: QueryDefinitionSpecV1) -> QueryDefinitionV1:
    if isinstance(spec.entry, QueryEntryV1):
        for index, kind in enumerate(spec.entry.subject_kinds):
            vocabulary.require_kind(kind, field_path=f"spec.entry.subject_kinds[{index}]")
    referenced = spec.referenced_predicates
    for predicate in referenced:
        if predicate not in vocabulary.predicates:
            raise query_refusal(
                "playbill.query.unknown_field",
                f"the spec reads predicate {predicate!r}, which is not an accepted ClaimType",
                nearest=nearest(predicate, vocabulary.predicates),
                repair="use accepted predicates (query kind=ClaimType lists them)",
                field_path="spec",
            )
    explicit = {pin.target.name: pin for pin in spec.pins if pin.role == CLAIM_TYPE_PIN_ROLE}
    for predicate, pin in explicit.items():
        if pin.artifact_digest != vocabulary.predicates[predicate].claim_type_digest:
            raise query_refusal(
                "playbill.query.pin_stale",
                f"the spec pins ClaimType {predicate} at a version that is not accepted here",
                repair="omit ClaimType pins to resolve them at this coordinate",
                field_path="spec.pins",
            )
    others = tuple(pin for pin in spec.pins if pin.role != CLAIM_TYPE_PIN_ROLE)
    pins = tuple(
        sorted(
            (*others, *_pins(vocabulary, referenced)),
            key=lambda pin: (
                pin.role.encode("utf-8"),
                pin.target.qualified.encode("utf-8"),
                pin.artifact_digest.encode("ascii"),
            ),
        )
    )
    try:
        return QueryDefinitionV1.model_validate(
            {
                **spec.model_dump(mode="json"),
                "pins": [pin.model_dump(mode="json") for pin in pins],
            }
        )
    except ValueError as exc:
        raise query_refusal(
            "playbill.query.spec_invalid",
            f"the spec does not validate once pinned: {str(exc).splitlines()[0]}",
            repair="check the spec against QueryDefinitionSpecV1",
            field_path="spec",
        ) from exc


def _field_column(
    vocabulary: QueryVocabulary, name: str, ref: QueryValueRefV1
) -> PlaybillQueryColumnV1:
    if isinstance(ref, QueryClaimValueRefV1):
        info = vocabulary.predicates.get(ref.predicate)
        if info is not None:
            return PlaybillQueryColumnV1(
                name=name,
                predicate=info.predicate,
                type=object_label(info),
                members=info.members or None,
                cardinality=info.cardinality,
            )
        return PlaybillQueryColumnV1(name=name, predicate=ref.predicate, type="json")
    if isinstance(ref, QuerySubjectFieldRefV1):
        return PlaybillQueryColumnV1(name=name, type="string")
    if isinstance(ref, QueryEvaluationTimeRefV1):
        return PlaybillQueryColumnV1(name=name, type="timestamp")
    return PlaybillQueryColumnV1(name=name, type="json")


def _engine_answer(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    vocabulary: QueryVocabulary,
    *,
    definition: AcceptedQueryDefinitionV1,
    result: ClaimQueryResultV1,
    evaluation_time: datetime,
    mode: QueryMode,
    content: ExactContentReader,
) -> _Answer:
    """Shape a governed evaluation's rows as values and flags."""

    query = definition.query
    capped, cap_notes = _capped(result)
    projection = query.projection
    notes: tuple[str, ...] = ()
    renderer_columns: list[_Column] = []
    output: list[PlaybillQueryColumnV1] = []
    if projection is not None:
        output = [
            _field_column(vocabulary, _out(item.name), item.value) for item in projection.fields
        ]
    elif (
        isinstance(query.entry, QueryEntryV1)
        and len(query.entry.subject_kinds) == 1
        and (query.result_binding == query.entry.binding)
    ):
        kind = query.entry.subject_kinds[0]
        shown, notes = _default_columns(vocabulary, kind)
        wanted = [
            _Wanted(
                owner=(info.predicate,),
                names=(name, info.predicate),
                item=_Field(name=name, binding=ROOT, info=info, label=info.predicate),
            )
            for info in shown
            for name in (vocabulary.field_name(info, (kind,)),)
        ]
        for item, key in zip(wanted, _column_keys(wanted), strict=True):
            assert isinstance(item.item, _Field)
            renderer_columns.append(_Column(name=key, binding=ROOT, field=item.item))
            output.append(_column(item.item, name=key))
    renderer = _RowRenderer(
        instance=instance,
        coordinate=coordinate,
        vocabulary=vocabulary,
        evaluation_time=evaluation_time,
        columns=renderer_columns,
        content=content,
    )
    # A projected exact-content field reads as text too, from the live values of
    # the slot it projects, with its digests beside it.
    exact_fields: dict[str, QueryClaimValueRefV1] = {}
    if projection is not None:
        for projected_field in projection.fields:
            ref = projected_field.value
            if not isinstance(ref, QueryClaimValueRefV1):
                continue
            info = vocabulary.predicates.get(ref.predicate)
            if info is not None and info.value_type == "exact_content":
                exact_fields[_out(projected_field.name)] = ref
    candidates = list(result.rows)
    keys = [
        (
            *(binding.subject_path or "" for binding in row.bindings),
            *(item.claim_path for item in row.path),
        )
        for row in candidates
    ]

    def render(page: Sequence[Any]) -> list[dict[str, Any]]:
        rows = page
        bound = [{ROOT: _subject_path_of(row, query.result_binding)} for row in rows]
        extra: list[set[QueryFlag]] = []
        read_identities: set[str] = set()
        for row in rows:
            marks: set[QueryFlag] = set()
            for visibility in row.read_claims:
                marks.update(verdict_flags(visibility.verdict))
                read_identities.add(visibility.claim_path)
            if row.conflicts:
                marks.add("contested")
            extra.append(marks)
        by_path = _flags_for_paths(instance, coordinate, read_identities, evaluation_time)
        for row, marks in zip(rows, extra, strict=True):
            for item in row.read_claims:
                marks.update(by_path.get(item.claim_path, ()))
        for row, marks in zip(rows, extra, strict=True):
            if any(projected.state == "conflict" for projected in row.fields):
                marks.add("contested")
        rendered = renderer.render(bound, extra_flags=extra)
        for out, row in zip(rendered, rows, strict=True):
            flags = out.pop("flags")
            for projected in row.fields:
                value = projected.value if projected.state == "present" else None
                if isinstance(value, str) and value.startswith("Subject:"):
                    value = value.removeprefix("Subject:")
                out[_out(projected.name)] = value
            states = {_out(projected.name): projected.state for projected in row.fields}
            for key, ref in exact_fields.items():
                path = _subject_path_of(row, ref.binding)
                if path is None or states.get(key) != "present":
                    # The engine answered no value (absent), or surfaced a
                    # conflict exactly as it does for a literal (null plus the
                    # contested flag); the text never adds a value.
                    out[key] = None
                    continue
                ensure_values(
                    renderer.values,
                    instance,
                    coordinate,
                    paths=(path,),
                    predicates=(ref.predicate,),
                )
                # Only the Claims the engine read for this field under the
                # query's evaluation policy; never every live Claim in the slot.
                selected = _engine_selected(row, ref)
                slot = [
                    item
                    for item in renderer.values.slot(path, ref.predicate)
                    if item.identity in selected
                ]
                # A present value is the one distinct value the engine selected
                # (it keys exact content by digest and span), shown as its text.
                values = _exact_values(content, distinct(_value_identity(item) for item in slot))
                out[key] = values[0] if values else None
            out["flags"] = flags
        return rendered

    return _Answer(
        mode=mode,
        kind=(
            query.entry.subject_kinds[0]
            if isinstance(query.entry, QueryEntryV1) and len(query.entry.subject_kinds) == 1
            else None
        ),
        spec_digest=definition.artifact_digest,
        columns=tuple(output),
        candidates=candidates,
        keys=keys,
        render=render,
        capped=capped,
        notes=(*notes, *cap_notes),
    )


def _engine_selected(row: Any, ref: QueryClaimValueRefV1) -> frozenset[str]:
    """The Claim identities the engine read for one projected claim-value field."""

    subject = next(
        (item.subject_identity for item in row.bindings if item.binding == ref.binding), None
    )
    return frozenset(
        "Claim:" + PurePosixPath(item.claim_path).stem
        for item in row.read_claims
        if item.predicate == ref.predicate and item.subject_identity == subject
    )


def _subject_path_of(row: Any, binding: str) -> str | None:
    for item in row.bindings:
        if item.binding == binding:
            return cast(str | None, item.subject_path)
    return None


def _flags_for_paths(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    claim_paths: set[str],
    evaluation_time: datetime,
) -> dict[str, tuple[QueryFlag, ...]]:
    """The shared flags of the Claims a governed evaluation read, by Claim path.

    Flags are derived over every live contender of each slot those Claims sit
    in, so a slot's resolution status and its ``unsure`` holds are the whole
    slot's, exactly as ``next`` decides them.
    """

    if not claim_paths:
        return {}
    path_of: dict[str, str] = {}
    slots: set[tuple[str, str]] = set()
    ordered = sorted(claim_paths)
    with instance.bind_accepted_projection(coordinate) as projection:
        connection = projection.typed.connection
        for start in range(0, len(ordered), 400):
            chunk = ordered[start : start + 400]
            marks = ",".join("?" for _ in chunk)
            for identity, path, subject_path, predicate in connection.execute(
                "SELECT identity, path, subject_path, predicate FROM claims "
                f"WHERE path IN ({marks})",
                tuple(chunk),
            ):
                path_of[str(identity)] = str(path)
                slots.add((str(subject_path), str(predicate)))
        contenders = set(path_of)
        for subject_path, predicate in sorted(slots):
            contenders.update(
                str(row[0])
                for row in connection.execute(
                    "SELECT identity FROM claims WHERE lifecycle='live' "
                    "AND subject_path=? AND predicate=?",
                    (subject_path, predicate),
                )
            )
    flags = claim_flags(
        instance, coordinate, identities=contenders, evaluation_time=evaluation_time
    )
    return {path: flags.get(identity, ()) for identity, path in path_of.items()}


def _named_answer(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    vocabulary: QueryVocabulary,
    request: PlaybillQueryRequestV1,
    evaluation_time: datetime,
    *,
    content: ExactContentReader,
) -> _Answer:
    from cruxible_core.service.discovery.query import service_run_playbill_query

    assert request.name is not None
    with instance.bind_accepted_projection(coordinate) as projection:
        names = tuple(
            row.identity.removeprefix("QueryDefinition:")
            for row in projection.typed.envelopes(kind="query-definition")
        )
    if request.name not in names:
        raise query_not_found(request.name, nearest=nearest(request.name, names))
    from cruxible_core.service.discovery.query_definitions import accepted_query_definition

    definition = accepted_query_definition(instance, name=request.name, coordinate=coordinate)
    artifacts = isinstance(definition.query.entry, QueryArtifactsEntryV2)
    budgets = _server_budgets(
        definition.query.default_budgets,
        ARTIFACT_QUERY_MAX_RESULTS if artifacts else COMPACT_QUERY_MAX_RESULTS,
    )
    run = service_run_playbill_query(
        instance,
        name=request.name,
        evaluation_time=evaluation_time,
        parameters=dict(request.params or {}),
        at=PlaybillAcceptedCoordinate.from_internal(coordinate),
        budgets=budgets,
    )
    _refuse_engine(run.result, declared=tuple(item.name for item in definition.query.parameters))
    if isinstance(definition.query.entry, QueryArtifactsEntryV2):
        return _artifact_answer(
            instance,
            coordinate,
            vocabulary,
            kind=definition.query.entry.artifact_kind,
            definition=definition,
            evaluation_time=evaluation_time,
            mode="named",
            request=None,
            budgets=budgets,
        )
    return _engine_answer(
        instance,
        coordinate,
        vocabulary,
        definition=definition,
        result=run.result,
        evaluation_time=evaluation_time,
        mode="named",
        content=content,
    )


def _spec_answer(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    vocabulary: QueryVocabulary,
    request: PlaybillQueryRequestV1,
    evaluation_time: datetime,
    *,
    content: ExactContentReader,
) -> _Answer:
    assert request.spec is not None
    definition = _accepted(_pinned_spec(vocabulary, request.spec))
    # The server ceiling is an execution input, never part of the spec, so the
    # submitted spec keeps its digest while every budget is clipped.
    artifacts = isinstance(definition.query.entry, QueryArtifactsEntryV2)
    budgets = _server_budgets(
        definition.query.default_budgets,
        ARTIFACT_QUERY_MAX_RESULTS if artifacts else COMPACT_QUERY_MAX_RESULTS,
    )
    if isinstance(definition.query.entry, QueryArtifactsEntryV2):
        return _artifact_answer(
            instance,
            coordinate,
            vocabulary,
            kind=definition.query.entry.artifact_kind,
            definition=definition,
            evaluation_time=evaluation_time,
            mode="spec",
            request=None,
            budgets=budgets,
        )
    result = evaluate_accepted_query(
        instance,
        definition,
        coordinate=coordinate,
        evaluation_time=evaluation_time,
        budgets=budgets,
    )
    _refuse_engine(result, declared=tuple(item.name for item in definition.query.parameters))
    return _engine_answer(
        instance,
        coordinate,
        vocabulary,
        definition=definition,
        result=result,
        evaluation_time=evaluation_time,
        mode="spec",
        content=content,
    )


# -- the verb ---------------------------------------------------------------------


def _selection(request: PlaybillQueryRequestV1, mode: QueryMode) -> dict[str, Any]:
    """The digest of everything that shapes the listing, so a cursor binds to it compactly."""

    body = request.model_dump(
        mode="json", exclude={"cursor", "limit", "at", "evaluation_time"}, by_alias=True
    )
    body["mode"] = mode
    return {"query": typed_digest(Sha256Value, "playbill-query-selection-v1", body).tagged}


def service_playbill_query(
    instance: PlaybillInstance,
    *,
    request: PlaybillQueryRequestV1,
) -> PlaybillQueryResult:
    """Answer one ``query`` call: one page of values, flags and paging.

    Exact-content values read as their text, for every caller.
    """

    mode = _mode(request)
    selection = _selection(request, mode)
    continuation = (
        None
        if request.cursor is None
        else decode_list_cursor(request.cursor, list_name=LIST_NAME, selection=selection)
    )
    evaluation_time = request.evaluation_time
    at: AcceptedCoordinate | str | None = request.at
    if continuation is not None:
        pinned = AcceptedCoordinate.model_validate(continuation.coordinate["at"])
        if at is not None:
            requested = AcceptedCoordinate.from_internal(resolve_read_coordinate(instance, at))
            if requested != pinned:
                raise PlaybillListCursorMismatch(
                    f"{PlaybillListCursorMismatch.error_code}: the cursor continues a different "
                    "coordinate; query again without a cursor"
                )
        at = pinned
        pinned_time = datetime.fromisoformat(str(continuation.coordinate["evaluation_time"]))
        if evaluation_time is not None and evaluation_time != pinned_time:
            raise PlaybillListCursorStale(
                f"{PlaybillListCursorStale.error_code}: the cursor continues an answer "
                f"evaluated at {pinned_time.isoformat()}, not {evaluation_time.isoformat()}; "
                "omit evaluation_time to continue it, or query again without a cursor"
            )
        evaluation_time = pinned_time
    if evaluation_time is None:
        evaluation_time = utc_now()
    if evaluation_time.tzinfo is None or evaluation_time.utcoffset() is None:
        raise query_refusal(
            "playbill.query.evaluation_time_invalid",
            "evaluation_time must carry a timezone",
            repair="pass an ISO-8601 instant such as 2026-09-28T12:00:00Z",
            field_path="evaluation_time",
        )
    coordinate = resolve_read_coordinate(instance, at)
    vocabulary = load_query_vocabulary(instance, coordinate)
    content = ExactContentReader(instance)
    if mode == "named":
        answer = _named_answer(
            instance, coordinate, vocabulary, request, evaluation_time, content=content
        )
    elif mode == "spec":
        answer = _spec_answer(
            instance, coordinate, vocabulary, request, evaluation_time, content=content
        )
    elif request.kind in ARTIFACT_KINDS:
        if request.follow:
            raise query_refusal(
                "playbill.query.follow_not_relation",
                f"{request.kind} definitions have no relations to follow",
                repair="drop follow",
                field_path="follow",
            )
        _require_artifact_names(instance, coordinate, vocabulary, request)
        answer = _artifact_answer(
            instance,
            coordinate,
            vocabulary,
            kind=request.kind,
            definition=_artifact_definition(vocabulary, request),
            evaluation_time=evaluation_time,
            mode="inline",
            request=request,
        )
    elif request.kind is None:
        if request.where or request.select or request.follow or request.order_by:
            raise query_refusal(
                "playbill.query.mode_invalid",
                "where, select, follow and order_by need a kind",
                repair="pass kind, or search values with contains alone",
            )
        answer = _contains_everywhere(
            instance, coordinate, vocabulary, request, evaluation_time, content=content
        )
    else:
        answer = _compact_subject_query(
            instance, coordinate, vocabulary, request, evaluation_time, content=content
        )

    served = AcceptedCoordinate.from_internal(coordinate)
    snapshot = list_snapshot([list(key) for key in answer.keys])
    page, truncated = page_after_boundary(
        answer.candidates,
        keys=answer.keys,
        snapshot=snapshot,
        continuation=continuation,
        limit=request.limit,
        list_name=LIST_NAME,
    )
    start = 0 if continuation is None else list(answer.keys).index(continuation.last_key) + 1
    last_key = answer.keys[start + len(page) - 1] if page else None
    # Every row is bounded by get's card rule: a string over 500 characters is
    # cut to {value, truncated, length}; get(detail="evidence") reads it whole.
    rows = [
        {key: value if key == "flags" else summary_value(value) for key, value in row.items()}
        for row in answer.render(page)
    ]
    next_cursor = None
    if truncated and last_key is not None:
        next_cursor = encode_list_cursor(
            list_name=LIST_NAME,
            coordinate={
                "at": served.model_dump(mode="json"),
                "evaluation_time": evaluation_time.isoformat(),
            },
            selection=selection,
            snapshot=snapshot,
            last_key=last_key,
        )
    return PlaybillQueryResult(
        kind=answer.kind,
        columns=answer.columns,
        rows=tuple(rows),
        truncated=truncated or bool(answer.capped),
        next_cursor=next_cursor,
        capped=answer.capped,
        notes=answer.notes,
        receipt=PlaybillQueryReceiptV1(
            mode=answer.mode,
            spec_digest=answer.spec_digest,
            coordinate=served,
            evaluation_time=evaluation_time,
        ),
    )


__all__ = [
    "COMPACT_QUERY_MAX_RESULTS",
    "DEFAULT_COLUMN_CAP",
    "service_playbill_query",
]
