"""Current build artifacts from historical package-runtime test fixtures."""

from cruxible_client.contracts.providers import (
    ProviderBuild,
    ProviderRuntimeArtifactPayload,
    ProviderV2,
    provider_build_key,
    provider_expected_implementation_records,
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
