"""Governed hypothesis tests and exact-evidence settlement request/response wires."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.canonical import Sha256Value, normalize_canonical
from cruxible_client.contracts.procedures.windows import TriggerEventReferenceV1
from cruxible_client.contracts.resolution_contracts import (
    ClaimIdInput,
    ClaimVersionInput,
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
    "prediction_unsettleable_rule",
    "prediction_deadline_passed",
    "settlement_evidence_mismatch",
    "prediction_window_unknown",
]


class _StrictPredictionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResolutionContractInputV1(ResolutionContractV1):
    """A ResolutionContract whose hypothesis may be named by Claim ID.

    The daemon resolves a Claim ID to the exact accepted version -- artifact and
    statement digests and the coordinate that accepted it -- before the
    contract is authored, so the accepted artifact always pins one version.
    """

    hypothesis: ClaimVersionInput  # type: ignore[assignment]


class PlaybillPredictRequestV2(_StrictPredictionModel):
    tag: Literal["playbill-predict-request-v2"] = "playbill-predict-request-v2"
    contract: ResolutionContractV1 | ResolutionContractInputV1


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
    claim: ClaimVersionInput


class TerminalSettlementEvidenceV2(_StrictPredictionModel):
    tag: Literal["playbill-terminal-settlement-evidence-v2"] = (
        "playbill-terminal-settlement-evidence-v2"
    )
    claim: ClaimVersionInput
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
    """Settle one prediction; usually just the observation's Claim ID.

    The route names the prediction (its contract name or a bound window's
    RSC-... id), so the daemon resolves the exact contract reference and, for a
    bound window, its anchor event. ``contract``, ``trigger_event`` and
    ``evidence`` are the advanced forms: an exact reference, an explicit anchor,
    or terminal evidence from a mandated settle run.
    """

    tag: Literal["playbill-settle-request-v2"] = "playbill-settle-request-v2"
    observation: ClaimIdInput | None = Field(
        default=None,
        description="Claim ID of the accepted observation that settles the prediction.",
    )
    contract: ResolutionContractReferenceV1 | None = Field(
        default=None,
        description="Advanced: the exact contract reference; omit to resolve it from the route.",
    )
    trigger_event: TriggerEventReferenceV1 | None = None
    evidence: PredictionSettlementEvidenceV2 | None = Field(
        default=None,
        description="Advanced: explicit observation or terminal evidence instead of observation.",
    )

    @model_validator(mode="after")
    def _one_evidence(self) -> PlaybillSettleRequestV2:
        if (self.observation is None) == (self.evidence is None):
            raise ValueError("settle takes exactly one of observation (a Claim ID) or evidence")
        return self


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
