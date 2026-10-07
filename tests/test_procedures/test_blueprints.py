"""Blueprints: a Procedure skeleton with open Provider slots, instantiated once."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import cruxible_core.proposals.proposals as proposal_module
from cruxible_client.contracts.authoring.inputs import (
    BlueprintInstanceInput,
    lower_authoring_input,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.get_reads import GetBlueprintCard, GetProcedureCard, GetRequest
from cruxible_client.contracts.procedures.artifacts import (
    BlueprintArtifact,
    ProcedureArtifact,
    evaluate_procedure_law,
    parse_procedure,
    procedure_artifact_digest,
    procedure_path,
    render_procedure,
)
from cruxible_client.contracts.procedures.blueprints import (
    BlueprintInstantiationError,
    blueprint_digest,
    blueprint_path,
    instantiate_blueprint,
    render_blueprint,
)
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.models import ProcedureDefinition
from cruxible_client.contracts.provider_interfaces import render_provider_interface
from cruxible_client.contracts.providers import AcceptedProvider, render_provider
from cruxible_core.authoring import lowering
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.service.discovery.get import service_playbill_get
from tests.core_support._p2b1_support import (
    accepted_interface,
    accepted_provider,
    interface_fixture,
)
from tests.core_support._support import initialize_local
from tests.support.procedures import procedure_artifact
from tests.test_indexes.test_resolution_contracts import _accept_tree
from tests.test_integration.test_graph_v4_provider_closure import (  # noqa: F401 - fixture
    _definition,
    contracted_demo,
)

TIMESTAMP = "2026-08-24T16:00:00.000000Z"


def _concrete() -> ProcedureArtifact:
    definition, provider_pin, interface_pin = _definition()
    return procedure_artifact(
        definition, extra_pins=(provider_pin, interface_pin), activation_policy="drain"
    )


def _blueprint(name: str = "provider-blueprint") -> BlueprintArtifact:
    """The concrete Procedure with its one Provider position left as an open slot."""

    concrete = _concrete()
    raw = concrete.definition.model_dump(mode="json", by_alias=True)
    raw["name"] = name
    node = raw["nodes"][0]
    node["provider"] = {"tag": "playbill-procedure-pin-slot-ref-v1", "slot_name": "lookup"}
    node["implementation_digest"] = None
    raw["pin_slots"] = [
        {
            "slot_name": "lookup",
            "pin_role": "provider",
            "artifact_kind": "Provider",
            "interface_digest": node["interface_digest"],
        }
    ]
    definition = ProcedureDefinition.model_validate(raw)
    return BlueprintArtifact(
        identity={"kind": "Blueprint", "name": name},  # type: ignore[arg-type]
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition).tagged,
        pins=tuple(pin for pin in concrete.pins if pin.role != "provider"),
        owned_contracts=concrete.owned_contracts,
        activation_policy="drain",
    )


def accept_blueprint_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """An instance holding the contracted demo interface, its Provider and one Blueprint.

    The caller must have the demo interface contracted (``contracted_demo``).
    """

    instance, owner = initialize_local(tmp_path)
    fixture = interface_fixture()
    monkeypatch.setattr(
        proposal_module,
        "core_provider_bucket_conformance_fixtures",
        lambda: {fixture.fixture_id: fixture},
    )
    interface = accepted_interface()
    provider = accepted_provider()
    blueprint = _blueprint()
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[interface.path] = render_provider_interface(interface.registration)
    tree[provider.path] = render_provider(provider.provider)
    tree[blueprint_path(blueprint.identity.name)] = render_blueprint(blueprint)
    _accept_tree(instance, owner, tree, timestamp=TIMESTAMP, proposal_name="blueprint")

    return instance, owner


@pytest.mark.usefixtures("contracted_demo")
def test_instantiating_binds_each_slot_and_reproduces_the_exact_procedure() -> None:
    blueprint = _blueprint()
    provider = accepted_provider()
    procedure = instantiate_blueprint(
        blueprint,
        blueprint_artifact_digest=blueprint_digest(blueprint).tagged,
        name="provider-v4",
        providers={"lookup": provider},
    )

    concrete = _concrete()
    assert procedure.definition == concrete.definition
    assert procedure.pins == concrete.pins
    assert procedure.blueprint is not None
    assert procedure.blueprint.blueprint.target == blueprint.identity
    assert [item.slot_name for item in procedure.blueprint.bindings] == ["lookup"]
    assert procedure.blueprint.bindings[0].artifact_pin.artifact_digest == provider.artifact_digest
    law = evaluate_procedure_law(
        procedure,
        path=procedure_path("provider-v4"),
        predecessor=None,
        providers={provider.artifact_digest: provider},
        provider_interfaces={accepted_interface().artifact_digest: accepted_interface()},
    )
    assert law.verdict == "accepted", law.diagnostics
    # The provenance round-trips through the canonical wire.
    assert parse_procedure(render_procedure(procedure), path=procedure_path("provider-v4")) == (
        procedure
    )


@pytest.mark.usefixtures("contracted_demo")
def test_bindings_must_close_every_slot_with_a_fitting_provider() -> None:
    blueprint = _blueprint()
    digest = blueprint_digest(blueprint).tagged
    with pytest.raises(BlueprintInstantiationError) as missing:
        instantiate_blueprint(blueprint, blueprint_artifact_digest=digest, name="x", providers={})
    assert missing.value.code == "cruxible.blueprint.binding_set_mismatch"

    provider = accepted_provider()
    unfit = AcceptedProvider.model_construct(
        path=provider.path,
        provider=provider.provider.model_copy(update={"implementations": ()}),
        artifact_digest=provider.artifact_digest,
    )
    with pytest.raises(BlueprintInstantiationError) as mismatch:
        instantiate_blueprint(
            blueprint, blueprint_artifact_digest=digest, name="x", providers={"lookup": unfit}
        )
    assert mismatch.value.code == "cruxible.blueprint.provider_interface_mismatch"
    assert mismatch.value.slot == "lookup"


@pytest.mark.usefixtures("contracted_demo")
def test_open_slots_belong_only_to_blueprints() -> None:
    blueprint = _blueprint()
    as_procedure = ProcedureArtifact(
        identity={"kind": "Procedure", "name": "provider-blueprint"},  # type: ignore[arg-type]
        definition=blueprint.definition,
        definition_digest=blueprint.definition_digest,
        pins=blueprint.pins,
        owned_contracts=blueprint.owned_contracts,
        activation_policy="drain",
    )
    law = evaluate_procedure_law(
        as_procedure, path=procedure_path("provider-blueprint"), predecessor=None
    )
    assert law.verdict == "refused"
    assert law.diagnostics[0].code == "cruxible.procedure.open_slots"

    concrete = _concrete()
    with pytest.raises(ValueError, match="at least one open Provider slot"):
        BlueprintArtifact(
            identity={"kind": "Blueprint", "name": "provider-v4"},  # type: ignore[arg-type]
            definition=concrete.definition,
            definition_digest=concrete.definition_digest,
            pins=concrete.pins,
            owned_contracts=concrete.owned_contracts,
            activation_policy="drain",
        )


def test_blueprint_instance_input_lowers_to_a_procedure_definition_naming_it() -> None:
    payload = lower_authoring_input(
        BlueprintInstanceInput(
            kind="blueprint_instance",
            name="lookup-a",
            blueprint="provider-blueprint",
            bindings={"lookup": "demo-provider"},
        )
    )
    assert payload.definition == {  # type: ignore[union-attr]
        "bindings": {"lookup": "demo-provider"},
        "blueprint": "provider-blueprint",
        "name": "lookup-a",
    }


@pytest.mark.usefixtures("contracted_demo")
def test_blueprint_is_accepted_previewed_and_instantiated_through_authoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, owner = accept_blueprint_world(tmp_path, monkeypatch)
    interface = accepted_interface()
    provider = accepted_provider()
    blueprint = _blueprint()

    access = BodyAccessContext(principal_id="reader", can_read_body=True)
    card = service_playbill_get(
        instance,
        request=GetRequest(ref="Blueprint:provider-blueprint", evaluation_time=datetime.now(UTC)),
        access=access,
    ).card
    assert isinstance(card, GetBlueprintCard)
    assert [(slot.slot, slot.interface, slot.fits) for slot in card.slots] == [
        ("lookup", interface.registration.identity.qualified, ("demo-provider",))
    ]

    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    payload = lower_authoring_input(
        BlueprintInstanceInput(
            kind="blueprint_instance",
            name="lookup-a",
            blueprint="provider-blueprint",
            bindings={"lookup": "demo-provider"},
            activation_policy="drain",
        )
    )
    intent = coordinator.create(actor=actor, payload=payload, canonical_timestamp=TIMESTAMP).intent
    lowered = lowering.lower_authoring(instance, intent=intent, actor_id=actor.actor_id)
    procedure = parse_procedure(
        lowered.proposed_tree[procedure_path("lookup-a")], path=procedure_path("lookup-a")
    )
    assert procedure.blueprint is not None
    assert procedure.definition.open_slots == ()
    _accept_tree(
        instance, owner, lowered.proposed_tree, timestamp=TIMESTAMP, proposal_name="instance"
    )
    procedure_card = service_playbill_get(
        instance,
        request=GetRequest(ref="Procedure:lookup-a", evaluation_time=datetime.now(UTC)),
        access=access,
    ).card
    assert isinstance(procedure_card, GetProcedureCard)
    assert procedure_card.runnable == "direct"

    # A Procedure that names its Blueprint must be exactly that Blueprint instantiated.
    legit = instantiate_blueprint(
        blueprint,
        blueprint_artifact_digest=blueprint_digest(blueprint).tagged,
        name="lookup-b",
        providers={"lookup": provider},
    )
    tampered = ProcedureDefinition.model_validate(
        {**legit.definition.model_dump(mode="json", by_alias=True), "description": "tampered"}
    )
    forged = ProcedureArtifact(
        identity=legit.identity,
        definition=tampered,
        definition_digest=compute_procedure_definition_digest(tampered).tagged,
        pins=legit.pins,
        owned_contracts=legit.owned_contracts,
        activation_policy=legit.activation_policy,
        blueprint=legit.blueprint,
    )
    forged_tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    forged_tree[procedure_path("lookup-b")] = render_procedure(forged)
    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/forged",
            proposed_base_oid=instance.accepted_coordinate().git_oid,
        ),
        candidate_tree=forged_tree,
        timestamp=TIMESTAMP,
    )
    assert result.candidate is None
    assert [item.code for item in result.evaluation.diagnostics] == [
        "cruxible.procedure.blueprint_origin_mismatch"
    ]


@pytest.mark.usefixtures("contracted_demo")
def test_source_with_an_open_slot_compiles_to_a_blueprint_and_instantiates_by_recompiling() -> None:
    import textwrap

    from cruxible_client.contracts.artifacts import ArtifactPin
    from cruxible_client.contracts.procedures.artifacts import ProcedureOwnedContract
    from cruxible_client.contracts.procedures.models import iter_pin_bindings
    from cruxible_client.contracts.procedures.source_compiler import compile_source
    from cruxible_client.contracts.procedures.source_program import (
        ProcedureSource,
        SourceProviderBinding,
        SourceSlotBinding,
    )
    from cruxible_client.contracts.provider_contracts import read_provider_operation_contract
    from tests.test_procedures.test_source_compiler import INPUT, OUTPUT
    from tests.test_procedures.test_source_compiler import _budget as source_budget
    from tests.test_procedures.test_source_compiler import _hard_caps as source_hard_caps

    interface = accepted_interface()
    registration = interface.registration
    slot = SourceSlotBinding(
        interface=registration.identity.name,
        interface_version=interface.artifact_digest,
        interface_digest=registration.interface_digest,
        effect_class=registration.effect_class,
        operation=read_provider_operation_contract(registration.interface_bytes_hex),
    )
    program = ProcedureSource(
        text=textwrap.dedent("""
        def example(request, bindings):
            looked = call(bindings.lookup, input=bindings.lookup.input())
            return Output.value(value='done')
    """),
        filename="blueprint.py",
        function="example",
        contracts={"Output": OUTPUT},
        bindings={"lookup": slot},
    )
    compiled = compile_source(
        program,
        name="source-blueprint",
        input=INPUT,
        output=OUTPUT,
        budget=source_budget(),
        hard_caps=source_hard_caps(),
    )
    definition = compiled.definition
    assert definition.open_slots == ("lookup",)
    assert [slot.slot_name for slot in definition.pin_slots] == ["lookup"]
    owned = tuple(
        ProcedureOwnedContract(
            identity={"kind": "Contract", "name": item.name},  # type: ignore[arg-type]
            schema=item.schema_,
        )
        for item in compiled.contracts
    )
    pins = {
        (pin.role, pin.target.qualified, pin.artifact_digest): pin
        for pin in iter_pin_bindings(definition)
        if isinstance(pin, ArtifactPin)
    }
    blueprint = BlueprintArtifact(
        identity={"kind": "Blueprint", "name": "source-blueprint"},  # type: ignore[arg-type]
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition).tagged,
        pins=tuple(
            pins[key] for key in sorted(pins, key=lambda item: tuple(p.encode() for p in item))
        ),
        owned_contracts=owned,
        activation_policy="snapshot",
    )
    procedure = instantiate_blueprint(
        blueprint,
        blueprint_artifact_digest=blueprint_digest(blueprint).tagged,
        name="source-instance",
        providers={"lookup": accepted_provider()},
    )
    assert procedure.definition.open_slots == ()
    assert procedure.definition.pin_slots == ()
    assert procedure.definition.source is not None
    assert isinstance(procedure.definition.source.bindings["lookup"], SourceProviderBinding)
    call_node = procedure.definition.nodes[0]
    assert getattr(call_node, "implementation_digest") is not None


def test_procedure_source_declares_slots_and_builds_a_blueprint_until_each_is_bound() -> None:
    from types import SimpleNamespace

    from cruxible_client.authoring.inputs import CarriedContractInput
    from cruxible_client.authoring.procedures import ProviderBinding
    from cruxible_client.authoring.source import ProcedureSource, procedure
    from cruxible_client.contracts.procedures.contract_schema import PropertySchema
    from cruxible_client.contracts.procedures.source_requests import (
        SourceProviderSelection,
        SourceSlotSelection,
    )
    from tests.test_procedures.test_procedure_execution import _budget, _hard_caps

    Request = CarriedContractInput(
        name="lookup.request", fields={"key": PropertySchema(type="string")}
    )
    Result = CarriedContractInput(
        name="lookup.result", fields={"done": PropertySchema(type="bool")}
    )

    def declare(**slots: str) -> ProcedureSource:
        @procedure(
            name="lookup",
            input=Request,
            output=Result,
            budget=_budget(providers=1),
            hard_caps=_hard_caps(providers=1),
            slots=slots,
        )
        def lookup(request, bindings):
            call(bindings.fetch, input=bindings.fetch.input())  # noqa: F821
            return Result.value(done=True)

        return lookup

    with pytest.raises(ValueError, match="never uses"):
        declare(fetch="demo.interface", other="demo.interface")
    source = declare(fetch="ProviderInterface:demo.interface")
    assert source.slots == {"fetch": "demo.interface"}
    assert source.is_blueprint and source.open_slots == ("fetch",)
    assert source._at(SimpleNamespace()).bindings == {
        "fetch": SourceSlotSelection(interface="demo.interface")
    }
    with pytest.raises(TypeError, match="Bindings require"):
        source.bind(fetch=SimpleNamespace())  # type: ignore[arg-type]
    coordinate = SimpleNamespace()
    binding = ProviderBinding(
        provider="demo-provider",
        interface="demo.interface",
        interface_digest="sha256:" + "1" * 64,
        implementation_digest="sha256:" + "2" * 64,
    ).model_copy(update={"coordinate": coordinate})
    bound = source.bind(fetch=binding)
    assert not bound.is_blueprint
    world = SimpleNamespace(_playbill=SimpleNamespace(_assert_coordinate=lambda value: None))
    assert bound._at(world).bindings == {  # type: ignore[arg-type]
        "fetch": SourceProviderSelection(provider="demo-provider", interface="demo.interface")
    }


# --- instances after their Blueprint moves on (S3 review a F-001, F-007) ------


def _instance_lookup_a(tmp_path, monkeypatch, *, retire: bool = False):  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.authoring.inputs import (
        BlueprintInstanceInput,
        lower_authoring_input,
    )

    instance, owner = accept_blueprint_world(tmp_path, monkeypatch)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    payload = lower_authoring_input(
        BlueprintInstanceInput(
            kind="blueprint_instance",
            name="lookup-a",
            blueprint="provider-blueprint",
            bindings={"lookup": "demo-provider"},
            activation_policy="drain",
        )
    )
    intent = coordinator.create(actor=actor, payload=payload, canonical_timestamp=TIMESTAMP).intent
    lowered = lowering.lower_authoring(instance, intent=intent, actor_id=actor.actor_id)
    _accept_tree(
        instance, owner, lowered.proposed_tree, timestamp=TIMESTAMP, proposal_name="instance"
    )
    return instance, owner, coordinator, actor


def _succeed_blueprint(instance, owner, *, retire: bool = False):  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.procedures.blueprints import (
        blueprint_digest,
        blueprint_path,
        parse_blueprint,
        render_blueprint,
    )

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = blueprint_path("provider-blueprint")
    current = parse_blueprint(tree[path], path=path)
    definition = current.definition.model_copy(update={"description": "v2"})
    successor = current.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest(definition).tagged,
            "lifecycle": ArtifactLifecycle(
                state="retired" if retire else "live",
                predecessor_digest=blueprint_digest(current).tagged,
            ),
        }
    )
    tree[path] = render_blueprint(type(current).model_validate(successor.model_dump()))
    _accept_tree(instance, owner, tree, timestamp=TIMESTAMP, proposal_name="blueprint-v2")


def _submit_tree(instance, tree, name):  # type: ignore[no-untyped-def]
    return instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/{name}",
            proposed_base_oid=instance.accepted_coordinate().git_oid,
        ),
        candidate_tree=tree,
        timestamp=TIMESTAMP,
    )


@pytest.mark.usefixtures("contracted_demo")
@pytest.mark.parametrize("blueprint_retired", [False, True])
def test_an_instance_retires_by_successor_bytes_whatever_became_of_its_blueprint(
    tmp_path, monkeypatch, blueprint_retired
):
    from cruxible_client.contracts.artifacts import ArtifactLifecycle

    instance, owner, _coordinator, _actor = _instance_lookup_a(tmp_path, monkeypatch)
    _succeed_blueprint(instance, owner, retire=blueprint_retired)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = procedure_path("lookup-a")
    accepted = parse_procedure(tree[path], path=path)
    tree[path] = render_procedure(
        accepted.model_copy(
            update={
                "lifecycle": ArtifactLifecycle(
                    state="retired", predecessor_digest=procedure_artifact_digest(accepted).tagged
                )
            }
        )
    )

    result = _submit_tree(instance, tree, "retire-instance")

    assert result.candidate is not None, result.evaluation.diagnostics


@pytest.mark.usefixtures("contracted_demo")
def test_blueprint_instance_retire_keeps_the_definition_that_ran(tmp_path, monkeypatch):
    from cruxible_client.contracts.authoring.inputs import (
        BlueprintInstanceInput,
        lower_authoring_input,
    )

    instance, owner, coordinator, actor = _instance_lookup_a(tmp_path, monkeypatch)
    path = procedure_path("lookup-a")
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    before = parse_procedure(tree[path], path=path)
    _succeed_blueprint(instance, owner, retire=True)
    payload = lower_authoring_input(
        BlueprintInstanceInput(
            kind="blueprint_instance",
            name="lookup-a",
            blueprint="provider-blueprint",
            bindings={"lookup": "demo-provider"},
            activation_policy="drain",
            retire=True,
        )
    )
    intent = coordinator.create(actor=actor, payload=payload, canonical_timestamp=TIMESTAMP).intent

    lowered = lowering.lower_authoring(instance, intent=intent, actor_id=actor.actor_id)

    retired = parse_procedure(lowered.proposed_tree[path], path=path)
    assert retired.lifecycle.state == "retired"
    assert retired.definition == before.definition and retired.blueprint == before.blueprint


@pytest.mark.usefixtures("contracted_demo")
def test_an_instance_changes_only_its_lifecycle_under_the_same_origin(tmp_path, monkeypatch):
    instance, _owner, _coordinator, _actor = _instance_lookup_a(tmp_path, monkeypatch)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = procedure_path("lookup-a")
    accepted = parse_procedure(tree[path], path=path)
    definition = accepted.definition.model_copy(update={"description": "hand edited"})
    tree[path] = render_procedure(
        accepted.model_copy(
            update={
                "definition": definition,
                "definition_digest": compute_procedure_definition_digest(definition).tagged,
                "lifecycle": accepted.lifecycle.model_copy(
                    update={"predecessor_digest": procedure_artifact_digest(accepted).tagged}
                ),
            }
        )
    )

    result = _submit_tree(instance, tree, "edit-instance")

    assert result.candidate is None
    assert "cruxible.procedure.blueprint_origin_mismatch" in {
        item.code for item in result.evaluation.diagnostics
    }


@pytest.mark.usefixtures("contracted_demo")
def test_admission_refuses_a_new_instance_of_a_retired_blueprint(tmp_path, monkeypatch):
    """Lowering refuses it; a raw tree or kit proposal is held to the same rule."""

    from cruxible_client.contracts.procedures.blueprints import (
        blueprint_digest,
        blueprint_path,
        instantiate_blueprint,
        parse_blueprint,
    )
    from cruxible_client.contracts.providers import parse_provider, provider_digest, provider_path

    instance, owner, _coordinator, _actor = _instance_lookup_a(tmp_path, monkeypatch)
    _succeed_blueprint(instance, owner, retire=True)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    retired = parse_blueprint(
        tree[blueprint_path("provider-blueprint")], path=blueprint_path("provider-blueprint")
    )
    provider = parse_provider(
        tree[provider_path("demo-provider")], path=provider_path("demo-provider")
    )
    # A raw tree instantiating the retired version, as a kit proposal could.
    copy = instantiate_blueprint(
        retired,
        blueprint_artifact_digest=blueprint_digest(retired).tagged,
        name="lookup-b",
        providers={
            "lookup": AcceptedProvider(
                path=provider_path("demo-provider"),
                provider=provider,
                artifact_digest=provider_digest(provider).tagged,
            )
        },
        activation_policy="drain",
    )
    tree[procedure_path("lookup-b")] = render_procedure(copy)

    result = _submit_tree(instance, tree, "copy-instance")

    assert result.candidate is None
    assert "cruxible.procedure.blueprint_origin_mismatch" in {
        item.code for item in result.evaluation.diagnostics
    }


@pytest.mark.usefixtures("contracted_demo")
def test_one_provider_fills_two_slots_of_different_interfaces() -> None:
    """S3 review a F-002: slot interfaces are verified per slot, not per Provider."""

    from cruxible_client.contracts.procedures.blueprints import (
        blueprint_digest,
        instantiate_blueprint,
    )
    from tests.core_support._p2b1_support import accepted_provider

    other_interface, other_implementation = "sha256:" + "7" * 64, "sha256:" + "8" * 64
    concrete = _concrete()
    raw = concrete.definition.model_dump(mode="json", by_alias=True)
    raw["name"] = "two-slot"
    first = raw["nodes"][0]
    first["provider"] = {"tag": "playbill-procedure-pin-slot-ref-v1", "slot_name": "a"}
    first["implementation_digest"] = None
    second = {
        **first,
        "node_id": "second",
        "as": "result2",
        "provider": {"tag": "playbill-procedure-pin-slot-ref-v1", "slot_name": "b"},
        "interface_digest": other_interface,
    }
    second.pop("next", None)
    first["next"] = "second"
    raw["nodes"] = [first, second]
    raw["returns"] = "result2"
    raw["pin_slots"] = [
        {
            "slot_name": "a",
            "pin_role": "provider",
            "artifact_kind": "Provider",
            "interface_digest": first["interface_digest"],
        },
        {
            "slot_name": "b",
            "pin_role": "provider",
            "artifact_kind": "Provider",
            "interface_digest": other_interface,
        },
    ]
    definition = ProcedureDefinition.model_validate(raw)
    blueprint = BlueprintArtifact(
        identity={"kind": "Blueprint", "name": "two-slot"},  # type: ignore[arg-type]
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition).tagged,
        pins=tuple(pin for pin in concrete.pins if pin.role != "provider"),
        owned_contracts=concrete.owned_contracts,
        activation_policy="drain",
    )
    provider = accepted_provider()
    implementation = provider.provider.implementations[0]
    serves_both = AcceptedProvider.model_construct(
        path=provider.path,
        provider=provider.provider.model_copy(
            update={
                "implementations": (
                    implementation,
                    implementation.model_copy(
                        update={
                            "interface_digest": other_interface,
                            "implementation_digest": other_implementation,
                            "interface_id": implementation.interface_id + ".b",
                        }
                    ),
                )
            }
        ),
        artifact_digest=provider.artifact_digest,
    )

    instance_of = instantiate_blueprint(
        blueprint,
        blueprint_artifact_digest=blueprint_digest(blueprint).tagged,
        name="two-slot-instance",
        providers={"a": serves_both, "b": serves_both},
    )

    assert instance_of.blueprint is not None
    assert {
        (binding.slot_name, binding.interface_digest) for binding in instance_of.blueprint.bindings
    } == {("a", first["interface_digest"]), ("b", other_interface)}
    nodes = {node.node_id: node for node in instance_of.definition.nodes}
    assert nodes["second"].implementation_digest == other_implementation  # type: ignore[union-attr]
