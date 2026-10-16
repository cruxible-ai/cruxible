"""Current build artifacts from historical package-runtime test fixtures."""

from cruxible_client.contracts.captures import CaptureContract
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


def capture_provider_build(contract: CaptureContract) -> ProviderBuild:
    """A current package build declaring the PC-C fixture's CaptureContract."""
    from tests.core_support._p2b1_support import provider_v2
    from tests.core_support._pc_c_support import provider

    runtime = as_build(provider_v2())
    metadata = provider(contract)
    return runtime.model_copy(
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
