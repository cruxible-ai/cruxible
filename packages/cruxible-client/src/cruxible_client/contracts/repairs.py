"""Structured, runnable repair carriers shared by served refusal envelopes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RepairOperationV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class HandEditInstructionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: str = Field(min_length=1)
    required_change: str = Field(min_length=1)


class HandEditRepairV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    hand_edit: HandEditInstructionV1


ServedRepairV1: TypeAlias = RepairOperationV1 | HandEditRepairV1

# A refusal whose specific change has not been declared says exactly that and
# nothing more. Deriving a token from the code (``repair_<code>``) reads like a
# declared change while carrying no instruction, which is the fabricated repair
# the hand-edit exemption forbids.
UNDECLARED_HAND_EDIT_CHANGE = "read_the_refusal_details_and_revise_the_named_artifact"


# Codes whose repair needs judgment. Each names the change to make; membership
# is declared here beside the vocabularies it covers, never derived from the
# code, and the served refusal models read it when a producer carries none.
DECLARED_HAND_EDIT_CHANGES: Mapping[str, str] = {
    "line_not_accepted": "accept_the_line_before_triggering_it",
    "line_closure_incomplete": "restore_or_succeed_the_missing_accepted_closure_member",
    "occurrence_already_admitted": "read_the_existing_run_state_instead_of_readmitting",
    "procedure_runtime_policy_absent": "accept_a_procedure_runtime_policy",
    "provider_lane_unavailable": "restore_the_daemon_provider_lane_then_retry",
    "procedure_projection_missing": "add_procedure_projection_catalog_entries",
    "exhaust_binding_carrier_required": "trigger_through_a_carrier_aware_line_scheduler",
    "environment_divergence": "rematerialize_the_provider_environment_against_its_seal",
    "provider_replay_receipt_required": "record_the_durable_provider_completion_before_replay",
    "state_tap_refused": "restore_the_accepted_state_query_backend",
    "settlement_lost_cas": "resubmit_the_settlement_against_the_current_accepted_coordinate",
    "settlement_activation_coordinate_changed": (
        "resubmit_the_settlement_against_the_current_accepted_coordinate"
    ),
    "settlement_actor_principal_invalid": (
        "settle_as_an_active_principal_at_the_accepted_coordinate"
    ),
    "settlement_base_semantic_root_mismatch": (
        "rebase_the_candidate_onto_the_current_semantic_root"
    ),
    "settlement_candidate_mismatch": "resubmit_the_exact_admitted_candidate",
    "settlement_candidate_scope_mismatch": (
        "resubmit_the_candidate_whose_scope_matches_its_admission"
    ),
    "settlement_proposal_id_mismatch": "settle_the_proposal_the_candidate_was_admitted_under",
    "settlement_receipt_mismatch": "reproduce_the_terminal_receipt_before_settling",
}


# Codes whose repair is one served CLI leaf. The operation is the leaf's dotted
# path, so the guardrail resolves it against the live lazy command map instead
# of trusting the string; a repair naming a command that does not exist is worse
# than no repair at all. The table lives beside the vocabularies it covers so
# every served refusal producer -- core service, client authoring, CLI -- reads
# the same one instead of re-inventing prose at its own boundary.
RUNNABLE_REFUSAL_REPAIRS: Mapping[str, RepairOperationV1] = {
    "binding_required": RepairOperationV1(operation="playbill.procedure.bind"),
    "line_mandate_required": RepairOperationV1(
        operation="playbill.authoring.create",
        arguments={"example": "procedure-mandate"},
    ),
    "prediction_unsettleable_rule": RepairOperationV1(operation="playbill.predict"),
    "prediction_deadline_passed": RepairOperationV1(operation="playbill.predict"),
    "settlement_evidence_mismatch": RepairOperationV1(operation="playbill.settle"),
    "block_backing_changed": RepairOperationV1(
        operation="playbill.block.sync", arguments={"all": True}
    ),
    "block_backing_missing": RepairOperationV1(
        operation="playbill.block.sync", arguments={"all": True}
    ),
    "block_backing_retired": RepairOperationV1(
        operation="playbill.block.sync", arguments={"all": True}
    ),
    "block_concurrent_edit": RepairOperationV1(
        operation="playbill.block.sync", arguments={"all": True}
    ),
    "block_locally_modified": RepairOperationV1(
        operation="playbill.block.sync", arguments={"all": True}
    ),
    "block_successor_ambiguous": RepairOperationV1(operation="playbill.block.repin"),
    "block_sync_failed": RepairOperationV1(
        operation="playbill.block.sync", arguments={"all": True}
    ),
    "projection_backing_stale": RepairOperationV1(
        operation="playbill.block.sync", arguments={"all": True}
    ),
    "projection_dirty": RepairOperationV1(operation="playbill.block.sync", arguments={"all": True}),
    "occurrence_not_due": RepairOperationV1(operation="playbill.line.run"),
    "occurrence_id_mismatch": RepairOperationV1(operation="playbill.line.run"),
    "line_identity_mismatch": RepairOperationV1(operation="playbill.line.run"),
    "document_modified": RepairOperationV1(operation="playbill.document.propose"),
    "workspace_binding_invalid": RepairOperationV1(
        operation="playbill.host.create",
        arguments={"workspace": ".", "replace": True},
    ),
    "workspace_instance_mismatch": RepairOperationV1(
        operation="playbill.host.create",
        arguments={"workspace": ".", "replace": True},
    ),
    "workspace_not_attached": RepairOperationV1(
        operation="playbill.host.create", arguments={"workspace": "."}
    ),
}


class ServedRepairEnvelopeV1(BaseModel):
    """Validation utility proving the repair union has exactly one branch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repair: ServedRepairV1

    @model_validator(mode="after")
    def _one_branch(self) -> "ServedRepairEnvelopeV1":
        if isinstance(self.repair, RepairOperationV1) == isinstance(self.repair, HandEditRepairV1):
            raise ValueError("repair must select exactly one structured branch")
        return self


def hand_edit_repair(code: str, *, required_change: str | None = None) -> HandEditRepairV1:
    """Return an explicit non-command repair for a refusal needing judgment."""

    return HandEditRepairV1(
        hand_edit=HandEditInstructionV1(
            target=f"refusal/{code}",
            required_change=(
                required_change
                or DECLARED_HAND_EDIT_CHANGES.get(code)
                or UNDECLARED_HAND_EDIT_CHANGE
            ),
        )
    )


def render_served_repair(repair: ServedRepairV1) -> str:
    """Render one structured repair for a terminal without inventing a command.

    A runnable repair prints the served operation and the exact arguments it
    carries; a hand edit prints its target and the change it requires. Neither
    branch composes an invocation the caller could paste and have fail.
    """

    if isinstance(repair, HandEditRepairV1):
        return f"hand edit {repair.hand_edit.target}: {repair.hand_edit.required_change}"
    if not repair.arguments:
        return repair.operation
    arguments = json.dumps(repair.arguments, sort_keys=True, separators=(",", ":"))
    return f"{repair.operation} {arguments}"


def served_repair_for_refusal(code: str) -> ServedRepairV1:
    """Resolve one served refusal code to its declared structured repair.

    Producers call this instead of building a repair at their own boundary, so
    a code that has a served command always reaches the wire with it and a code
    that needs judgment reaches it with the declared change. Membership in a
    closed vocabulary is checked by the source-owned catalog, not here: this
    module is imported by the vocabularies themselves.
    """

    runnable = RUNNABLE_REFUSAL_REPAIRS.get(code)
    if runnable is not None:
        return runnable
    return hand_edit_repair(code)


__all__ = [
    "DECLARED_HAND_EDIT_CHANGES",
    "RUNNABLE_REFUSAL_REPAIRS",
    "UNDECLARED_HAND_EDIT_CHANGE",
    "HandEditInstructionV1",
    "HandEditRepairV1",
    "RepairOperationV1",
    "ServedRepairEnvelopeV1",
    "ServedRepairV1",
    "hand_edit_repair",
    "render_served_repair",
    "served_repair_for_refusal",
]
