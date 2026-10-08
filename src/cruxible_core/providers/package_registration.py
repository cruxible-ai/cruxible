"""Lower verified package metadata into ordinary governed Provider definitions.

This module imports no provider code and makes no execution-readiness claim.
The installer supplies the measured local artifact pins separately; package
metadata cannot choose the installed wheel, environment or execution grants.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.provider_contracts import read_provider_operation_contract
from cruxible_client.contracts.provider_interfaces import (
    ProviderBucketConformanceFixture,
    ProviderBucketConformanceFixtureProof,
    ProviderBucketVocabulary,
    ProviderClassifierCode,
    ProviderInterfaceRegistration,
    ProviderInterfaceRegistrationV1,
    provider_bucket_fixture_digest,
    provider_bucket_fixture_set_digest,
    provider_bucket_vocabulary_digest,
    provider_interface_digest,
    provider_package_classifier_digest,
)
from cruxible_client.contracts.providers import (
    Provider,
    ProviderLocalDistributionPin,
    ProviderLocalEnvBackendPin,
    ProviderRuntimeArtifactPayload,
    ProviderRuntimeManifest,
    provider_expected_implementation_records,
    provider_manifest_digest,
)
from cruxible_core.providers.web_fetch import core_owned_interface_registration


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PackageInterfaceExportV1(_Strict):
    interface_id: str
    interface_digest: str
    definition: dict[str, Any]
    vocabulary: ProviderBucketVocabulary
    classifier_identity: str
    classifier_version: int
    classifier_code: ProviderClassifierCode
    fixtures: tuple[ProviderBucketConformanceFixture, ...]


class PackageRuntimeRequirementV1(_Strict):
    interface_id: str
    kind: Literal["python_extra", "runtime_resource"]
    name: str
    description: str
    probe_entrypoint: str | None = None


class PackageRegistrationDocumentV1(_Strict):
    """Data exported by the provider runtime's verified registration reader."""

    schema_version: Literal[1]
    manifest: ProviderRuntimeManifest
    interfaces: tuple[PackageInterfaceExportV1, ...]
    runtime_requirements: tuple[PackageRuntimeRequirementV1, ...] = ()
    governed_definitions: tuple[dict[str, Any], ...] = ()

    @model_validator(mode="after")
    def _coverage(self) -> PackageRegistrationDocumentV1:
        ids = [item.interface_id for item in self.interfaces]
        implementations = {item.interface_id: item for item in self.manifest.implementations}
        if len(set(ids)) != len(ids) or set(ids) != set(implementations):
            raise ValueError("package must export each implemented interface exactly once")
        for requirement in self.runtime_requirements:
            implementation = implementations.get(requirement.interface_id)
            if implementation is None or (
                requirement.kind == "python_extra"
                and requirement.name not in implementation.requires_extras
            ):
                raise ValueError("runtime requirement does not match a declared implementation")
        return self

    def interface_registrations(self) -> tuple[ProviderInterfaceRegistrationV1, ...]:
        """The registration installing this package proposes for each exported interface.

        A definition core owns (``web.fetch``) registers as core's registration of
        it, whatever vocabulary, classifier and fixtures the package ships: the
        contract, not the implementation, owns the ProviderInterface, so every
        implementation of it binds the same artifact and core classifies its
        runs. Any other definition registers as the package exports it.
        """

        result: list[ProviderInterfaceRegistrationV1] = []
        for exported in sorted(self.interfaces, key=lambda item: item.interface_id.encode()):
            interface_bytes = self._checked_definition(exported)
            core = core_owned_interface_registration(exported.interface_digest)
            if core is None:
                result.append(self._package_registration(exported, interface_bytes))
                continue
            if core.interface_bytes_hex != interface_bytes:
                raise ValueError("package definition differs from core's under its digest")
            result.append(core)
        return tuple(result)

    def package_registration(self, interface_id: str) -> ProviderInterfaceRegistration:
        """The registration this package itself exports for one interface, classifier
        and fixtures included, even for a definition core owns: what an earlier
        install of it registered, so it can host that registration's classifier."""

        exported = next(item for item in self.interfaces if item.interface_id == interface_id)
        return self._package_registration(exported, self._checked_definition(exported))

    def _checked_definition(self, exported: PackageInterfaceExportV1) -> str:
        """The exported definition's canonical bytes, once it agrees with its implementation."""

        implementation = next(
            item
            for item in self.manifest.implementations
            if item.interface_id == exported.interface_id
        )
        definition = exported.definition
        if (
            definition.get("interface_id") != exported.interface_id
            or implementation.interface_digest != exported.interface_digest
            or implementation.side_effects
            != (definition.get("effect_class") == "external_mutation")
        ):
            raise ValueError("package definition and implementation disagree")
        interface_bytes = canonical_bytes(definition).hex()
        read_provider_operation_contract(interface_bytes)
        if set(implementation.bucket_conformance) != set(implementation.declared_input_buckets):
            raise ValueError("each declared selector must have a fixture")
        return interface_bytes

    def _package_registration(
        self, exported: PackageInterfaceExportV1, interface_bytes: str
    ) -> ProviderInterfaceRegistration:
        implementation = next(
            item
            for item in self.manifest.implementations
            if item.interface_id == exported.interface_id
        )
        fixtures = {fixture.fixture_id: fixture for fixture in exported.fixtures}
        if len(fixtures) != len(exported.fixtures):
            raise ValueError("package fixtures must have unique identities")
        proofs = []
        for selector, fixture_id in sorted(implementation.bucket_conformance.items()):
            if fixture_id not in fixtures:
                raise ValueError("conformance proof names a missing fixture")
            fixture = fixtures[fixture_id]
            proofs.append(
                ProviderBucketConformanceFixtureProof(
                    selector=selector,
                    fixture_id=fixture_id,
                    fixture_digest=provider_bucket_fixture_digest(fixture),
                    measured_bucket_id=fixture.measured_bucket_id,
                )
            )
        fixture_set_digest = provider_bucket_fixture_set_digest(tuple(proofs))
        vocabulary = exported.vocabulary.model_dump(mode="json")
        vocabulary["status"] = "accepted"
        vocabulary_bytes = canonical_bytes(vocabulary).hex()
        effect_class = exported.definition["effect_class"]
        return ProviderInterfaceRegistration(
            identity=ArtifactIdentity(kind="ProviderInterface", name=exported.interface_id),
            interface_id=exported.interface_id,
            interface_bytes_hex=interface_bytes,
            interface_digest_domain="cruxible.interface.stub.v1",
            interface_digest=exported.interface_digest,
            vocabulary_bytes_hex=vocabulary_bytes,
            vocabulary_digest=provider_bucket_vocabulary_digest(vocabulary_bytes),
            classifier_identity=exported.classifier_identity,
            classifier_version=exported.classifier_version,
            classifier_code=exported.classifier_code,
            classifier_digest=provider_package_classifier_digest(
                classifier_identity=exported.classifier_identity,
                classifier_version=exported.classifier_version,
                conformance_fixture_set_digest=fixture_set_digest,
                code=exported.classifier_code,
            ),
            conformance_fixture_set_digest=fixture_set_digest,
            conformance_proofs=tuple(proofs),
            conformance_fixtures=tuple(
                sorted(exported.fixtures, key=lambda item: item.fixture_id.encode())
            ),
            effect_class="none" if effect_class == "pure" else effect_class,
        )

    def provider_definition(
        self,
        *,
        distribution: ProviderLocalDistributionPin,
        local_env: ProviderLocalEnvBackendPin,
        control_domain: str,
        interfaces: tuple[ProviderInterfaceRegistrationV1, ...],
    ) -> Provider:
        """The Provider pinning ``interfaces``: the registration each exported
        interface binds here (the package's, core's, or one already live)."""

        expected = {item.interface_id: item.interface_digest for item in self.interfaces}
        if (
            len(interfaces) != len(expected)
            or {item.interface_id: item.interface_digest for item in interfaces} != expected
        ):
            raise ValueError("Provider must pin every exported interface exactly once")
        payload = ProviderRuntimeArtifactPayload(
            provider_id=self.manifest.provider_id,
            status="accepted",
            manifest=self.manifest,
            manifest_digest=provider_manifest_digest(self.manifest),
            distribution=distribution,
            local_env=local_env,
        )
        return Provider(
            identity=ArtifactIdentity(kind="Provider", name=payload.provider_id),
            control_domain=control_domain,
            signing_keys=(),
            capture_contract_digests=(),
            runtime_artifact=payload,
            implementations=provider_expected_implementation_records(payload),
            pins=tuple(
                ArtifactPin(
                    role="provider-interface",
                    target=interface.identity,
                    artifact_digest=provider_interface_digest(interface).tagged,
                )
                for interface in sorted(
                    interfaces, key=lambda item: item.identity.qualified.encode()
                )
            ),
        )
