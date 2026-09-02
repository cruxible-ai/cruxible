"""Execution-time source acquisition: the only place a typed result may be read.

``on_unavailable``/``on_stale``/``on_oversized`` never fire from a scan, a plan,
or an absence.  They fire from a :class:`ProcedureSourceAcquisitionResultV1`,
which one acquirer produced by really attempting the read.  The Capture such an
attempt produces belongs to the acquisition manifest; it never re-enters the
admitted-input tuple and never relabels its capture time.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_client.contracts.acquisition_policies import (
    AcquisitionInputDecisionV1,
    InputAcquisitionRuleV1,
)
from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.canonical import (
    CanonicalValue,
    Sha256Value,
    normalize_canonical,
)
from cruxible_client.contracts.captures import (
    CaptureContractV1,
    CaptureEnvelopeAny,
    capture_contract_is_self_asserted,
    capture_digest,
)
from cruxible_client.contracts.claim_verdicts import (
    EvidenceEpistemicGrade,
    EvidenceProvenanceGrade,
)
from cruxible_core.playbill.source_readers import ExternalCaptureAcquisitionV1

ProcedureAcquisitionOutcomeV1 = Literal[
    "acquired",
    "unavailable",
    "stale",
    "oversized",
    "refused",
]

ACQUISITION_UNAVAILABLE = "playbill.acquisition.unavailable"
ACQUISITION_STALE = "playbill.acquisition.stale"
ACQUISITION_OVERSIZED = "playbill.acquisition.oversized"
ACQUISITION_REFUSED = "playbill.acquisition.refused"

_OUTCOME_REASONS: dict[ProcedureAcquisitionOutcomeV1, str] = {
    "unavailable": ACQUISITION_UNAVAILABLE,
    "stale": ACQUISITION_STALE,
    "oversized": ACQUISITION_OVERSIZED,
    "refused": ACQUISITION_REFUSED,
}


class _StrictAcquisitionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProcedureSourceAcquisitionResultV1(_StrictAcquisitionModel):
    """One real, typed acquisition attempt outcome for one declared input."""

    tag: Literal["playbill-procedure-source-acquisition-v1"] = (
        "playbill-procedure-source-acquisition-v1"
    )
    node_id: str
    input_name: str
    outcome: ProcedureAcquisitionOutcomeV1
    acquisition: ExternalCaptureAcquisitionV1 | None = None
    reason_code: str | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> "ProcedureSourceAcquisitionResultV1":
        if (self.outcome == "acquired") != (self.acquisition is not None):
            raise ValueError("only an acquired source result carries a Capture acquisition")
        if self.outcome == "acquired":
            if self.reason_code is not None:
                raise ValueError("an acquired source result names no failure reason")
        elif self.reason_code != _OUTCOME_REASONS[self.outcome]:
            raise ValueError("source acquisition reason code disagrees with its outcome")
        return self


class ProcedureCaptureMaterialV1(_StrictAcquisitionModel):
    """One dereferenced Capture: exact envelope, committed value, and its grades."""

    tag: Literal["playbill-procedure-capture-material-v1"] = (
        "playbill-procedure-capture-material-v1"
    )
    capture_digest: str
    capture_contract_digest: str
    envelope: CaptureEnvelopeAny
    value: object
    epistemic_grade: EvidenceEpistemicGrade
    provenance_grade: EvidenceProvenanceGrade

    @field_validator("capture_digest", "capture_contract_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object) -> object:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _binding(self) -> "ProcedureCaptureMaterialV1":
        if capture_digest(self.envelope).tagged != self.capture_digest:
            raise ValueError("Capture material digest does not reproduce its envelope")
        if self.envelope.capture_contract_digest != self.capture_contract_digest:
            raise ValueError("Capture material names a different CaptureContract")
        return self


@runtime_checkable
class ProcedureSourceAcquirerProtocol(Protocol):
    """The complete execution-time acquisition surface: attempt and dereference."""

    def acquire(
        self,
        *,
        node_id: str,
        input_name: str,
        capture_contract: ArtifactPin,
        provider: ArtifactPin,
        request: CanonicalValue,
        run_id: str,
        bound_generation: str,
        observed_at: datetime,
    ) -> ProcedureSourceAcquisitionResultV1: ...

    def dereference(self, capture_digest_value: str) -> ProcedureCaptureMaterialV1: ...


def capture_provenance_grade(contract: CaptureContractV1) -> EvidenceProvenanceGrade:
    """Reuse the accepted Claim-side derivation; never invent a second ladder."""

    if capture_contract_is_self_asserted(contract):
        return "self-asserted"
    return "daemon-fetched"


def _refusal(
    node_id: str,
    input_name: str,
    outcome: ProcedureAcquisitionOutcomeV1,
    detail: str,
) -> ProcedureSourceAcquisitionResultV1:
    return ProcedureSourceAcquisitionResultV1(
        node_id=node_id,
        input_name=input_name,
        outcome=outcome,
        reason_code=_OUTCOME_REASONS[outcome],
        detail=detail,
    )


def apply_acquisition_result(
    rule: InputAcquisitionRuleV1,
    result: ProcedureSourceAcquisitionResultV1,
    *,
    default_authorized: bool,
) -> AcquisitionInputDecisionV1:
    """Apply the declared failure behaviour to a real typed acquisition result only."""

    if result.input_name != rule.input_name:
        raise ValueError("acquisition result names a different declared input")
    if result.outcome == "acquired":
        acquisition = result.acquisition
        if acquisition is None:  # pragma: no cover - model invariant
            raise ValueError("acquired result lost its Capture")
        return AcquisitionInputDecisionV1(
            input_name=rule.input_name,
            disposition="selected",
            considered_capture_digests=(acquisition.capture_digest,),
            selected_capture_digests=(acquisition.capture_digest,),
        )
    behavior = {
        "unavailable": rule.on_unavailable,
        "stale": rule.on_stale,
        "oversized": rule.on_oversized,
        "refused": "refuse",
    }[result.outcome]
    reason = _OUTCOME_REASONS[result.outcome]
    if behavior == "omit_optional" and rule.requirement == "optional":
        return AcquisitionInputDecisionV1(
            input_name=rule.input_name,
            disposition="omitted",
            reason_codes=(reason,),
        )
    if behavior == "declared_conservative_default" and default_authorized:
        return AcquisitionInputDecisionV1(
            input_name=rule.input_name,
            disposition="defaulted",
            default_value=rule.conservative_default,
            reason_codes=(reason,),
        )
    return AcquisitionInputDecisionV1(
        input_name=rule.input_name,
        disposition="refused",
        reason_codes=(reason,),
    )


__all__ = [
    "ACQUISITION_OVERSIZED",
    "ACQUISITION_REFUSED",
    "ACQUISITION_STALE",
    "ACQUISITION_UNAVAILABLE",
    "ProcedureAcquisitionOutcomeV1",
    "ProcedureCaptureMaterialV1",
    "ProcedureSourceAcquirerProtocol",
    "ProcedureSourceAcquisitionResultV1",
    "apply_acquisition_result",
    "capture_provenance_grade",
]
