"""Nested runs reuse the served admission, provider, evidence and terminal doors."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.procedure_mandates import ProcedureMandateV1
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedureV1
from cruxible_client.contracts.procedures.models import (
    TERMINAL_REQUIRED_RUNGS,
    InvokeNodeV6,
    ProcedureBudgetV3,
    ProcedureDefinitionV5,
    ProcedureDefinitionV6,
    ProcedureHardCapsV3,
)
from cruxible_core.documents.workspace_file import WorkspaceFileReader
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.procedures.egress import CaptureTerminalEgressSink
from cruxible_core.procedures.execution import (
    ProcedureBoundaryRefused,
    ProcedureClockProtocol,
    ProcedureRunAdmissionV1,
    ProcedureRunAdmissionV8,
    ProcedureRunResultV1,
)
from cruxible_core.procedures.nested import (
    ParentInvocationContext,
    ProcedureDelegation,
    constrained_budget,
    constrained_caps,
)
from cruxible_core.procedures.proposal_delivery import ProposalTerminalEgressSink
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.storage.material_reservations import (
    ProcedureMaterialReservationStore,
    ReservedCaptureStore,
)


def retained_delegation(
    instance: PlaybillInstance, admission: ProcedureRunAdmissionV8
) -> ProcedureDelegation:
    """Rebuild delegation from the recorded parent call, never a caller's ancestry claim."""
    from cruxible_core.exhaust.records import parse_journal_payload
    from cruxible_core.procedures.execution import ProcedureRunAdmissionV2, parse_admission_payload
    from cruxible_core.service.procedures.procedure_runs import (
        ProcedureRunRecoveryRequired,
        _accepted_procedure,
        _records_for_run,
    )
    from cruxible_core.storage.cas import BodyAccessContext

    binding = admission.parent_binding
    records = _records_for_run(instance, binding.parent_run_id)
    access = BodyAccessContext(principal_id="nested-recovery", can_read_body=True)
    payloads = [
        (
            row.record.event_kind,
            parse_journal_payload(
                instance.body_store().read(row.record.payload_digest, access=access)
            ),
        )
        for row in records
    ]
    admissions = [
        parse_admission_payload(value).admission
        for kind, value in payloads
        if kind == "admission_bound"
    ]
    calls = [
        value
        for kind, value in payloads
        if kind == "child_invocation"
        and isinstance(value, dict)
        and value.get("verdict") == "started"
        and value.get("node_id") == binding.node_id
    ]
    if len(admissions) != 1 or len(calls) != 1:
        raise ProcedureRunRecoveryRequired(
            "Nested recovery requires one retained parent invocation"
        )
    parent = admissions[0]
    if (
        not isinstance(parent, ProcedureRunAdmissionV2)
        or parent.admission_binding_digest != binding.parent_admission_digest
    ):
        raise ProcedureRunRecoveryRequired("Nested recovery names another parent admission")
    coordinate = instance.resolve_accepted_coordinate(
        git_oid=parent.accepted_coordinate.git_oid,
        semantic_root=parent.accepted_coordinate.semantic_root,
        generation_root=parent.accepted_coordinate.generation_root,
        compiler_digest=parent.accepted_coordinate.compiler_digest,
    )
    accepted = _accepted_procedure(
        instance, name=parent.procedure_identity.name, coordinate=coordinate
    )
    child = _accepted_procedure(
        instance, name=admission.procedure_identity.name, coordinate=coordinate
    )
    node = next(
        (
            node
            for node in accepted.procedure.definition.nodes
            if isinstance(node, InvokeNodeV6) and node.node_id == binding.node_id
        ),
        None,
    )
    if not isinstance(node, InvokeNodeV6) or calls[0].get("procedure") != node.procedure.model_dump(
        mode="json"
    ):
        raise ProcedureRunRecoveryRequired("Nested recovery has no matching accepted Invoke node")
    remaining = ProcedureBudgetV3.model_validate(calls[0].get("remaining_budget"))
    if constrained_budget(parent.budget, remaining, parent.hard_caps) != remaining:
        raise ProcedureRunRecoveryRequired("Nested recovery exceeds the parent budget")
    delegation = ProcedureDelegation(
        ParentInvocationContext(parent, accepted, node, remaining, None), child
    )
    delegation.authority(admission)
    if isinstance(parent, ProcedureRunAdmissionV8):
        retained_delegation(instance, parent)
    return delegation


if TYPE_CHECKING:
    from cruxible_core.service.procedures.procedure_runs import ProviderRuntimeOperatorProtocol


@dataclass
class ServedNestedProcedureRunner:
    instance: PlaybillInstance
    coordinate: AcceptedProjectionCoordinate
    head_at_admission: AcceptedProjectionCoordinate
    evaluation_time: datetime
    provider_runtime_operator: ProviderRuntimeOperatorProtocol | None
    workspace_file_reader: WorkspaceFileReader | None
    clock: ProcedureClockProtocol
    mandates: Mapping[str, ProcedureMandateV1] = field(default_factory=dict)

    def _accepted(self, pin: ArtifactPin) -> AcceptedProcedureV1:
        from cruxible_core.service.procedures.procedure_runs import _accepted_procedure

        accepted = _accepted_procedure(
            self.instance, name=pin.target.name, coordinate=self.coordinate
        )
        if accepted.artifact_digest != pin.artifact_digest:
            raise ProcedureBoundaryRefused(
                "pin_binding_mismatch",
                "An exact child Procedure is not accepted at the parent coordinate.",
            )
        if not accepted.procedure.directly_runnable or not isinstance(
            accepted.procedure.definition, ProcedureDefinitionV5
        ):
            raise ProcedureBoundaryRefused(
                "pin_binding_mismatch", "A child must be a closed runnable Procedure."
            )
        if accepted.procedure.definition.pin_slots:
            raise ProcedureBoundaryRefused(
                "pin_binding_mismatch", "A child cannot contain unresolved Line slots."
            )
        return accepted

    def preflight(self, accepted: AcceptedProcedureV1, admission: ProcedureRunAdmissionV1) -> None:
        from cruxible_core.service.procedures.procedure_runs import (
            ProcedureRunStateV2,
            _plan_direct_external_run,
            served_node_kinds,
        )

        ancestors = (
            tuple(pin.target for pin in admission.parent_binding.ancestors)
            if isinstance(admission, ProcedureRunAdmissionV8)
            else ()
        )

        def visit(
            parent: AcceptedProcedureV1,
            budget: ProcedureBudgetV3,
            caps: ProcedureHardCapsV3,
            lineage: tuple[ArtifactIdentity, ...],
        ) -> None:
            graph = parent.procedure.definition
            if not isinstance(graph, ProcedureDefinitionV6):
                return
            for node in graph.nodes:
                if not isinstance(node, InvokeNodeV6):
                    continue
                if node.procedure.target in lineage:
                    raise ProcedureBoundaryRefused(
                        "pin_binding_mismatch", "Recursive Procedure invocation is unsupported."
                    )
                child = self._accepted(node.procedure)
                definition = child.procedure.definition
                supported = served_node_kinds(definition.graph_format)
                if admission.invocation_origin == "line":
                    supported |= {"emit_capture", "propose_change_set"}
                if any(step.kind not in supported for step in definition.nodes):
                    raise ProcedureBoundaryRefused(
                        "pin_binding_mismatch",
                        "The child requires a terminal or node unavailable in the parent lane.",
                    )
                required_rung = max(
                    (TERMINAL_REQUIRED_RUNGS.get(step.kind, 0) for step in definition.nodes),
                    default=0,
                )
                if required_rung > graph.terminal_capability:
                    raise ProcedureBoundaryRefused(
                        "pin_binding_mismatch", "The child exceeds the parent terminal capability."
                    )
                child_caps = constrained_caps(definition.hard_caps, caps)
                child_budget = constrained_budget(definition.budget, budget, child_caps)
                plan = _plan_direct_external_run(
                    self.instance,
                    child,
                    coordinate=self.coordinate,
                    head_at_admission=self.head_at_admission,
                    evaluation_time=self.evaluation_time,
                    provider_runtime_operator=self.provider_runtime_operator,
                    budget=child_budget,
                )
                if isinstance(plan, ProcedureRunStateV2):
                    raise ProcedureBoundaryRefused(
                        "pin_binding_mismatch",
                        "Child external admission was refused.",
                        details=plan.model_dump(mode="json"),
                    )
                visit(child, child_budget, child_caps, (*lineage, child.procedure.identity))

        visit(
            accepted,
            admission.budget,
            admission.hard_caps,
            (*ancestors, accepted.procedure.identity),
        )

    def run(self, context: ParentInvocationContext, value: object) -> ProcedureRunResultV1:
        from cruxible_core.service.procedures.procedure_runs import (
            PROCEDURE_RUN_FENCING_TOKEN,
            ProcedureRunStateV2,
            _activate_writer,
            _CurrentProcedureAuthority,
            _journal_for_write,
            _LineTerminalEgressSink,
            _prepare_direct_external_run,
        )
        from cruxible_core.service.procedures.procedures import (
            PlaybillProcedureStateTapReader,
            service_execute_direct_procedure,
        )

        parent = context.admission
        if parent.accepted_coordinate != AcceptedCoordinate.from_internal(self.coordinate):
            raise ProcedureBoundaryRefused(
                "pin_binding_mismatch", "Nested runner names another accepted state."
            )
        accepted = self._accepted(context.node.procedure)
        caps = constrained_caps(accepted.procedure.definition.hard_caps, parent.hard_caps)
        budget = constrained_budget(accepted.procedure.definition.budget, context.remaining, caps)
        planned = _prepare_direct_external_run(
            self.instance,
            accepted,
            coordinate=self.coordinate,
            head_at_admission=self.head_at_admission,
            evaluation_time=self.evaluation_time,
            invocation_input=value,
            actor_context=parent.actor_context,
            state_reader=PlaybillProcedureStateTapReader(
                instance=self.instance, evaluation_time=self.evaluation_time
            ),
            journal_stream=parent.journal_stream,
            lane=parent.lane,
            provider_runtime_operator=self.provider_runtime_operator,
            effective_budget=budget,
            effective_caps=caps,
        )
        if isinstance(planned, ProcedureRunStateV2):
            raise ProcedureBoundaryRefused(
                "pin_binding_mismatch",
                "Child admission was refused.",
                details=planned.model_dump(mode="json"),
            )
        base, policy, contracts = planned
        prepared = context.bind(base)
        context.verify(prepared.admission, accepted)
        sink = None
        capture_store = None
        if parent.invocation_origin == "line":
            capture_store = ReservedCaptureStore(
                bodies=self.instance.body_store(),
                reservations=ProcedureMaterialReservationStore(
                    self.instance.body_store().reservation_root
                ),
                admission=prepared.admission,
                event_kind="terminal_egress",
            )
            sink = _LineTerminalEgressSink(
                capture=CaptureTerminalEgressSink(
                    store=capture_store,
                    contracts=contracts,
                    producer=accepted.procedure.identity,
                    producer_binding_digest=accepted.artifact_digest,
                ),
                proposal=ProposalTerminalEgressSink(
                    instance=self.instance,
                    accepted_mandates=self.mandates,
                    delegation=ProcedureDelegation(context, accepted),
                ),
            )
        journal, root = _journal_for_write(self.instance)
        _activate_writer(
            journal, prepared.admission.journal_stream, prepared.admission.journal_partition_id
        )
        operator = self.provider_runtime_operator
        result = service_execute_direct_procedure(
            prepared,
            accepted,
            journal=journal,
            bodies=self.instance.body_store(),
            run_index_path=root / "procedure-run-index.sqlite",
            fencing_token=PROCEDURE_RUN_FENCING_TOKEN,
            activation_authority=_CurrentProcedureAuthority(self.instance),
            provider_runtime_invoker_factory=(
                None
                if operator is None
                else lambda: operator.invoker_for(
                    self.instance, accepted_oid=self.coordinate.git_oid
                )
            ),
            acquisition_policy=policy,
            capture_contracts=contracts,
            workspace_file_reader=self.workspace_file_reader,
            effective_rung=context.child_rung(accepted),
            egress_sink=sink,
            clock=self.clock,
            nested_runner=self,
            parent_context=context,
        )
        if result.status == "succeeded" and capture_store is not None:
            capture_store.release()
        return result
