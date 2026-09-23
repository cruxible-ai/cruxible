"""Bounded, coordinate-pinned accepted Claim reads and projection backing reads."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cruxible_client.contracts import PlaybillAcceptedCoordinate, PlaybillClaimViewV2
from cruxible_client.contracts.declared_blocks import ProjectionClaimBackingV1

MAX_CLAIM_READ_BATCH = 256
MAX_CLAIM_VALUE_SUBJECTS = 1024
MAX_CLAIM_VALUE_PREDICATES = 64
MAX_CLAIM_VALUE_ROWS = 8192


class ClaimReadBatchRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    at: PlaybillAcceptedCoordinate | None = None
    claim_ids: tuple[str, ...] = Field(default=(), max_length=MAX_CLAIM_READ_BATCH)
    subject_paths: tuple[str, ...] = Field(default=(), max_length=MAX_CLAIM_READ_BATCH)
    predicates: tuple[str, ...] = Field(default=(), max_length=MAX_CLAIM_READ_BATCH)
    include_retired: bool = False
    limit: int = Field(default=128, ge=1, le=MAX_CLAIM_READ_BATCH)
    cursor: str | None = Field(default=None, max_length=2048)
    evaluation_time: datetime | None = None

    @model_validator(mode="after")
    def selection(self) -> ClaimReadBatchRequestV1:
        if self.cursor is not None and self.at is None:
            raise ValueError("cursor continuation requires the returned accepted coordinate")
        if bool(self.claim_ids) == bool(self.subject_paths):
            raise ValueError("select either Claim identities or explicit subject paths")
        if self.claim_ids and (self.predicates or self.cursor):
            raise ValueError("identity reads do not accept predicate filters or a cursor")
        for values in (self.claim_ids, self.subject_paths, self.predicates):
            if len(set(values)) != len(values) or any(not value for value in values):
                raise ValueError("selectors must be nonempty and unique")
        if self.evaluation_time is not None and self.evaluation_time.utcoffset() is None:
            raise ValueError("evaluation_time must be timezone-aware")
        return self


class ClaimReadBatchResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tag: Literal["playbill-claim-read-batch-v1"] = "playbill-claim-read-batch-v1"
    coordinate: PlaybillAcceptedCoordinate
    claims: tuple[PlaybillClaimViewV2, ...]
    truncated: bool = False
    cursor: str | None = None


class ClaimBackingsRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    at: PlaybillAcceptedCoordinate
    claim_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_CLAIM_READ_BATCH)


class ClaimBackingsResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tag: Literal["playbill-claim-backings-v1"] = "playbill-claim-backings-v1"
    coordinate: PlaybillAcceptedCoordinate
    backings: tuple[ProjectionClaimBackingV1, ...]


class ClaimValuesRequestV1(BaseModel):
    """Every live Claim's value and verdict for explicit Subjects and predicates."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    at: PlaybillAcceptedCoordinate | None = None
    subject_paths: tuple[str, ...] = Field(min_length=1, max_length=MAX_CLAIM_VALUE_SUBJECTS)
    predicates: tuple[str, ...] = Field(default=(), max_length=MAX_CLAIM_VALUE_PREDICATES)
    evaluation_time: datetime | None = None

    @model_validator(mode="after")
    def selection(self) -> ClaimValuesRequestV1:
        for values in (self.subject_paths, self.predicates):
            if len(set(values)) != len(values) or any(not value for value in values):
                raise ValueError("selectors must be nonempty and unique")
        if self.evaluation_time is not None and self.evaluation_time.utcoffset() is None:
            raise ValueError("evaluation_time must be timezone-aware")
        return self


class ClaimValueV1(BaseModel):
    """One live Claim's statement value and its verdict, without its full view."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    claim_id: str
    subject_path: str
    predicate: str
    qualifier: str | None
    role: str
    object_kind: Literal["literal", "subject"]
    # The literal itself, or the object Subject's artifact path.
    value: Any
    verdict: str
    status: str


class ClaimValuesResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tag: Literal["playbill-claim-values-v1"] = "playbill-claim-values-v1"
    coordinate: PlaybillAcceptedCoordinate
    evaluation_time: datetime
    values: tuple[ClaimValueV1, ...]
