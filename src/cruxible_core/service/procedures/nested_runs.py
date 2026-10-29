"""Nested runs reuse the served admission, provider, evidence and terminal doors."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.errors import ExecutionError
from cruxible_client.contracts.procedure_mandates import ProcedureMandateAny
from cruxible_client.contracts.procedures.artifacts import (
    DIRECT_NODE_KINDS,
    AcceptedProcedure,
    parse_procedure,
)
from cruxible_client.contracts.procedures.models import (
    TERMINAL_REQUIRED_RUNGS,
    InvokeNode,
    ProcedureBudget,
    ProcedureHardCaps,
)
from cruxible_client.contracts.provider_operations import LineBinding
from cruxible_core.documents.workspace_file import WorkspaceFileReader
from cruxible_core.errors import RequestRefusedError
from cruxible_core.exhaust.line_inbox import LineInboxStore
from cruxible_core.exhaust.payloads import UnavailablePayload, read_payload
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.indexes.typed_state import OWNER_BY_IDENTITY_KIND
from cruxible_core.procedures.egress import CaptureTerminalEgressSink
from cruxible_core.procedures.execution import (
    NestedProcedureRunAdmission,
    ProcedureBoundaryRefused,
    ProcedureClockProtocol,
    ProcedureRunAdmissionV1,
    ProcedureRunResultV1,
    nested_admission,
    procedure_node_pin_sets,
    procedure_parent_binding,
)
from cruxible_core.procedures.inbox_delivery import InboxTerminalEgressSink
from cruxible_core.procedures.nested import (
    ParentInvocationContext,
    ProcedureDelegation,
    constrained_budget,
    constrained_caps,
)
from cruxible_core.procedures.proposal_delivery import ProposalTerminalEgressSink
from cruxible_core.procedures.reference_binding import (
    recorded_slot_bindings,
    replay_procedure_references,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.storage.material_reservations import (
    ProcedureMaterialReservationStore,
    ReservedCaptureStore,
)


def retained_delegation(
    instance: PlaybillInstance, admission: NestedProcedureRunAdmission
) -> ProcedureDelegation | UnavailablePayload:
    """Rebuild delegation from the recorded parent call, never a caller's ancestry claim."""
    from cruxible_core.procedures.execution import ProcedureRunAdmissionV2, parse_admission_payload
    from cruxible_core.service.procedures.procedure_runs import (
        ProcedureRunRecoveryRequired,
        _records_for_run,
    )
    from cruxible_core.storage.cas import BodyAccessContext

    binding = procedure_parent_binding(admission)
    if binding is None:
        raise ProcedureRunRecoveryRequired("Nested recovery requires a delegation binding")
    records = _records_for_run(instance, binding.parent_run_id)
    access = BodyAccessContext(principal_id="nested-recovery", can_read_body=True)
    payloads = []
    for row in records:
        payload = read_payload(instance.body_store(), row.record.payload_digest, access=access)
        if isinstance(payload, UnavailablePayload):
            return payload
        payloads.append((row.record.event_kind, payload))
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
    instance.resolve_accepted_coordinate(
        git_oid=parent.accepted_coordinate.git_oid,
        semantic_root=parent.accepted_coordinate.semantic_root,
        generation_root=parent.accepted_coordinate.generation_root,
        compiler_digest=parent.accepted_coordinate.compiler_digest,
    )

    def version(run: ProcedureRunAdmissionV1) -> AcceptedProcedure:
        found = instance.accepted_artifact_version(
            run.accepted_coordinate, run.procedure_artifact_digest, family="procedures/"
        )
        if found is None:
            raise ProcedureRunRecoveryRequired("Recorded Procedure bytes are unavailable")
        path, raw = found
        authored = AcceptedProcedure(
            path=path,
            procedure=parse_procedure(raw, path=path),
            artifact_digest=run.procedure_artifact_digest,
        )

        def exact(identity: ArtifactIdentity, digest: str) -> tuple[Any, str] | None:
            owner = OWNER_BY_IDENTITY_KIND.get(identity.kind)
            if owner is None:
                return None
            location = instance.accepted_artifact_version(
                run.accepted_coordinate, digest, family=owner.table.replace("_", "-") + "/"
            )
            if location is None:
                return None
            member_path, body = location
            from cruxible_core.claims.closure import parse_dependency_artifact

            state = parse_dependency_artifact(member_path, body)
            if state is None or state.identity != identity or state.artifact_digest != digest:
                return None
            return owner.parse(body, path=member_path), digest

        try:
            replayed = replay_procedure_references(
                authored,
                pins=run.full_pins,
                lookup_exact=exact,
                bindings=recorded_slot_bindings(
                    getattr(run, "resolved_provider_bindings", ()), run.full_pins
                ),
            )
            if (
                replayed.procedure.pins != run.full_pins
                or procedure_node_pin_sets(replayed) != run.node_pin_sets
            ):
                raise ExecutionError("Recorded admission pins do not reproduce the execution plan")
            return replayed
        except (ExecutionError, RequestRefusedError) as exc:
            raise ProcedureRunRecoveryRequired(
                f"{ProcedureRunRecoveryRequired.code}: recorded bindings cannot reproduce "
                f"Procedure {run.procedure_identity.qualified}: {exc}"
            ) from exc

    accepted = version(parent)
    child = version(admission)
    node = next(
        (
            node
            for node in accepted.procedure.definition.nodes
            if isinstance(node, InvokeNode) and node.node_id == binding.node_id
        ),
        None,
    )
    if not isinstance(node, InvokeNode) or calls[0].get("procedure") != node.procedure.model_dump(
        mode="json"
    ):
        raise ProcedureRunRecoveryRequired("Nested recovery has no matching accepted Invoke node")
    remaining = ProcedureBudget.model_validate(calls[0].get("remaining_budget"))
    if constrained_budget(parent.budget, remaining, parent.hard_caps) != remaining:
        raise ProcedureRunRecoveryRequired("Nested recovery exceeds the parent budget")
    delegation = ProcedureDelegation(
        ParentInvocationContext(parent, accepted, node, remaining, None), child
    )
    try:
        delegation.authority(admission)
    except ExecutionError as exc:
        raise ProcedureRunRecoveryRequired(
            f"{ProcedureRunRecoveryRequired.code}: recorded child authority does not reproduce "
            f"its parent delegation: {exc}"
        ) from exc
    nested_parent = nested_admission(parent)
    if nested_parent is not None:
        retained = retained_delegation(instance, nested_parent)
        if isinstance(retained, UnavailablePayload):
            return retained
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
    mandates: Mapping[str, ProcedureMandateAny] = field(default_factory=dict)
    #: The root run's bindings (its Line's, or a direct run's ephemeral spec),
    #: keyed by slot or ``<invoke node path>.<slot>``; a child binds through it.
    bindings: Mapping[str, LineBinding] = field(default_factory=dict)
    #: The invoke node path of the Procedure this runner runs children for.
    prefix: str = ""

    def _child_prefix(self, node: InvokeNode) -> str:
        return self.prefix + node.node_id + "."

    def _accepted(self, pin: ArtifactPin, *, prefix: str) -> AcceptedProcedure:
        from cruxible_core.providers.operation_index import child_bindings
        from cruxible_core.service.procedures.procedure_runs import _accepted_procedure

        accepted = _accepted_procedure(
            self.instance,
            name=pin.target.name,
            coordinate=self.coordinate,
            bind_references=True,
            bindings=child_bindings(self.bindings, prefix),
        )
        if accepted.artifact_digest != pin.artifact_digest:
            raise ProcedureBoundaryRefused(
                "pin_binding_mismatch",
                "An exact child Procedure is not accepted at the parent coordinate.",
            )
        return accepted

    def preflight(self, accepted: AcceptedProcedure, admission: ProcedureRunAdmissionV1) -> None:
        from cruxible_core.service.procedures.procedure_runs import (
            ProcedureRunStateV2,
            _plan_direct_external_run,
        )

        parent_binding = procedure_parent_binding(admission)
        ancestors = (
            () if parent_binding is None else tuple(pin.target for pin in parent_binding.ancestors)
        )

        def visit(
            parent: AcceptedProcedure,
            budget: ProcedureBudget,
            caps: ProcedureHardCaps,
            lineage: tuple[ArtifactIdentity, ...],
            prefix: str,
        ) -> None:
            graph = parent.procedure.definition
            for node in graph.nodes:
                if not isinstance(node, InvokeNode):
                    continue
                if node.procedure.target in lineage:
                    raise ProcedureBoundaryRefused(
                        "pin_binding_mismatch", "Recursive Procedure invocation is unsupported."
                    )
                child_prefix = prefix + node.node_id + "."
                child = self._accepted(node.procedure, prefix=child_prefix)
                definition = child.procedure.definition
                supported = set(DIRECT_NODE_KINDS)
                if admission.invocation_origin == "line":
                    # A Line's child runs under the root Line's authority; its
                    # inbox posts land in that Line's inbox.
                    supported |= {"emit_capture", "post_inbox", "propose_change_set"}
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
                visit(
                    child,
                    child_budget,
                    child_caps,
                    (*lineage, child.procedure.identity),
                    child_prefix,
                )

        visit(
            accepted,
            admission.budget,
            admission.hard_caps,
            (*ancestors, accepted.procedure.identity),
            self.prefix,
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
        child_prefix = self._child_prefix(context.node)
        accepted = self._accepted(context.node.procedure, prefix=child_prefix)
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
        base, rules, contracts = planned
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
                # A child posts under the Line its root run ran as.
                inbox=None
                if (line := getattr(parent, "line_identity", None)) is None
                else InboxTerminalEgressSink(store=LineInboxStore(self.instance), line=line),
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
                    self.instance,
                    accepted_oid=self.coordinate.git_oid,
                    pins=accepted.procedure.pins,
                )
            ),
            acquisition_rules=rules,
            capture_contracts=contracts,
            workspace_file_reader=self.workspace_file_reader,
            effective_rung=context.child_rung(accepted),
            egress_sink=sink,
            clock=self.clock,
            nested_runner=replace(self, prefix=child_prefix),
            parent_context=context,
        )
        if result.status == "succeeded" and capture_store is not None:
            capture_store.release()
        return result
