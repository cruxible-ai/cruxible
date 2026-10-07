"""Closed typed terminal contracts for served Procedure runs."""

from __future__ import annotations

from typing import get_args

from pydantic import TypeAdapter, ValidationError

from cruxible_client.contracts.errors import CanonicalEncodingError
from cruxible_client.contracts.procedures.results import (
    ProcedureAdmissionRefusal,
    ProcedureInternalFailure,
    ProcedureJournalCoordinate,
    ProcedureNodeRefusal,
    ProcedureNodeRefusalCode,
    ProcedureOperationalFailure,
    ProcedureTerminal,
)


def _coordinate() -> ProcedureJournalCoordinate:
    return ProcedureJournalCoordinate(
        stream_instance_id="instance-a",
        journal_family="procedure-exhaust-v1",
        stream_id="procedures",
        partition_id="direct-runs",
        sequence=2,
        record_digest="sha256:" + "1" * 64,
    )


def test_terminal_union_round_trips_all_four_classes() -> None:
    terminals = (
        ProcedureAdmissionRefusal(
            code="unsupported_node",
            message="This Procedure runs only as a Line.",
            details={"runnable": "line", "unsupported_nodes": [{"node_id": "settle"}]},
        ),
        ProcedureNodeRefusal(
            code="guard_refused",
            message="Guard refused.",
            node_id="gate",
            journal_coordinate=_coordinate(),
            detail_code="query.empty",
        ),
        ProcedureOperationalFailure(
            code="journal_append_failed",
            message="Journal append failed.",
            journal_coordinate=_coordinate(),
        ),
        ProcedureInternalFailure(
            code="unexpected_exception",
            message="Procedure execution failed unexpectedly; inspect daemon logs.",
            correlation_id="RUN-abc",
            journal_coordinate=_coordinate(),
        ),
    )
    adapter = TypeAdapter(ProcedureTerminal)

    for terminal in terminals:
        assert adapter.validate_python(terminal.model_dump(mode="json")) == terminal


def test_terminal_contracts_are_closed_and_details_are_canonical() -> None:
    try:
        ProcedureAdmissionRefusal.model_validate(
            {
                "code": "unknown",
                "message": "bad",
                "details": {},
            }
        )
    except ValidationError:
        pass
    else:  # pragma: no cover - assertion spelling keeps the failure readable
        raise AssertionError("unknown admission code was accepted")

    try:
        ProcedureAdmissionRefusal(
            code="unsupported_node",
            message="bad",
            details={"value": 1.5},
        )
    except CanonicalEncodingError:
        pass
    else:  # pragma: no cover
        raise AssertionError("non-canonical terminal details were accepted")


def test_node_refusal_vocabulary_covers_every_executor_code() -> None:
    expected = {
        "guard_refused",
        "repeat_exhausted",
        "budget_exhausted",
        "runtime_reference_unresolved",
        "contract_input_refused",
        "contract_output_refused",
        "adapter_value_invalid",
        "shape_items_input_invalid",
        "filter_items_input_invalid",
        "dedupe_items_input_invalid",
        "join_items_left_input_invalid",
        "join_items_right_input_invalid",
        "aggregate_items_input_invalid",
        "result_not_canonical",
        "line_binding_required",
        "source_acquisition_unavailable",
        "source_material_unavailable",
        "terminal_not_available",
        "terminal_egress_unverified",
        "proposal_item_invalid",
        "proposal_item_evidence_missing",
        "proposal_item_evidence_ambiguous",
        "proposal_lowering_refused",
        "proposal_candidate_refused",
        "settle_mandate_missing",
        "settle_mandate_ambiguous",
        "settle_condition_refused",
        "settle_publication_refused",
        "proposal_target_paths_mismatch",
        "proposal_receipt_incomplete",
        "effectful_operation_payload_mismatch",
        "procedure_mandate_required",
        "procedure_mandate_superseded",
        "procedure_mandate_expired",
        "procedure_mandate_procedure_mismatch",
        "procedure_mandate_grant_insufficient",
        "procedure_mandate_authority_ceiling_insufficient",
        "procedure_mandate_namespace_mismatch",
        "procedure_mandate_not_applicable",
        "procedure_authority_admission_invalid",
        "procedure_authority_admission_mismatch",
        "provider_unavailable",
        "unclassified_input",
        "unclaimed_bucket",
        "cruxible.acquisition.unavailable",
        "cruxible.acquisition.stale",
        "cruxible.acquisition.oversized",
        "cruxible.acquisition.refused",
        "effect_grant_unrecognized",
        "effect_dispatch_requires_actor",
        "effect_dispatch_requires_authenticated_actor",
        "terminal_authority_capped_by_procedure_terminal_capability",
        "terminal_authority_capped_by_line_max_authority",
        "terminal_authority_capped_by_propagated_sensitivity",
        "terminal_authority_capped_by_mandate_grant",
        "terminal_authority_capped_by_calibration",
        "provider_acquisition_plan_required",
        "provider_acquisition_plan_mismatch",
        "workspace_file_read_refused",
        "provider_effect_declaration_mismatch",
        "classifier_not_installed",
        "classifier_digest_mismatch",
        "unknown_extra",
        "budget_wall_clock",
        "budget_output_size",
        "budget_cost",
        "provider_declined",
        "secret_bundle_too_large",
        "insufficient_series_length",
        "non_finite_input",
        "degenerate_scale",
        "mismatched_lengths",
        "unknown_method",
        "unknown_test_name",
        "declared_family_mismatch",
        "unsupported_aggregation",
        "unknown_column",
        "malformed_model_ref",
        "undeclared_match_parameters",
        "invalid_parameter",
        "cross_origin_credentialed_redirect",
        "unsupported_redirect_scheme",
        "redirect_limit",
        "provider_protocol_violation",
    }
    assert set(get_args(ProcedureNodeRefusalCode)) == expected

    for code in sorted(expected):
        values: dict[str, object] = {
            "code": code,
            "message": "Typed executor refusal.",
            "node_id": "node",
        }
        if code == "guard_refused":
            values["detail_code"] = "procedure.guard"
        if code == "budget_exhausted":
            values["budget"] = {
                "budget_kind": "max_provider_calls",
                "limit": 0,
                "observed": 1,
            }
        assert ProcedureNodeRefusal.model_validate(values).code == code
