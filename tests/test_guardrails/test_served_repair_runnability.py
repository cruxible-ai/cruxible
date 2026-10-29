"""Closed served refusal vocabularies always carry structured repair."""

from __future__ import annotations

from cruxible_client.contracts.authoring.models import BlockSyncItem
from cruxible_client.contracts.procedures.results import ProcedureAdmissionRefusal
from cruxible_client.contracts.repairs import (
    DECLARED_HAND_EDIT_CHANGES,
    UNDECLARED_HAND_EDIT_CHANGE,
    HandEditRepair,
    RepairOperation,
    ServedRepairEnvelope,
)
from cruxible_core.cli.main import CLI_COMMANDS, LazyCommandSpec
from cruxible_core.service.refusals import (
    ALL_SERVED_REFUSAL_CODES,
    CLOSED_SERVED_REFUSAL_VOCABULARIES,
    RUNNABLE_REFUSAL_REPAIRS,
    UNDECLARED_REFUSAL_CODE_COUNT,
    repair_for_refusal,
    undeclared_refusal_codes,
)

# Explicit inventory: B1 adds the three cruxible.procedure.slot_* codes.
EXPECTED_UNDECLARED_REFUSAL_CODES = frozenset(
    {
        "acceptance_divergence",
        "adapter_value_invalid",
        "admission_material_corrupt",
        "admission_material_unavailable_by_policy",
        "aggregate_items_input_invalid",
        "air_gapped_cache_miss",
        "ambiguous_implementation",
        "artifact_binding_mismatch",
        "artifact_hash_mismatch",
        "block_workspace_instance_mismatch",
        "bucket_fixture_missing",
        "budget_cost",
        "budget_exhausted",
        "budget_output_size",
        "budget_wall_clock",
        "cache_integrity",
        "cache_permissions",
        "cas_unavailable_at_replay",
        "citation_drifted",
        "citation_source_unobserved",
        "claim_attestation_threshold_met",
        "claim_conflicted",
        "claim_contradicting_evidence_available",
        "claim_dependency_stale",
        "claim_new_evidence_supporting",
        "claim_new_evidence_unreviewed",
        "claim_stale_evidence",
        "claim_uncovered",
        "classifier_digest_mismatch",
        "classifier_not_installed",
        "compiler_invariant_broken",
        "contract_input_refused",
        "contract_output_refused",
        "cross_origin_credentialed_redirect",
        "cruxible.acquisition.oversized",
        "cruxible.acquisition.refused",
        "cruxible.acquisition.stale",
        "cruxible.acquisition.unavailable",
        "cruxible.line.slot_ambiguous",
        "cruxible.line.slot_unfilled",
        "cruxible.procedure.slot_undeclared",
        "declared_family_mismatch",
        "dedupe_items_input_invalid",
        "degenerate_scale",
        "effect_dispatch_requires_actor",
        "effect_dispatch_requires_authenticated_actor",
        "effect_grant_unrecognized",
        "evidence_expiring",
        "fetch_failed",
        "filter_items_input_invalid",
        "floor_invalid",
        "git_unavailable",
        "guard_refused",
        "image_provenance_mismatch",
        "index_not_pinned",
        "index_redirect",
        "input_material_mismatch",
        "insufficient_series_length",
        "interface_digest_mismatch",
        "invalid_bucket_vocabulary",
        "invalid_parameter",
        "join_items_left_input_invalid",
        "join_items_right_input_invalid",
        "journal_append_failed",
        "journal_conflict",
        "journal_integrity_error",
        "journal_read_failed",
        "line_binding_required",
        "lock_ambiguous_fork",
        "lock_bytes_mismatch",
        "lock_mismatch",
        "lock_missing_hash",
        "malformed_model_ref",
        "manifest_divergence",
        "mismatched_lengths",
        "network_disabled",
        "no_compatible_artifact",
        "non_finite_input",
        "non_finite_output",
        "non_finite_result",
        "not_current",
        "object_format_mismatch",
        "pin_binding_mismatch",
        "projection_marker_invalid",
        "provider_acquisition_plan_mismatch",
        "provider_acquisition_plan_required",
        "provider_completion_not_durable",
        "provider_declined",
        "provider_effect_declaration_mismatch",
        "provider_process_group_survived_recovery",
        "provider_process_lease_echo_failed",
        "provider_process_lease_echo_mismatch",
        "provider_process_lease_invalid",
        "provider_process_lease_missing",
        "provider_protocol_violation",
        "provider_refusal_taxonomy_unknown",
        "provider_runtime_not_in_materialization",
        "provider_runtime_recovery_failed",
        "provider_unavailable",
        "redirect_limit",
        "remote_conflict",
        "repeat_exhausted",
        "replay_material_mismatch",
        "replay_material_unavailable",
        "result_not_canonical",
        "run_record_invalid",
        "run_recovery_required",
        "runtime_reference_unresolved",
        "secret_bundle_too_large",
        "secret_epoch_unavailable",
        "secret_leak",
        "secret_resolver_not_installed",
        "shape_items_input_invalid",
        "source_acquisition_unavailable",
        "source_material_unavailable",
        "terminal_authority_capped_by_line_max_authority",
        "terminal_authority_capped_by_mandate_grant",
        "terminal_authority_capped_by_procedure_terminal_capability",
        "terminal_authority_capped_by_propagated_sensitivity",
        "terminal_egress_unverified",
        "terminal_not_available",
        "unaccepted_provider",
        "unclaimed_bucket",
        "unclassified_input",
        "undeclared_egress",
        "undeclared_interface",
        "undeclared_match_parameters",
        "unexpected_exception",
        "unexpected_failure",
        "unknown_column",
        "unknown_extra",
        "unknown_interface",
        "unknown_manifest_field",
        "unknown_method",
        "unknown_run_context_field",
        "unknown_test_name",
        "unregistered_projection_block",
        "unresolvable_source",
        "unresolved_secret_ref",
        "unsupported_aggregation",
        "unsupported_backend",
        "unsupported_node",
        "unsupported_protocol",
        "unsupported_redirect_scheme",
        "wall_clock_exhausted",
        "workspace_missing",
        "workspace_not_git",
        "workspace_path_invalid",
    }
)


def _cli_leaves(commands: dict[str, LazyCommandSpec], path: tuple[str, ...] = ()) -> set[str]:
    leaves: set[str] = set()
    for name, spec in commands.items():
        if spec.commands:
            leaves |= _cli_leaves(spec.commands, (*path, name))
        else:
            leaves.add(".".join((*path, name)))
    return leaves


def test_every_registered_refusal_resolves_without_prose_parsing() -> None:
    assert ALL_SERVED_REFUSAL_CODES
    assert all(CLOSED_SERVED_REFUSAL_VOCABULARIES.values())

    for code in ALL_SERVED_REFUSAL_CODES:
        repair = repair_for_refusal(code)
        assert ServedRepairEnvelope(repair=repair).repair == repair
        if isinstance(repair, HandEditRepair):
            assert repair.hand_edit.target == f"refusal/{code}"
            assert repair.hand_edit.required_change


def test_every_runnable_repair_names_a_command_the_cli_actually_serves() -> None:
    """A repair naming a command that does not exist is worse than none."""

    leaves = _cli_leaves(CLI_COMMANDS, ("cruxible",))
    assert "cruxible.line.run" in leaves  # the map really is the served inventory
    for code, repair in RUNNABLE_REFUSAL_REPAIRS.items():
        assert code in ALL_SERVED_REFUSAL_CODES, code
        assert isinstance(repair, RepairOperation)
        assert repair.operation in leaves, f"{code} names unserved command {repair.operation}"


def test_server_envelope_repairs_name_commands_the_cli_actually_serves() -> None:
    """The daemon-scope and authentication emitters carry runnable repairs."""

    from cruxible_core.errors import AuthenticationError, DaemonOperationScopeError
    from cruxible_core.server.errors import _repair_for_error

    leaves = _cli_leaves(CLI_COMMANDS)
    scope = _repair_for_error(DaemonOperationScopeError("cruxible_server_info", "inst_a"))
    assert isinstance(scope, RepairOperation)
    assert scope.operation in leaves
    assert scope.arguments["refused_operation"] == "cruxible_server_info"

    unauthenticated = _repair_for_error(AuthenticationError("no credential"))
    assert isinstance(unauthenticated, RepairOperation)
    assert unauthenticated.operation in leaves

    from typing import get_args

    from cruxible_core.errors import BootstrapClaimRefusalCode, BootstrapClaimRefusedError

    for code in get_args(BootstrapClaimRefusalCode):
        bootstrap = _repair_for_error(BootstrapClaimRefusedError(code, instance_id="inst_a"))
        assert isinstance(bootstrap, RepairOperation)
        assert bootstrap.operation in leaves, code


def test_the_served_refusal_models_read_the_declared_change() -> None:
    """A producer that carries no repair still projects the declared change."""

    from cruxible_client.contracts.procedures.results import ProcedureNodeRefusal

    refusal = ProcedureNodeRefusal.model_validate(
        {
            "code": "proposal_target_paths_mismatch",
            "message": "the candidate scope differs from its admission",
            "node_id": "propose",
        }
    )
    assert isinstance(refusal.repair, HandEditRepair)
    assert (
        refusal.repair.hand_edit.required_change
        == (DECLARED_HAND_EDIT_CHANGES["proposal_target_paths_mismatch"])
    )


def test_declared_hand_edits_are_membership_not_derivation() -> None:
    for code, required_change in DECLARED_HAND_EDIT_CHANGES.items():
        assert code in ALL_SERVED_REFUSAL_CODES, code
        assert code not in RUNNABLE_REFUSAL_REPAIRS, code
        assert required_change != UNDECLARED_HAND_EDIT_CHANGE
        # The declared change may not be the code restated: `repair_<code>` and
        # its kin read like an instruction while carrying none.
        assert required_change != f"repair_{code}"
        assert required_change != code


def test_undeclared_repair_debt_is_pinned_and_can_only_shrink() -> None:
    """A new closed refusal member cannot join without being classified."""

    undeclared = undeclared_refusal_codes()
    assert undeclared == EXPECTED_UNDECLARED_REFUSAL_CODES
    assert len(undeclared) == UNDECLARED_REFUSAL_CODE_COUNT
    for code in undeclared:
        repair = repair_for_refusal(code)
        assert isinstance(repair, HandEditRepair)
        assert repair.hand_edit.required_change == UNDECLARED_HAND_EDIT_CHANGE


def test_unregistered_free_string_is_not_promoted_to_authority() -> None:
    try:
        repair_for_refusal("compiler diagnostic prose")
    except KeyError as exc:
        assert "unregistered served refusal" in str(exc)
    else:  # pragma: no cover - decisive guard
        raise AssertionError("free diagnostic prose entered the v1 refusal catalog")


def _admission_refusal_repair(code: str) -> RepairOperation | HandEditRepair:
    """Build the served admission refusal exactly as a producer that carries none."""

    return ProcedureAdmissionRefusal.model_validate({"code": code, "message": "refused"}).repair


def _block_sync_repair(code: str) -> RepairOperation | HandEditRepair:
    """Build the served block-sync item exactly as a producer that carries none."""

    item = BlockSyncItem.model_validate({"path": ".", "outcome": "refused", "reason": code})
    assert item.repair is not None
    return item.repair


def _prediction_refusal_repair(code: str) -> object:
    from cruxible_core.server.errors import error_to_response
    from cruxible_core.service.procedures.predictions import _refuse

    _status, response = error_to_response(_refuse(code, "refused"))  # type: ignore[arg-type]
    return response.repair


def _measurement_refusal_repair(code: str) -> object:
    from cruxible_core.server.errors import error_to_response
    from cruxible_core.service.procedures.measurements import ProcedureMeasurementRefused

    _status, response = error_to_response(ProcedureMeasurementRefused(code, "refused"))  # type: ignore[arg-type]
    return response.repair


def _resolution_disposition_refusal_repair(code: str) -> object:
    from cruxible_core.server.errors import error_to_response
    from cruxible_core.service.procedures.resolution_dispositions import (
        ResolutionDispositionRefused,
    )

    status, response = error_to_response(ResolutionDispositionRefused(code, "refused"))
    assert status == (404 if code == "cruxible.resolution.unknown" else 409)
    assert response.error_code == code
    return response.repair


def _next_refusal_repair(code: str) -> object:
    from cruxible_core.server.errors import error_to_response
    from cruxible_core.service.discovery.next import NextCursorMismatch

    assert code == NextCursorMismatch.code
    status, response = error_to_response(NextCursorMismatch("refused"))
    assert status == 400
    assert response.error_code == code
    return response.repair


def test_every_runnable_repair_reaches_a_live_wire_response() -> None:
    """A declared runnable repair that never reaches a served payload is prose."""

    from cruxible_core.service.discovery.next import _repair_command

    seen: dict[str, str] = {}
    for code, declared in RUNNABLE_REFUSAL_REPAIRS.items():
        owners = {
            name for name, members in CLOSED_SERVED_REFUSAL_VOCABULARIES.items() if code in members
        }
        assert owners, code
        if "procedure_admission_refusal" in owners:
            built = _admission_refusal_repair(code)
            assert built == declared, code
            seen[code] = "procedure_admission_refusal"
        elif owners & {"block_sync_reason", "block_sync_read_reason"}:
            built = _block_sync_repair(code)
            assert built == declared, code
            seen[code] = "block_sync"
        elif "prediction_refusal" in owners:
            built = _prediction_refusal_repair(code)
            assert isinstance(built, RepairOperation), code
            assert built.operation == declared.operation, code
            seen[code] = "prediction_refusal"
        elif "procedure_measurement_refusal" in owners:
            built = _measurement_refusal_repair(code)
            assert isinstance(built, RepairOperation), code
            assert built.operation == declared.operation, code
            seen[code] = "procedure_measurement_refusal"
        elif "resolution_disposition_refusal" in owners:
            built = _resolution_disposition_refusal_repair(code)
            assert built == declared, code
            seen[code] = "resolution_disposition_refusal"
        elif "playbill_next_refusal" in owners:
            built = _next_refusal_repair(code)
            assert built == declared, code
            seen[code] = "playbill_next_refusal"
        elif "playbill_next_reason" in owners:
            # The next lane carries its own structured repair; the catalog entry
            # is honest only if that lane really composes a runnable command for
            # the same served operation.
            command = _repair_command(declared.operation, arguments=declared.arguments)
            assert command is not None and command.startswith("cruxible "), code
            seen[code] = "playbill_next_reason"
        else:  # pragma: no cover - decisive guard
            raise AssertionError(f"{code} has no live served carrier")
    assert set(seen) == set(RUNNABLE_REFUSAL_REPAIRS)
