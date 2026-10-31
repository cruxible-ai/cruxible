"""Admit the exact retained Capture which caused a Line occurrence."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from datetime import datetime

from cruxible_client.contracts.capture_reads import CaptureReadRequest
from cruxible_client.contracts.captures import CaptureContractAny as CaptureContract
from cruxible_client.contracts.captures import capture_retention_policy
from cruxible_client.contracts.procedures.acquisition_rules import AcquisitionRule
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedure
from cruxible_client.contracts.procedures.line_specs import LineSpec
from cruxible_client.contracts.procedures.trigger_rules import trigger_capture_target
from cruxible_client.contracts.procedures.windows import TriggerEventReference
from cruxible_core.procedures.acquisition import (
    ACQUISITION_STALE,
    ProcedureCaptureMaterialV1,
    capture_provenance_grade,
    capture_selection_failure,
)
from cruxible_core.procedures.execution import LandedCaptureRunMaterialV1
from cruxible_core.procedures.input_planes import LandedCaptureRunInputV1
from cruxible_core.procedures.reference_binding import resolved_pin
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.evidence.capture_reads import service_read_playbill_capture
from cruxible_core.service.procedures.resolution_contracts import (
    TriggerCaptureRefused,
    read_capture_event,
)
from cruxible_core.storage.cas import BodyAccessContext


def bind_trigger_capture(
    instance: PlaybillInstance,
    *,
    line: LineSpec,
    procedure: AcceptedProcedure,
    event: TriggerEventReference | None,
    contracts: MutableMapping[str, CaptureContract],
    rules: Mapping[str, AcquisitionRule],
    evaluation_time: datetime,
    max_bytes: int,
) -> LandedCaptureRunMaterialV1:
    """A trigger input must select its exact event; defaults and re-fetch are not substitutions.

    The event is the one the Trigger fired on, or, for a manual ``line run``,
    the one the caller passed as the Line's event input.
    """
    node, output = trigger_capture_target(line, procedure)
    selector = line.trigger_event
    if event is None or selector is None:
        raise TriggerCaptureRefused(
            "trigger_capture_invalid",
            "trigger_capture_input: an exact retained event is required "
            "(a manual run passes it as the event input)",
        )
    rule = rules.get(output.acquisition_key)
    if rule is None:
        raise TriggerCaptureRefused(
            "trigger_capture_invalid",
            "trigger_capture_input: the capture output has no acquisition rule",
        )
    contract = contracts.get(resolved_pin(procedure, node.captures[output.field]).artifact_digest)
    if contract is None or contract.identity != selector.capture_contract_identity:
        raise TriggerCaptureRefused(
            "trigger_capture_invalid",
            "trigger_capture_input: exact accepted CaptureContract missing",
        )
    if capture_retention_policy(contract).body_retention == "never_materialize":
        raise TriggerCaptureRefused(
            "trigger_capture_forbidden",
            "trigger_capture_input: CaptureContract forbids materialization",
        )
    record, payload = read_capture_event(instance, selector, event, now=evaluation_time)
    digest = payload.get("capture_digest")
    if not isinstance(digest, str):
        raise TriggerCaptureRefused(
            "trigger_capture_invalid", "trigger_capture_input: event has no Capture digest"
        )
    read = service_read_playbill_capture(
        instance,
        request=CaptureReadRequest(
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
    if envelope.capture_contract_digest != payload.get("capture_contract_digest"):
        raise TriggerCaptureRefused(
            "trigger_capture_invalid",
            "trigger_capture_input: Capture differs from the event contract",
        )
    exact = instance.accepted_capture_contract_version(
        record.accepted_coordinate, envelope.capture_contract_digest
    )
    if exact is None or exact.contract.identity != selector.capture_contract_identity:
        raise TriggerCaptureRefused(
            "trigger_capture_invalid",
            "The event's exact contract is unavailable or names another contract",
        )
    contracts[exact.artifact_digest] = exact.contract
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
            f"Capture output {output.acquisition_key!r} has Capture age "
            f"{(evaluation_time - envelope.observed_at).total_seconds()}s, exceeding max_age; "
            f"repair: supply a fresh Capture for {output.acquisition_key!r}."
            if failure == ACQUISITION_STALE
            else "The acquisition rule forbids this Capture.",
        )
    if material.material_kind != "bytes" or material.body_access is None:
        raise TriggerCaptureRefused(
            "trigger_capture_invalid", "trigger_capture_input: Capture bytes unavailable"
        )
    # The substituted call does not run: its value names only the Capture its
    # one capture field binds, and an extract reads the Capture's own bytes.
    value = {output.field: digest}
    # No material change or fresh acquisition: keep the original observation and producer proof.
    return LandedCaptureRunMaterialV1(
        input=LandedCaptureRunInputV1(
            input_name=output.acquisition_key,
            capture_digest=digest,
            capture_contract_digest=envelope.capture_contract_digest,
            landing_cursor="procedure-event:" + event.record_digest,
        ),
        material=ProcedureCaptureMaterialV1(
            capture_digest=digest,
            capture_contract_digest=envelope.capture_contract_digest,
            envelope=envelope,
            value=value,
            epistemic_grade=contract.epistemic_grade,
            provenance_grade=capture_provenance_grade(contract),
        ),
    )
