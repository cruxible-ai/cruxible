"""Exact Provider pins on a graph-v6 Call node, and the real proposal path over them.

Graph-v4 Provider slots and Line Provider closure are gone: a Procedure pins
every Provider exactly. The helpers here (``_accepted_procedure``, ``_line``)
are shared fixtures for "one accepted Procedure that calls the demo Provider".
"""

from __future__ import annotations

import json

import pytest

import cruxible_core.proposals.proposals as proposal_module
import tests.core_support._p2b1_support as p2b1_support
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    evaluate_procedure_law,
    procedure_path,
    render_procedure,
)
from cruxible_client.contracts.procedures.line_specs import (
    LineSpec,
    line_spec_path,
    render_line_spec,
)
from cruxible_client.contracts.procedures.models import (
    CallNode,
    ProcedureBudget,
    ProcedureDefinition,
    ProcedureHardCaps,
)
from cruxible_client.contracts.provider_interfaces import (
    ProviderInterfaceRegistrationV1,
    provider_interface_definition_digest,
    render_provider_interface,
)
from cruxible_client.contracts.providers import render_provider
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from tests.core_support._p2b1_support import (
    accepted_interface,
    accepted_provider,
    digest,
    interface_fixture,
    pin,
)
from tests.core_support._support import initialize_local
from tests.support.procedures import PERMISSIVE_CONTRACT, accepted_procedure

_DEMO_INTERFACE_REGISTRATION = p2b1_support.interface_registration


def _definition() -> tuple[ProcedureDefinition, ArtifactPin, ArtifactPin]:
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
    definition = ProcedureDefinition(
        name="provider-v4",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            CallNode(
                node_id="direct",
                provider=provider_pin,
                interface=interface_pin,
                interface_digest=interface.registration.interface_digest,
                implementation_digest=implementation_digest,
                contract_in=contract_in,
                contract_out=contract_out,
                input={"value": 1},
                as_="result",
            ),
        ),
        returns="result",
        budget=ProcedureBudget(
            wall_clock=CanonicalDuration(microseconds=2_000_000),
            max_provider_calls=2,
            max_capture_bytes=1024,
            max_items=10,
        ),
        hard_caps=ProcedureHardCaps(
            max_wall_clock=CanonicalDuration(microseconds=4_000_000),
            max_provider_calls=4,
            max_capture_bytes=2048,
            max_items=20,
            max_repeat_attempts=2,
        ),
        terminal_capability=1,
    )
    return definition, provider_pin, interface_pin


def _accepted_procedure() -> AcceptedProcedure:
    """One accepted Procedure whose single Call pins the demo Provider exactly."""

    definition, provider_pin, interface_pin = _definition()
    return accepted_procedure(
        definition,
        extra_pins=(provider_pin, interface_pin),
        activation_policy="drain",
    )


def _line() -> LineSpec:
    procedure = _accepted_procedure()
    provider = accepted_provider()
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
    return LineSpec(
        identity=ArtifactIdentity(kind="Line", name="provider-v4-line"),
        occurrence_epoch=1,
        procedure=procedure_pin,
        parameters={},
        max_authority="observe",
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
                    item.role.encode("utf-8"),
                    item.target.qualified.encode("utf-8"),
                    item.artifact_digest.encode("ascii"),
                ),
            )
        ),
    )


def _contracted_registration() -> ProviderInterfaceRegistrationV1:
    """The demo interface, declaring the operation contracts a Call is checked against."""

    base = _DEMO_INTERFACE_REGISTRATION()
    definition = json.loads(bytes.fromhex(base.interface_bytes_hex))
    schema = PERMISSIVE_CONTRACT.model_dump(mode="json")
    definition["contracts"] = {"input": schema, "output": schema}
    definition["effect_class"] = base.effect_class
    interface_hex = canonical_bytes(definition).hex()
    return ProviderInterfaceRegistrationV1.model_validate(
        {
            **base.model_dump(mode="json"),
            "interface_bytes_hex": interface_hex,
            "interface_digest": provider_interface_definition_digest(interface_hex),
        }
    )


@pytest.fixture
def contracted_demo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every demo fixture (interface, Provider, Procedure) speaks the contracted interface."""

    monkeypatch.setattr(p2b1_support, "interface_registration", _contracted_registration)


def test_procedure_law_resolves_direct_pins_by_digest_and_never_by_order(
    contracted_demo: None,
) -> None:
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
    assert accepted.verdict == "accepted", accepted.diagnostics

    refused = evaluate_procedure_law(
        procedure,
        path=procedure_path(procedure.identity.name),
        predecessor=None,
        providers={},
        provider_interfaces={interface.artifact_digest: interface},
    )
    assert refused.diagnostics[0].code == ("cruxible.procedure.provider_runtime_manifest_required")

    definition, provider_pin, interface_pin = _definition()
    (call,) = definition.nodes
    assert isinstance(call, CallNode)
    uninstalled = accepted_procedure(
        definition.model_copy(
            update={"nodes": (call.model_copy(update={"implementation_digest": digest("absent")}),)}
        ),
        extra_pins=(provider_pin, interface_pin),
        activation_policy="drain",
    ).procedure
    unavailable = evaluate_procedure_law(
        uninstalled,
        path=procedure_path(uninstalled.identity.name),
        predecessor=None,
        providers={provider.artifact_digest: provider},
        provider_interfaces={interface.artifact_digest: interface},
    )
    assert unavailable.diagnostics[0].code == (
        "cruxible.procedure.provider_implementation_unavailable"
    )


def test_real_proposal_path_closes_interface_provider_procedure_and_line(
    tmp_path,
    monkeypatch,
    contracted_demo: None,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    interface = accepted_interface()
    provider = accepted_provider()
    procedure = _accepted_procedure()
    line = _line()
    fixture = interface_fixture()
    monkeypatch.setattr(
        proposal_module,
        "core_provider_bucket_conformance_fixtures",
        lambda: {fixture.fixture_id: fixture},
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[interface.path] = render_provider_interface(interface.registration)
    tree[provider.path] = render_provider(provider.provider)
    tree[procedure.path] = render_procedure(procedure.procedure)
    tree[line_spec_path(line.identity.name)] = render_line_spec(line)

    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/provider-line-e2e",
            proposed_base_oid=instance.accepted_coordinate().git_oid,
        ),
        candidate_tree=tree,
        timestamp="2026-08-24T16:00:00.000000Z",
    )

    assert result.evaluation.verdict == "candidate", result.evaluation.diagnostics
    assert result.candidate is not None
    assert {member.artifact_kind for member in result.candidate.members} == {
        "line",
        "procedure",
        "provider",
        "provider-interface",
    }
