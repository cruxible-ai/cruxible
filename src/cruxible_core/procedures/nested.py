"""Exact nested invocation bindings; no public author supplies these coordinates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.errors import ExecutionError
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedure
from cruxible_client.contracts.procedures.models import (
    InvokeNode,
    ProcedureBudget,
    ProcedureDefinition,
    ProcedureHardCaps,
)
from cruxible_client.contracts.procedures.results import procedure_acquisition_plan_digest
from cruxible_core.procedures.egress import EffectiveRungV1
from cruxible_core.procedures.execution import (
    PreparedProcedureRun,
    ProcedureParentBindingV1,
    ProcedureRunAdmission,
    ProcedureRunResultV1,
    admission_fields,
    seal_procedure_run_admission,
)
from cruxible_core.procedures.reference_binding import resolved_pin


def _minimum(*values: int | None) -> int | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


def constrained_caps(child: ProcedureHardCaps, parent: ProcedureHardCaps) -> ProcedureHardCaps:
    fields = {
        name: _minimum(getattr(child, name), getattr(parent, name))
        for name in type(child).model_fields
        if name not in {"tag", "max_wall_clock"}
    }
    return ProcedureHardCaps.model_validate(
        {
            **fields,
            "max_wall_clock": min(
                (child.max_wall_clock, parent.max_wall_clock), key=lambda d: d.microseconds
            ),
        }
    )


def constrained_budget(
    child: ProcedureBudget,
    remaining: ProcedureBudget,
    caps: ProcedureHardCaps,
) -> ProcedureBudget:
    return ProcedureBudget(
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
        # Reads are cumulative across the parent and its children: the child
        # gets what the parent has left of its read rows.
        max_read_rows=_minimum(child.max_read_rows, remaining.max_read_rows, caps.max_read_rows),
    )


@dataclass(frozen=True)
class ParentInvocationContext:
    """Constructed by the executing parent, never deserialized from an SDK request."""

    admission: ProcedureRunAdmission
    accepted: AcceptedProcedure
    node: InvokeNode
    remaining: ProcedureBudget
    rung: EffectiveRungV1 | None
    deadline_ns: int | None = None

    @property
    def binding(self) -> ProcedureParentBindingV1:
        grandparent = self.admission.parent_binding
        return ProcedureParentBindingV1(
            parent_run_id=self.admission.run_id,
            parent_admission_digest=self.admission.admission_binding_digest,
            node_id=self.node.node_id,
            ancestors=(
                *(() if grandparent is None else grandparent.ancestors),
                ArtifactPin(
                    role="procedure",
                    target=self.accepted.procedure.identity,
                    artifact_digest=self.accepted.artifact_digest,
                ),
            ),
            activation_policies=(
                *(() if grandparent is None else grandparent.activation_policies),
                self.admission.activation_policy,
            ),
        )

    def child_rung(self, accepted: AcceptedProcedure) -> EffectiveRungV1 | None:
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

    def bind(self, prepared: PreparedProcedureRun) -> PreparedProcedureRun:
        """Reuse the shared planner, then bind its child admission to this occurrence.

        The child keeps its own plan, inputs and constrained budget, and takes
        the root's actor, lane, authority coordinates, investigation and
        trigger, plus the delegation binding naming this exact occurrence.
        """

        parent = self.admission
        plan = prepared.acquisition_plan
        if plan is None:
            raise ExecutionError("a nested child is planned by the shared external planner")
        plan = plan.model_copy(
            update={
                "line_identity": parent.line_identity,
                "line_spec_digest": parent.line_spec_digest,
                "occurrence_id": parent.occurrence_id,
            }
        )
        plan_digest = procedure_acquisition_plan_digest(plan)
        inherited = {
            name: getattr(parent, name)
            for name in (
                "invocation_origin",
                "actor_context",
                "line_identity",
                "line_spec_digest",
                "occurrence_id",
                "sensitivity_policy_digest",
                "mandate_coordinate_digest",
                "calibration_coordinate_digest",
                "taint_labels",
                "epsilon_member",
                "investigation",
                "trigger_binding",
                "attempt",
            )
        }
        admission = seal_procedure_run_admission(
            {
                **admission_fields(prepared.admission),
                **inherited,
                "parent_binding": self.binding,
                "acquisition_plan_digest": plan_digest,
            }
        )
        return PreparedProcedureRun.model_validate(
            {
                **prepared.model_dump(mode="python", exclude={"tag", "admission"}),
                "admission": admission,
                "acquisition_plan": plan,
                "acquisition_plan_digest": plan_digest,
            }
        )

    def verify(self, child: ProcedureRunAdmission, accepted: AcceptedProcedure) -> None:
        definition = accepted.procedure.definition
        caps = constrained_caps(definition.hard_caps, self.admission.hard_caps)
        if (
            child.parent_binding != self.binding
            or self.admission.procedure_identity != self.accepted.procedure.identity
            or self.admission.procedure_artifact_digest != self.accepted.artifact_digest
            or self.admission.definition_digest != self.accepted.procedure.definition_digest
            or not isinstance(self.accepted.procedure.definition, ProcedureDefinition)
            or self.node not in self.accepted.procedure.definition.nodes
            or self.node.procedure.target != accepted.procedure.identity
            or resolved_pin(self.accepted, self.node.procedure).artifact_digest
            != accepted.artifact_digest
            or child.accepted_coordinate != self.admission.accepted_coordinate
            or child.actor_context != self.admission.actor_context
            or child.invocation_origin != self.admission.invocation_origin
            or child.hard_caps != caps
            or child.budget != constrained_budget(definition.budget, self.remaining, caps)
        ):
            raise ExecutionError("nested admission differs from its executing parent")
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
                raise ExecutionError("nested invocation changed inherited authority")
        if child.line_identity != self.admission.line_identity:
            raise ExecutionError("nested invocation changed its accepted Line")


@dataclass(frozen=True)
class ProcedureDelegation:
    """In-process proof from a checked parent frame, absent from every wire API.

    Re-check the actual accepted Invoke node, exact versions, admitted actor,
    authority coordinates and constrained budget whenever a terminal uses it.
    An admission's claimed ancestry alone never authorizes a terminal.
    """

    context: ParentInvocationContext
    child: AcceptedProcedure

    def authority(self, admission: ProcedureRunAdmission) -> ArtifactPin:
        self.context.verify(admission, self.child)
        return self.context.binding.ancestors[0]


def authority_procedure(
    admission: ProcedureRunAdmission,
    delegation: ProcedureDelegation | None = None,
) -> ArtifactPin:
    if admission.parent_binding is not None:
        if delegation is None:
            raise ExecutionError("nested terminal requires verified parent delegation")
        return delegation.authority(admission)
    if delegation is not None:
        raise ExecutionError("standalone run cannot claim delegated authority")
    return ArtifactPin(
        role="procedure",
        target=admission.procedure_identity,
        artifact_digest=admission.procedure_artifact_digest,
    )


def authority_line(
    admission: ProcedureRunAdmission, delegation: ProcedureDelegation | None = None
) -> tuple[ArtifactIdentity | None, str | None]:
    """The exact Line a run's terminal authority comes from (ruling C3).

    A nested run inherits its root's Line (the delegation re-checks the parent
    frame first); a direct run has none, so no Mandate covers it.
    """

    authority_procedure(admission, delegation)
    return admission.line_identity, admission.line_spec_digest


class NestedProcedureRunner(Protocol):
    def preflight(self, accepted: AcceptedProcedure, admission: ProcedureRunAdmission) -> None:
        """Verify all child capabilities before the parent's first effect."""
        ...

    def run(self, context: ParentInvocationContext, value: object) -> ProcedureRunResultV1: ...
