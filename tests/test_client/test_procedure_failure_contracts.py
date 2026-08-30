"""Closed typed terminal contracts for served Procedure runs."""

from __future__ import annotations

from pydantic import TypeAdapter, ValidationError

from cruxible_client.contracts.errors import CanonicalEncodingError
from cruxible_client.contracts.procedures.results import (
    ProcedureAdmissionRefusalV1,
    ProcedureInternalFailureV1,
    ProcedureJournalCoordinateV1,
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
