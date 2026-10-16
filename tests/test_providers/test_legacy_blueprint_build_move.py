from tests.test_procedures.test_blueprints import _blueprint
from tests.test_integration.test_graph_v4_provider_closure import contracted_demo
from tests.core_support._p2b1_support import accepted_interface, accepted_provider
from tests.support.provider_builds import as_build
from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.providers import (
    AcceptedProvider,
    render_provider,
    provider_path,
    provider_digest,
)
from cruxible_client.contracts.provider_interfaces import (
    provider_interface_contract,
    provider_interface_path,
    provider_interface_digest,
    render_provider_interface,
    AcceptedProviderInterfaceRegistration,
)
from cruxible_client.contracts.procedures.blueprints import (
    instantiate_blueprint,
    blueprint_digest,
    render_blueprint,
    blueprint_path,
)
from cruxible_client.contracts.procedures.artifacts import render_procedure, parse_procedure
from cruxible_core.providers.build_moves import _reinstantiated, build_move


def legacy_blueprint_move():
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
    bp = _blueprint()
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


def test_legacy_blueprint_move_binds_the_registration_of_the_new_build(contracted_demo):
    old, new, bp, proc, path, tree, move = legacy_blueprint_move()
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
    LocalProviderExecutionDriver._bound_result(
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
        SimpleNamespace(deployment_digest="sha256:" + "f" * 64),
        entrypoint="demo.runtime:Provider",
        endpoints=(),
        materialization_digest="sha256:" + "e" * 64,
        environment_manifest_digest="sha256:" + "d" * 64,
    )
    assert parse_procedure(render_procedure(moved), path=path) == moved
