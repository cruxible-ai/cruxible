"""Current build artifacts from historical package-runtime test fixtures."""

from cruxible_client.contracts.captures import CaptureContract, NamedCaptureContract
from cruxible_client.contracts.providers import (
    AcceptedProvider,
    ProviderBuild,
    ProviderRuntimeArtifactPayload,
    ProviderV2,
    provider_build_key,
    provider_digest,
    provider_expected_implementation_records,
    provider_path,
)


def as_build(provider: ProviderV2) -> ProviderBuild:
    payload = provider.model_dump(mode="json")
    payload["artifact_format"] = "cruxible-provider-v4"
    payload["runtime_artifact"]["schema_version"] = 2
    payload["identity"]["name"] = (
        provider.provider_name + "/" + provider_build_key(provider.runtime_artifact).value
    )
    runtime = ProviderRuntimeArtifactPayload.model_validate(payload["runtime_artifact"])
    payload["implementations"] = [
        item.model_dump(mode="json") for item in provider_expected_implementation_records(runtime)
    ]
    return ProviderBuild.model_validate(payload)


def accepted_build(provider: ProviderV2) -> AcceptedProvider:
    build = as_build(provider)
    return AcceptedProvider(
        path=provider_path(build.identity.name),
        provider=build,
        artifact_digest=provider_digest(build).tagged,
    )


def capture_provider_interface():
    """Typed interface for current signing/capture metadata integration worlds."""
    from cruxible_client.contracts.provider_interfaces import (
        AcceptedProviderInterfaceRegistration,
        ProviderInterfaceDefinition,
        provider_interface_contract,
        provider_interface_digest,
        provider_interface_path,
    )

    registration = provider_interface_contract(
        interface=ProviderInterfaceDefinition(
            interface_id="demo.interface",
            version=1,
            effect_class="external_read",
            contracts={
                "input": {"fields": {"size": {"type": "int"}}},
                "output": {"fields": {"size": {"type": "int"}}},
            },
        )
    )
    return AcceptedProviderInterfaceRegistration(
        path=provider_interface_path(registration.identity.name),
        registration=registration,
        artifact_digest=provider_interface_digest(registration).tagged,
    )


def capture_provider_build(contract: CaptureContract, *, named: bool = True) -> ProviderBuild:
    """A current package build declaring the PC-C fixture's CaptureContract."""
    from tests.core_support._p2b1_support import provider_v2
    from tests.core_support._pc_c_support import provider

    template = provider_v2()
    if isinstance(contract, NamedCaptureContract):
        from cruxible_client.contracts.artifacts import ArtifactPin
        from cruxible_client.contracts.provider_interfaces import interface_definition_digest
        from cruxible_client.contracts.providers import provider_manifest_digest

        interface = capture_provider_interface()
        manifest = template.runtime_artifact.manifest.model_copy(
            update={
                "implementations": (
                    template.runtime_artifact.manifest.implementations[0].model_copy(
                        update={
                            "interface_digest": interface_definition_digest(interface.registration),
                        }
                    ),
                )
            }
        )
        artifact = template.runtime_artifact.model_copy(
            update={
                "manifest": manifest,
                "manifest_digest": provider_manifest_digest(manifest),
            }
        )
        template = template.model_copy(
            update={
                "runtime_artifact": artifact,
                "implementations": provider_expected_implementation_records(artifact),
                "pins": (
                    ArtifactPin(
                        role="provider-interface",
                        target=interface.registration.identity,
                        artifact_digest=interface.artifact_digest,
                    ),
                ),
            }
        )
    runtime = as_build(template)
    metadata = provider(contract)
    build = runtime.model_copy(
        update={
            "control_domain": metadata.control_domain,
            "signing_keys": metadata.signing_keys,
            "capture_contract_digests": metadata.capture_contract_digests,
            "pins": tuple(
                sorted(
                    (*runtime.pins, *metadata.pins),
                    key=lambda pin: (pin.role, pin.target.qualified),
                )
            ),
        }
    )

    if named and isinstance(contract, NamedCaptureContract):
        from cruxible_client.contracts.named_references import name_provider

        return name_provider(build)
    return build


def legacy_interface_contract(**kwargs):
    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.provider_interfaces import (
        ProviderInterfaceContractV3,
        provider_interface_contract_key,
    )

    key = provider_interface_contract_key(
        **{k: v for k, v in kwargs.items() if k != "interface_bytes_hex"}
    )
    return ProviderInterfaceContractV3(
        identity=ArtifactIdentity(
            kind="ProviderInterface", name=f"{kwargs['interface_id']}/{key.value}"
        ),
        **kwargs,
    )
