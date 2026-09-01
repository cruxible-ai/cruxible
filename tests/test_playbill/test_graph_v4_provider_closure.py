"""Graph-v4 explicit Provider pins and accepted Line-v2 closure laws."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.captures import CanonicalDurationV1
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedureV1,
    ProcedureArtifactV1,
    evaluate_procedure_law,
    parse_procedure,
    procedure_artifact_digest,
    procedure_path,
    render_procedure,
)
from cruxible_client.contracts.procedures.closure import (
    LineSlotBindingV1,
    ProviderExtrasEnvironmentPinMapV1,
    ProviderImplementationClosureV1,
)
from cruxible_client.contracts.procedures.graph import (
    compute_procedure_definition_digest_v4,
    compute_procedure_node_digests_v4,
)
from cruxible_client.contracts.procedures.line_specs import (
    LineSpecV1,
    LineSpecV2,
    ManualTriggerPolicyV1,
    evaluate_line_spec_law,
    line_spec_digest,
    line_spec_path,
    parse_line_spec,
    render_line_spec,
)
from cruxible_client.contracts.procedures.models import (
    GuardPredicateV1,
    PredicateOperandV1,
    ProcedureBudgetV3,
    ProcedureDefinitionV4,
    ProcedureHardCapsV3,
    ProcedurePinSlotRefV1,
    ProcedurePinSlotV1,
    ProviderNodeV4,
    RepeatBodyNodeV4,
    RepeatNodeV4,
)
from tests.test_playbill._p2b1_support import (
    accepted_interface,
    accepted_provider,
    digest,
    pin,
)


def _definition() -> tuple[ProcedureDefinitionV4, ArtifactPin, ArtifactPin]:
    provider = accepted_provider()
    interface = accepted_interface()
    provider_pin = pin(
        "provider",
        "Provider",
        "demo-provider",
        value=provider.artifact_digest,
    )
    interface_pin = pin(
        "provider-interface",
        "ProviderInterface",
        "demo.interface",
        value=interface.artifact_digest,
    )
    contract_in = pin("contract-in", "Contract", "provider-input")
    contract_out = pin("contract-out", "Contract", "provider-output")
    implementation_digest = provider.provider.implementations[0].implementation_digest
    definition = ProcedureDefinitionV4(
        name="provider-v4",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            ProviderNodeV4(
                node_id="direct",
                provider=provider_pin,
                interface=interface_pin,
                interface_digest=interface.registration.interface_digest,
                implementation_digest=implementation_digest,
                contract_in=contract_in,
                contract_out=contract_out,
                input={"value": 1},
                as_="direct_result",
            ),
            ProviderNodeV4(
                node_id="slot",
                provider=ProcedurePinSlotRefV1(slot_name="provider"),
                interface=interface_pin,
                interface_digest=interface.registration.interface_digest,
                contract_in=contract_in,
                contract_out=contract_out,
                input={"value": "$steps.direct_result"},
                as_="result",
            ),
        ),
        returns="result",
        pin_slots=(
            ProcedurePinSlotV1(
                slot_name="provider",
                pin_role="provider",
                artifact_kind="Provider",
                interface_digest=interface.registration.interface_digest,
            ),
        ),
        budget=ProcedureBudgetV3(
            wall_clock=CanonicalDurationV1(microseconds=2_000_000),
            max_provider_calls=2,
            max_capture_bytes=1024,
            max_items=10,
        ),
        hard_caps=ProcedureHardCapsV3(
            max_wall_clock=CanonicalDurationV1(microseconds=4_000_000),
            max_provider_calls=4,
            max_capture_bytes=2048,
            max_items=20,
            max_repeat_attempts=2,
        ),
        terminal_capability=1,
    )
    return definition, provider_pin, interface_pin


def _accepted_procedure() -> AcceptedProcedureV1:
    definition, provider_pin, interface_pin = _definition()
    pins = {
        definition.contract_in,
        definition.contract_out,
        provider_pin,
        interface_pin,
    }
    procedure = ProcedureArtifactV1(
        identity=ArtifactIdentity(kind="Procedure", name=definition.name),
        definition=definition,
        definition_digest=compute_procedure_definition_digest_v4(definition).tagged,
        pins=tuple(
            sorted(
                pins,
                key=lambda item: (
                    item.role,
                    item.target.qualified,
                    item.artifact_digest,
                ),
            )
        ),
        activation_policy="drain",
    )
    return AcceptedProcedureV1(
        path=procedure_path(procedure.identity.name),
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )


def _line() -> LineSpecV2:
    procedure = _accepted_procedure()
    provider = accepted_provider()
    interface = accepted_interface()
    provider_pin = pin(
        "provider",
        "Provider",
        "demo-provider",
        value=provider.artifact_digest,
    )
    procedure_pin = pin(
        "procedure",
        "Procedure",
        procedure.procedure.identity.name,
        value=procedure.artifact_digest,
    )
    implementation = provider.provider.implementations[0]
    environment_map = ProviderExtrasEnvironmentPinMapV1(
        required_extras=("engine",),
        eligible_environment_pin_keys=("linux-cp311+engine",),
    )
    closures = (
        ProviderImplementationClosureV1(
            node_id="slot",
            slot_name="provider",
            provider_artifact_digest=provider.artifact_digest,
            interface_artifact_digest=interface.artifact_digest,
            interface_digest=interface.registration.interface_digest,
            implementation_digest=implementation.implementation_digest,
            environment_pin_map=environment_map,
        ),
    )
    return LineSpecV2(
        identity=ArtifactIdentity(kind="Line", name="provider-v4-line"),
        occurrence_epoch=1,
        procedure=procedure_pin,
        parameters={},
        slot_bindings=(LineSlotBindingV1(slot_name="provider", artifact_pin=provider_pin),),
        trigger_policy=ManualTriggerPolicyV1(),
        requested_terminal_rung=1,
        budgets={
            "max_capture_bytes": 1024,
            "max_items": 10,
            "max_provider_calls": 2,
            "max_wall_clock_microseconds": 2_000_000,
        },
        epsilon={"$decimal": "0"},
        pins=tuple(
            sorted(
                (procedure_pin, provider_pin),
                key=lambda item: (
                    item.role,
                    item.target.qualified,
                    item.artifact_digest,
                ),
            )
        ),
        provider_implementation_closures=closures,
    )


def test_graph_v4_digest_and_outer_procedure_round_trip() -> None:
    accepted = _accepted_procedure()
    definition = accepted.procedure.definition
    assert isinstance(definition, ProcedureDefinitionV4)
    assert definition.graph_format == 4
    assert definition.nodes[0].model_dump(mode="json").get("environment") is None
    assert definition.nodes[0].implementation_digest is not None
    assert accepted.procedure.definition_digest == (
        "sha256:9c3610739f920df547c48c272b9992a81f41026f486fe3ec14996569bac99571"
    )
    assert compute_procedure_node_digests_v4(definition)["direct"].subtree_digest == (
        "sha256:47eb4684da298a9972a3ed1bc3001cae6a5c457becdd9ff221290cc2ceb31887"
    )
    content = render_procedure(accepted.procedure)
    assert parse_procedure(content, path=accepted.path) == accepted.procedure


def test_graph_v4_direct_and_slot_implementation_conditions_are_closed() -> None:
    definition, _provider_pin, _interface_pin = _definition()
    payload = definition.model_dump(mode="json", by_alias=True)
    payload["nodes"][0].pop("implementation_digest")
    with pytest.raises(ValidationError, match="direct Provider bindings require"):
        ProcedureDefinitionV4.model_validate(payload)

    payload = definition.model_dump(mode="json", by_alias=True)
    payload["nodes"][1]["implementation_digest"] = digest("forbidden")
    with pytest.raises(ValidationError, match="slot Provider bindings prohibit"):
        ProcedureDefinitionV4.model_validate(payload)

    payload = definition.model_dump(mode="json", by_alias=True)
    payload["nodes"][0]["environment"] = pin(
        "environment",
        "EnvironmentManifest",
        "forbidden",
    ).model_dump(mode="json")
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ProcedureDefinitionV4.model_validate(payload)


def test_procedure_law_resolves_direct_pins_by_digest_and_never_by_order() -> None:
    procedure = _accepted_procedure().procedure
    provider = accepted_provider()
    interface = accepted_interface()

    accepted = evaluate_procedure_law(
        procedure,
        path=procedure_path(procedure.identity.name),
        predecessor=None,
        providers={provider.artifact_digest: provider},
        provider_interfaces={interface.artifact_digest: interface},
    )
    assert accepted.verdict == "accepted"

    refused = evaluate_procedure_law(
        procedure,
        path=procedure_path(procedure.identity.name),
        predecessor=None,
        providers={},
        provider_interfaces={interface.artifact_digest: interface},
    )
    assert refused.diagnostics[0].code == ("playbill.procedure.provider_runtime_manifest_required")


def test_repeat_body_provider_has_the_same_explicit_pin_block() -> None:
    _definition_v4, provider_pin, interface_pin = _definition()
    interface = accepted_interface()
    provider = accepted_provider()
    contract_in = pin("contract-in", "Contract", "repeat-input")
    contract_out = pin("contract-out", "Contract", "repeat-output")
    repeat = RepeatNodeV4(
        node_id="retry",
        max_attempts=2,
        body=(
            RepeatBodyNodeV4(
                node_id="invoke",
                operation="provider",
                provider=provider_pin,
                interface=interface_pin,
                interface_digest=interface.registration.interface_digest,
                implementation_digest=provider.provider.implementations[0].implementation_digest,
                contract_in=contract_in,
                contract_out=contract_out,
                spec={"value": 1},
                as_="body_result",
            ),
        ),
        until=GuardPredicateV1(
            left=PredicateOperandV1(kind="step", alias="body_result"),
            operator="eq",
            right=PredicateOperandV1(kind="literal", value=True),
        ),
        as_="result",
    )
    definition = ProcedureDefinitionV4(
        name="repeat-provider-v4",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(repeat,),
        returns="result",
        budget=ProcedureBudgetV3(
            wall_clock=CanonicalDurationV1(microseconds=2_000_000),
            max_provider_calls=2,
            max_capture_bytes=1024,
            max_items=10,
        ),
        hard_caps=ProcedureHardCapsV3(
            max_wall_clock=CanonicalDurationV1(microseconds=4_000_000),
            max_provider_calls=4,
            max_capture_bytes=2048,
            max_items=20,
            max_repeat_attempts=2,
        ),
        terminal_capability=1,
    )
    assert definition.nodes[0].body[0].implementation_digest is not None
    assert "environment" not in definition.nodes[0].body[0].model_dump(mode="json")


def test_line_v2_closure_round_trip_no_tie_break_and_v1_refusal() -> None:
    line = _line()
    procedure = _accepted_procedure()
    provider = accepted_provider()
    interface = accepted_interface()
    content = render_line_spec(line)
    assert parse_line_spec(content, path=line_spec_path(line.identity.name)) == line
    assert line_spec_digest(line).tagged.startswith("sha256:")

    result = evaluate_line_spec_law(
        line,
        path=line_spec_path(line.identity.name),
        procedure=procedure,
        interface_digests={
            provider.artifact_digest: interface.registration.interface_digest,
        },
        predecessor=None,
        providers={provider.artifact_digest: provider},
        provider_interfaces={interface.artifact_digest: interface},
    )
    assert result.verdict == "accepted"
    assert tuple(item.node_id for item in line.provider_implementation_closures) == ("slot",)

    historical_payload = line.model_dump(mode="json")
    historical_payload.pop("provider_implementation_closures")
    historical_payload["artifact_format"] = "playbill-line-v1"
    historical = LineSpecV1.model_validate(historical_payload)
    refused = evaluate_line_spec_law(
        historical,
        path=line_spec_path(historical.identity.name),
        procedure=procedure,
        interface_digests={
            provider.artifact_digest: interface.registration.interface_digest,
        },
        predecessor=None,
    )
    assert refused.diagnostics[0].code == ("playbill.line.provider_closure_successor_required")

    payload = line.model_dump(mode="json")
    extra = line.provider_implementation_closures[0].model_copy(
        update={"node_id": "z-slot", "slot_name": "z-provider"}
    )
    payload["provider_implementation_closures"] = [
        extra.model_dump(mode="json"),
        payload["provider_implementation_closures"][0],
    ]
    with pytest.raises(ValidationError, match="canonically node/slot sorted"):
        LineSpecV2.model_validate(payload)


def test_line_v2_wrong_implementation_refuses_without_order_selection() -> None:
    line = _line()
    procedure = _accepted_procedure()
    provider = accepted_provider()
    interface = accepted_interface()
    closures = list(line.provider_implementation_closures)
    closures[0] = closures[0].model_copy(update={"implementation_digest": digest("not-installed")})
    changed = line.model_copy(update={"provider_implementation_closures": tuple(closures)})
    result = evaluate_line_spec_law(
        changed,
        path=line_spec_path(changed.identity.name),
        procedure=procedure,
        interface_digests={
            provider.artifact_digest: interface.registration.interface_digest,
        },
        predecessor=None,
        providers={provider.artifact_digest: provider},
        provider_interfaces={interface.artifact_digest: interface},
    )
    assert result.diagnostics[0].code == ("playbill.line.provider_implementation_unavailable")
