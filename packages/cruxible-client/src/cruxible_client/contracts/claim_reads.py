"""Bounded, coordinate-pinned accepted Claim reads and projection backing reads."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cruxible_client.contracts import PlaybillAcceptedCoordinate, PlaybillClaimViewV2
from cruxible_client.contracts.declared_blocks import ProjectionClaimBackingV1

MAX_CLAIM_READ_BATCH = 256


class ClaimReadBatchRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    at: PlaybillAcceptedCoordinate
    claim_ids: tuple[str, ...] = Field(default=(), max_length=MAX_CLAIM_READ_BATCH)
    subject_paths: tuple[str, ...] = Field(default=(), max_length=MAX_CLAIM_READ_BATCH)
    predicates: tuple[str, ...] = Field(default=(), max_length=MAX_CLAIM_READ_BATCH)
    include_retired: bool = False
    limit: int = Field(default=128, ge=1, le=MAX_CLAIM_READ_BATCH)
    cursor: str | None = Field(default=None, max_length=2048)
    evaluation_time: datetime | None = None

    @model_validator(mode="after")
    def selection(self) -> ClaimReadBatchRequestV1:
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
