"""LineSpec identity, Blueprint slot closure, cap, and epoch-successor laws."""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import ArtifactDigest, typed_digest
from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedure
from cruxible_client.contracts.procedures.closure import (
    ProcedurePinClosureError,
    ProcedureSlotBinding,
    close_procedure_pin_slots,
)
from cruxible_client.contracts.procedures.line_specs import (
    RUNG_AUTHORITY,
    AcceptedLineSpec,
    LineSpec,
    evaluate_line_spec_law,
    line_spec_digest,
    line_spec_path,
    parse_line_spec,
    render_line_spec,
)
from cruxible_client.contracts.procedures.models import (
    ProcedureBudget,
    ProcedureDefinition,
    ProcedureHardCaps,
    ProcedurePinSlot,
    ProcedurePinSlotRef,
    ProjectNode,
    StateTapNode,
)
from tests.support.procedures import accepted_procedure, carried_definition


def _digest(label: str) -> str:
    return typed_digest(ArtifactDigest, "playbill-line-test-v1", {"label": label}).tagged


def _pin(role: str, kind: str, name: str, *, digest: str | None = None) -> ArtifactPin:
    return ArtifactPin(
        role=role,
        target=ArtifactIdentity(kind=kind, name=name),
        artifact_digest=digest or _digest(name),
    )


def _definition(query: ArtifactPin | ProcedurePinSlotRef) -> ProcedureDefinition:
    contract_in = _pin("contract-in", "Contract", "empty-input")
    contract_out = _pin("contract-out", "Contract", "claim-rows")
    return ProcedureDefinition(
        name="triage",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            StateTapNode(
                node_id="read",
                query=query,
                parameters={},
                as_="rows",
            ),
            ProjectNode(
                node_id="shape",
                fields={"rows": "$steps.rows"},
                contract_out=contract_out,
                as_="result",
            ),
        ),
        returns="result",
        pin_slots=(
            (
                ProcedurePinSlot(
                    slot_name="query",
                    pin_role="query",
                    artifact_kind="QueryDefinition",
                    interface_digest=_digest("query-interface"),
                ),
            )
            if isinstance(query, ProcedurePinSlotRef)
            else ()
        ),
        budget=ProcedureBudget(
            wall_clock=CanonicalDuration(microseconds=1_000_000),
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=100,
        ),
        hard_caps=ProcedureHardCaps(
            max_wall_clock=CanonicalDuration(microseconds=2_000_000),
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=200,
            max_repeat_attempts=1,
        ),
        terminal_capability=2,
    )


def _accepted_procedure() -> tuple[AcceptedProcedure, ArtifactPin, Mapping[str, str]]:
    """The exact Procedure: its state tap pins the query it reads."""

    query_pin = _pin("query", "QueryDefinition", "claims-by-status")
    accepted = accepted_procedure(_definition(query_pin), activation_policy="drain")
    return accepted, query_pin, {query_pin.artifact_digest: _digest("query-interface")}


def _line(
    *,
    epoch: int = 1,
    predecessor_digest: str | None = None,
    requested_rung: int = 2,
) -> tuple[LineSpec, AcceptedProcedure, Mapping[str, str]]:
    accepted, _query_pin, interfaces = _accepted_procedure()
    procedure_pin = _pin(
        "procedure",
        "Procedure",
        "triage",
        digest=accepted.artifact_digest,
    )
    line = LineSpec(
        identity=ArtifactIdentity(kind="Line", name="triage-hourly"),
        occurrence_epoch=epoch,
        procedure=procedure_pin,
        parameters={"status": "open"},
        max_authority=RUNG_AUTHORITY[requested_rung],
        budgets={
            "max_capture_bytes": 0,
            "max_items": 100,
            "max_provider_calls": 0,
            "max_wall_clock_microseconds": 1_000_000,
        },
        epsilon={"$decimal": "0.1"},
        pins=(procedure_pin,),
        lifecycle=ArtifactLifecycle(predecessor_digest=predecessor_digest),
    )
    return line, accepted, interfaces


def test_line_spec_round_trip_and_digest_golden() -> None:
    line, accepted, _interfaces = _line()

    assert line_spec_digest(line).tagged == (
        "sha256:df53d6b78589e4a43408c423247c09cdc611d48ad801535e5950dba766995ef6"
    )
    content = render_line_spec(line)
    assert parse_line_spec(content, path=line_spec_path("triage-hourly")) == line
    assert (
        evaluate_line_spec_law(
            line,
            path=line_spec_path("triage-hourly"),
            procedure=accepted,
            predecessor=None,
        ).verdict
        == "accepted"
    )


def test_slot_closure_refuses_missing_extra_kind_role_and_interface() -> None:
    blueprint, _owned = carried_definition(_definition(ProcedurePinSlotRef(slot_name="query")))
    _accepted, query_pin, interfaces = _accepted_procedure()
    binding = ProcedureSlotBinding(slot_name="query", artifact_pin=query_pin)

    closed = close_procedure_pin_slots(
        blueprint, (), bindings=(binding,), interface_digests=interfaces
    )
    assert closed.exact_pins == (query_pin,)
    assert closed.bound_slot_names == ("query",)

    with pytest.raises(ProcedurePinClosureError, match="unfilled_pin_slot"):
        close_procedure_pin_slots(blueprint, (), bindings=(), interface_digests=interfaces)

    with pytest.raises(ProcedurePinClosureError, match="extra pin slots"):
        close_procedure_pin_slots(
            blueprint,
            (),
            bindings=(binding, ProcedureSlotBinding(slot_name="zz", artifact_pin=query_pin)),
            interface_digests=interfaces,
        )

    wrong_role = ProcedureSlotBinding(
        slot_name="query",
        artifact_pin=query_pin.model_copy(update={"role": "provider"}),
    )
    with pytest.raises(ProcedurePinClosureError, match="requires role"):
        close_procedure_pin_slots(
            blueprint, (), bindings=(wrong_role,), interface_digests=interfaces
        )

    wrong_kind = ProcedureSlotBinding(
        slot_name="query",
        artifact_pin=query_pin.model_copy(
            update={"target": ArtifactIdentity(kind="Provider", name="claims-by-status")}
        ),
    )
    with pytest.raises(ProcedurePinClosureError, match="requires kind"):
        close_procedure_pin_slots(
            blueprint, (), bindings=(wrong_kind,), interface_digests=interfaces
        )

    with pytest.raises(ProcedurePinClosureError, match="interface digest"):
        close_procedure_pin_slots(
            blueprint,
            (),
            bindings=(binding,),
            interface_digests={query_pin.artifact_digest: _digest("wrong")},
        )


def test_a_line_refuses_a_procedure_with_open_slots() -> None:
    blueprint = accepted_procedure(
        _definition(ProcedurePinSlotRef(slot_name="query")), activation_policy="drain"
    )
    line, _accepted, _interfaces = _line()
    procedure_pin = line.procedure.model_copy(update={"artifact_digest": blueprint.artifact_digest})
    over_blueprint = line.model_copy(update={"procedure": procedure_pin, "pins": (procedure_pin,)})

    result = evaluate_line_spec_law(
        over_blueprint,
        path=line_spec_path(over_blueprint.identity.name),
        procedure=blueprint,
        predecessor=None,
    )
    assert result.diagnostics[0].code == "cruxible.line.procedure_open_slots"


def test_line_refuses_noncanonical_epsilon_and_rung_above_procedure_cap() -> None:
    line, accepted, _interfaces = _line()
    payload = line.model_dump(mode="json")
    payload["epsilon"] = {"$decimal": "0.10"}
    with pytest.raises(ValidationError, match="spelling is not canonical"):
        LineSpec.model_validate(payload)

    too_high, _accepted, _interfaces = _line(requested_rung=3)
    result = evaluate_line_spec_law(
        too_high,
        path=line_spec_path(too_high.identity.name),
        procedure=accepted,
        predecessor=None,
    )
    assert result.diagnostics[0].code == "cruxible.line.rung_exceeds_procedure_cap"


def test_a_served_line_interprets_its_result_budget() -> None:
    from cruxible_core.service.procedures.procedure_runs import _line_budget

    line, procedure, _ = _line()
    value = line.model_dump(mode="json")
    value["budgets"]["max_result_bytes"] = 2 * 1024 * 1024
    served = LineSpec.model_validate(value)
    accepted = AcceptedLineSpec(
        path=line_spec_path(served.identity.name),
        line=served,
        artifact_digest=line_spec_digest(served).tagged,
    )
    assert _line_budget(accepted, procedure).max_result_bytes == 2 * 1024 * 1024


def test_a_line_advances_its_epoch_exactly_when_its_accepted_event_changes() -> None:
    from cruxible_client.contracts.procedures.windows import CaptureEventSelector

    first, accepted, _interfaces = _line()
    prior = AcceptedLineSpec(
        path=line_spec_path(first.identity.name),
        line=first,
        artifact_digest=line_spec_digest(first).tagged,
    )
    rebound = first.model_copy(
        update={
            "parameters": {"status": "closed"},
            "lifecycle": ArtifactLifecycle(predecessor_digest=prior.artifact_digest),
        }
    )

    def verdict(candidate):  # type: ignore[no-untyped-def]
        return evaluate_line_spec_law(
            candidate,
            path=line_spec_path(candidate.identity.name),
            procedure=accepted,
            predecessor=prior,
        )

    assert verdict(rebound).verdict == "accepted"
    assert (
        verdict(rebound.model_copy(update={"occurrence_epoch": 2})).diagnostics[0].code
        == "cruxible.line.occurrence_epoch_mismatch"
    )
    selector = CaptureEventSelector(
        capture_contract_identity=ArtifactIdentity(kind="CaptureContract", name="anchor"),
        capture_contract_digest=_digest("anchor"),
    )
    with pytest.raises(ValidationError, match="come together"):
        LineSpec.model_validate({**first.model_dump(mode="python"), "trigger_event": selector})
    with pytest.raises(ValidationError, match="pins exactly the CaptureContract"):
        LineSpec.model_validate(
            {**first.model_dump(mode="python"), "trigger_event": selector, "trigger_input": "feed"}
        )
