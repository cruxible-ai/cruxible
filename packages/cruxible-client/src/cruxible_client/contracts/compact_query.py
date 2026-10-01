"""Request and answer contracts for the ``query`` read verb.

``query`` answers any question over accepted state in one of three modes:

- **compact**: a Subject ``kind`` and/or free-text ``contains``, with optional
  ``where`` filters, ``select`` columns, one-hop ``follow`` relations (forward
  along the kind's own Subject-valued predicate, or reverse along another
  kind's predicate that points at it) and ``order_by`` keys. It lowers to a
  ``QueryDefinitionSpecV1`` and runs through the same evaluator as a governed
  QueryDefinition.
- **spec**: a full ``QueryDefinitionSpecV1`` evaluated inline.
- **name**: an accepted named QueryDefinition with its ``params``.

A filter is a union discriminated by its operator key, so the schema fixes the
value's shape: ``{field, eq: scalar}``, ``{field, in: [scalar, ...]}``,
``{field, exists: bool}``, ``{field, contains: str}``, and so on. Filters
combine as all-of.

The answer leads with values: typed ``columns``, compact ``rows`` whose cells
are values (an array for a many-valued predicate or a contested slot) plus the
verdict ``flags`` that apply, and top-level ``truncated``/``next_cursor``
paging. The ``receipt`` names the mode, the digest of the definition that ran,
the accepted coordinate and the evaluation time.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, ClassVar, Literal, Union

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, field_validator

from cruxible_client.contracts.claim_type_structure import ClaimRole
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.query.definitions import QueryDefinitionSpecV1
from cruxible_client.contracts.query.grammar import QueryBudgetsV1
from cruxible_client.contracts.query.results import ClaimQueryResultV1, QueryExecutionReceiptV1

PLAYBILL_QUERY_DEFAULT_LIMIT = 50
PLAYBILL_QUERY_MAX_LIMIT = 500
PLAYBILL_QUERY_MAX_FILTERS = 32
PLAYBILL_QUERY_MAX_FOLLOWS = 4
PLAYBILL_QUERY_MAX_SELECT = 64

QueryScalar = Union[str, int, bool]
"""One filter value. Dates, times and Subject references are strings."""

QueryParameterValue = Union[str, int, bool, None]
"""One named query parameter value. ``null`` binds an optional parameter
explicitly, which is not the same as omitting it (omission takes its default);
the accepted QueryDefinition decides whether a value is valid."""

QueryFilterOperator = Literal["eq", "ne", "lt", "lte", "gt", "gte", "in", "exists", "contains"]
QUERY_FILTER_OPERATORS: tuple[QueryFilterOperator, ...] = (
    "eq",
    "ne",
    "lt",
    "lte",
    "gt",
    "gte",
    "in",
    "exists",
    "contains",
)
QueryFlag = Literal["stale", "contested", "contradicted", "uncovered", "unsure_hold"]
#: Which Claims a compact query's cells show. ``live`` is each slot's answer as
#: ``get`` shows it: its accepted and conflicted Claims, or, when resolution
#: accepted none, every live Claim. ``overturned`` and ``refused`` add live
#: Claims resolution set aside; ``retired`` adds withdrawn ones.
QueryClaimStatus = Literal["live", "overturned", "refused", "retired"]
#: One Claim's own status: how resolution placed it in its slot, or ``retired``.
QueryCellClaimStatus = Literal["accepted", "conflicted", "overturned", "refused", "retired"]
#: How much of what ran a named query's receipt carries.
QueryReceiptDetail = Literal["compact", "full"]
QueryFollowDirection = Literal["forward", "reverse"]
QueryMode = Literal["inline", "named", "spec"]

_FIELD_DESCRIPTION = (
    "A short predicate name of the kind (adoption_state), a fully qualified predicate, "
    "subject_id, or alias.field after follow."
)


class _QueryFilterBase(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )

    operator: ClassVar[QueryFilterOperator]
    field: str = Field(min_length=1, max_length=256, description=_FIELD_DESCRIPTION)

    @property
    def value(self) -> object:
        return getattr(self, self.operator if self.operator != "in" else "in_")


class QueryFilterEqV1(_QueryFilterBase):
    """The value equals this one."""

    operator: ClassVar[QueryFilterOperator] = "eq"
    eq: QueryScalar


class QueryFilterNeV1(_QueryFilterBase):
    """No value equals this one; a Subject without the value matches."""

    operator: ClassVar[QueryFilterOperator] = "ne"
    ne: QueryScalar


class QueryFilterLtV1(_QueryFilterBase):
    operator: ClassVar[QueryFilterOperator] = "lt"
    lt: QueryScalar


class QueryFilterLteV1(_QueryFilterBase):
    operator: ClassVar[QueryFilterOperator] = "lte"
    lte: QueryScalar


class QueryFilterGtV1(_QueryFilterBase):
    operator: ClassVar[QueryFilterOperator] = "gt"
    gt: QueryScalar


class QueryFilterGteV1(_QueryFilterBase):
    operator: ClassVar[QueryFilterOperator] = "gte"
    gte: QueryScalar


class QueryFilterInV1(_QueryFilterBase):
    """The value is one of these."""

    operator: ClassVar[QueryFilterOperator] = "in"
    in_: tuple[QueryScalar, ...] = Field(alias="in", min_length=1, max_length=256)


class QueryFilterExistsV1(_QueryFilterBase):
    """The Subject carries (true) or lacks (false) a live Claim of this predicate."""

    operator: ClassVar[QueryFilterOperator] = "exists"
    exists: bool


class QueryFilterContainsV1(_QueryFilterBase):
    """Case-insensitive substring of a string value; compact queries only."""

    operator: ClassVar[QueryFilterOperator] = "contains"
    contains: str = Field(min_length=1, max_length=256)


def _filter_operator(value: object) -> str | None:
    if isinstance(value, _QueryFilterBase):
        return value.operator
    if isinstance(value, dict):
        named = [key for key in value if key in QUERY_FILTER_OPERATORS or key == "in_"]
        if len(named) == 1:
            return "in" if named[0] == "in_" else str(named[0])
    return None


QueryFilterV1 = Annotated[
    Union[
        Annotated[QueryFilterEqV1, Tag("eq")],
        Annotated[QueryFilterNeV1, Tag("ne")],
        Annotated[QueryFilterLtV1, Tag("lt")],
        Annotated[QueryFilterLteV1, Tag("lte")],
        Annotated[QueryFilterGtV1, Tag("gt")],
        Annotated[QueryFilterGteV1, Tag("gte")],
        Annotated[QueryFilterInV1, Tag("in")],
        Annotated[QueryFilterExistsV1, Tag("exists")],
        Annotated[QueryFilterContainsV1, Tag("contains")],
    ],
    Discriminator(
        _filter_operator,
        custom_error_type="query_filter_operator",
        custom_error_message=(
            "a filter names its field and exactly one operator "
            "(eq, ne, lt, lte, gt, gte, in, exists, contains), "
            'for example {"field": "adoption_state", "eq": "adopted"}'
        ),
    ),
]

_FILTER_MODELS: dict[str, type[_QueryFilterBase]] = {
    "eq": QueryFilterEqV1,
    "ne": QueryFilterNeV1,
    "lt": QueryFilterLtV1,
    "lte": QueryFilterLteV1,
    "gt": QueryFilterGtV1,
    "gte": QueryFilterGteV1,
    "in": QueryFilterInV1,
    "exists": QueryFilterExistsV1,
    "contains": QueryFilterContainsV1,
}


def query_filter(field: str, operator: str, value: object) -> QueryFilterV1:
    """Build one typed filter from a field, an operator name and its value."""

    model = _FILTER_MODELS.get(operator)
    if model is None:
        spelled = ", ".join(QUERY_FILTER_OPERATORS)
        raise ValueError(f"unknown query filter operator {operator!r}; use one of: {spelled}")
    key = "in" if operator == "in" else operator
    return model.model_validate({"field": field, key: value})  # type: ignore[return-value]


def _is_forward(value: object) -> bool:
    return value == "forward"


class QueryFollowV1(BaseModel):
    """One hop along a Subject-valued relation Claim, bound under an alias; one row per pair."""

    # ``reverse`` follows ANOTHER kind's predicate backwards: ``field`` names it
    # (in full, or short against the kind that carries it), its values must name
    # Subjects of the queried kind, and the alias binds the Subjects pointing
    # here. From ``dev.roadmap_item``: ``{"field": "dev.batch.delivers", "as":
    # "batch", "direction": "reverse"}``. ``alias.field`` then reads them.

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )

    field: str = Field(
        min_length=1,
        max_length=256,
        description=(
            "A Subject-valued predicate of the kind (forward), or of another kind whose "
            "values name this kind (reverse; orient kind=K lists them as incoming)."
        ),
    )
    as_: str = Field(
        alias="as",
        min_length=1,
        max_length=64,
        description="The alias later fields use as alias.field.",
    )
    direction: QueryFollowDirection = Field(
        default="forward",
        exclude_if=_is_forward,
        description="reverse follows another kind's predicate back to this kind.",
    )


def _is_none(value: object) -> bool:
    return value is None


class PlaybillQueryRequestV1(BaseModel):
    """One ``query`` call. Exactly one mode: compact (kind/contains), spec, or name.

    ``status`` and ``claims`` shape a compact Subject-kind query's cells;
    ``budgets`` and ``receipt`` apply to a named query.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str | None = Field(
        default=None,
        max_length=256,
        description="A Subject kind, or ClaimType / Procedure for definitions.",
    )
    where: tuple[QueryFilterV1, ...] = Field(default=(), max_length=PLAYBILL_QUERY_MAX_FILTERS)
    contains: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description="Case-insensitive text in any live Claim value; without kind, across kinds.",
    )
    select: tuple[str, ...] = Field(default=(), max_length=PLAYBILL_QUERY_MAX_SELECT)
    follow: tuple[QueryFollowV1, ...] = Field(default=(), max_length=PLAYBILL_QUERY_MAX_FOLLOWS)
    order_by: tuple[str, ...] = Field(
        default=(),
        max_length=8,
        description="Fields to order by; prefix - for descending.",
    )
    status: tuple[QueryClaimStatus, ...] = Field(
        default=("live",),
        min_length=1,
        max_length=4,
        description=(
            "Which Claims cells show: live (each slot's answer, the default), and opt-in "
            "overturned, refused or retired."
        ),
    )
    claims: bool = Field(
        default=False,
        description="Also answer each cell's Claims (id, value, verdict, status) as rows[].claims.",
    )
    limit: int = Field(default=PLAYBILL_QUERY_DEFAULT_LIMIT, ge=1, le=PLAYBILL_QUERY_MAX_LIMIT)
    cursor: str | None = Field(default=None, max_length=512)
    spec: QueryDefinitionSpecV1 | None = None
    name: str | None = Field(default=None, max_length=256)
    params: dict[str, QueryParameterValue] | None = None
    budgets: QueryBudgetsV1 | None = Field(
        default=None,
        description="Named query budgets, up to the definition's maximum; default its own.",
    )
    receipt: QueryReceiptDetail = Field(
        default="compact",
        description=(
            "full adds the named query's replay receipt (Claims read, paths, verdict), "
            "run at its declared budgets."
        ),
    )
    at: AcceptedCoordinate | str | None = Field(
        default=None,
        description=(
            "An accepted coordinate, a git oid (a unique prefix of 12+ hex characters is "
            "enough), or a generation number; the default is the current head."
        ),
    )
    evaluation_time: datetime | None = Field(
        default=None, description="ISO-8601 instant; the default is now."
    )

    @field_validator("status")
    @classmethod
    def _unique_status(cls, value: tuple[QueryClaimStatus, ...]) -> tuple[QueryClaimStatus, ...]:
        if len(set(value)) != len(value):
            raise ValueError("status names each Claim status once")
        return value


class PlaybillQueryClaimV1(BaseModel):
    """One Claim behind a cell value: ``rows[i].claims[column]`` lists them.

    ``status`` tells a slot's winner (``accepted``) from the Claims resolution
    set aside (``overturned``, ``refused``) and from contenders it left
    unresolved (``conflicted``); ``verdict`` is the Claim's own evidence
    verdict (``retired`` for a withdrawn Claim). ``get(claim)`` reads it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim: str
    value: Any
    verdict: str
    status: QueryCellClaimStatus
    role: ClaimRole
    qualifier: str | None = Field(default=None, exclude_if=_is_none)


class PlaybillQueryClaimValueV1(PlaybillQueryClaimV1):
    """One Claim's value with the Subject and predicate its cell sits in."""

    subject: str
    predicate: str


class PlaybillQueryColumnV1(BaseModel):
    """One typed column of a query answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    predicate: str | None = None
    type: str
    members: tuple[str, ...] | None = None
    cardinality: Literal["one", "many"] = "one"


class PlaybillQueryReplayV1(BaseModel):
    """A named query's replay receipt: the engine's whole result and its execution receipt.

    ``result`` names every row's bindings, the Claims each row read, traversal
    paths, the bound parameters and the verdict; ``execution`` is the
    ``playbill-query-execution-receipt-v1`` whose digest identifies the run.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    definition_path: str
    result: ClaimQueryResultV1
    execution: QueryExecutionReceiptV1


class PlaybillQueryReceiptV1(BaseModel):
    """What ran: the mode, the definition digest, the coordinate and the time.

    ``replay`` is present when a named query asked for ``receipt="full"``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: QueryMode
    spec_digest: str
    coordinate: AcceptedCoordinate
    evaluation_time: datetime
    replay: PlaybillQueryReplayV1 | None = Field(default=None, exclude_if=_is_none)


class PlaybillQueryResult(BaseModel):
    """One page of a ``query`` answer: values first, flags per row.

    Rows are bounded by ``get``'s card rule: a string value over 500 characters
    is cut to ``{value, truncated: true, length}`` (``get(detail="evidence")``
    reads it whole), and an exact-content value is its text, or a typed marker
    when it cannot be shown as text.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-query-page-v1"] = "playbill-query-page-v1"
    kind: str | None = None
    columns: tuple[PlaybillQueryColumnV1, ...]
    rows: tuple[dict[str, Any], ...]
    truncated: bool = False
    next_cursor: str | None = None
    capped: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    receipt: PlaybillQueryReceiptV1


__all__ = [
    "PLAYBILL_QUERY_DEFAULT_LIMIT",
    "PLAYBILL_QUERY_MAX_FILTERS",
    "PLAYBILL_QUERY_MAX_FOLLOWS",
    "PLAYBILL_QUERY_MAX_LIMIT",
    "PLAYBILL_QUERY_MAX_SELECT",
    "QUERY_FILTER_OPERATORS",
    "PlaybillQueryClaimV1",
    "PlaybillQueryClaimValueV1",
    "PlaybillQueryColumnV1",
    "PlaybillQueryReceiptV1",
    "PlaybillQueryReplayV1",
    "PlaybillQueryRequestV1",
    "PlaybillQueryResult",
    "QueryFilterContainsV1",
    "QueryFilterEqV1",
    "QueryFilterExistsV1",
    "QueryFilterGteV1",
    "QueryFilterGtV1",
    "QueryFilterInV1",
    "QueryFilterLteV1",
    "QueryFilterLtV1",
    "QueryFilterNeV1",
    "QueryFilterOperator",
    "QueryCellClaimStatus",
    "QueryClaimStatus",
    "QueryFilterV1",
    "QueryFlag",
    "QueryFollowDirection",
    "QueryFollowV1",
    "QueryMode",
    "QueryReceiptDetail",
    "QueryParameterValue",
    "QueryScalar",
    "query_filter",
]
