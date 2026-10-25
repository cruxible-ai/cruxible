"""Lower verified package metadata into ordinary governed Provider definitions.

This module imports no provider code and makes no execution-readiness claim.
The installer supplies the measured local artifact pins separately; package
metadata cannot choose the installed wheel, environment or execution grants.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.provider_interfaces import (
    LegacyProviderEffectClass,
    ProviderBucketConformanceFixture,
    ProviderClassifierCode,
    ProviderInterfaceContractV3,
    ProviderInterfaceDefinition,
    ProviderInterfaceDigestDomain,
    ProviderInterfaceRegistrationAny,
    interface_definition_bytes,
    interface_definition_digest,
    provider_external_interface_definition_digest,
    provider_interface_contract,
    provider_interface_contract_key,
    provider_interface_digest,
    provider_vocabulary_document,
)
from cruxible_client.contracts.providers import (
    ProviderBuild,
    ProviderLocalDistributionPin,
    ProviderLocalEnvBackendPin,
    ProviderRuntimeArtifactPayload,
    ProviderRuntimeManifest,
    provider_build_key,
    provider_expected_implementation_records,
    provider_manifest_digest,
)
from cruxible_client.contracts.repairs import HandEditInstruction, HandEditRepair
from cruxible_core.errors import RequestRefusedError


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PackageInterfaceExportV1(_Strict):
    """One exported interface. Its vocabulary, classifier and fixtures are the
    provider-runtime 0.2.x package format's bucket data: read so the package
    parses, and nothing else -- no registration carries them, so a package that
    changes only them (or its code) registers byte-identical interfaces."""

    interface_id: str
    interface_digest: str
    definition: dict[str, Any]
    vocabulary: dict[str, Any]
    classifier_identity: str
    classifier_version: int
    classifier_code: ProviderClassifierCode
    fixtures: tuple[ProviderBucketConformanceFixture, ...]

    @field_validator("vocabulary")
    @classmethod
    def _inert_vocabulary(cls, value: dict[str, Any]) -> dict[str, Any]:
        return provider_vocabulary_document(value)


class PackageRuntimeRequirementV1(_Strict):
    interface_id: str
    kind: Literal["python_extra", "runtime_resource"]
    name: str
    description: str
    probe_entrypoint: str | None = None


class PackageRegistrationDocumentV1(_Strict):
    """A provider package's registration, as ``package_metadata`` reads it from the wheel."""

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

    def interface_registrations(
        self, *, named: bool = True
    ) -> tuple[ProviderInterfaceRegistrationAny, ...]:
        """The contract registration each exported interface binds.

        A registration holds one typed definition under its name and version.
        Every package implementing those exact bytes binds the same registration;
        package code and inert bucket metadata never enter that definition.
        """

        result: list[ProviderInterfaceRegistrationAny] = []
        for exported in sorted(self.interfaces, key=lambda item: item.interface_id.encode()):
            try:
                definition = ProviderInterfaceDefinition.model_validate(exported.definition)
                self._check_definition(exported)
                registration = provider_interface_contract(interface=definition)
                if (
                    provider_external_interface_definition_digest(
                        interface_definition_bytes(registration).hex(),
                        domain="cruxible.interface.stub.v1",
                    )
                    != exported.interface_digest
                ):
                    raise ValueError(
                        "stored interface JSON differs from the wheel's declared digest"
                    )
            except ValueError as exc:
                raise RequestRefusedError(
                    "cruxible.provider_interface.definition_invalid",
                    f"Provider interface {exported.interface_id!r} cannot register as v4: {exc}",
                    repair=HandEditRepair(
                        hand_edit=HandEditInstruction(
                            target=(
                                f"the {exported.interface_id!r} definition and wheel registration"
                            ),
                            required_change=(
                                "Declare only the v4 definition fields and rebuild the wheel "
                                "so its interface_digest covers them."
                            ),
                        )
                    ),
                ) from exc
            if named:
                result.append(registration)
            else:
                effect: LegacyProviderEffectClass = (
                    "none" if registration.effect_class == "pure" else registration.effect_class
                )
                domain: ProviderInterfaceDigestDomain = "cruxible.interface.stub.v1"
                key = provider_interface_contract_key(
                    interface_id=registration.interface_id,
                    interface_digest_domain=domain,
                    interface_digest=exported.interface_digest,
                    effect_class=effect,
                )
                result.append(
                    ProviderInterfaceContractV3(
                        identity=ArtifactIdentity(
                            kind="ProviderInterface",
                            name=f"{registration.interface_id}/{key.value}",
                        ),
                        interface_id=registration.interface_id,
                        interface_bytes_hex=interface_definition_bytes(registration).hex(),
                        interface_digest_domain=domain,
                        interface_digest=exported.interface_digest,
                        effect_class=effect,
                    )
                )
        return tuple(result)

    def _check_definition(self, exported: PackageInterfaceExportV1) -> None:
        """Check that an exported definition agrees with its implementation."""

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

    def provider_definition(
        self,
        *,
        distribution: ProviderLocalDistributionPin,
        local_env: ProviderLocalEnvBackendPin,
        control_domain: str,
        interfaces: tuple[ProviderInterfaceRegistrationAny, ...],
    ) -> ProviderBuild:
        """The provider build pinning ``interfaces``: the contract registration each
        exported interface binds. Its identity names the build
        (``<provider_id>/<build key>``), so it installs beside other builds."""

        expected = {item.interface_id: item.interface_digest for item in self.interfaces}
        if (
            len(interfaces) != len(expected)
            or {item.interface_id: interface_definition_digest(item) for item in interfaces}
            != expected
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
        return ProviderBuild(
            identity=ArtifactIdentity(
                kind="Provider",
                name=f"{payload.provider_id}/{provider_build_key(payload).value}",
            ),
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
