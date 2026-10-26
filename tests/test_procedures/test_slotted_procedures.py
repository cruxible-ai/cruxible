"""Provider slots belong to Procedures; Lines and direct admission select builds."""

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactNameRef
from cruxible_client.contracts.authoring.inputs import lower_authoring_input
from cruxible_client.contracts.named_references import name_provider
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    NamedProcedureArtifact,
    procedure_artifact_digest,
    render_procedure,
)
from cruxible_client.contracts.procedures.authored import ProcedureSpec, lower_procedure_spec
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.named_models import NamedProcedureDefinition
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistration,
    ProviderInterfaceDefinition,
    provider_interface_contract,
    provider_interface_digest,
    provider_interface_path,
)
from cruxible_client.contracts.providers import (
    AcceptedProvider,
    provider_digest,
    provider_path,
)
from cruxible_core.errors import RequestRefusedError
from cruxible_core.procedures.reference_binding import (
    bind_procedure_references,
    replay_procedure_references,
)
from tests.core_support._p2b1_support import provider_v2
from tests.support.provider_builds import as_build


def slotted():
    interface = provider_interface_contract(
        interface=ProviderInterfaceDefinition(
            interface_id="test.compute",
            version=1,
            effect_class="pure",
            contracts={
                "input": {"fields": {"value": {"type": "int"}}},
                "output": {"fields": {"value": {"type": "int"}}},
            },
        )
    )
    registration = AcceptedProviderInterfaceRegistration(
        path=provider_interface_path(interface.identity.name),
        registration=interface,
        artifact_digest=provider_interface_digest(interface).tagged,
    )
    # The fixture's manifest is authored against the exact typed operation.
    provider = name_provider(as_build(provider_v2(registration=interface)))
    accepted_provider = AcceptedProvider(
        path=provider_path(provider.identity.name),
        provider=provider,
        artifact_digest=provider_digest(provider).tagged,
    )
    spec = ProcedureSpec.model_validate(
        {
            "procedure": "test.slotted",
            "input": {"value": "integer"},
            "output": {"value": "integer"},
            "slots": {"compute": "test.compute@1"},
            "steps": [
                {
                    "call": "result",
                    "interface": "test.compute@1",
                    "provider": "compute",
                    "input": {"value": "$input.value"},
                }
            ],
            "returns": "result",
            "budget": {"provider_calls": 1},
        }
    )
    payload = lower_authoring_input(lower_procedure_spec(spec))
    definition = NamedProcedureDefinition.model_validate(payload.definition)
    artifact = NamedProcedureArtifact(
        identity=ArtifactIdentity(kind="Procedure", name=definition.name),
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition).tagged,
        owned_contracts=payload.owned_contracts,
        activation_policy="snapshot",
    )
    accepted = AcceptedProcedure(
        path="procedures/test.slotted.json",
        procedure=artifact,
        artifact_digest=procedure_artifact_digest(artifact).tagged,
    )
    return spec, accepted, accepted_provider, registration


def test_slot_binding_selects_one_build_and_preserves_authored_bytes():
    _, accepted, provider, interface = slotted()
    sources = {
        provider.provider.identity: (provider.provider, provider.artifact_digest),
        interface.registration.identity: (interface.registration, interface.artifact_digest),
    }
    original = render_procedure(accepted.procedure)
    bound = bind_procedure_references(
        accepted, lookup=sources.get, slots=lambda identity: (sources[provider.provider.identity],)
    )
    assert bound.procedure.definition.nodes[0].provider.target == provider.provider.identity
    assert not bound.procedure.definition.open_slots
    assert render_procedure(bound.authored) == original
    replayed = replay_procedure_references(
        accepted, pins=bound.procedure.pins, lookup_exact=lambda identity, digest: sources[identity]
    )
    assert replayed.procedure == bound.procedure


@pytest.mark.parametrize("count,code", [(0, "slot_binding_missing"), (2, "slot_binding_ambiguous")])
def test_slot_binding_cardinality_has_typed_repair(count, code):
    _, accepted, provider, interface = slotted()
    with pytest.raises(RequestRefusedError) as caught:
        bind_procedure_references(
            accepted,
            lookup=lambda identity: (interface.registration, interface.artifact_digest),
            slots=lambda identity: ((provider.provider, provider.artifact_digest),) * count,
        )
    assert caught.value.error_code == "cruxible.procedure." + code
    assert "compute" in str(caught.value)
    assert caught.value.repair.hand_edit.target == "bindings"
    if count:
        assert provider.provider.identity.qualified in str(caught.value)


def test_explicit_slot_binding_overrides_ambiguous_fit():
    _, accepted, provider, interface = slotted()
    sources = {
        provider.provider.identity: (provider.provider, provider.artifact_digest),
        interface.registration.identity: (interface.registration, interface.artifact_digest),
    }

    def no_search(identity):
        raise AssertionError("Explicit bindings never enumerate candidates")

    bound = bind_procedure_references(
        accepted,
        lookup=sources.get,
        slots=no_search,
        bindings={"compute": provider.provider.identity},
    )
    assert bound.procedure.definition.nodes[0].provider.target == provider.provider.identity


def test_line_bindings_are_law_checked_and_indexed():
    from cruxible_client.contracts.dependencies import dependency_pins
    from cruxible_client.contracts.procedures.line_specs import (
        NamedLineSpec,
        evaluate_line_spec_law,
        parse_line_spec,
        render_line_spec,
    )

    _, accepted, provider, interface = slotted()
    line = NamedLineSpec(
        identity=ArtifactIdentity(kind="Line", name="test.slotted"),
        occurrence_epoch=1,
        procedure=ArtifactNameRef(target=accepted.procedure.identity),
        bindings={"compute": ArtifactNameRef(target=provider.provider.identity)},
        parameters={"value": 1},
        max_authority="observe",
        budgets={
            "max_wall_clock_microseconds": 1000000,
            "max_provider_calls": 1,
            "max_capture_bytes": 0,
            "max_items": 0,
        },
        epsilon={"$decimal": "0.1"},
    )
    kwargs = dict(
        path="lines/test.slotted.json",
        procedure=accepted,
        predecessor=None,
        providers={provider.provider.identity.qualified: provider},
        provider_interfaces={interface.registration.identity.qualified: interface},
    )
    assert evaluate_line_spec_law(line, **kwargs).verdict == "accepted"
    assert parse_line_spec(render_line_spec(line), path=kwargs["path"]) == line
    assert any(pin.target == provider.provider.identity for pin in dependency_pins(line))
    bad = line.model_copy(update={"bindings": {"typo": next(iter(line.bindings.values()))}})
    bad_result = evaluate_line_spec_law(bad, **kwargs)
    assert bad_result.verdict == "refused"
    assert bad_result.diagnostics[0].code == "cruxible.procedure.slot_undeclared"
    assert "compute" in bad_result.diagnostics[0].message
    assert "typo" in bad_result.diagnostics[0].message
    assert evaluate_line_spec_law(line, **{**kwargs, "providers": {}}).verdict == "refused"


def test_authored_slots_admit_on_36_render_and_run_with_explicit_override(tmp_path):
    from cruxible_client.contracts.authoring.inputs import ProcedureInput
    from cruxible_client.contracts.cas_contracts import BodyAccessContext
    from cruxible_client.contracts.get_reads import GetRequest
    from cruxible_client.contracts.provider_execution import ProviderEgressObservation
    from cruxible_client.contracts.provider_interfaces import render_provider_interface
    from cruxible_client.contracts.providers import render_provider
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.authoring.lowering import lower_authoring
    from cruxible_core.proposals.proposals import AuthenticatedActor
    from cruxible_core.providers.provider_local_runtime import ProviderDriverOutcomeV1
    from cruxible_core.providers.provider_runtime_contract import ProviderRuntimeResultEnvelopeV1
    from cruxible_core.service.discovery.get import service_playbill_get
    from cruxible_core.service.procedures.procedure_runs import (
        ProcedureRunRequest,
        service_run_playbill_procedure,
    )
    from tests.core_support._identity_support import accept_tree
    from tests.core_support._support import initialize_local
    from tests.test_procedures.test_procedure_execution import _actor
    from tests.test_procedures.test_procedure_source_runs import _Operator, _WorkspaceInvoker

    spec, accepted, provider, interface = slotted()
    instance, _ = initialize_local(tmp_path)
    accept_tree(
        instance,
        {
            interface.path: render_provider_interface(interface.registration),
        },
        name="build",
    )
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    intent = coordinator.create(
        actor=AuthenticatedActor(actor_id="owner"),
        payload=lower_authoring_input(ProcedureInput(kind="procedure", spec=spec)),
        canonical_timestamp="2026-10-25T04:00:00.000000Z",
    ).intent
    lowered = lower_authoring(instance, intent=intent, actor_id="owner")
    assert lowered.proposed_tree[accepted.path] == render_procedure(accepted.procedure)
    accept_tree(instance, {accepted.path: lowered.proposed_tree[accepted.path]}, name="slotted")
    card = service_playbill_get(
        instance,
        request=GetRequest(ref="Procedure:test.slotted"),
        access=BodyAccessContext(principal_id="reader", can_read_body=True),
    ).card
    assert card.authored.slots == spec.slots

    class Invoker(_WorkspaceInvoker):
        def invoke_provider(self, *, occurrence, context, invocation_id, bound, deadline):
            return ProviderDriverOutcomeV1(
                envelope=ProviderRuntimeResultEnvelopeV1(
                    protocol_version="1.0", run_id=context.run_id, status="ok", output={"value": 42}
                ),
                stderr="",
                duration_seconds=0.001,
                egress=ProviderEgressObservation(
                    observer_backend="test", observer_grade="attribution"
                ),
                verified_binding=bound.binding,
            )

    with pytest.raises(RequestRefusedError) as undeclared:
        service_run_playbill_procedure(
            instance,
            name=spec.procedure,
            request=ProcedureRunRequest(input={"value": 1}, bindings={"typo": "not-installed"}),
            actor_context=_actor(),
        )
    assert undeclared.value.error_code == "cruxible.procedure.slot_undeclared"
    assert "declared slots: ['compute']" in str(undeclared.value)
    assert "typo" in str(undeclared.value)
    with pytest.raises(RequestRefusedError) as missing:
        service_run_playbill_procedure(
            instance,
            name=spec.procedure,
            request=ProcedureRunRequest(input={"value": 1}),
            actor_context=_actor(),
        )
    assert missing.value.error_code == "cruxible.procedure.slot_binding_missing"
    accept_tree(instance, {provider.path: render_provider(provider.provider)}, name="first-build")
    for bindings in ({}, {"compute": provider.provider.identity.name}):
        result = service_run_playbill_procedure(
            instance,
            name=spec.procedure,
            request=ProcedureRunRequest(input={"value": 1}, bindings=bindings),
            actor_context=_actor(),
            provider_runtime_operator=_Operator(Invoker()),
        )
        assert result.status == "succeeded", (result.terminal, result.outcomes)
        assert result.result == {"value": 42}
        assert any(
            pin.target == provider.provider.identity
            and pin.artifact_digest == provider.artifact_digest
            for pin in result.receipt.validated_pins
        )

    from cruxible_client.contracts.providers import provider_expected_implementation_records
    from tests.core_support._p2b1_support import provider_v2

    template = provider_v2(registration=interface.registration)
    runtime = template.runtime_artifact.model_copy(
        update={
            "distribution": template.runtime_artifact.distribution.model_copy(
                update={"sha256": "sha256:" + "f" * 64}
            )
        }
    )
    other = name_provider(
        as_build(
            template.model_copy(
                update={
                    "runtime_artifact": runtime,
                    "implementations": provider_expected_implementation_records(runtime),
                }
            )
        )
    )
    accept_tree(
        instance, {provider_path(other.identity.name): render_provider(other)}, name="second-build"
    )
    with pytest.raises(RequestRefusedError) as ambiguous:
        service_run_playbill_procedure(
            instance,
            name=spec.procedure,
            request=ProcedureRunRequest(input={"value": 1}),
            actor_context=_actor(),
        )
    assert ambiguous.value.error_code == "cruxible.procedure.slot_binding_ambiguous"
    assert other.identity.name in str(ambiguous.value)
    chosen = service_run_playbill_procedure(
        instance,
        name=spec.procedure,
        request=ProcedureRunRequest(
            input={"value": 1}, bindings={"compute": provider.provider.identity.name}
        ),
        actor_context=_actor(),
        provider_runtime_operator=_Operator(Invoker()),
    )
    assert chosen.status == "succeeded", chosen.terminal


def test_explicit_binding_must_fit_its_slot_interface():
    from tests.core_support._p2b1_support import accepted_provider

    _, accepted, _, interface = slotted()
    unfit = accepted_provider()
    sources = {
        unfit.provider.identity: (unfit.provider, unfit.artifact_digest),
        interface.registration.identity: (interface.registration, interface.artifact_digest),
    }
    with pytest.raises(RequestRefusedError) as refused:
        bind_procedure_references(
            accepted,
            lookup=sources.get,
            slots=lambda _: (),
            bindings={"compute": unfit.provider.identity},
        )
    assert refused.value.error_code == "cruxible.procedure.slot_binding_missing"
    assert "does not fit slot 'compute'" in str(refused.value)
