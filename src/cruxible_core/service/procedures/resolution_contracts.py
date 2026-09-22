"""Exact contract and event binding shared by direct runs and Lines."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claims import ClaimArtifactAny, claim_path, parse_claim
from cruxible_client.contracts.errors import PlaybillExecutionError, PlaybillFormatError
from cruxible_client.contracts.procedures.windows import (
    BoundObservationWindowV1,
    CaptureEventSelectorV1,
    CaptureEventWindowV1,
    FixedWindowV1,
    LineTriggerBindingV1,
    ObservationWindowV1,
    TriggerEventReferenceV1,
    bind_observation_window,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.resolution_contracts import (
    ClaimVersionReferenceV1,
    InvestigationBindingV1,
    ResolutionContractReferenceV1,
    ResolutionContractsRequestV1,
    ResolutionContractsResultV1,
    ResolutionContractV1,
    ResolutionContractViewV1,
    parse_resolution_contract,
    resolution_contract_digest,
    resolution_contract_path,
)
from cruxible_core.compiler.compiler import artifact_codec_for_compiler
from cruxible_core.exhaust.records import ProcedureJournalRecordV1, parse_journal_payload
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.storage.cas import BodyAccessContext


def read_claim_reference(
    instance: PlaybillInstance, reference: ClaimVersionReferenceV1
) -> ClaimArtifactAny:
    coordinate = instance.resolve_accepted_coordinate(
        **reference.coordinate.model_dump(exclude={"tag"})
    )
    path = claim_path(reference.identity.name)
    raw = instance.blob_at(coordinate.git_oid, path)
    if raw is None:
        raise PlaybillFormatError("exact accepted Claim is absent")
    claim = parse_claim(raw, path=path, codec=artifact_codec_for_compiler(coordinate.compiler))
    reference.verify(claim)
    return claim


def read_resolution_contract(
    instance: PlaybillInstance, reference: ResolutionContractReferenceV1
) -> ResolutionContractV1:
    coordinate = instance.resolve_accepted_coordinate(
        **reference.coordinate.model_dump(exclude={"tag"})
    )
    path = resolution_contract_path(reference.identity.name)
    with instance.bind_accepted_projection(coordinate) as projection:
        row = projection.typed.envelope(reference.identity.qualified)
        if row is None or row.path != path or row.artifact_digest != reference.artifact_digest:
            raise PlaybillExecutionError("resolution contract does not match its accepted binding")
        contract = parse_resolution_contract(
            projection.typed.member_bytes(path),
            path=path,
            codec=artifact_codec_for_compiler(coordinate.compiler),
        )
    if resolution_contract_digest(contract).tagged != reference.artifact_digest:
        raise PlaybillExecutionError("resolution contract digest does not reproduce")
    with instance.accepted_history_reader(at=reference.coordinate) as history:
        if history.generation_for_oid(contract.hypothesis.coordinate.git_oid) is None:
            raise PlaybillExecutionError("hypothesis is not accepted before its contract")
    contract.verify_hypothesis(read_claim_reference(instance, contract.hypothesis))
    return contract


def read_capture_event(
    instance: PlaybillInstance,
    selector: CaptureEventSelectorV1,
    reference: TriggerEventReferenceV1,
    *,
    now: datetime,
) -> tuple[ProcedureJournalRecordV1, Mapping[str, object]]:
    from cruxible_core.service.procedures.procedure_runs import _journal, _stream

    journal, _ = _journal(instance)
    selected = journal.range_from_sequences(
        _stream(instance),
        reference.partition_id,
        first_sequence=reference.sequence,
        last_sequence=reference.sequence,
    )
    if selected.expected_head_digest != reference.record_digest:
        raise PlaybillExecutionError("trigger event does not reproduce its retained record")
    (stored,) = journal.read_exact_range(selected)
    record = stored.record
    if record.run_id != reference.run_id or record.event_kind != "produced_capture":
        raise PlaybillExecutionError("trigger requires a produced Capture from the exact run")
    payload = parse_journal_payload(
        instance.body_store().read(
            record.payload_digest,
            access=BodyAccessContext(principal_id="trigger-binding", can_read_body=True),
        )
    )
    if (
        not isinstance(payload, dict)
        or payload.get("tag") != "playbill-procedure-produced-capture-v1"
        or payload.get("capture_contract_digest") != selector.capture_contract_digest
    ):
        raise PlaybillExecutionError("trigger event does not match its CaptureContract selector")
    if record.recorded_at > now:
        raise PlaybillExecutionError("trigger event has not occurred at the evaluation instant")
    return record, payload


def capture_event_time(
    instance: PlaybillInstance,
    selector: CaptureEventSelectorV1,
    reference: TriggerEventReferenceV1,
    *,
    now: datetime,
) -> datetime:
    record, _ = read_capture_event(instance, selector, reference, now=now)
    return record.recorded_at


def bind_window(
    instance: PlaybillInstance,
    policy: ObservationWindowV1,
    event: TriggerEventReferenceV1 | None,
    *,
    now: datetime,
) -> BoundObservationWindowV1:
    if isinstance(policy, CaptureEventWindowV1):
        if event is None:
            raise PlaybillExecutionError("window is waiting for its retained capture event")
        instant = capture_event_time(instance, policy.event, event, now=now)
        return bind_observation_window(policy, event=event, event_time=instant)
    return bind_observation_window(policy, event=event)


def bind_investigation(
    instance: PlaybillInstance,
    reference: ResolutionContractReferenceV1,
    *,
    event: TriggerEventReferenceV1 | None,
    now: datetime,
    trigger_binding: LineTriggerBindingV1 | None = None,
) -> InvestigationBindingV1:
    contract = read_resolution_contract(instance, reference)
    reference = canonical_contract_reference(instance, reference)
    if now < artifact_accepted_time(instance, reference):
        raise PlaybillExecutionError("investigation cannot precede acceptance of its contract")
    # A capture can trigger a Line without defining the contract's fixed window.
    # Only ignore it here when that same event is already bound to the trigger.
    window_event = event
    if (
        isinstance(contract.window, FixedWindowV1)
        and trigger_binding is not None
        and trigger_binding.event == event
    ):
        window_event = None
    return InvestigationBindingV1(
        contract=reference,
        hypothesis=contract.hypothesis,
        window=bind_window(instance, contract.window, window_event, now=now),
    )


def canonical_contract_reference(
    instance: PlaybillInstance, reference: ResolutionContractReferenceV1
) -> ResolutionContractReferenceV1:
    """An unchanged contract keeps its identity across later lookup snapshots."""
    with instance.accepted_history_reader(at=reference.coordinate) as history:
        occurrence = history.artifact(
            reference.artifact_digest, identity=reference.identity.qualified
        )
        if occurrence is None:
            raise PlaybillExecutionError("contract version has no accepted occurrence")
        generation = history.generation(occurrence.occurrence_sequence)
    return reference.model_copy(
        update={
            "coordinate": AcceptedCoordinate(
                git_oid=generation.git_oid,
                semantic_root=generation.semantic_root,
                generation_root=generation.generation_root,
                compiler_digest=generation.compiler_digest,
            )
        }
    )


def artifact_accepted_time(
    instance: PlaybillInstance, reference: ResolutionContractReferenceV1 | ClaimVersionReferenceV1
) -> datetime:
    """Use the actual occurrence, not an arbitrary later snapshot containing it."""
    with instance.accepted_history_reader(at=reference.coordinate) as history:
        occurrence = history.artifact(
            reference.artifact_digest, identity=reference.identity.qualified
        )
        if occurrence is None:
            raise PlaybillExecutionError("artifact version has no accepted occurrence")
        generation = history.generation(occurrence.occurrence_sequence)
    return instance.accepted_evaluation_time(generation.git_oid)


def require_current_investigation(
    instance: PlaybillInstance, binding: InvestigationBindingV1
) -> None:
    """Admission gate for a new attempt; retained run replay uses its original binding."""
    current = instance.accepted_coordinate()
    with instance.bind_accepted_projection(current) as projection:
        state = projection.typed.dependency_state(binding.contract.identity.qualified)
        if (
            state is None
            or state.artifact_digest != binding.contract.artifact_digest
            or state.lifecycle.state != "live"
        ):
            raise PlaybillExecutionError(
                "new investigation requires the current live contract version"
            )


def service_resolution_contracts(
    instance: PlaybillInstance, request: ResolutionContractsRequestV1
) -> ResolutionContractsResultV1:
    """Index lookup by exact hypothesis; only matching contract bodies are loaded."""
    at = (
        instance.accepted_coordinate()
        if request.at is None
        else instance.resolve_accepted_coordinate(**request.at.model_dump(exclude={"tag"}))
    )
    coordinate = AcceptedCoordinate.from_internal(at)
    with instance.accepted_history_reader(at=coordinate) as history:
        if history.generation_for_oid(request.hypothesis.coordinate.git_oid) is None:
            raise PlaybillExecutionError("hypothesis is outside the requested accepted history")
    read_claim_reference(instance, request.hypothesis)
    with instance.bind_accepted_projection(at) as projection:
        rows = projection.typed.connection.execute(
            "SELECT identity,artifact_digest,path FROM resolution_contracts WHERE "
            "hypothesis_identity=? AND hypothesis_artifact_digest=? ORDER BY identity",
            (request.hypothesis.identity.qualified, request.hypothesis.artifact_digest),
        ).fetchall()
        projection.typed.prefetch_members(tuple(row[2] for row in rows))
        views = []
        for identity, digest, path in rows:
            kind, name = identity.split(":", 1)
            ref = ResolutionContractReferenceV1(
                identity=ArtifactIdentity(kind=kind, name=name),
                artifact_digest=digest,
                coordinate=coordinate,
            )
            contract = parse_resolution_contract(
                projection.typed.member_bytes(path),
                path=path,
                codec=artifact_codec_for_compiler(at.compiler),
            )
            if (
                resolution_contract_digest(contract).tagged != digest
                or contract.hypothesis.statement_digest != request.hypothesis.statement_digest
            ):
                raise PlaybillExecutionError("indexed contract differs from its exact hypothesis")
            views.append(
                ResolutionContractViewV1(
                    reference=canonical_contract_reference(instance, ref), contract=contract
                )
            )
    return ResolutionContractsResultV1(coordinate=coordinate, contracts=tuple(views))
