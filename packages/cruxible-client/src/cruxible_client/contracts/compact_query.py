"""Request and answer contracts for the ``query`` read verb.

``query`` answers any question over accepted state in one of three modes:

- **compact**: a Subject ``kind`` and/or free-text ``contains``, with optional
  ``where`` filters, ``select`` columns, one-hop ``follow`` relations and
  ``order_by`` keys. It lowers to a ``QueryDefinitionSpecV1`` and runs through
  the same evaluator as a governed QueryDefinition.
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

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag

from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.query.definitions import QueryDefinitionSpecV1

PLAYBILL_QUERY_DEFAULT_LIMIT = 50
PLAYBILL_QUERY_MAX_LIMIT = 500
PLAYBILL_QUERY_MAX_FILTERS = 32
PLAYBILL_QUERY_MAX_FOLLOWS = 4
PLAYBILL_QUERY_MAX_SELECT = 64

QueryScalar = Union[str, int, bool]
"""One filter value. Dates, times and Subject references are strings."""

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
QueryFlag = Literal["stale", "contested", "contradicted", "unsure_hold"]
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


class QueryFollowV1(BaseModel):
    """One hop along a Subject-valued relation Claim, bound under an alias."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )

    field: str = Field(
        min_length=1,
        max_length=256,
        description="A Subject-valued predicate of the kind, short or fully qualified.",
    )
    as_: str = Field(
        alias="as",
        min_length=1,
        max_length=64,
        description="The alias later fields use as alias.field.",
    )


class PlaybillQueryRequestV1(BaseModel):
    """One ``query`` call. Exactly one mode: compact (kind/contains), spec, or name."""

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
    limit: int = Field(default=PLAYBILL_QUERY_DEFAULT_LIMIT, ge=1, le=PLAYBILL_QUERY_MAX_LIMIT)
    cursor: str | None = Field(default=None, max_length=16384)
    spec: QueryDefinitionSpecV1 | None = None
    name: str | None = Field(default=None, max_length=256)
    params: dict[str, QueryScalar] | None = None
    at: AcceptedCoordinate | str | None = Field(
        default=None,
        description=(
            "An accepted coordinate or a git oid (a unique prefix of 12+ hex characters is "
            "enough); the default is the current head."
        ),
    )
    evaluation_time: datetime | None = Field(
        default=None, description="ISO-8601 instant; the default is now."
    )


class PlaybillQueryColumnV1(BaseModel):
    """One typed column of a query answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    predicate: str | None = None
    type: str
    members: tuple[str, ...] | None = None
    cardinality: Literal["one", "many"] = "one"


class PlaybillQueryReceiptV1(BaseModel):
    """What ran: the mode, the definition digest, the coordinate and the time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: QueryMode
    spec_digest: str
    coordinate: AcceptedCoordinate
    evaluation_time: datetime


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
    "PlaybillQueryColumnV1",
    "PlaybillQueryReceiptV1",
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
    "QueryFilterV1",
    "QueryFlag",
    "QueryFollowV1",
    "QueryMode",
    "QueryScalar",
    "query_filter",
]
