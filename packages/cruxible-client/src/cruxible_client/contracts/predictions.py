"""Served prediction declarations and settlement request/response wires."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal, TypeAlias, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import (
    AuthoringIntentViewV1,
    ClaimAuthoringPayloadV1,
    ClaimAuthoringPayloadV2,
    ClaimAuthoringPayloadV3,
)
from cruxible_client.contracts.canonical import (
    CanonicalValue,
    Sha256Value,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.temporal import ensure_utc, format_datetime

PREDICTION_DECLARATION_DIGEST_DOMAIN = "playbill-prediction-declaration-v1"
PREDICTION_ID_RE = re.compile(r"^PRD-[0-9a-f]{32}$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
PredictionRefusalCodeV1: TypeAlias = Literal[
    "prediction_unsettleable_rule",
    "prediction_deadline_passed",
    "settlement_evidence_mismatch",
]


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


PredictionClaimPayloadV1: TypeAlias = Annotated[
    ClaimAuthoringPayloadV1 | ClaimAuthoringPayloadV2 | ClaimAuthoringPayloadV3,
    Field(discriminator="tag"),
]


class PlaybillPredictRequestV1(_StrictPredictionModel):
    tag: Literal["playbill-predict-request-v1"] = "playbill-predict-request-v1"
    prediction: PredictionClaimPayloadV1
    procedure: str
    measurement_name: str
    observation: PredictionObservationSelectorV1
    rule: PredictionRuleV1
    outcome_class: str = "prediction-correctness"
    deadline: datetime = Field(description="Reads VALIDITY WINDOW.")

    @field_validator("procedure", "measurement_name", "outcome_class")
    @classmethod
    def _names(cls, value: str) -> str:
        if not _NAME_RE.fullmatch(value):
            raise ValueError("prediction identifier is not canonical")
        return value

    @field_validator("deadline")
    @classmethod
    def _deadline(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("deadline", when_used="json")
    def _serialize_deadline(self, value: datetime) -> str:
        rendered = format_datetime(value)
        assert rendered is not None
        return rendered


class PlaybillPredictionDeclarationV1(_StrictPredictionModel):
    tag: Literal["playbill-prediction-declaration-v1"] = "playbill-prediction-declaration-v1"
    prediction_id: str
    intent_id: str
    proposal_id: str
    candidate_digest: str
    predicted_claim_id: str
    actor_id: str
    base_coordinate: AcceptedCoordinate
    procedure_identity: ArtifactIdentity
    procedure_path: str
    procedure_artifact_digest: str
    measurement_name: str
    observation: PredictionObservationSelectorV1
    rule: PredictionRuleV1
    outcome_class: str
    declared_at: datetime = Field(description="Reads EVALUATION INSTANT.")
    deadline: datetime = Field(description="Reads VALIDITY WINDOW.")
    declaration_digest: str

    @field_validator("prediction_id")
    @classmethod
    def _prediction_id(cls, value: str) -> str:
        if not PREDICTION_ID_RE.fullmatch(value):
            raise ValueError("prediction_id must be PRD- plus 128-bit lowercase hex")
        return value

    @field_validator("candidate_digest", "procedure_artifact_digest", "declaration_digest")
    @classmethod
    def _digests(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("declared_at", "deadline")
    @classmethod
    def _times(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("declared_at", "deadline", when_used="json")
    def _serialize_times(self, value: datetime) -> str:
        rendered = format_datetime(value)
        assert rendered is not None
        return rendered

    @model_validator(mode="after")
    def _reproduces(self) -> "PlaybillPredictionDeclarationV1":
        if self.procedure_identity.kind != "Procedure":
            raise ValueError("prediction declaration must name a Procedure")
        if self.declared_at >= self.deadline:
            raise ValueError("prediction deadline must follow declaration")
        if self.prediction_id != prediction_id(self):
            raise ValueError("prediction_id does not reproduce")
        if self.declaration_digest != prediction_declaration_digest(self):
            raise ValueError("prediction declaration digest does not reproduce")
        return self


def _declaration_payload(value: PlaybillPredictionDeclarationV1) -> dict[str, object]:
    payload = value.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("prediction_id")
    payload.pop("declaration_digest")
    return cast(dict[str, object], payload)


def prediction_id(value: PlaybillPredictionDeclarationV1) -> str:
    digest = typed_digest(
        Sha256Value,
        PREDICTION_DECLARATION_DIGEST_DOMAIN,
        _declaration_payload(value),
    ).value
    return f"PRD-{digest[:32]}"


def prediction_declaration_digest(value: PlaybillPredictionDeclarationV1) -> str:
    return typed_digest(
        Sha256Value,
        PREDICTION_DECLARATION_DIGEST_DOMAIN,
        {"prediction_id": value.prediction_id, **_declaration_payload(value)},
    ).tagged


def build_prediction_declaration(**values: object) -> PlaybillPredictionDeclarationV1:
    provisional = PlaybillPredictionDeclarationV1.model_construct(
        _fields_set=None,
        **values,
        prediction_id="PRD-" + "0" * 32,
        declaration_digest="sha256:" + "0" * 64,
    )
    identifier = prediction_id(provisional)
    with_identifier = provisional.model_copy(update={"prediction_id": identifier})
    return PlaybillPredictionDeclarationV1.model_validate(
        {
            **values,
            "prediction_id": identifier,
            "declaration_digest": prediction_declaration_digest(with_identifier),
        }
    )


class PlaybillPredictResultV1(_StrictPredictionModel):
    tag: Literal["playbill-predict-result-v1"] = "playbill-predict-result-v1"
    declaration: PlaybillPredictionDeclarationV1
    intent: AuthoringIntentViewV1


class ObservationSettlementEvidenceV1(_StrictPredictionModel):
    tag: Literal["playbill-observation-settlement-evidence-v1"] = (
        "playbill-observation-settlement-evidence-v1"
    )
    claim_id: str


class TerminalSettlementEvidenceV1(_StrictPredictionModel):
    tag: Literal["playbill-terminal-settlement-evidence-v1"] = (
        "playbill-terminal-settlement-evidence-v1"
    )
    claim_id: str
    run_id: str
    terminal_record_digest: str

    @field_validator("terminal_record_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


PredictionSettlementEvidenceV1: TypeAlias = Annotated[
    ObservationSettlementEvidenceV1 | TerminalSettlementEvidenceV1,
    Field(discriminator="tag"),
]


class PlaybillSettleRequestV1(_StrictPredictionModel):
    tag: Literal["playbill-settle-request-v1"] = "playbill-settle-request-v1"
    evidence: PredictionSettlementEvidenceV1


class PlaybillSettleResultV1(_StrictPredictionModel):
    tag: Literal["playbill-settle-result-v1"] = "playbill-settle-result-v1"
    prediction_id: str
    status: Literal["settled"] = "settled"
    activation: dict[str, object]
    resolution: dict[str, object]
    relation: dict[str, object]

    @field_validator("activation", "resolution", "relation", mode="before")
    @classmethod
    def _canonical_objects(cls, value: object) -> dict[str, object]:
        normalized = normalize_canonical(value)
        if not isinstance(normalized, dict):
            raise ValueError("prediction settlement models must be canonical objects")
        return cast(dict[str, object], normalized)


__all__ = [
    "ObservationSettlementEvidenceV1",
    "PREDICTION_DECLARATION_DIGEST_DOMAIN",
    "PREDICTION_ID_RE",
    "PlaybillPredictRequestV1",
    "PlaybillPredictResultV1",
    "PlaybillPredictionDeclarationV1",
    "PlaybillSettleRequestV1",
    "PlaybillSettleResultV1",
    "PredictionEqualityRuleV1",
    "PredictionObservationSelectorV1",
    "PredictionPresenceRuleV1",
    "PredictionRefusalCodeV1",
    "PredictionRuleV1",
    "PredictionSettlementEvidenceV1",
    "PredictionThresholdRuleV1",
    "TerminalSettlementEvidenceV1",
    "build_prediction_declaration",
    "prediction_declaration_digest",
    "prediction_id",
]
