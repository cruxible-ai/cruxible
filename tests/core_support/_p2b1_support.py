"""Focused provider and pin fixtures (operations replace interfaces, B2)."""

from __future__ import annotations

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import ArtifactDigest, typed_digest
from cruxible_client.contracts.providers import (
    AcceptedProvider,
    NamedProviderBuild,
    provider_digest,
    provider_path,
)


def digest(label: str) -> str:
    return typed_digest(ArtifactDigest, "playbill-p2b1-test-v1", {"label": label}).tagged


def pin(role: str, kind: str, name: str, *, value: str | None = None) -> ArtifactPin:
    return ArtifactPin(
        role=role,
        target=ArtifactIdentity(kind=kind, name=name),
        artifact_digest=value or digest(name),
    )


def provider_v2(*, build: str = "a") -> NamedProviderBuild:
    """One demo provider build serving ``compute: test.value@1 -> test.value@1``."""

    from tests.support.operation_world import operation_build

    return operation_build(build=build)


def accepted_provider() -> AcceptedProvider:
    provider = provider_v2()
    return AcceptedProvider(
        path=provider_path(provider.identity.name),
        provider=provider,
        artifact_digest=provider_digest(provider).tagged,
    )
