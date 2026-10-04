"""Bounded, coordinate-pinned accepted Claim reads and projection backing reads."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cruxible_client.contracts import ClaimViewRecord, PlaybillAcceptedCoordinate
from cruxible_client.contracts.claims import ClaimObject
from cruxible_client.contracts.declared_blocks import ProjectionClaimBacking

MAX_CLAIM_READ_BATCH = 256
MAX_CLAIM_VALUE_SUBJECTS = 1024
MAX_CLAIM_VALUE_PREDICATES = 64
MAX_CLAIM_VALUE_ROWS = 8192


class ClaimReadBatchRequest(BaseModel):
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
    def selection(self) -> ClaimReadBatchRequest:
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


class ClaimReadBatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tag: Literal["playbill-claim-read-batch-v1"] = "playbill-claim-read-batch-v1"
    coordinate: PlaybillAcceptedCoordinate
    claims: tuple[ClaimViewRecord, ...]
    truncated: bool = False
    cursor: str | None = None


class ClaimBackingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    at: PlaybillAcceptedCoordinate
    claim_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_CLAIM_READ_BATCH)


class ClaimBackingsResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tag: Literal["playbill-claim-backings-v1"] = "playbill-claim-backings-v1"
    coordinate: PlaybillAcceptedCoordinate
    backings: tuple[ProjectionClaimBacking, ...]


class ClaimValuesRequest(BaseModel):
    """Every live Claim's value and verdict for selected Subjects and predicates.

    Subjects are selected either by explicit paths or by one Subject kind.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    at: PlaybillAcceptedCoordinate | None = None
    subject_paths: tuple[str, ...] = Field(default=(), max_length=MAX_CLAIM_VALUE_SUBJECTS)
    subject_kind: str | None = Field(default=None, min_length=1)
    predicates: tuple[str, ...] = Field(default=(), max_length=MAX_CLAIM_VALUE_PREDICATES)
    evaluation_time: datetime | None = None

    @model_validator(mode="after")
    def selection(self) -> ClaimValuesRequest:
        if bool(self.subject_paths) == (self.subject_kind is not None):
            raise ValueError("select either explicit subject paths or one subject kind")
        for values in (self.subject_paths, self.predicates):
            if len(set(values)) != len(values) or any(not value for value in values):
                raise ValueError("selectors must be nonempty and unique")
        if self.evaluation_time is not None and self.evaluation_time.utcoffset() is None:
            raise ValueError("evaluation_time must be timezone-aware")
        return self

    @classmethod
    def for_kind(
        cls,
        subject_kind: str,
        *,
        subject_ids: Sequence[str] = (),
        predicates: Sequence[str] = (),
        evaluation_time: datetime | None = None,
    ) -> ClaimValuesRequest:
        """Every Subject of one kind, or just the named IDs of that kind."""
        from cruxible_client.contracts.subjects import subject_path

        if subject_ids:
            selection: dict[str, Any] = {
                "subject_paths": tuple(
                    subject_path(subject_kind, subject_id) for subject_id in subject_ids
                )
            }
        else:
            selection = {"subject_kind": subject_kind}
        return cls.model_validate(
            {
                **selection,
                "predicates": tuple(predicates),
                "evaluation_time": evaluation_time,
            }
        )


class ClaimValueRecord(BaseModel):
    """One live Claim's statement value and its verdict, without its full view."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    claim_id: str
    subject_path: str
    subject_id: str
    predicate: str
    qualifier: str | None
    role: str
    object_kind: Literal["literal", "subject", "exact_content"]
    # The statement's object exactly as accepted.
    object: ClaimObject
    # The literal itself; the object Subject's artifact path; or the exact
    # content digest. ``object`` carries the selector or span.
    value: Any
    verdict: str
    status: str


class ClaimValuesResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tag: Literal["playbill-claim-values-v1"] = "playbill-claim-values-v1"
    coordinate: PlaybillAcceptedCoordinate
    evaluation_time: datetime
    values: tuple[ClaimValueRecord, ...]
