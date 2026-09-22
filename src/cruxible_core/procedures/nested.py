"""Exact nested invocation bindings; no public author supplies these coordinates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.errors import PlaybillExecutionError
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedureV1
from cruxible_client.contracts.procedures.models import (
    InvokeNodeV6,
    ProcedureBudgetV3,
    ProcedureDefinitionV6,
    ProcedureHardCapsV3,
)
from cruxible_client.contracts.procedures.results import procedure_acquisition_plan_digest
from cruxible_core.procedures.egress import EffectiveRungV1
from cruxible_core.procedures.execution import (
    PreparedProcedureRunV5,
    PreparedProcedureRunV8,
    ProcedureParentBindingV1,
    ProcedureRunAdmissionV1,
    ProcedureRunAdmissionV2,
    ProcedureRunAdmissionV7,
    ProcedureRunAdmissionV8,
    ProcedureRunResultV1,
    procedure_admission_digest,
    procedure_direct_partition,
    procedure_line_run_id,
    procedure_semantic_replay_key_digest,
    procedure_semantic_run_id,
)


def _minimum(*values: int | None) -> int | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


def constrained_caps(
    child: ProcedureHardCapsV3, parent: ProcedureHardCapsV3
) -> ProcedureHardCapsV3:
    fields = {
        name: _minimum(getattr(child, name), getattr(parent, name))
        for name in type(child).model_fields
        if name not in {"tag", "max_wall_clock"}
    }
    return ProcedureHardCapsV3.model_validate(
        {
            **fields,
            "max_wall_clock": min(
                (child.max_wall_clock, parent.max_wall_clock), key=lambda d: d.microseconds
            ),
        }
    )


def constrained_budget(
    child: ProcedureBudgetV3,
    remaining: ProcedureBudgetV3,
    caps: ProcedureHardCapsV3,
) -> ProcedureBudgetV3:
    return ProcedureBudgetV3(
        wall_clock=min(
            (child.wall_clock, remaining.wall_clock, caps.max_wall_clock),
            key=lambda d: d.microseconds,
        ),
        max_provider_calls=min(
            child.max_provider_calls, remaining.max_provider_calls, caps.max_provider_calls
        ),
        max_capture_bytes=min(
            child.max_capture_bytes, remaining.max_capture_bytes, caps.max_capture_bytes
        ),
        max_result_bytes=_minimum(
            child.max_result_bytes, remaining.max_result_bytes, caps.max_result_bytes
        ),
        max_items=_minimum(child.max_items, remaining.max_items, caps.max_items),
    )


@dataclass(frozen=True)
class ParentInvocationContext:
    """Constructed by the executing parent, never deserialized from an SDK request."""

    admission: ProcedureRunAdmissionV2
    accepted: AcceptedProcedureV1
    node: InvokeNodeV6
    remaining: ProcedureBudgetV3
    rung: EffectiveRungV1 | None
    deadline_ns: int | None = None

    @property
    def binding(self) -> ProcedureParentBindingV1:
        ancestors = (
            self.admission.parent_binding.ancestors
            if isinstance(self.admission, ProcedureRunAdmissionV8)
            else ()
        )
        return ProcedureParentBindingV1(
            parent_run_id=self.admission.run_id,
            parent_admission_digest=self.admission.admission_binding_digest,
            node_id=self.node.node_id,
            ancestors=(
                *ancestors,
                ArtifactPin(
                    role="procedure",
                    target=self.accepted.procedure.identity,
                    artifact_digest=self.accepted.artifact_digest,
                ),
            ),
            activation_policies=(
                *(
                    self.admission.parent_binding.activation_policies
                    if isinstance(self.admission, ProcedureRunAdmissionV8)
                    else ()
                ),
                self.admission.activation_policy,
            ),
        )

    def child_rung(self, accepted: AcceptedProcedureV1) -> EffectiveRungV1 | None:
        if self.rung is None:
            return None
        terms = tuple(
            term.model_copy(
                update={"rung": min(term.rung, accepted.procedure.definition.terminal_capability)}
            )
            if term.term == "procedure_terminal_capability"
            else term
            for term in self.rung.terms
        )
        lowest = min(term.rung for term in terms)
        return EffectiveRungV1.model_validate(
            {
                **self.rung.model_dump(mode="python"),
                "procedure_definition_digest": accepted.procedure.definition_digest,
                "terms": terms,
                "effective_rung": lowest,
                "limiting_term": next(term.term for term in terms if term.rung == lowest),
            }
        )

    def bind(self, prepared: PreparedProcedureRunV5) -> PreparedProcedureRunV8:
        """Reuse the shared planner, then bind its child admission to this occurrence."""
        parent = self.admission
        fields = {
            name: getattr(prepared.admission, name)
            for name in type(prepared.admission).model_fields
            if name != "tag"
        }
        fields.update(parent_binding=self.binding, investigation=None, trigger_binding=None)
        if isinstance(parent, ProcedureRunAdmissionV7):
            fields.update(
                investigation=parent.investigation, trigger_binding=parent.trigger_binding
            )
        # Keep the root actor/lane and authority coordinates, but the child's own
        # external plan and deployments. A separate partition keeps both receipts contiguous.
        for name in (
            "invocation_origin",
            "actor_context",
            "line_spec_digest",
            "occurrence_id",
            "sensitivity_policy_digest",
            "mandate_coordinate_digest",
            "calibration_coordinate_digest",
            "taint_labels",
            "epsilon_member",
        ):
            fields[name] = getattr(parent, name)
        fields["line_identity"] = getattr(parent, "line_identity", None)
        fields["attempt"] = parent.attempt
        plan = prepared.acquisition_plan.model_copy(
            update={
                "line_identity": fields["line_identity"],
                "line_spec_digest": fields["line_spec_digest"],
                "occurrence_id": fields["occurrence_id"],
            }
        )
        plan_digest = procedure_acquisition_plan_digest(plan)
        fields["acquisition_plan_digest"] = plan_digest
        provisional = ProcedureRunAdmissionV8.model_construct(**fields)
        replay_key = procedure_semantic_replay_key_digest(provisional)
        provisional = provisional.model_copy(update={"semantic_replay_key_digest": replay_key})
        digest = procedure_admission_digest(provisional)
        run_id = (
            procedure_semantic_run_id(replay_key)
            if parent.invocation_origin == "actor"
            else procedure_line_run_id(
                occurrence_id=parent.occurrence_id or "",
                attempt=parent.attempt,
                admission_binding_digest=digest,
                occurrence_evaluation_time=provisional.occurrence_evaluation_time,
            )
        )
        admission = ProcedureRunAdmissionV8.model_validate(
            {
                **provisional.model_dump(mode="python"),
                "run_id": run_id,
                "admission_binding_digest": digest,
                "journal_partition_id": procedure_direct_partition(replay_key),
            }
        )
        return PreparedProcedureRunV8.model_validate(
            {
                **prepared.model_dump(mode="python", exclude={"tag", "admission"}),
                "admission": admission,
                "acquisition_plan": plan,
                "acquisition_plan_digest": plan_digest,
            }
        )

    def verify(self, child: ProcedureRunAdmissionV8, accepted: AcceptedProcedureV1) -> None:
        definition = accepted.procedure.definition
        caps = constrained_caps(definition.hard_caps, self.admission.hard_caps)
        if (
            child.parent_binding != self.binding
            or self.admission.procedure_identity != self.accepted.procedure.identity
            or self.admission.procedure_artifact_digest != self.accepted.artifact_digest
            or self.admission.definition_digest != self.accepted.procedure.definition_digest
            or not isinstance(self.accepted.procedure.definition, ProcedureDefinitionV6)
            or self.node not in self.accepted.procedure.definition.nodes
            or self.node.procedure.target != accepted.procedure.identity
            or self.node.procedure.artifact_digest != accepted.artifact_digest
            or child.accepted_coordinate != self.admission.accepted_coordinate
            or child.actor_context != self.admission.actor_context
            or child.invocation_origin != self.admission.invocation_origin
            or child.hard_caps != caps
            or child.budget != constrained_budget(definition.budget, self.remaining, caps)
        ):
            raise PlaybillExecutionError("nested admission differs from its executing parent")
        for name in (
            "line_spec_digest",
            "occurrence_id",
            "sensitivity_policy_digest",
            "mandate_coordinate_digest",
            "calibration_coordinate_digest",
            "taint_labels",
            "epsilon_member",
        ):
            if getattr(child, name) != getattr(self.admission, name):
                raise PlaybillExecutionError("nested invocation changed inherited authority")
        if getattr(child, "line_identity", None) != getattr(self.admission, "line_identity", None):
            raise PlaybillExecutionError("nested invocation changed its accepted Line")


@dataclass(frozen=True)
class ProcedureDelegation:
    """In-process proof from a checked parent frame, absent from every wire API.

    Re-check the actual accepted Invoke node, exact versions, admitted actor,
    authority coordinates and constrained budget whenever a terminal uses it.
    An admission's claimed ancestry alone never authorizes a terminal.
    """

    context: ParentInvocationContext
    child: AcceptedProcedureV1

    def authority(self, admission: ProcedureRunAdmissionV8) -> ArtifactPin:
        self.context.verify(admission, self.child)
        return self.context.binding.ancestors[0]


def authority_procedure(
    admission: ProcedureRunAdmissionV1,
    delegation: ProcedureDelegation | None = None,
) -> ArtifactPin:
    if isinstance(admission, ProcedureRunAdmissionV8):
        if delegation is None:
            raise PlaybillExecutionError("nested terminal requires verified parent delegation")
        return delegation.authority(admission)
    if delegation is not None:
        raise PlaybillExecutionError("standalone run cannot claim delegated authority")
    return ArtifactPin(
        role="procedure",
        target=admission.procedure_identity,
        artifact_digest=admission.procedure_artifact_digest,
    )


class NestedProcedureRunner(Protocol):
    def preflight(self, accepted: AcceptedProcedureV1, admission: ProcedureRunAdmissionV1) -> None:
        """Verify all child capabilities before the parent's first effect."""
        ...

    def run(self, context: ParentInvocationContext, value: object) -> ProcedureRunResultV1: ...
