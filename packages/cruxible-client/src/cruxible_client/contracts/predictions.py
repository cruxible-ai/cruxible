"""Governed hypothesis tests and exact-evidence settlement request/response wires."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_client.contracts.canonical import Sha256Value, normalize_canonical
from cruxible_client.contracts.procedures.windows import TriggerEventReferenceV1
from cruxible_client.contracts.resolution_contracts import (
    ClaimVersionReferenceV1,
    ResolutionContractReferenceV1,
    ResolutionContractV1,
)
from cruxible_client.contracts.resolution_rules import (
    PredictionEqualityRuleV1 as PredictionEqualityRuleV1,
)
from cruxible_client.contracts.resolution_rules import (
    PredictionObservationSelectorV1 as PredictionObservationSelectorV1,
)
from cruxible_client.contracts.resolution_rules import (
    PredictionPresenceRuleV1 as PredictionPresenceRuleV1,
)
from cruxible_client.contracts.resolution_rules import (
    PredictionRuleV1 as PredictionRuleV1,
)
from cruxible_client.contracts.resolution_rules import (
    PredictionThresholdRuleV1 as PredictionThresholdRuleV1,
)

PredictionRefusalCodeV1: TypeAlias = Literal[
    "prediction_unsettleable_rule", "prediction_deadline_passed", "settlement_evidence_mismatch"
]


class _StrictPredictionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlaybillPredictRequestV2(_StrictPredictionModel):
    tag: Literal["playbill-predict-request-v2"] = "playbill-predict-request-v2"
    contract: ResolutionContractV1


class PlaybillPredictResultV2(_StrictPredictionModel):
    tag: Literal["playbill-predict-result-v2"] = "playbill-predict-result-v2"
    contract_identity: str
    contract_digest: str
    proposal_id: str
    intent: dict[str, Any]


class ObservationSettlementEvidenceV2(_StrictPredictionModel):
    tag: Literal["playbill-observation-settlement-evidence-v2"] = (
        "playbill-observation-settlement-evidence-v2"
    )
    claim: ClaimVersionReferenceV1


class TerminalSettlementEvidenceV2(_StrictPredictionModel):
    tag: Literal["playbill-terminal-settlement-evidence-v2"] = (
        "playbill-terminal-settlement-evidence-v2"
    )
    claim: ClaimVersionReferenceV1
    run_id: str
    terminal_record_digest: str

    @field_validator("terminal_record_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


PredictionSettlementEvidenceV2: TypeAlias = Annotated[
    ObservationSettlementEvidenceV2 | TerminalSettlementEvidenceV2,
    Field(discriminator="tag"),
]


class PlaybillSettleRequestV2(_StrictPredictionModel):
    tag: Literal["playbill-settle-request-v2"] = "playbill-settle-request-v2"
    contract: ResolutionContractReferenceV1
    trigger_event: TriggerEventReferenceV1 | None = None
    evidence: PredictionSettlementEvidenceV2


class PlaybillSettleResultV2(_StrictPredictionModel):
    tag: Literal["playbill-settle-result-v2"] = "playbill-settle-result-v2"
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
