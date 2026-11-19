"""Revision-36 authoring conversions, restricted to declared reference fields."""

from __future__ import annotations

from cruxible_client.contracts.providers import (
    NamedProviderBuild,
    NamedProviderIdentity,
    ProviderBuild,
    ProviderV1,
)


def name_provider(provider: ProviderBuild) -> NamedProviderBuild:
    if isinstance(provider, NamedProviderBuild):
        return provider
    return NamedProviderBuild.model_validate(
        {
            **provider.model_dump(
                mode="json",
                by_alias=True,
                exclude={"pins", "artifact_format", "capture_contract_digests"},
            ),
            "capture_contracts": _capture_names(provider),
        }
    )


def _capture_names(provider: ProviderV1) -> list[dict[str, object]]:
    return [
        {"target": pin.target.model_dump(mode="json")}
        for pin in provider.pins
        if pin.role == "capture-contract"
        and pin.artifact_digest in provider.capture_contract_digests
    ]


def name_signing_provider(provider: ProviderV1) -> NamedProviderIdentity:
    """Migrate signing metadata on edit; package runtimes use name_provider."""
    if isinstance(provider, NamedProviderIdentity):
        return provider
    return NamedProviderIdentity.model_validate(
        {
            **provider.model_dump(
                mode="json", exclude={"pins", "artifact_format", "capture_contract_digests"}
            ),
            "capture_contracts": _capture_names(provider),
        }
    )
