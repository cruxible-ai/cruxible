import pytest

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.procedures.artifacts import parse_procedure, render_procedure
from cruxible_client.contracts.procedures.blueprints import (
    blueprint_digest,
    blueprint_path,
    instantiate_blueprint,
    render_blueprint,
)
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistration,
    provider_interface_contract,
    provider_interface_digest,
    provider_interface_path,
    render_provider_interface,
)
from cruxible_client.contracts.providers import (
    AcceptedProvider,
    provider_digest,
    provider_path,
    render_provider,
)
from cruxible_core.providers.build_moves import _reinstantiated, build_move
from tests.core_support._p2b1_support import accepted_interface, accepted_provider
from tests.support.provider_builds import as_build
from tests.test_integration.test_graph_v4_provider_closure import contracted_demo as contracted_demo
from tests.test_procedures.test_blueprints import _blueprint


def legacy_blueprint_move(source=False):
    old = accepted_provider()
    registration = accepted_interface().registration
    contract = provider_interface_contract(
        interface_id=registration.interface_id,
        interface_bytes_hex=registration.interface_bytes_hex,
        interface_digest_domain=registration.interface_digest_domain,
        interface_digest=registration.interface_digest,
        effect_class=registration.effect_class,
    )
    new = as_build(old.provider).model_copy(
        update={
            "pins": (
                ArtifactPin(
                    role="provider-interface",
                    target=contract.identity,
                    artifact_digest=provider_interface_digest(contract).tagged,
                ),
            )
        }
    )
    bp = _source_blueprint() if source else _blueprint()
    proc = instantiate_blueprint(
        bp,
        blueprint_artifact_digest=blueprint_digest(bp).tagged,
        name="user.legacy",
        providers={"lookup": old},
    )
    path = "procedures/user.legacy.json"
    tree = {
        old.path: render_provider(old.provider),
        accepted_interface().path: render_provider_interface(registration),
        provider_path(new.identity.name): render_provider(new),
        provider_interface_path(contract.identity.name): render_provider_interface(contract),
        blueprint_path(bp.identity.name): render_blueprint(bp),
        path: render_procedure(proc),
    }
    move = build_move(tree, old.provider.identity.name, new.identity.name)
    return old, new, bp, proc, path, tree, move


@pytest.mark.usefixtures("contracted_demo")
@pytest.mark.parametrize("source", [False, True])
def test_legacy_blueprint_move_binds_the_registration_of_the_new_build(source, tmp_path, request):
    old, new, bp, proc, path, tree, move = legacy_blueprint_move(source)
    moved = parse_procedure(
        _reinstantiated(tree, {}, path, {old.provider.identity.qualified: move}), path=path
    )
    assert moved.definition.nodes[0].provider.target == new.identity
    # Runtime requires the selected build to pin the occurrence's exact registration.
    assert moved.definition.nodes[0].interface in new.pins, (
        moved.definition.nodes[0].interface,
        new.pins,
    )

    from types import SimpleNamespace

    from cruxible_core.providers.provider_local_runtime import LocalProviderExecutionDriver

    contract = new.pins[0]
    registration = provider_interface_contract(
        interface_id=accepted_interface().registration.interface_id,
        interface_bytes_hex=accepted_interface().registration.interface_bytes_hex,
        interface_digest_domain=accepted_interface().registration.interface_digest_domain,
        interface_digest=accepted_interface().registration.interface_digest,
        effect_class=accepted_interface().registration.effect_class,
    )
    from tests.test_providers.test_provider_local_driver import _fake_interpreter

    interpreter = _fake_interpreter(tmp_path / "provider-python")
    bound = LocalProviderExecutionDriver._bound_result(
        AcceptedProvider(
            path=provider_path(new.identity.name),
            provider=new,
            artifact_digest=provider_digest(new).tagged,
        ),
        AcceptedProviderInterfaceRegistration(
            path=provider_interface_path(contract.target.name),
            registration=registration,
            artifact_digest=contract.artifact_digest,
        ),
        moved.definition.nodes[0].implementation_digest,
        SimpleNamespace(deployment_digest="sha256:" + "f" * 64, interpreter_path=interpreter),
        entrypoint="demo.runtime:Provider",
        endpoints=("https://example.test",),
        materialization_digest="sha256:" + "e" * 64,
        environment_manifest_digest="sha256:" + "d" * 64,
    )
    import shutil

    from cruxible_client.contracts.provider_execution import ProviderSecretResolutionPlan
    from cruxible_core.providers.provider_local_runtime import ProviderSecretResolverRegistry
    from cruxible_core.providers.provider_process_leases import ProviderProcessLeaseStore
    from cruxible_core.providers.provider_runtime_contract import (
        ProviderRuntimeBudgetsV1,
        ProviderRuntimeRunContextV1,
    )
    from tests.support.invocation_ids import unique_invocation_id
    from tests.support.short_temporary_root import short_temporary_directory

    control = short_temporary_directory("moved-provider-")
    request.addfinalizer(lambda: shutil.rmtree(control, ignore_errors=True))
    context = ProviderRuntimeRunContextV1(
        protocol_version="1.0",
        run_id="RUN-moved-legacy",
        interface_id=registration.interface_id,
        interface_digest=registration.interface_digest,
        implementation_digest=moved.definition.nodes[0].implementation_digest,
        entrypoint="demo.runtime:Provider",
        input={"value": "after-move"},
        input_bucket="small",
        budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=2, output_bytes=16384),
    )
    outcome = LocalProviderExecutionDriver().invoke(
        bound,
        context,
        secret_plan=ProviderSecretResolutionPlan(),
        secret_resolvers=ProviderSecretResolverRegistry(()),
        invocation_id=unique_invocation_id(),
        process_leases=ProviderProcessLeaseStore(tmp_path / "leases", control_root=control),
    )
    assert outcome.envelope.output == {"echo": "after-move"}
    assert parse_procedure(render_procedure(moved), path=path) == moved
    if source:
        binding = moved.definition.source.bindings["lookup"]
        assert binding.interface == contract.target.name
        assert binding.interface_version == contract.artifact_digest


def _source_blueprint():
    import textwrap

    from cruxible_client.contracts.artifacts import ArtifactPin
    from cruxible_client.contracts.procedures.artifacts import (
        BlueprintArtifact,
        ProcedureOwnedContract,
    )
    from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
    from cruxible_client.contracts.procedures.models import iter_pin_bindings
    from cruxible_client.contracts.procedures.source_compiler import compile_source
    from cruxible_client.contracts.procedures.source_program import (
        ProcedureSourceProgram,
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
    program = ProcedureSourceProgram(
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
    return blueprint


@pytest.mark.usefixtures("contracted_demo")
def test_unmoved_blueprint_slot_keeps_its_exact_registration():
    from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
    from cruxible_client.contracts.procedures.models import ProcedureDefinition

    old, new, bp, proc, path, tree, move = legacy_blueprint_move()
    runtime = old.provider.runtime_artifact
    held = as_build(
        old.provider.model_copy(
            update={
                "runtime_artifact": runtime.model_copy(
                    update={
                        "distribution": runtime.distribution.model_copy(
                            update={"sha256": "sha256:" + "9" * 64}
                        )
                    }
                )
            }
        )
    )
    held = AcceptedProvider(
        path=provider_path(held.identity.name),
        provider=held,
        artifact_digest=provider_digest(held).tagged,
    )
    raw = bp.definition.model_dump(mode="json", by_alias=True)
    first = raw["nodes"][0]
    second = {
        **first,
        "node_id": "held",
        "as": "held",
        "provider": {"tag": "playbill-procedure-pin-slot-ref-v1", "slot_name": "held"},
    }
    first["next"] = "held"
    raw["nodes"].insert(1, second)
    raw["returns"] = "held"
    raw["pin_slots"].append({**raw["pin_slots"][0], "slot_name": "held"})
    raw["pin_slots"].sort(key=lambda item: item["slot_name"])
    definition = ProcedureDefinition.model_validate(raw)
    bp = bp.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest(definition).tagged,
        }
    )
    proc = instantiate_blueprint(
        bp,
        blueprint_artifact_digest=blueprint_digest(bp).tagged,
        name=proc.identity.name,
        providers={"lookup": old, "held": held},
    )
    tree.update(
        {
            path: render_procedure(proc),
            held.path: render_provider(held.provider),
            blueprint_path(bp.identity.name): render_blueprint(bp),
        }
    )
    moved = parse_procedure(
        _reinstantiated(tree, {}, path, {old.provider.identity.qualified: move}), path=path
    )
    occurrence = next(node for node in moved.definition.nodes if node.node_id == "held")
    assert occurrence.provider.target == held.provider.identity
    assert occurrence.interface == proc.definition.nodes[1].interface
    assert moved.definition.nodes[0].interface != occurrence.interface


@pytest.mark.usefixtures("contracted_demo")
@pytest.mark.parametrize("source", [False, True])
def test_blueprint_build_move_preserves_a_named_acquisition_override(source):
    old, _new, _bp, procedure, path, tree, move = legacy_blueprint_move(source)
    procedure = procedure.model_copy(update={"acquisition_policy": "custom-ages"})
    tree[path] = render_procedure(procedure)
    moved = parse_procedure(
        _reinstantiated(tree, {}, path, {old.provider.identity.qualified: move}), path=path
    )
    assert moved.acquisition_policy == "custom-ages"
    assert all(pin.target.kind != "SourceAcquisitionPolicy" for pin in moved.pins)
