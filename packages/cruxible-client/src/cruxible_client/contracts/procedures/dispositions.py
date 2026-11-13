"""Reviewer judgments on Procedure measurement resolutions (operational exhaust)."""

from datetime import datetime
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_client.contracts.projection import AcceptedCoordinate

ResolutionDispositionVerdict: TypeAlias = Literal["upheld", "overturned"]


class _DispositionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")


class ProcedureResolutionDispositionRequest(_DispositionModel):
    resolution_id: str = Field(pattern=r"^RSR-[0-9a-f]{32}$")
    at: AcceptedCoordinate | None = None
    verdict: ResolutionDispositionVerdict
    reason: str = Field(min_length=1, max_length=4096)

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value


class ResolutionDispositionSummary(_DispositionModel):
    disposition_id: str
    resolution_id: str
    sequence: int = Field(ge=1)
    verdict: ResolutionDispositionVerdict
    reviewer_id: str
    recorded_at: datetime
    reason: str | None


class ProcedureResolutionDispositionResult(_DispositionModel):
    disposition: ResolutionDispositionSummary
    standing_resolution_id: str | None
    written_now: bool


class ProcedureMeasurementReview(_DispositionModel):
    measurement_name: str
    contract_id: str
    latest_resolution_id: str | None
    standing_resolution_id: str | None
    unavailable: bool = False
    last_disposition: ResolutionDispositionSummary | None = None
    # Newest first, at most the requested history limit. Operational records
    # are live even when the Procedure revision was selected at a historical coordinate.
    history: tuple[ResolutionDispositionSummary, ...] = ()
    history_total: int = Field(default=0, ge=0)
    history_truncated: bool = False


ProcedureResolutionDispositionRefusalCode: TypeAlias = Literal[
    "cruxible.resolution.unknown",
    "cruxible.resolution.already_disposed",
    "cruxible.resolution.not_latest",
    "cruxible.resolution.contract_closed",
    "cruxible.resolution.unavailable",
    "cruxible.resolution.conflict",
]
