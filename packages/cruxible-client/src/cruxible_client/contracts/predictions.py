"""Governed hypothesis tests and exact-evidence settlement request/response wires."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.canonical import Sha256Value, normalize_canonical
from cruxible_client.contracts.procedures.windows import TriggerEventReference
from cruxible_client.contracts.resolution_contracts import (
    ClaimIdInput,
    ClaimVersionInput,
    ResolutionContract,
    ResolutionContractReference,
)
from cruxible_client.contracts.resolution_rules import (
    PredictionEqualityRule as PredictionEqualityRule,
)
from cruxible_client.contracts.resolution_rules import (
    PredictionObservationSelector as PredictionObservationSelector,
)
from cruxible_client.contracts.resolution_rules import (
    PredictionPresenceRule as PredictionPresenceRule,
)
from cruxible_client.contracts.resolution_rules import (
    PredictionRule as PredictionRule,
)
from cruxible_client.contracts.resolution_rules import (
    PredictionThresholdRule as PredictionThresholdRule,
)

PredictionRefusalCode: TypeAlias = Literal[
    "prediction_unsettleable_rule",
    "prediction_deadline_passed",
    "settlement_evidence_mismatch",
    "prediction_window_unknown",
]


class _StrictPredictionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResolutionContractInput(ResolutionContract):
    """A ResolutionContract whose hypothesis may be named by Claim ID.

    The daemon resolves a Claim ID to the exact accepted version -- artifact and
    statement digests and the coordinate that accepted it -- before the
    contract is authored, so the accepted artifact always pins one version.
    """

    hypothesis: ClaimVersionInput  # type: ignore[assignment]


class PredictRequest(_StrictPredictionModel):
    tag: Literal["playbill-predict-request-v2"] = "playbill-predict-request-v2"
    contract: ResolutionContract | ResolutionContractInput


class PredictResult(_StrictPredictionModel):
    tag: Literal["playbill-predict-result-v2"] = "playbill-predict-result-v2"
    contract_identity: str
    contract_digest: str
    proposal_id: str
    intent: dict[str, Any]


class ObservationSettlementEvidence(_StrictPredictionModel):
    tag: Literal["playbill-observation-settlement-evidence-v2"] = (
        "playbill-observation-settlement-evidence-v2"
    )
    claim: ClaimVersionInput


class TerminalSettlementEvidence(_StrictPredictionModel):
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


PredictionSettlementEvidence: TypeAlias = Annotated[
    ObservationSettlementEvidence | TerminalSettlementEvidence,
    Field(discriminator="tag"),
]


class SettleRequest(_StrictPredictionModel):
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
    contract: ResolutionContractReference | None = Field(
        default=None,
        description="Advanced: the exact contract reference; omit to resolve it from the route.",
    )
    trigger_event: TriggerEventReference | None = None
    evidence: PredictionSettlementEvidence | None = Field(
        default=None,
        description="Advanced: explicit observation or terminal evidence instead of observation.",
    )

    @model_validator(mode="after")
    def _one_evidence(self) -> SettleRequest:
        if (self.observation is None) == (self.evidence is None):
            raise ValueError("settle takes exactly one of observation (a Claim ID) or evidence")
        return self


class SettleResult(_StrictPredictionModel):
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
