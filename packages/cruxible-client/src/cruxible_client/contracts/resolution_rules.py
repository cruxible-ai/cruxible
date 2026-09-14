"""Mechanical comparison rules and observation selection shared by predictions and contracts."""

from __future__ import annotations

import re
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.canonical import CanonicalValue, normalize_canonical
from cruxible_client.contracts.semantic import SemanticAddress

_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


class _StrictPredictionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PredictionEqualityRuleV1(_StrictPredictionModel):
    tag: Literal["playbill-prediction-equality-rule-v1"] = "playbill-prediction-equality-rule-v1"
    operator: Literal["equality"] = "equality"


class PredictionThresholdRuleV1(_StrictPredictionModel):
    tag: Literal["playbill-prediction-threshold-rule-v1"] = "playbill-prediction-threshold-rule-v1"
    operator: Literal["threshold"] = "threshold"
    comparison: Literal["gt", "gte", "lt", "lte"]
    threshold: object

    @field_validator("threshold", mode="before")
    @classmethod
    def _threshold(cls, value: object) -> CanonicalValue:
        normalized = normalize_canonical(value)
        if not isinstance(normalized, int | dict) or isinstance(normalized, bool):
            raise ValueError("prediction threshold must be an integer or canonical decimal")
        if isinstance(normalized, dict) and tuple(normalized) != ("$decimal",):
            raise ValueError("prediction threshold must be an integer or canonical decimal")
        return normalized


class PredictionPresenceRuleV1(_StrictPredictionModel):
    tag: Literal["playbill-prediction-presence-rule-v1"] = "playbill-prediction-presence-rule-v1"
    operator: Literal["presence"] = "presence"


PredictionRuleV1: TypeAlias = Annotated[
    PredictionEqualityRuleV1 | PredictionThresholdRuleV1 | PredictionPresenceRuleV1,
    Field(discriminator="tag"),
]


class PredictionObservationSelectorV1(_StrictPredictionModel):
    tag: Literal["playbill-prediction-observation-selector-v1"] = (
        "playbill-prediction-observation-selector-v1"
    )
    subject: SemanticAddress
    predicate: str
    qualifier: str | None = None
    role: Literal["observation"] = "observation"

    @field_validator("predicate")
    @classmethod
    def _predicate(cls, value: str) -> str:
        if not _NAME_RE.fullmatch(value):
            raise ValueError("prediction observation predicate is not canonical")
        return value

    @model_validator(mode="after")
    def _subject(self) -> "PredictionObservationSelectorV1":
        if (
            self.subject.selector.scheme != "artifact-v1"
            or not self.subject.artifact_path.startswith("subjects/")
        ):
            raise ValueError("prediction observation must select one exact Subject")
        return self
