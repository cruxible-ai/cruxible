"""Recover a `propose_change_set` egress that was prepared and never resolved.

A terminal journals a `prepared` record carrying its exact v2 request before
the proposal door is called, and a resolving record after. A process that
dies between the two leaves a run whose index says `running` with one more
prepared egress than resolved, and possibly a proposal the door already
created under the operation's ref.

Recovery re-derives the preparation from the journaled request -- the same
items, the same admitted base, the same deterministic Claim IDs -- and drives
the same idempotent door: an existing ref carrying the same member bytes
yields the same receipt, an absent ref is submitted exactly once, other bytes
under the key refuse. It then appends the resolving record and finalizes the
attempt as `failed` with `terminal_egress_recovered`, because the attempt did
not complete on its own; the delivered receipt on the run state is what says
the proposal exists and which one it is.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import ValidationError

from cruxible_client.contracts.canonical import CanonicalValue
from cruxible_client.contracts.procedures.results import (
    ProcedureBudgetBoundaryObservationV1,
    ProcedureRunBudgetDeclaredV1,
    ProcedureRunBudgetObservedV1,
    ProcedureRunBudgetV1,
)
from cruxible_client.contracts.temporal import format_datetime
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.exhaust import ProcedureExhaustWriter
from cruxible_core.playbill.exhaust.records import JournalEventKindV1, parse_journal_payload
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.procedures.egress import (
    TerminalEgressError,
    TerminalEgressRequestV2,
    verify_terminal_egress_receipt,
)
from cruxible_core.playbill.procedures.execution import (
    ProcedureAdmissionBoundPayloadV5,
    ProcedureRunAdmissionV5,
)
from cruxible_core.playbill.procedures.proposal_delivery import ProposalTerminalEgressSink
from cruxible_core.playbill.procedures.terminal_services import ProposalDeliveryRefused
from cruxible_core.service.playbill_procedure_runs import (
    PROCEDURE_RUN_FENCING_TOKEN,
    ProcedureRunRecoveryRequired,
    _accepted_line_mandates,
    _accepted_procedure,
    _activate_writer,
    _journal_for_write,
    _stream,
)

RecoveredProposalEgressDisposition = Literal["delivered", "refused"]


def service_recover_proposal_egress(
    instance: PlaybillInstance,
    *,
    recorded_at: datetime,
) -> dict[str, RecoveredProposalEgressDisposition]:
    """Resolve every prepared-but-unresolved proposal egress; return run -> disposition."""

    journal, _root = _journal_for_write(instance)
    stream = _stream(instance)
    bodies = instance.body_store()
    access = BodyAccessContext(principal_id="proposal-egress-recovery", can_read_body=True)
    recovered: dict[str, RecoveredProposalEgressDisposition] = {}
    for partition_id in journal.partition_ids(stream):
        records = journal.all_records(stream, partition_id)
        admission: ProcedureRunAdmissionV5 | None = None
        finalized = False
        prepared: dict[str, dict[str, Any]] = {}
        provider_calls = 0
        invocation_receipt_digests: list[str] = []
        associations: list[dict[str, str]] = []
        capture_bytes = 0
        for stored in records:
            payload = parse_journal_payload(
                bodies.read(stored.record.payload_digest, access=access)
            )
            kind = stored.record.event_kind
            if kind == "admission_bound" and isinstance(payload, dict):
                if payload.get("tag") == "playbill-procedure-admission-bound-payload-v5":
                    admission = ProcedureAdmissionBoundPayloadV5.model_validate(payload).admission
            elif kind == "provider_invocation_completed" and isinstance(payload, dict):
                provider_calls += 1
                digest = payload.get("receipt_digest")
                if isinstance(digest, str):
                    invocation_receipt_digests.append(digest)
            elif kind == "produced_capture" and isinstance(payload, dict):
                occurrence_path = payload.get("occurrence_path")
                receipt_digest = payload.get("invocation_receipt_digest")
                if isinstance(occurrence_path, str) and isinstance(receipt_digest, str):
                    associations.append(
                        {
                            "occurrence_path": occurrence_path,
                            "invocation_receipt_digest": receipt_digest,
                            "capture_digest": str(payload.get("capture_digest")),
                        }
                    )
            elif kind == "terminal_egress" and isinstance(payload, dict):
                node_id = str(payload.get("node_id"))
                verdict = payload.get("verdict")
                if verdict == "prepared":
                    prepared[node_id] = payload
                elif verdict in {"delivered", "refused", "failed"}:
                    prepared.pop(node_id, None)
            elif kind == "attempt_finalized":
                finalized = True
        if admission is None or finalized or not prepared:
            continue
        if len(prepared) != 1:
            raise ProcedureRunRecoveryRequired(
                f"{ProcedureRunRecoveryRequired.code}: run {admission.run_id} prepared more than "
                "one unresolved terminal egress"
            )
        ((node_id, prepared_payload),) = prepared.items()
        disposition, resolving = _resolve_prepared(
            instance,
            admission=admission,
            prepared_payload=prepared_payload,
            recorded_at=recorded_at,
        )
        writer = ProcedureExhaustWriter(
            journal=journal,
            bodies=bodies,
            fencing_token=PROCEDURE_RUN_FENCING_TOKEN,
        )
        writer_state = journal.writer_state(stream, partition_id)
        if (
            writer_state is not None
            and writer_state.active
            and writer_state.fencing_token != PROCEDURE_RUN_FENCING_TOKEN
        ):
            journal.fence_writer(
                stream,
                partition_id,
                expected_fencing_token=writer_state.fencing_token,
            )
        _activate_writer(journal, stream, partition_id)
        try:

            def _append(event_kind: JournalEventKindV1, payload: object) -> None:
                assert admission is not None
                writer.append(
                    stream=stream,
                    partition_id=partition_id,
                    event_kind=event_kind,
                    accepted_coordinate=admission.accepted_coordinate,
                    procedure_artifact_digest=admission.procedure_artifact_digest,
                    definition_digest=admission.definition_digest,
                    run_id=admission.run_id,
                    line_spec_digest=admission.line_spec_digest,
                    occurrence_id=admission.occurrence_id,
                    attempt=admission.attempt,
                    admission_binding_digest=admission.admission_binding_digest,
                    actor_context=admission.actor_context,
                    recorded_at=recorded_at,
                    payload=payload,
                )

            _append("terminal_egress", resolving)
            _append(
                "attempt_finalized",
                {
                    "status": "failed",
                    "output": None,
                    "refusal": None,
                    "failure": (
                        "The attempt crashed after preparing its proposal egress; recovery "
                        f"resolved the egress as {disposition}."
                    ),
                    "failure_code": "terminal_egress_recovered",
                    "failure_details": {"node_id": node_id, "disposition": disposition},
                    "halt": None,
                    "semantic_result_digest": None,
                    "provider_calls": provider_calls,
                    "capture_bytes": capture_bytes,
                    "invocation_receipt_digests": invocation_receipt_digests,
                    "source_capture_associations": [
                        {
                            "tag": "playbill-procedure-source-capture-association-v1",
                            **item,
                        }
                        for item in sorted(
                            associations,
                            key=lambda item: item["occurrence_path"].encode("utf-8"),
                        )
                    ],
                    "budget": ProcedureRunBudgetV1(
                        declared=ProcedureRunBudgetDeclaredV1(
                            budget=admission.budget,
                            hard_caps=admission.hard_caps,
                        ),
                        observed=ProcedureRunBudgetObservedV1(
                            max_items=ProcedureBudgetBoundaryObservationV1(high_water=0),
                            result_bytes=ProcedureBudgetBoundaryObservationV1(high_water=0),
                            provider_calls=provider_calls,
                            capture_bytes=capture_bytes,
                            wall_clock_microseconds=0,
                        ),
                    ).model_dump(mode="json"),
                },
            )
        finally:
            journal.fence_writer(
                stream,
                partition_id,
                expected_fencing_token=PROCEDURE_RUN_FENCING_TOKEN,
            )
        recovered[admission.run_id] = disposition
    return recovered


def _resolve_prepared(
    instance: PlaybillInstance,
    *,
    admission: ProcedureRunAdmissionV5,
    prepared_payload: Mapping[str, Any],
    recorded_at: datetime,
) -> tuple[RecoveredProposalEgressDisposition, dict[str, CanonicalValue]]:
    """Re-drive the idempotent door from the journaled request; return the resolving record."""

    base_payload: dict[str, CanonicalValue] = {
        key: value
        for key, value in prepared_payload.items()
        if key not in {"verdict", "request", "prepared", "evidence"}
    }
    try:
        request = TerminalEgressRequestV2.model_validate(prepared_payload.get("request"))
    except ValidationError as exc:
        raise ProcedureRunRecoveryRequired(
            f"{ProcedureRunRecoveryRequired.code}: run {admission.run_id} journaled an "
            "invalid prepared terminal request"
        ) from exc
    if (
        request.run_id != admission.run_id
        or request.admission_binding_digest != admission.admission_binding_digest
        or request.kind != "propose_change_set"
    ):
        raise ProcedureRunRecoveryRequired(
            f"{ProcedureRunRecoveryRequired.code}: run {admission.run_id} journaled a prepared "
            "request for another run"
        )
    raw_evidence = prepared_payload.get("evidence")
    evidence = (
        {str(key): str(value) for key, value in raw_evidence.items()}
        if isinstance(raw_evidence, dict)
        else {}
    )
    coordinate = instance.resolve_accepted_coordinate(
        git_oid=admission.accepted_coordinate.git_oid,
        semantic_root=admission.accepted_coordinate.semantic_root,
        generation_root=admission.accepted_coordinate.generation_root,
        compiler_digest=admission.accepted_coordinate.compiler_digest,
    )
    tree = instance.tree_at(coordinate.git_oid)
    accepted = _accepted_procedure(
        instance,
        name=admission.procedure_identity.name,
        coordinate=coordinate,
    )
    mandates = _accepted_line_mandates(
        tree,
        accepted,
        evaluation_time=request.evaluation_time,
    )
    sink = ProposalTerminalEgressSink(instance=instance, accepted_mandates=dict(mandates))
    try:
        prepared = sink.prepare_terminal_egress(
            request=request,
            admission=admission,
            evidence=evidence,
        )
        if (
            prepared.target_paths != request.target_paths
            or prepared.procedure_mandate_digest != request.procedure_mandate_digest
        ):
            raise ProposalDeliveryRefused(
                "effectful_operation_payload_mismatch",
                "Recovery re-derived other targets or another mandate than the journaled "
                "preparation.",
                details={
                    "journaled_target_paths": list(request.target_paths),
                    "rederived_target_paths": list(prepared.target_paths),
                },
            )
        receipt = sink.deliver_terminal_egress(request=request, admission=admission)
        verify_terminal_egress_receipt(request, receipt)
    except TerminalEgressError as exc:
        code = getattr(exc, "code", "terminal_egress_unverified")
        return "refused", {
            **base_payload,
            "verdict": "refused",
            "refusal_code": str(code),
            "recovery": {"detail": str(exc)},
        }
    return "delivered", {
        **base_payload,
        "verdict": "delivered",
        "granted_operation": request.granted_operation,
        "receipt": receipt.model_dump(mode="json"),
        "recovery": {"resolved_at": format_datetime(recorded_at)},
    }


__all__ = ["service_recover_proposal_egress"]
