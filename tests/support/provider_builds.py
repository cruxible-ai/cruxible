"""Current build artifacts from historical package-runtime test fixtures."""

from cruxible_client.contracts.providers import ProviderBuild, ProviderV2, provider_build_key


def as_build(provider: ProviderV2) -> ProviderBuild:
    payload = provider.model_dump(mode="json")
    payload["artifact_format"] = "cruxible-provider-v4"
    payload["runtime_artifact"]["schema_version"] = 2
    payload["identity"]["name"] = (
        provider.provider_name + "/" + provider_build_key(provider.runtime_artifact).value
    )
    return ProviderBuild.model_validate(payload)
