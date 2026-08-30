"""Closed typed terminal contracts for served Procedure runs."""

from __future__ import annotations

from typing import get_args

from pydantic import TypeAdapter, ValidationError

from cruxible_client.contracts.errors import CanonicalEncodingError
from cruxible_client.contracts.procedures.results import (
    ProcedureAdmissionRefusalV1,
    ProcedureInternalFailureV1,
    ProcedureJournalCoordinateV1,
    ProcedureNodeRefusalCodeV1,
    ProcedureNodeRefusalV1,
    ProcedureOperationalFailureV1,
    ProcedureTerminalV1,
)


def _coordinate() -> ProcedureJournalCoordinateV1:
    return ProcedureJournalCoordinateV1(
        stream_instance_id="instance-a",
        journal_family="procedure-exhaust-v1",
        stream_id="procedures",
        partition_id="direct-runs",
        sequence=2,
        record_digest="sha256:" + "1" * 64,
    )


def test_terminal_union_round_trips_all_four_classes() -> None:
    terminals = (
        ProcedureAdmissionRefusalV1(
            code="binding_required",
            message="Bindings are incomplete.",
            details={"required_slots": ["query"]},
        ),
        ProcedureNodeRefusalV1(
            code="guard_refused",
            message="Guard refused.",
            node_id="gate",
            journal_coordinate=_coordinate(),
            detail_code="query.empty",
        ),
        ProcedureOperationalFailureV1(
            code="journal_append_failed",
            message="Journal append failed.",
            journal_coordinate=_coordinate(),
        ),
        ProcedureInternalFailureV1(
            code="unexpected_exception",
            message="Procedure execution failed unexpectedly; inspect daemon logs.",
            correlation_id="RUN-abc",
            journal_coordinate=_coordinate(),
        ),
    )
    adapter = TypeAdapter(ProcedureTerminalV1)

    for terminal in terminals:
        assert adapter.validate_python(terminal.model_dump(mode="json")) == terminal


def test_terminal_contracts_are_closed_and_details_are_canonical() -> None:
    try:
        ProcedureAdmissionRefusalV1.model_validate(
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
        ProcedureAdmissionRefusalV1(
            code="binding_required",
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
        "provider_unavailable",
        "playbill.acquisition.unavailable",
        "playbill.acquisition.stale",
        "playbill.acquisition.oversized",
        "playbill.acquisition.refused",
        "effect_grant_unrecognized",
        "effect_dispatch_requires_actor",
        "effect_dispatch_requires_authenticated_actor",
        "terminal_rung_capped_by_procedure_terminal_capability",
        "terminal_rung_capped_by_line_requested_rung",
        "terminal_rung_capped_by_propagated_sensitivity",
        "terminal_rung_capped_by_mandate_grant",
        "terminal_rung_capped_by_calibration",
    }
    assert set(get_args(ProcedureNodeRefusalCodeV1)) == expected

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
        assert ProcedureNodeRefusalV1.model_validate(values).code == code
