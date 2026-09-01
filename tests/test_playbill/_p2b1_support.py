"""Focused P2-B1 Provider/interface/graph-v4 fixtures."""

from __future__ import annotations

from datetime import UTC, datetime

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import ArtifactDigest, canonical_bytes, typed_digest
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistrationV1,
    ProviderBucketClassV1,
    ProviderBucketConformanceFixtureProofV1,
    ProviderBucketConformanceFixtureV1,
    ProviderBucketDimensionV1,
    ProviderBucketVocabularyV1,
    ProviderInterfaceRegistrationV1,
    provider_bucket_classifier_digest,
    provider_bucket_fixture_digest,
    provider_bucket_fixture_set_digest,
    provider_bucket_vocabulary_digest,
    provider_interface_definition_digest,
    provider_interface_digest,
    provider_interface_path,
)
from cruxible_client.contracts.providers import (
    AcceptedProviderV1,
    ProviderDistributionPinV1,
    ProviderDistributionRefV1,
    ProviderImplementationManifestV1,
    ProviderLocalEnvBackendPinV1,
    ProviderRuntimeArtifactPayloadV1,
    ProviderRuntimeManifestV1,
    ProviderSigningKeyV1,
    ProviderV2,
    provider_digest,
    provider_expected_implementation_records,
    provider_manifest_digest,
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


def interface_fixture() -> ProviderBucketConformanceFixtureV1:
    return ProviderBucketConformanceFixtureV1(
        fixture_id="demo.small",
        canonical_input={"size": 3},
        measured_bucket_id="size=small",
    )


def interface_registration() -> ProviderInterfaceRegistrationV1:
    interface_id = "demo.interface"
    interface_bytes = canonical_bytes({"name": interface_id, "version": 1})
    vocabulary = ProviderBucketVocabularyV1(
        interface_id=interface_id,
        status="accepted",
        dimensions=(
            ProviderBucketDimensionV1(
                name="size",
                description="input size",
                classes=(
                    ProviderBucketClassV1(id="small", description="small input"),
                    ProviderBucketClassV1(id="large", description="large input"),
                ),
            ),
        ),
    )
    vocabulary_bytes = canonical_bytes(vocabulary.model_dump(mode="json"))
    fixture = interface_fixture()
    proof = ProviderBucketConformanceFixtureProofV1(
        selector="size=*",
        fixture_id=fixture.fixture_id,
        fixture_digest=provider_bucket_fixture_digest(fixture),
        measured_bucket_id=fixture.measured_bucket_id,
    )
    proofs = (proof,)
    fixture_set_digest = provider_bucket_fixture_set_digest(proofs)
    return ProviderInterfaceRegistrationV1(
        identity=ArtifactIdentity(kind="ProviderInterface", name=interface_id),
        interface_id=interface_id,
        interface_bytes_hex=interface_bytes.hex(),
        interface_digest=provider_interface_definition_digest(interface_bytes.hex()),
        vocabulary_bytes_hex=vocabulary_bytes.hex(),
        vocabulary_digest=provider_bucket_vocabulary_digest(vocabulary_bytes.hex()),
        classifier_identity="core.demo.size",
        classifier_version=1,
        classifier_digest=provider_bucket_classifier_digest(
            classifier_identity="core.demo.size",
            classifier_version=1,
            conformance_fixture_set_digest=fixture_set_digest,
        ),
        conformance_fixture_set_digest=fixture_set_digest,
        conformance_proofs=proofs,
        effect_class="external_read",
    )


def accepted_interface() -> AcceptedProviderInterfaceRegistrationV1:
    registration = interface_registration()
    return AcceptedProviderInterfaceRegistrationV1(
        path=provider_interface_path(registration.interface_id),
        registration=registration,
        artifact_digest=provider_interface_digest(registration).tagged,
    )


def provider_v2() -> ProviderV2:
    registration = interface_registration()
    implementation = ProviderImplementationManifestV1(
        interface_id=registration.interface_id,
        interface_digest=registration.interface_digest,
        entrypoint="demo.runtime:Provider",
        backends=("local_env",),
        declared_input_buckets=("size=*",),
        bucket_conformance={"size=*": "demo.small"},
        declared_endpoints=("https://example.test",),
        requires_extras=("engine",),
        deterministic=True,
        side_effects=False,
    )
    manifest = ProviderRuntimeManifestV1(
        provider_id="demo-provider",
        distribution=ProviderDistributionRefV1(name="demo-provider", version="1.0.0"),
        supported_protocol_majors=(1,),
        implementations=(implementation,),
    )
    payload = ProviderRuntimeArtifactPayloadV1(
        provider_id="demo-provider",
        status="accepted",
        manifest=manifest,
        manifest_digest=provider_manifest_digest(manifest),
        distribution=ProviderDistributionPinV1(
            name="demo-provider",
            version="1.0.0",
            filename="demo_provider-1.0.0-py3-none-any.whl",
            sha256=f"sha256:{'a' * 64}",
            index_url="https://packages.example.test/simple",
            url="https://packages.example.test/demo-provider.whl",
        ),
        local_env=ProviderLocalEnvBackendPinV1(
            lock_sha256=f"sha256:{'b' * 64}",
            materialization_digests={
                "linux-cp311": f"sha256:{'c' * 64}",
                "linux-cp311+engine": f"sha256:{'d' * 64}",
                "linux-cp311+other": f"sha256:{'e' * 64}",
            },
        ),
    )
    return ProviderV2(
        identity=ArtifactIdentity(kind="Provider", name="demo-provider"),
        control_domain="demo-provider",
        signing_keys=(
            ProviderSigningKeyV1(
                key_id="primary",
                public_key="11" * 32,
                valid_from=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        ),
        capture_contract_digests=(),
        runtime_artifact=payload,
        implementations=provider_expected_implementation_records(payload),
    )


def accepted_provider() -> AcceptedProviderV1:
    provider = provider_v2()
    return AcceptedProviderV1(
        path=provider_path(provider.identity.name),
        provider=provider,
        artifact_digest=provider_digest(provider).tagged,
    )
