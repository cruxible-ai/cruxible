"""Re-lower one retained Procedure terminal; never invoke its providers or state taps."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone

from pydantic import ValidationError

from cruxible_client.contracts.candidates import canonical_candidate_timestamp
from cruxible_client.contracts.errors import CruxibleError, ProposalStale
from cruxible_client.contracts.procedure_mandates import ProcedureMandate
from cruxible_core.authoring.assignment_records import procedure_operation_id
from cruxible_core.exhaust.payloads import UnavailablePayload, read_payload
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.procedures.egress import (
    PreparedTerminalEgressV1,
    TerminalEgressRequestV2,
    require_procedure_mandate_at_head,
)
from cruxible_core.procedures.execution import (
    ProcedureAdmissionBoundPayloadV5,
    ProcedureRunAdmissionV5,
    parse_admission_payload,
)
from cruxible_core.procedures.proposal_delivery import ProposalTerminalEgressSink
from cruxible_core.procedures.terminal_services import ProposalDeliveryRefused
from cruxible_core.proposals.proposals import ProposalResult
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.procedures.procedure_runs import (
    _accepted_line_mandates,
    _accepted_procedure,
    _journal,
    _stream,
)
from cruxible_core.service.proposals.recompile import RecompileInput
from cruxible_core.storage.cas import BodyAccessContext


def procedure_input(
    instance: PlaybillInstance, origin: ProposalResult, head: AcceptedCoordinate
) -> RecompileInput:
    """Locate by operation/member then indexed run/event, bounded by this run's terminals."""
    source_id = origin.admission.proposal_id
    operation = origin.admission.procedure_operation
    assert operation is not None
    candidate = origin.candidate
    reason = next(
        (
            reason
            for member in (() if candidate is None else candidate.members)
            if (reason := instance.proposal_evidence().read_item_reason(operation, member.path))
            is not None
        ),
        None,
    )
    if reason is None:
        raise ProposalStale(
            source_id, reason="input_unavailable", detail="Procedure run locator is unavailable"
        )
    if not (instance.root / instance.descriptor.storage.exhaust / "procedure-runs").is_dir():
        raise ProposalStale(
            source_id, reason="input_unavailable", detail="Procedure journal is unavailable"
        )
    journal, _ = _journal(instance)
    stream = _stream(instance)
    bodies = instance.body_store()
    access = BodyAccessContext(principal_id="proposal-recompile", can_read_body=True)
    admission: ProcedureRunAdmissionV5 | None = None
    prepared_payload: Mapping[str, object] | None = None
    for stored in journal.select_records(
        stream, run_id=reason.run_id, event_kind="admission_bound"
    ):
        payload = read_payload(bodies, stored.record.payload_digest, access=access)
        if not isinstance(payload, UnavailablePayload):
            parsed = parse_admission_payload(payload)
            if isinstance(parsed, ProcedureAdmissionBoundPayloadV5):
                admission = parsed.admission
    for stored in journal.select_records(
        stream, run_id=reason.run_id, event_kind="terminal_egress"
    ):
        payload = read_payload(bodies, stored.record.payload_digest, access=access)
        if (
            isinstance(payload, dict)
            and payload.get("node_id") == reason.terminal
            and payload.get("verdict") == "prepared"
        ):
            prepared_payload = payload
    if admission is None or prepared_payload is None:
        raise ProposalStale(
            source_id,
            reason="input_unavailable",
            detail="Procedure prepared request or admission is unavailable",
        )
    try:
        request = TerminalEgressRequestV2.model_validate(prepared_payload.get("request"))
        journaled = PreparedTerminalEgressV1.model_validate(prepared_payload.get("prepared"))
    except ValidationError as exc:
        raise ProposalStale(
            source_id, reason="input_unavailable", detail="Invalid retained Procedure request"
        ) from exc
    if (
        request.run_id != admission.run_id
        or request.admission_binding_digest != admission.admission_binding_digest
        or procedure_operation_id(
            admission_binding_digest=request.admission_binding_digest, node_id=request.node_id
        )
        != operation
        or request.actor_context.actor_id != origin.admission.actor_id
    ):
        raise ProposalStale(
            source_id,
            reason="input_unavailable",
            detail="Procedure request does not match its origin",
        )
    admitted_request = request
    request = request.model_copy(update={"accepted_coordinate": head})
    raw_evidence = prepared_payload.get("evidence")
    evidence = (
        {str(key): str(value) for key, value in raw_evidence.items()}
        if isinstance(raw_evidence, dict)
        else {}
    )
    coordinate = instance.accepted_coordinate()
    delegation = None
    from cruxible_core.procedures.execution import nested_admission

    nested = nested_admission(admission)
    if nested is not None:
        from cruxible_core.service.procedures.nested_runs import retained_delegation

        retained = retained_delegation(instance, nested)
        if isinstance(retained, UnavailablePayload):
            raise ProposalStale(
                source_id, reason="input_unavailable", detail="Procedure delegation is unavailable"
            )
        delegation = retained
    name = (
        request.procedure_identity.name
        if delegation is None or nested is None
        else delegation.authority(nested).target.name
    )
    try:
        accepted = _accepted_procedure(instance, name=name, coordinate=coordinate)
        mandates = _accepted_line_mandates(
            instance, accepted, coordinate=coordinate, evaluation_time=request.evaluation_time
        )
        sink = ProposalTerminalEgressSink(
            instance=instance, accepted_mandates=dict(mandates), delegation=delegation
        )
        prepared = sink.prepare_recompilation(
            request=request, admission=admission, evidence=evidence, journaled=journaled
        )
        request = request.model_copy(
            update={
                "target_paths": prepared.prepared.target_paths,
                "procedure_mandate_digest": prepared.prepared.procedure_mandate_digest,
            }
        )
        # A delegated settle cannot silently change its bound grant or become
        # an ordinary fallback. Settlement rechecks its condition at the head.
        settle = origin.admission.settle_submission
        if settle is not None and settle.mode == "delegated":
            if request.procedure_mandate_digest != settle.mandate_digest:
                raise ProposalDeliveryRefused(
                    "settle_mandate_missing",
                    "The original settle mandate no longer authorizes this change.",
                )
            mandate = dict(mandates).get(settle.mandate_digest)
            now = datetime.now(timezone.utc)
            if (
                not isinstance(mandate, ProcedureMandate)
                or mandate.suspended
                or not (mandate.valid_from <= now < mandate.expires_at)
            ):
                raise ProposalDeliveryRefused(
                    "settle_publication_refused",
                    "The settle mandate expired or was suspended before publication.",
                )
    except CruxibleError as exc:
        raise ProposalStale(
            source_id, reason=getattr(exc, "code", "lowering_refused"), detail=str(exc)
        ) from exc
    # Authority authenticates the original admitted run; only lowering moves
    # its coordinate. The current projection below rechecks the bound grant,
    # so this does not invent a new admission or read the old accepted state.
    authority_request = admitted_request.model_copy(
        update={
            "target_paths": prepared.prepared.target_paths,
            "procedure_mandate_digest": prepared.prepared.procedure_mandate_digest,
        }
    )
    claim_outputs = {
        path: content for path, content in prepared.changed_members if path.startswith("claims/")
    }

    def authorize(
        current: AcceptedProjectionCoordinate, _tree: Mapping[str, bytes]
    ) -> Mapping[str, bytes]:
        with instance.bind_accepted_projection(current) as projection:
            require_procedure_mandate_at_head(
                authority_request, admission=admission, projection=projection, delegation=delegation
            )
        return claim_outputs

    return RecompileInput(
        tree=prepared.candidate_tree,
        timestamp=canonical_candidate_timestamp(request.evaluation_time),
        rationale=prepared.rationale,
        operation=operation,
        creations=prepared.creations,
        authorize=authorize,
        settle_submission=origin.admission.settle_submission,
    )
