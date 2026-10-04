"""LineSpec identity, slot closure, cap, and trigger-successor laws."""

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
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    ProcedureArtifactV1,
    procedure_artifact_digest,
    procedure_path,
)
from cruxible_client.contracts.procedures.closure import (
    LineSlotBinding,
    ProcedurePinClosureError,
    close_procedure_pin_slots,
)
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest_v3
from cruxible_client.contracts.procedures.line_specs import (
    AcceptedLineSpec,
    CadenceTriggerPolicy,
    CaptureLandingTriggerPolicyV1,
    LineSpecV1,
    ManualTriggerPolicy,
    TriggerPolicyV1,
    WindowCloseTriggerPolicyV1,
    evaluate_line_spec_law,
    line_spec_digest,
    line_spec_path,
    parse_line_spec,
    render_line_spec,
)
from cruxible_client.contracts.procedures.models import (
    ProcedureBudget,
    ProcedureDefinitionV3,
    ProcedureHardCaps,
    ProcedurePinSlot,
    ProcedurePinSlotRef,
    ProjectNode,
    StateTapNodeV3,
)


def _digest(label: str) -> str:
    return typed_digest(ArtifactDigest, "playbill-line-test-v1", {"label": label}).tagged


def _pin(role: str, kind: str, name: str, *, digest: str | None = None) -> ArtifactPin:
    return ArtifactPin(
        role=role,
        target=ArtifactIdentity(kind=kind, name=name),
        artifact_digest=digest or _digest(name),
    )


def _accepted_procedure() -> tuple[AcceptedProcedure, ArtifactPin, Mapping[str, str]]:
    interface_digest = _digest("query-interface")
    query_pin = _pin("query", "QueryDefinition", "claims-by-status")
    contract_in = _pin("contract-in", "Contract", "empty-input")
    contract_out = _pin("contract-out", "Contract", "claim-rows")
    definition = ProcedureDefinitionV3(
        name="triage",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            StateTapNodeV3(
                node_id="read",
                query=ProcedurePinSlotRef(slot_name="query"),
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
            ProcedurePinSlot(
                slot_name="query",
                pin_role="query",
                artifact_kind="QueryDefinition",
                interface_digest=interface_digest,
            ),
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
    procedure = ProcedureArtifactV1(
        identity=ArtifactIdentity(kind="Procedure", name="triage"),
        definition=definition,
        definition_digest=compute_procedure_definition_digest_v3(definition).tagged,
        pins=tuple(
            sorted(
                (contract_in, contract_out),
                key=lambda pin: (pin.role, pin.target.qualified, pin.artifact_digest),
            )
        ),
        activation_policy="drain",
    )
    accepted = AcceptedProcedure(
        path=procedure_path("triage"),
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )
    return accepted, query_pin, {query_pin.artifact_digest: interface_digest}


def _line(
    *,
    trigger: TriggerPolicyV1 | None = None,
    epoch: int = 1,
    predecessor_digest: str | None = None,
    bindings: tuple[LineSlotBinding, ...] | None = None,
    requested_rung: int = 2,
) -> tuple[LineSpecV1, AcceptedProcedure, Mapping[str, str]]:
    accepted, query_pin, interfaces = _accepted_procedure()
    procedure_pin = _pin(
        "procedure",
        "Procedure",
        "triage",
        digest=accepted.artifact_digest,
    )
    bindings = (
        bindings
        if bindings is not None
        else (LineSlotBinding(slot_name="query", artifact_pin=query_pin),)
    )
    trigger = trigger or ManualTriggerPolicy()
    pins = [procedure_pin, *(binding.artifact_pin for binding in bindings)]
    if isinstance(trigger, CadenceTriggerPolicy):
        pins.append(
            _pin(
                "trigger-cadence-policy",
                "Policy",
                "hourly",
                digest=trigger.cadence_policy_digest,
            )
        )
    elif isinstance(trigger, CaptureLandingTriggerPolicyV1):
        pins.extend(
            (
                _pin(
                    "trigger-capture-contract",
                    "CaptureContract",
                    "anchor-capture",
                    digest=trigger.anchor_capture_contract_digest,
                ),
                _pin(
                    "trigger-landing-filter",
                    "LandingFilter",
                    "landing-filter",
                    digest=trigger.landing_filter_digest,
                ),
            )
        )
    elif isinstance(trigger, WindowCloseTriggerPolicyV1):
        pins.append(
            _pin(
                "trigger-window-policy",
                "Policy",
                "window-policy",
                digest=trigger.window_policy_digest,
            )
        )
    line = LineSpecV1(
        identity=ArtifactIdentity(kind="Line", name="triage-hourly"),
        occurrence_epoch=epoch,
        procedure=procedure_pin,
        parameters={"status": "open"},
        slot_bindings=bindings,
        trigger_policy=trigger,
        requested_terminal_rung=requested_rung,  # type: ignore[arg-type]
        budgets={
            "max_capture_bytes": 0,
            "max_items": 100,
            "max_provider_calls": 0,
            "max_wall_clock_microseconds": 1_000_000,
        },
        epsilon={"$decimal": "0.1"},
        pins=tuple(
            sorted(
                pins,
                key=lambda pin: (pin.role, pin.target.qualified, pin.artifact_digest),
            )
        ),
        lifecycle=ArtifactLifecycle(predecessor_digest=predecessor_digest),
    )
    return line, accepted, interfaces


def test_line_spec_round_trip_and_digest_golden() -> None:
    line, accepted, interfaces = _line()

    assert line_spec_digest(line).tagged == (
        "sha256:505c97bf53b24399417bd5c13cc0bac0707b432ad3e92aced26c499322d0ca52"
    )
    content = render_line_spec(line)
    assert parse_line_spec(content, path=line_spec_path("triage-hourly")) == line
    assert (
        evaluate_line_spec_law(
            line,
            path=line_spec_path("triage-hourly"),
            procedure=accepted,
            interface_digests=dict(interfaces),
            predecessor=None,
        ).verdict
        == "accepted"
    )


def test_slot_closure_refuses_missing_extra_kind_role_and_interface() -> None:
    line, accepted, interfaces = _line()
    with pytest.raises(ProcedurePinClosureError, match="unfilled_pin_slot"):
        close_procedure_pin_slots(
            accepted.procedure,
            bindings=(),
            interface_digests=interfaces,
        )

    query_binding = line.slot_bindings[0]
    wrong_role = LineSlotBinding(
        slot_name="query",
        artifact_pin=query_binding.artifact_pin.model_copy(update={"role": "provider"}),
    )
    with pytest.raises(ProcedurePinClosureError, match="requires role"):
        close_procedure_pin_slots(
            accepted.procedure,
            bindings=(wrong_role,),
            interface_digests=interfaces,
        )

    wrong_kind = LineSlotBinding(
        slot_name="query",
        artifact_pin=query_binding.artifact_pin.model_copy(
            update={"target": ArtifactIdentity(kind="Provider", name="claims-by-status")}
        ),
    )
    with pytest.raises(ProcedurePinClosureError, match="requires kind"):
        close_procedure_pin_slots(
            accepted.procedure,
            bindings=(wrong_kind,),
            interface_digests=interfaces,
        )

    with pytest.raises(ProcedurePinClosureError, match="interface digest"):
        close_procedure_pin_slots(
            accepted.procedure,
            bindings=line.slot_bindings,
            interface_digests={
                line.slot_bindings[0].artifact_pin.artifact_digest: _digest("wrong")
            },
        )


def test_line_trigger_change_advances_epoch_but_rebinding_does_not() -> None:
    original, accepted, interfaces = _line()
    prior = AcceptedLineSpec(
        path=line_spec_path(original.identity.name),
        line=original,
        artifact_digest=line_spec_digest(original).tagged,
    )
    same_trigger, _accepted, _interfaces = _line(
        epoch=1,
        predecessor_digest=prior.artifact_digest,
    )
    assert (
        evaluate_line_spec_law(
            same_trigger,
            path=line_spec_path(same_trigger.identity.name),
            procedure=accepted,
            interface_digests=dict(interfaces),
            predecessor=prior,
        ).verdict
        == "accepted"
    )

    cadence = CadenceTriggerPolicy(cadence_policy_digest=_digest("hourly"), interval_seconds=3600)
    changed, _accepted, _interfaces = _line(
        trigger=cadence,
        epoch=2,
        predecessor_digest=prior.artifact_digest,
    )
    assert (
        evaluate_line_spec_law(
            changed,
            path=line_spec_path(changed.identity.name),
            procedure=accepted,
            interface_digests=dict(interfaces),
            predecessor=prior,
        ).verdict
        == "accepted"
    )

    wrong_epoch, _accepted, _interfaces = _line(
        trigger=cadence,
        epoch=1,
        predecessor_digest=prior.artifact_digest,
    )
    result = evaluate_line_spec_law(
        wrong_epoch,
        path=line_spec_path(wrong_epoch.identity.name),
        procedure=accepted,
        interface_digests=dict(interfaces),
        predecessor=prior,
    )
    assert result.diagnostics[0].code == "playbill.line.occurrence_epoch_mismatch"


@pytest.mark.parametrize(
    ("trigger", "pin_role"),
    (
        (
            CadenceTriggerPolicy(cadence_policy_digest=_digest("hourly"), interval_seconds=3600),
            "trigger-cadence-policy",
        ),
        (
            CaptureLandingTriggerPolicyV1(
                anchor_capture_contract_digest=_digest("anchor-capture"),
                landing_filter_digest=_digest("landing-filter"),
            ),
            "trigger-capture-contract",
        ),
        (
            CaptureLandingTriggerPolicyV1(
                anchor_capture_contract_digest=_digest("anchor-capture"),
                landing_filter_digest=_digest("landing-filter"),
            ),
            "trigger-landing-filter",
        ),
        (
            WindowCloseTriggerPolicyV1(
                window_policy_digest=_digest("window-policy"), window_seconds=86400
            ),
            "trigger-window-policy",
        ),
    ),
)
def test_line_trigger_pins_enforce_artifact_kind(
    trigger: TriggerPolicyV1,
    pin_role: str,
) -> None:
    line, _accepted, _interfaces = _line(trigger=trigger)
    payload = line.model_dump(mode="json")
    for pin in payload["pins"]:
        if pin["role"] == pin_role:
            pin["target"]["kind"] = "Provider"
            break
    else:  # pragma: no cover - fixture invariant
        raise AssertionError(f"missing test trigger pin {pin_role!r}")

    with pytest.raises(ValidationError, match="LineSpec trigger pin.*requires"):
        LineSpecV1.model_validate(payload)


def test_line_refuses_noncanonical_epsilon_and_rung_above_procedure_cap() -> None:
    line, accepted, interfaces = _line()
    payload = line.model_dump(mode="json")
    payload["epsilon"] = {"$decimal": "0.10"}
    with pytest.raises(ValidationError, match="spelling is not canonical"):
        LineSpecV1.model_validate(payload)

    too_high, _accepted, _interfaces = _line(requested_rung=3)
    result = evaluate_line_spec_law(
        too_high,
        path=line_spec_path(too_high.identity.name),
        procedure=accepted,
        interface_digests=dict(interfaces),
        predecessor=None,
    )
    assert result.diagnostics[0].code == "playbill.line.rung_exceeds_procedure_cap"


def test_a_served_line_interprets_its_result_budget() -> None:
    from cruxible_client.contracts.procedures.line_specs import LineSpec
    from cruxible_core.service.procedures.procedure_runs import _line_budget

    line, procedure, _ = _line()
    value = line.model_dump(mode="json")
    for field in ("artifact_format", "trigger_policy", "requested_terminal_rung"):
        value.pop(field)
    value["budgets"]["max_result_bytes"] = 2 * 1024 * 1024
    served = LineSpec.model_validate(
        {**value, "max_authority": "propose", "provider_implementation_closures": []}
    )
    accepted = AcceptedLineSpec(
        path=line_spec_path(served.identity.name),
        line=served,
        artifact_digest=line_spec_digest(served).tagged,
    )
    assert _line_budget(accepted, procedure).max_result_bytes == 2 * 1024 * 1024


def test_a_v6_line_advances_its_epoch_exactly_when_its_accepted_event_changes() -> None:
    from cruxible_client.contracts.procedures.line_specs import LineSpec
    from cruxible_client.contracts.procedures.windows import CaptureEventSelector
    from tests.support.lines import graph_v4
    from tests.test_procedures.test_procedure_run_surface import _slotless_procedure
    from tests.test_server.test_playbill_line_run_refusals import (
        _acquisition_policy,
        _served_line,
    )

    accepted = graph_v4(_slotless_procedure("epoch-method"))
    interfaces: dict[str, str] = {}
    first = _served_line("epoch-line", accepted=accepted, policy=_acquisition_policy("epoch"))
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
            interface_digests=dict(interfaces),
            predecessor=prior,
        )

    assert verdict(rebound).verdict == "accepted"
    assert (
        verdict(rebound.model_copy(update={"occurrence_epoch": 2})).diagnostics[0].code
        == "playbill.line.occurrence_epoch_mismatch"
    )
    # A v6 lineage is never succeeded by a Line that embeds its trigger.
    from cruxible_client.contracts.procedures.line_specs import LineSpecV5

    embedded = first.model_dump(mode="python")
    for field in ("artifact_format", "trigger_input", "trigger_event"):
        embedded.pop(field)
    downgraded = LineSpecV5.model_validate(
        {
            **embedded,
            "trigger_policy": ManualTriggerPolicy(),
            "lifecycle": ArtifactLifecycle(predecessor_digest=prior.artifact_digest),
        }
    )
    assert verdict(downgraded).diagnostics[0].code == "playbill.line.wire_downgrade"
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


def test_a_v6_line_instantiates_an_earlier_graph_without_provider_closures() -> None:
    from tests.test_integration.test_graph_v4_provider_closure import _line as graph_line
    from tests.test_procedures.test_procedure_run_surface import _slotless_procedure
    from tests.test_server.test_playbill_line_run_refusals import (
        _acquisition_policy,
        _served_line,
    )

    accepted = _slotless_procedure("graph-v3-method")
    line = _served_line("graph-v3-line", accepted=accepted, policy=_acquisition_policy("g3"))

    def verdict(candidate):  # type: ignore[no-untyped-def]
        return evaluate_line_spec_law(
            candidate,
            path=line_spec_path(candidate.identity.name),
            procedure=accepted,
            interface_digests={},
            predecessor=None,
        )

    assert verdict(line).verdict == "accepted"
    closures = graph_line().provider_implementation_closures
    assert (
        verdict(line.model_copy(update={"provider_implementation_closures": closures}))
        .diagnostics[0]
        .code
        == "playbill.line.graph_v4_required"
    )
