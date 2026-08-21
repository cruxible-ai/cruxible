"""Frozen request/result wire for deterministic headless Playbill discovery."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from cruxible_core.playbill.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.discovery import (
    DiscoveryMatchBasisV1,
    normalize_discovery_term,
)
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.query.grammar import byte_sorted
from cruxible_core.playbill.semantic import SemanticAddress
from cruxible_core.temporal import ensure_utc, format_datetime

SEARCH_SELECTION_BASIS_DOMAIN = "playbill-search-selection-basis-v1"
SEARCH_RESULT_DOMAIN = "playbill-search-result-v1"
SEARCH_CURSOR_DOMAIN = "playbill-search-cursor-v1"

SearchMode = Literal["search", "list", "orient"]
SearchKind = Literal["claim", "brief", "procedure", "demand"]
SearchStatus = Literal["accepted", "conflicted", "overturned", "refused", "retired"]
SearchKindAvailability = Literal["installed", "not_installed"]

SEARCH_KINDS: tuple[SearchKind, ...] = ("brief", "claim", "demand", "procedure")
SEARCH_STATUSES: tuple[SearchStatus, ...] = (
    "accepted",
    "conflicted",
    "overturned",
    "refused",
    "retired",
)


class _StrictSearchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlaybillSearchBudgetsV1(_StrictSearchModel):
    tag: Literal["playbill-search-budgets-v1"] = "playbill-search-budgets-v1"
    max_rows: int = Field(default=50, ge=1, le=200)
    max_result_bytes: int = Field(default=1_048_576, ge=1, le=1_048_576)


class PlaybillSearchCursorV1(_StrictSearchModel):
    tag: Literal["playbill-search-cursor-v1"] = "playbill-search-cursor-v1"
    selection_basis_digest: str
    coordinate: AcceptedCoordinate
    last_match_priority: int = Field(ge=0, le=7)
    last_kind: SearchKind
    last_identity: str
    budgets: PlaybillSearchBudgetsV1
    cursor_digest: str

    @field_validator("selection_basis_digest", "cursor_digest")
    @classmethod
    def _digests(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _digest(self) -> "PlaybillSearchCursorV1":
        if self.cursor_digest != playbill_search_cursor_digest(self):
            raise ValueError("Playbill search cursor digest does not reproduce")
        return self


class PlaybillSearchRequestV1(_StrictSearchModel):
    tag: Literal["playbill-search-request-v1"] = "playbill-search-request-v1"
    mode: SearchMode
    accepted_coordinate: AcceptedCoordinate
    evaluation_time: datetime
    access_profile: CoverageAccessProfileV1
    kinds: tuple[SearchKind, ...] = SEARCH_KINDS
    query: str | None = None
    subject: SemanticAddress | None = None
    statuses: tuple[SearchStatus, ...] = ()
    cursor: PlaybillSearchCursorV1 | None = None
    budgets: PlaybillSearchBudgetsV1 = PlaybillSearchBudgetsV1()

    @field_validator("evaluation_time")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("evaluation_time", when_used="json")
    def _serialize_time(self, value: datetime) -> str:
        rendered = format_datetime(value)
        assert rendered is not None
        return rendered

    @field_validator("kinds")
    @classmethod
    def _kinds(cls, value: tuple[SearchKind, ...]) -> tuple[SearchKind, ...]:
        if not value or value != byte_sorted(value):
            raise ValueError("search kinds must be nonempty, byte-sorted, and unique")
        return value

    @field_validator("statuses")
    @classmethod
    def _statuses(cls, value: tuple[SearchStatus, ...]) -> tuple[SearchStatus, ...]:
        if value != byte_sorted(value):
            raise ValueError("search statuses must be byte-sorted and unique")
        return value

    @field_validator("query")
    @classmethod
    def _query(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_discovery_term(value)
        if not normalized:
            raise ValueError("search query must be nonblank")
        return normalized

    @model_validator(mode="after")
    def _mode_shape(self) -> "PlaybillSearchRequestV1":
        if (self.mode == "search") != (self.query is not None):
            raise ValueError("search mode requires a query and list/orient forbid one")
        if self.mode == "orient" and self.cursor is not None:
            raise ValueError("orient is a complete summary and does not accept a cursor")
        return self


class PlaybillSearchRowV1(_StrictSearchModel):
    tag: Literal["playbill-search-row-v1"] = "playbill-search-row-v1"
    kind: SearchKind
    identity: str
    address: SemanticAddress
    status: SearchStatus
    subject: SemanticAddress | None = None
    predicate: str | None = None
    title: str
    summary: str | None = None
    healthy: bool | None = None
    match_basis: tuple[DiscoveryMatchBasisV1, ...] = ()
    brief_health_receipt_digest: str | None = None

    @field_validator("brief_health_receipt_digest")
    @classmethod
    def _health_digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _brief_shape(self) -> "PlaybillSearchRowV1":
        is_brief = self.kind == "brief"
        if is_brief != (self.healthy is not None):
            raise ValueError("exactly Brief rows carry health")
        if is_brief != (self.brief_health_receipt_digest is not None):
            raise ValueError("exactly Brief rows carry a health receipt digest")
        return self


class PlaybillSearchCountV1(_StrictSearchModel):
    key: str
    count: int = Field(ge=0)


class PlaybillSearchKindAvailabilityV1(_StrictSearchModel):
    kind: SearchKind
    availability: SearchKindAvailability


class PlaybillSearchFollowUpV1(_StrictSearchModel):
    mode: Literal["search", "list"]
    kinds: tuple[SearchKind, ...]
    statuses: tuple[SearchStatus, ...] = ()
    subject: SemanticAddress | None = None


class PlaybillSearchOrientationV1(_StrictSearchModel):
    tag: Literal["playbill-search-orientation-v1"] = "playbill-search-orientation-v1"
    coordinate: AcceptedCoordinate
    generation: int = Field(ge=0)
    counts_by_kind: tuple[PlaybillSearchCountV1, ...]
    counts_by_status: tuple[PlaybillSearchCountV1, ...]
    unhealthy_brief_count: int = Field(ge=0)
    conflicted_count: int = Field(ge=0)
    available_kinds: tuple[SearchKind, ...]
    kind_availability: tuple[PlaybillSearchKindAvailabilityV1, ...]
    truncated: bool
    follow_ups: tuple[PlaybillSearchFollowUpV1, ...]


class PlaybillSearchResultV1(_StrictSearchModel):
    tag: Literal["playbill-search-result-v1"] = "playbill-search-result-v1"
    mode: SearchMode
    coordinate: AcceptedCoordinate
    evaluation_time: datetime
    rows: tuple[PlaybillSearchRowV1, ...]
    orientation: PlaybillSearchOrientationV1 | None = None
    selection_basis_digest: str
    next_cursor: PlaybillSearchCursorV1 | None = None
    truncated: bool
    result_digest: str

    @field_validator("evaluation_time")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("selection_basis_digest", "result_digest")
    @classmethod
    def _digests(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_serializer("evaluation_time", when_used="json")
    def _serialize_time(self, value: datetime) -> str:
        rendered = format_datetime(value)
        assert rendered is not None
        return rendered

    @model_validator(mode="after")
    def _shape_and_digest(self) -> "PlaybillSearchResultV1":
        if (self.mode == "orient") != (self.orientation is not None):
            raise ValueError("exactly orient results carry orientation")
        if self.mode == "orient" and (self.rows or self.next_cursor is not None):
            raise ValueError("orient returns no arbitrary rows or cursor")
        if self.result_digest != playbill_search_result_digest(self):
            raise ValueError("Playbill search result digest does not reproduce")
        return self


def playbill_search_selection_basis_digest(request: PlaybillSearchRequestV1) -> str:
    payload = request.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("cursor")
    return typed_digest(Sha256Value, SEARCH_SELECTION_BASIS_DOMAIN, payload).tagged


def playbill_search_cursor_digest(cursor: PlaybillSearchCursorV1) -> str:
    payload = cursor.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("cursor_digest")
    return typed_digest(Sha256Value, SEARCH_CURSOR_DOMAIN, payload).tagged


def playbill_search_result_digest(result: PlaybillSearchResultV1) -> str:
    payload = result.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("result_digest")
    return typed_digest(Sha256Value, SEARCH_RESULT_DOMAIN, payload).tagged


def build_playbill_search_cursor(
    *,
    selection_basis_digest: str,
    coordinate: AcceptedCoordinate,
    last_match_priority: int,
    last_kind: SearchKind,
    last_identity: str,
    budgets: PlaybillSearchBudgetsV1,
) -> PlaybillSearchCursorV1:
    values = {
        "selection_basis_digest": selection_basis_digest,
        "coordinate": coordinate,
        "last_match_priority": last_match_priority,
        "last_kind": last_kind,
        "last_identity": last_identity,
        "budgets": budgets,
    }
    provisional = PlaybillSearchCursorV1.model_construct(
        **cast(dict[str, Any], values),
        cursor_digest="sha256:" + "0" * 64,
    )
    return PlaybillSearchCursorV1.model_validate(
        {**values, "cursor_digest": playbill_search_cursor_digest(provisional)}
    )


def build_playbill_search_result(**values: object) -> PlaybillSearchResultV1:
    provisional = PlaybillSearchResultV1.model_construct(
        **cast(dict[str, Any], values),
        result_digest="sha256:" + "0" * 64,
    )
    return PlaybillSearchResultV1.model_validate(
        {**values, "result_digest": playbill_search_result_digest(provisional)}
    )


def playbill_search_result_bytes(result: PlaybillSearchResultV1) -> bytes:
    return canonical_bytes(result.model_dump(mode="json"))


__all__ = [
    "SEARCH_CURSOR_DOMAIN",
    "SEARCH_KINDS",
    "SEARCH_RESULT_DOMAIN",
    "SEARCH_SELECTION_BASIS_DOMAIN",
    "SEARCH_STATUSES",
    "PlaybillSearchBudgetsV1",
    "PlaybillSearchCountV1",
    "PlaybillSearchCursorV1",
    "PlaybillSearchFollowUpV1",
    "PlaybillSearchKindAvailabilityV1",
    "PlaybillSearchOrientationV1",
    "PlaybillSearchRequestV1",
    "PlaybillSearchResultV1",
    "PlaybillSearchRowV1",
    "SearchKind",
    "SearchKindAvailability",
    "SearchMode",
    "SearchStatus",
    "build_playbill_search_cursor",
    "build_playbill_search_result",
    "playbill_search_cursor_digest",
    "playbill_search_result_bytes",
    "playbill_search_result_digest",
    "playbill_search_selection_basis_digest",
]
