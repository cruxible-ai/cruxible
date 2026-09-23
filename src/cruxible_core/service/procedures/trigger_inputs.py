"""Admit the exact retained Capture which caused a Line occurrence."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import datetime

from cruxible_client.contracts.acquisition_policies import (
    IndependentCoherenceV1,
    SourceAcquisitionPolicyV1,
)
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.capture_reads import CaptureReadRequestV1
from cruxible_client.contracts.captures import CaptureContractV1
from cruxible_client.contracts.errors import PlaybillExecutionError
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedureV1
from cruxible_client.contracts.procedures.line_specs import (
    LineSpecV4,
    trigger_capture_selector,
    trigger_capture_source,
)
from cruxible_client.contracts.procedures.results import ProcedureAdmissionRefusalCodeV1
from cruxible_client.contracts.procedures.windows import LineTriggerBindingV1
from cruxible_core.procedures.acquisition import (
    ACQUISITION_STALE,
    ProcedureCaptureMaterialV1,
    capture_provenance_grade,
    capture_selection_failure,
)
from cruxible_core.procedures.execution import LandedCaptureRunMaterialV1
from cruxible_core.procedures.input_planes import LandedCaptureRunInputV1
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.evidence.capture_reads import service_read_playbill_capture
from cruxible_core.service.procedures.resolution_contracts import read_capture_event
from cruxible_core.storage.cas import BodyAccessContext


class TriggerCaptureRefused(PlaybillExecutionError):
    def __init__(
        self,
        code: ProcedureAdmissionRefusalCodeV1,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.refusal_code = code
        self.retryable = retryable
        self.details = details or {}


def bind_trigger_capture(
    instance: PlaybillInstance,
    *,
    line: LineSpecV4,
    procedure: AcceptedProcedureV1,
    binding: LineTriggerBindingV1 | None,
    contracts: Mapping[str, CaptureContractV1],
    policy: SourceAcquisitionPolicyV1,
    evaluation_time: datetime,
    max_bytes: int,
) -> LandedCaptureRunMaterialV1:
    """A trigger input must select its exact event; defaults and re-fetch are not substitutions."""
    node = trigger_capture_source(line, procedure)
    selector = trigger_capture_selector(line)
    if binding is None or binding.event is None or selector is None:
        raise TriggerCaptureRefused(
            "trigger_capture_invalid", "trigger_capture_input: an exact retained event is required"
        )
    rule = next((r for r in policy.inputs if r.input_name == node.as_), None)
    if rule is None or not isinstance(policy.coherence, IndependentCoherenceV1):
        raise TriggerCaptureRefused(
            "trigger_capture_invalid",
            "trigger_capture_input: named independent acquisition rule required",
        )
    contract = contracts.get(selector.capture_contract_digest)
    if contract is None or contract.identity != selector.capture_contract_identity:
        raise TriggerCaptureRefused(
            "trigger_capture_invalid",
            "trigger_capture_input: exact accepted CaptureContract missing",
        )
    if contract.retention_erasure_policy.body_retention == "never_materialize":
        raise TriggerCaptureRefused(
            "trigger_capture_forbidden",
            "trigger_capture_input: CaptureContract forbids materialization",
        )
    record, payload = read_capture_event(instance, selector, binding.event, now=evaluation_time)
    digest = payload.get("capture_digest")
    if not isinstance(digest, str):
        raise TriggerCaptureRefused(
            "trigger_capture_invalid", "trigger_capture_input: event has no Capture digest"
        )
    read = service_read_playbill_capture(
        instance,
        request=CaptureReadRequestV1(
            capture_digest=digest,
            at=record.accepted_coordinate,
            max_bytes=max_bytes,
        ),
        access=BodyAccessContext(principal_id="line-trigger-input", can_read_body=True),
    )
    material = read.material
    envelope = read.envelope
    if material is not None and "resource_budget_exceeded" in material.coverage.reason_codes:
        raise TriggerCaptureRefused(
            "trigger_capture_over_budget",
            "The exact triggering Capture exceeds its bound read budget.",
            details={
                "line_max_capture_bytes": max_bytes,
                "capture_contract_max_bytes": contract.selection_budget.max_bytes,
                "effective_max_bytes": min(max_bytes, contract.selection_budget.max_bytes),
                "limiting_budget": "line"
                if max_bytes < contract.selection_budget.max_bytes
                else "capture_contract"
                if max_bytes > contract.selection_budget.max_bytes
                else "line_and_capture_contract",
            },
        )
    if (
        read.status != "verified"
        or envelope is None
        or material is None
        or material.status != "verified"
    ):
        raise TriggerCaptureRefused(
            "trigger_capture_unavailable", "The exact retained Capture material is unavailable."
        )
    if envelope.capture_contract_digest != selector.capture_contract_digest:
        raise TriggerCaptureRefused(
            "trigger_capture_invalid",
            "trigger_capture_input: Capture differs from the event contract",
        )
    if envelope.observed_at > evaluation_time:
        raise TriggerCaptureRefused(
            "trigger_capture_not_yet_observed",
            "Capture observation is in the future.",
            retryable=True,
        )
    failure = capture_selection_failure(rule, envelope, evaluation_time=evaluation_time)
    if failure is not None:
        raise TriggerCaptureRefused(
            "trigger_capture_stale"
            if failure == ACQUISITION_STALE
            else "trigger_capture_forbidden",
            "The exact triggering Capture exceeds max_age at admission time."
            if failure == ACQUISITION_STALE
            else "The acquisition policy forbids this Capture replayability.",
        )
    if material.material_kind == "bytes":
        body = material.body_access
        if body is None or body.body_base64 is None:
            raise TriggerCaptureRefused(
                "trigger_capture_invalid", "trigger_capture_input: Capture bytes unavailable"
            )
        raw = base64.b64decode(body.body_base64, validate=True)
        value = json.loads(raw)
        if canonical_bytes(value) != raw:
            raise TriggerCaptureRefused(
                "trigger_capture_invalid",
                "trigger_capture_input: Source input must be canonical JSON",
            )
    elif material.material_kind in {"canonical_value", "query_result"}:
        value = material.canonical_material
    else:
        raise TriggerCaptureRefused(
            "trigger_capture_invalid", "trigger_capture_input: Capture has no executable material"
        )
    # No material change or fresh acquisition: keep the original observation and producer proof.
    return LandedCaptureRunMaterialV1(
        input=LandedCaptureRunInputV1(
            input_name=node.as_,
            capture_digest=digest,
            capture_contract_digest=selector.capture_contract_digest,
            landing_cursor="procedure-event:" + binding.event.record_digest,
        ),
        material=ProcedureCaptureMaterialV1(
            capture_digest=digest,
            capture_contract_digest=selector.capture_contract_digest,
            envelope=envelope,
            value=value,
            epistemic_grade=contract.epistemic_grade,
            provenance_grade=capture_provenance_grade(contract),
        ),
    )
