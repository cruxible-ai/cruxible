"""Structured, runnable repair carriers shared by served refusal envelopes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cruxible_client.contracts.codes import normalize_code


class RepairOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class HandEditInstruction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: str = Field(min_length=1)
    required_change: str = Field(min_length=1)


class HandEditRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    hand_edit: HandEditInstruction


ServedRepair: TypeAlias = RepairOperation | HandEditRepair

# A refusal whose specific change has not been declared says exactly that and
# nothing more. Deriving a token from the code (``repair_<code>``) reads like a
# declared change while carrying no instruction, which is the fabricated repair
# the hand-edit exemption forbids.
UNDECLARED_HAND_EDIT_CHANGE = "read_the_refusal_details_and_revise_the_named_artifact"


# Codes whose repair needs judgment. Each names the change to make; membership
# is declared here beside the vocabularies it covers, never derived from the
# code, and the served refusal models read it when a producer carries none.
DECLARED_HAND_EDIT_CHANGES: Mapping[str, str] = {
    # The bound provider operation refused under a code it declares: the code
    # is the refusal's `detail_code`, its declared detail is `details`.
    "operation_refused": (
        "Read detail_code and details: the bound provider operation declined this "
        "input under its own declared refusal. Revise the input it names, or bind "
        "the slot to an operation that serves it, then rerun."
    ),
    # The failure's `cause` carries the Provider's kind and message: a transport
    # error clears once the target is reachable; an HTTP status is the input's.
    "provider_execution_error": (
        "Read the failure cause. A transport error (connect, DNS, timeout): rerun "
        "once the target is reachable. An HTTP status: revise the URL input, then rerun."
    ),
    "workspace_binding_missing": (
        "Restore the bound file at the catalog entry's locator, or correct or remove "
        "that entry in the workspace file catalog."
    ),
    "trigger_capture_stale": (
        "Acquire a fresh Capture and evaluate its new occurrence. "
        "Freshness is checked at admission time."
    ),
    "trigger_capture_unavailable": (
        "Restore the exact retained Capture if available, then "
        "explicitly retry this occurrence; otherwise acquire a new "
        "Capture."
    ),
    "trigger_capture_over_budget": (
        "If the Line budget is limiting, accept a successor Line with a larger budget, "
        "then use line dispatch --occurrence-id DIGEST --retry. If the exact "
        "CaptureContract cap is limiting, use a new contract and fresh Capture; "
        "a Line budget increase cannot override that cap."
    ),
    "trigger_capture_forbidden": (
        "Accept a successor Line or acquisition policy that permits "
        "this material, then explicitly retry; do not bypass "
        "retention policy."
    ),
    "trigger_capture_invalid": (
        "Inspect the exact event and Capture integrity; repair the "
        "retained material or use a new verified Capture."
    ),
    "trigger_capture_not_yet_observed": (
        "Wait until the recorded observation time, then retry the occurrence."
    ),
    "trigger_event_precedes_acceptance": (
        "A Trigger never fires retroactively: use an event that lands, or a window "
        "that closes, after the Trigger version was accepted."
    ),
    "line_binding_superseded": (
        "Explicitly retry this occurrence to bind the current Line in "
        "the same epoch, or evaluate the new trigger epoch."
    ),
    # The Trigger a pending occurrence names no longer aims at the Line.
    "line_trigger_mismatch": "evaluate_the_line_against_the_triggers_aimed_at_it_now",
    "prediction_unsettleable_rule": (
        "revise_the_resolution_contract_rule_or_hypothesis_and_submit_a_successor"
    ),
    "prediction_deadline_passed": "use_evidence_within_the_bound_window_or_accept_a_new_contract",
    "prediction_window_unknown": "read_playbill_next_for_the_bound_contract_ids_the_worker_holds",
    "settlement_evidence_mismatch": "bind_the_exact_contract_and_matching_accepted_observation",
    "line_not_accepted": "accept_the_line_before_triggering_it",
    "line_closure_incomplete": "restore_or_succeed_the_missing_accepted_closure_member",
    "occurrence_already_admitted": "read_the_existing_run_state_instead_of_readmitting",
    "procedure_runtime_policy_absent": "accept_a_procedure_runtime_policy",
    "exhaust_binding_carrier_required": "trigger_through_a_carrier_aware_line_scheduler",
    "environment_divergence": "rematerialize_the_provider_environment_against_its_seal",
    # The daemon's Provider lane precondition: its state root is too long for
    # the control sockets and no private runtime directory stands in.
    "provider_runtime_directory_unavailable": (
        "Set XDG_RUNTIME_DIR (Linux) or TMPDIR (macOS) to a short private directory "
        "owned by the daemon user (mode 0700) and restart the daemon, or restart it "
        "with a shorter --state-root."
    ),
    "provider_replay_receipt_required": "record_the_durable_provider_completion_before_replay",
    "state_tap_refused": "restore_the_accepted_state_query_backend",
    "read_ceiling_exceeded": "raise_the_procedure_max_read_rows_or_narrow_the_query",
    "decode_invalid": "acquire_bytes_that_decode_as_the_capture_contract_declares",
    "decoded_limit_exceeded": "raise_the_capture_contract_decoding_limits_or_acquire_less",
    "extract.header_invalid@1": "acquire_a_document_whose_required_header_field_reads",
    "extract_decoding_undeclared": "declare_the_capture_contract_decoding",
    "extract_duplicate_key": "declare_duplicates_refuse_key_or_first_or_dedupe_the_source",
    "extract_format_unsupported": "extract_json_or_csv_or_parse_through_a_provider_operation",
    "consumer_stalled": "restart_the_consumer_through_its_named_repair",
    "line_coverage_gap": "evaluate_the_uncovered_range_then_dispatch_what_it_finds",
    "line_work_pending": "dispatch_the_lines_pending_work",
    "line_disabled_by_upgrade": "enable_the_upgraded_line_to_resume",
    # A Line accepted and never enabled: it runs on its Triggers only once enabled.
    "line_not_enabled": "enable_the_line_to_run_it_on_its_triggers",
    # A run of a Trigger's occurrence failed; the row carries the run and its cause.
    "line_run_failed": "read_the_failed_run_fix_its_cause_then_let_the_line_run_again",
    # Items a Line posted for judgment: read them, then ack, dismiss or link each.
    "line_inbox_unread": "judge_the_lines_unread_inbox_items",
    "evidence_unavailable": "restore_the_capture_from_backup_or_recapture_and_recite_its_claims",
    # A Claim whose cited Captures' bodies were erased moves onto a fresh
    # observation by a re-bind; revising or retiring it carries the erased ones.
    "claim_evidence_erased": "rebind_onto_a_fresh_capture_under_the_same_contract",
    # The row names `settle RSC-...`; the daemon resolves the exact contract and
    # bound window, and which accepted observation settles it is the settler's call.
    "prediction_settleable": "settle_with_the_claim_id_of_an_accepted_observation_in_its_window",
    "prediction_window_unbindable": "restore_the_anchor_capture_material_or_retire_the_contract",
    "block_query_unchecked": "restore_query_evaluation_then_check_the_block",
    "block_frame_invalid": "restore_the_block_marker_frame_the_stamp_declares",
    # The refusal already names its path class and its repair commands. The
    # change itself is the author's: point the Source at a regular, unlinked
    # file inside an authorized root, or attach the root that holds it.
    "workspace_file_read_refused": (
        "point_the_source_at_a_regular_file_inside_an_authorized_workspace_root"
    ),
    "block_marker_malformed": "repair_the_block_marker_to_the_declared_grammar",
    # A first stamp names the backing the author chose, so no served command can
    # supply it: sync skips the block until the marker carries one.
    "block_unstamped": "stamp_the_block_with_an_explicit_claim_or_query_backing",
    "projection_processing_incomplete": (
        "increase_the_projection_processing_budget_or_check_a_smaller_scope"
    ),
    "source_path_invalid": "name_a_workspace_source_path_the_catalog_resolves",
    # A workspace-wide sync infers its targets, and prose that QUOTES the marker
    # grammar looks exactly like a page that declares a block badly. Inference
    # skips it; an operator who believes it is a projection page says so by
    # naming the path, and gets the marker refusal that path deserves.
    "source_not_projection_target": "name_this_path_explicitly_if_it_declares_a_projection_block",
    "workspace_file_catalog_invalid": "repair_the_workspace_file_catalog",
    # A `propose_change_set` item is the Procedure author's: the template
    # resolved to something that is not one Claim proposal item, or to a Claim
    # no observation in the item's own closure can back.
    "proposal_item_invalid": "resolve_each_candidate_template_to_one_claim_proposal_item",
    "proposal_item_evidence_missing": (
        "flow_exactly_one_source_capture_into_each_proposed_claim_item"
    ),
    "proposal_item_evidence_ambiguous": (
        "flow_exactly_one_source_capture_into_each_proposed_claim_item"
    ),
    # A `post_inbox` input is the Procedure author's: an explicit fan-out must
    # resolve to a list, each element's evidence must be its own, and every item
    # and the run's whole post stay within the inbox byte bounds.
    "inbox_item_invalid": "resolve_the_inbox_input_or_its_items_to_a_list",
    "inbox_item_evidence_ambiguous": (
        "keep_per_element_lineage_for_lists_built_from_several_captures"
    ),
    "inbox_item_over_budget": "post_smaller_items_or_fewer_items_per_run",
    "proposal_lowering_refused": "repair_the_proposed_claim_the_lowering_diagnostic_names",
    "proposal_candidate_refused": "repair_the_proposed_member_the_candidate_diagnostic_names",
    "settle_mandate_missing": (
        "Accept one live settle ProcedureMandate for this Procedure whose scope covers every "
        "changed ClaimType and change kind, or use a propose terminal."
    ),
    "settle_mandate_ambiguous": (
        "Narrow or retire all but one of the settle mandates that cover this change; exactly "
        "one may."
    ),
    "settle_condition_refused": (
        "The mandate's condition did not hold for every target and it declares no proposal "
        "fallback; change the facts it reads, or govern the change as a proposal."
    ),
    "settle_publication_refused": (
        "Re-run the occurrence: the mandate expired, was suspended, or accepted state moved "
        "before the settlement published."
    ),
    "proposal_target_paths_mismatch": "rebind_the_terminal_from_the_exact_admitted_run",
    "proposal_receipt_incomplete": "rebind_the_terminal_from_the_exact_admitted_run",
    # The same operation key names another payload: the run that prepared
    # this egress is not the run that created the proposal under it.
    "effectful_operation_payload_mismatch": (
        "read_the_proposal_the_operation_key_already_names_instead_of_replacing_it"
    ),
    # A node refusal has no served carrier for a runnable repair, so the mandate
    # family declares its change: author the mandate the terminal request lacks,
    # exactly as the admission-time `line_mandate_required` names.
    "procedure_mandate_required": "accept_a_procedure_mandate_covering_this_terminal_request",
    "procedure_mandate_superseded": "accept_a_live_successor_procedure_mandate",
    "procedure_mandate_expired": (
        "accept_a_procedure_mandate_whose_window_covers_the_evaluation_instant"
    ),
    "procedure_mandate_grant_insufficient": (
        "accept_a_procedure_mandate_granting_the_required_authority"
    ),
    "procedure_mandate_authority_ceiling_insufficient": (
        "accept_a_procedure_mandate_whose_ceiling_covers_the_admitted_hard_caps"
    ),
    "procedure_mandate_namespace_mismatch": (
        "accept_a_procedure_mandate_whose_namespace_covers_the_target_paths"
    ),
    # The attempt crashed after its proposal was created; recovery published
    # the same proposal receipt, so the next step is to read it, not re-run.
    "terminal_egress_recovered": "read_the_delivered_terminal_receipt_recovery_published",
    "procedure_mandate_procedure_mismatch": (
        "accept_a_procedure_mandate_pinning_this_exact_procedure_artifact"
    ),
    "procedure_mandate_not_applicable": "use_the_declared_terminal_authority",
    "procedure_authority_admission_invalid": "rebuild_the_terminal_from_the_exact_admitted_run",
    "procedure_authority_admission_mismatch": "rebuild_the_terminal_from_the_exact_admitted_run",
    # Only the proposal's author may readmit it and either the author or a
    # daemon-wide operator may withdraw it; which one is the author's call.
    "proposal_stale": "readmit_as_its_author_or_withdraw_the_stale_proposal",
    # The row names the approve command for the calling principal; only the
    # signer holds the key it signs with, so the review and the key are theirs.
    "proposal_awaiting_approval": "review_and_approve_the_candidate_with_your_signing_key",
    # The caller's own candidate needs no further approval: activating it is next.
    "proposal_awaiting_activation": "activate_your_proposal_its_approvals_are_met",
    # A recorded kit add whose provider installs activated, but proposing its
    # definitions refused at the head it resumed on; the detail says why.
    "kit_resume_blocked": "resolve_the_refusal_then_add_the_kit_again",
}


# Codes whose repair is one served CLI leaf. The operation is the leaf's dotted
# path, so the guardrail resolves it against the live lazy command map instead
# of trusting the string; a repair naming a command that does not exist is worse
# than no repair at all. The table lives beside the vocabularies it covers so
# every served refusal producer -- core service, client authoring, CLI -- reads
# the same one instead of re-inventing prose at its own boundary.
RUNNABLE_REFUSAL_REPAIRS: Mapping[str, RepairOperation] = {
    "line_mandate_required": RepairOperation(
        operation="cruxible.authoring.example",
        arguments={"example": "procedure-mandate"},
    ),
    # Subjects of the kind exist, but no SubjectKind says how a new one is named,
    # so none can be created (identity D1): author one.
    "subject_kind_undeclared": RepairOperation(
        operation="cruxible.authoring.example",
        arguments={"example": "subject-kind"},
    ),
    # Nothing renews a mandate: its successor, or its retirement, is authored.
    "mandate_expiring": RepairOperation(
        operation="cruxible.authoring.example",
        arguments={"example": "procedure-mandate"},
    ),
    # Demoting a Line, or repointing it at a fixed Procedure, is a Line successor.
    "line_settled_prior_refusals": RepairOperation(
        operation="cruxible.authoring.example",
        arguments={"example": "line"},
    ),
    # Nothing renders a block, so no sync converges one; what `block sync --all`
    # does is FIND every block whose held list has moved and name the repin that
    # answers each. A producer that knows the source and block names the repin
    # itself, with its arguments; this fallback is the runnable command for a
    # caller that knows only the code.
    "block_backing_changed": RepairOperation(
        operation="cruxible.block.sync", arguments={"all": True}
    ),
    "block_backing_overturned": RepairOperation(
        operation="cruxible.block.sync", arguments={"all": True}
    ),
    "block_backing_missing": RepairOperation(
        operation="cruxible.block.sync", arguments={"all": True}
    ),
    # A retired member cannot be un-retired, so the block either stops holding
    # it or the marker leaves the page. The producer names `block detach PATH`
    # with the path it knows; this fallback names the sweep that finds them.
    "block_backing_retired": RepairOperation(
        operation="cruxible.block.sync", arguments={"all": True}
    ),
    "block_concurrent_edit": RepairOperation(
        operation="cruxible.block.sync", arguments={"all": True}
    ),
    # The prose moved away from what the stamp committed. The author wrote it, so
    # the repair is to re-read the block against its backings and re-stamp it;
    # the producer names that repin with its source and block.
    "block_locally_modified": RepairOperation(
        operation="cruxible.block.sync", arguments={"all": True}
    ),
    "block_successor_ambiguous": RepairOperation(operation="cruxible.block.repin"),
    # Detach strips only a block the instance no longer publishes. The producer
    # names `block depublish FILE BLOCK` with the source and block it knows.
    "block_still_published": RepairOperation(operation="cruxible.block.depublish"),
    "block_sync_failed": RepairOperation(operation="cruxible.block.sync", arguments={"all": True}),
    "projection_backing_stale": RepairOperation(
        operation="cruxible.block.sync", arguments={"all": True}
    ),
    "projection_dirty": RepairOperation(operation="cruxible.block.sync", arguments={"all": True}),
    # The run's acquisition rules (the Line's, else the Procedure's defaults)
    # deny a declared Source input; a Line successor with its acquisition
    # override is the repair, so the runnable command is the Line example.
    "source_acquisition_refused": RepairOperation(
        operation="cruxible.authoring.example",
        arguments={"example": "line"},
    ),
    # Measurement doors. A declaration, run, or subject that does not fit is
    # repaired by inspecting what stands (the readings door is read-only) or
    # by re-running the evaluation once the named fault is corrected.
    "cruxible.resolution.unknown": RepairOperation(operation="cruxible.procedure.readings"),
    "cruxible.resolution.already_disposed": RepairOperation(
        operation="cruxible.procedure.readings"
    ),
    "cruxible.resolution.not_latest": RepairOperation(operation="cruxible.procedure.readings"),
    "cruxible.resolution.contract_closed": RepairOperation(operation="cruxible.procedure.readings"),
    "cruxible.resolution.unavailable": RepairOperation(operation="cruxible.procedure.readings"),
    "cruxible.resolution.conflict": RepairOperation(operation="cruxible.procedure.readings"),
    "measurement_not_declared": RepairOperation(operation="cruxible.procedure.readings"),
    "measurement_run_mismatch": RepairOperation(operation="cruxible.procedure.readings"),
    "measurement_subject_absent": RepairOperation(operation="cruxible.procedure.readings"),
    "measurement_subject_mismatch": RepairOperation(operation="cruxible.procedure.readings"),
    "measurement_basis_unsupported": RepairOperation(operation="cruxible.procedure.readings"),
    "measurement_reading_conflict": RepairOperation(operation="cruxible.procedure.readings"),
    "measurement_resolution_conflict": RepairOperation(operation="cruxible.procedure.measure"),
    "occurrence_not_due": RepairOperation(operation="cruxible.line.run"),
    "occurrence_id_mismatch": RepairOperation(operation="cruxible.line.dispatch"),
    "evaluation_instant_skewed": RepairOperation(operation="cruxible.line.run"),
    "line_identity_mismatch": RepairOperation(operation="cruxible.line.run"),
    "document_modified": RepairOperation(operation="cruxible.files.propose"),
    # A next page cursor names the whole queue it continues; once that queue
    # moves, the repair is to read page one again.
    "cruxible.next.cursor_mismatch": RepairOperation(operation="cruxible.next"),
    "workspace_binding_invalid": RepairOperation(
        operation="cruxible.host.create",
        arguments={"workspace": ".", "replace": True},
    ),
    "workspace_instance_mismatch": RepairOperation(
        operation="cruxible.host.create",
        arguments={"workspace": ".", "replace": True},
    ),
    "workspace_not_attached": RepairOperation(
        operation="cruxible.host.create", arguments={"workspace": "."}
    ),
}


class ServedRepairEnvelope(BaseModel):
    """Validation utility proving the repair union has exactly one branch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repair: ServedRepair

    @model_validator(mode="after")
    def _one_branch(self) -> "ServedRepairEnvelope":
        if isinstance(self.repair, RepairOperation) == isinstance(self.repair, HandEditRepair):
            raise ValueError("repair must select exactly one structured branch")
        return self


def hand_edit_repair(code: str, *, required_change: str | None = None) -> HandEditRepair:
    """Return an explicit non-command repair for a refusal needing judgment."""

    return HandEditRepair(
        hand_edit=HandEditInstruction(
            target=f"refusal/{code}",
            required_change=(
                required_change
                or DECLARED_HAND_EDIT_CHANGES.get(code)
                or UNDECLARED_HAND_EDIT_CHANGE
            ),
        )
    )


def render_served_repair(repair: ServedRepair) -> str:
    """Render one structured repair for a terminal without inventing a command.

    A runnable repair prints the served operation and the exact arguments it
    carries; a hand edit prints its target and the change it requires. Neither
    branch composes an invocation the caller could paste and have fail.
    """

    if isinstance(repair, HandEditRepair):
        return f"hand edit {repair.hand_edit.target}: {repair.hand_edit.required_change}"
    if not repair.arguments:
        return repair.operation
    arguments = json.dumps(repair.arguments, sort_keys=True, separators=(",", ":"))
    return f"{repair.operation} {arguments}"


def served_repair_for_refusal(code: str) -> ServedRepair:
    """Resolve one served refusal code to its declared structured repair.

    Producers call this instead of building a repair at their own boundary, so
    a code that has a served command always reaches the wire with it and a code
    that needs judgment reaches it with the declared change. Membership in a
    closed vocabulary is checked by the source-owned catalog, not here: this
    module is imported by the vocabularies themselves.
    """

    code = normalize_code(code)
    runnable = RUNNABLE_REFUSAL_REPAIRS.get(code)
    if runnable is not None:
        return runnable
    return hand_edit_repair(code)


__all__ = [
    "DECLARED_HAND_EDIT_CHANGES",
    "RUNNABLE_REFUSAL_REPAIRS",
    "UNDECLARED_HAND_EDIT_CHANGE",
    "HandEditInstruction",
    "HandEditRepair",
    "RepairOperation",
    "ServedRepairEnvelope",
    "ServedRepair",
    "hand_edit_repair",
    "render_served_repair",
    "served_repair_for_refusal",
]
