"""Execution-time source acquisition: the only place a typed result may be read.

``on_unavailable``/``on_stale``/``on_oversized`` never fire from a scan, a plan,
or an absence.  They fire from a :class:`ProcedureSourceAcquisitionResultV1`,
which one acquirer produced by really attempting the read.  The Capture such an
attempt produces belongs to the acquisition manifest; it never re-enters the
admitted-input tuple and never relabels its capture time.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_core.playbill.acquisition_policies import (
    AcquisitionInputDecisionV1,
    InputAcquisitionRuleV1,
)
from cruxible_core.playbill.artifacts import ArtifactPin
from cruxible_core.playbill.canonical import (
    CanonicalValue,
    Sha256Value,
    normalize_canonical,
)
from cruxible_core.playbill.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    CaptureContractV1,
    CaptureEnvelopeV1,
    CaptureObjectStoreProtocol,
    CaptureRunCoordinateV1,
    CaptureSelectionBudgetV1,
    capture_contract_digest,
    capture_digest,
    parse_capture_envelope,
)
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.claim_verdicts import (
    EvidenceEpistemicGrade,
    EvidenceProvenanceGrade,
)
from cruxible_core.playbill.errors import PlaybillCasError
from cruxible_core.playbill.providers import ProviderV1, provider_digest
from cruxible_core.playbill.source_readers import (
    ExternalCaptureAcquisitionV1,
    ExternalSourceError,
    ExternalSourceReaderProtocol,
    ExternalSourceReadRequestV1,
    ProducerBindingV1,
)

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
    envelope: CaptureEnvelopeV1
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

    if contract == DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT:
        return "self-asserted"
    return "daemon-fetched"


class ExternalSourceAcquirer:
    """Bind one read-only external reader to the exact accepted contracts it serves."""

    def __init__(
        self,
        *,
        reader: ExternalSourceReaderProtocol,
        store: CaptureObjectStoreProtocol,
        contracts: dict[str, CaptureContractV1],
        providers: dict[str, ProviderV1],
        bindings: dict[str, ProducerBindingV1],
        budgets: dict[str, CaptureSelectionBudgetV1] | None = None,
        access: BodyAccessContext | None = None,
    ) -> None:
        self.reader = reader
        self.store = store
        self.contracts = contracts
        self.providers = providers
        self.bindings = bindings
        self.budgets = budgets or {}
        self.access = access or BodyAccessContext(
            principal_id="procedure-source-acquirer",
            can_read_body=True,
        )

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
    ) -> ProcedureSourceAcquisitionResultV1:
        contract = self.contracts.get(capture_contract.artifact_digest)
        accepted_provider = self.providers.get(provider.artifact_digest)
        binding = self.bindings.get(provider.artifact_digest)
        if contract is None or accepted_provider is None or binding is None:
            return _refusal(node_id, input_name, "unavailable", "source binding is unresolved")
        if not isinstance(request, dict):
            return _refusal(node_id, input_name, "refused", "source request is not an object")
        materialization = request.get("materialization", "cas")
        budget = self.budgets.get(input_name) or contract.selection_budget
        try:
            run_coordinate = CaptureRunCoordinateV1(
                run_kind="provider",
                run_id=run_id,
                bound_generation=bound_generation,
                executable_identity=accepted_provider.identity,
                executable_digest=provider_digest(accepted_provider).tagged,
            )
            read = ExternalSourceReadRequestV1(
                contract=contract,
                provider=accepted_provider,
                binding=binding,
                coordinate_type=str(request.get("coordinate_type")),
                coordinate=request.get("coordinate"),
                selector_type=str(request.get("selector_type")),
                selector=request.get("selector"),
                materialization=materialization,  # type: ignore[arg-type]
                run_coordinate=run_coordinate,
                observed_at=observed_at,
                resource_budget=budget,
            )
        except ValueError as exc:
            return _refusal(node_id, input_name, "refused", str(exc))
        try:
            acquisition = self.reader.acquire(read, store=self.store)
        except ExternalSourceError as exc:
            outcome: ProcedureAcquisitionOutcomeV1 = (
                "oversized" if "budget" in str(exc) else "unavailable"
            )
            return _refusal(node_id, input_name, outcome, str(exc))
        return ProcedureSourceAcquisitionResultV1(
            node_id=node_id,
            input_name=input_name,
            outcome="acquired",
            acquisition=acquisition,
        )

    def dereference(self, capture_digest_value: str) -> ProcedureCaptureMaterialV1:
        try:
            envelope_bytes = self.store.read(capture_digest_value, access=self.access)
        except PlaybillCasError as exc:
            raise ExternalSourceError("admitted Capture envelope is unavailable") from exc
        envelope = parse_capture_envelope(envelope_bytes)
        contract = self.contracts.get(envelope.capture_contract_digest)
        if contract is None or capture_contract_digest(contract).tagged != (
            envelope.capture_contract_digest
        ):
            raise ExternalSourceError("admitted Capture names an unresolved CaptureContract")
        try:
            material = self.store.read(envelope.commitment.digest, access=self.access)
        except PlaybillCasError as exc:
            raise ExternalSourceError("admitted Capture material is unavailable") from exc
        try:
            value = normalize_canonical(json.loads(material))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ExternalSourceError("admitted Capture material is not canonical") from exc
        return ProcedureCaptureMaterialV1(
            capture_digest=capture_digest_value,
            capture_contract_digest=envelope.capture_contract_digest,
            envelope=envelope,
            value=value,
            epistemic_grade=contract.epistemic_grade,
            provenance_grade=capture_provenance_grade(contract),
        )


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
    "ExternalSourceAcquirer",
    "ProcedureAcquisitionOutcomeV1",
    "ProcedureCaptureMaterialV1",
    "ProcedureSourceAcquirerProtocol",
    "ProcedureSourceAcquisitionResultV1",
    "apply_acquisition_result",
    "capture_provenance_grade",
]
