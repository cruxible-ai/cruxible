"""Lower verified package metadata into ordinary governed Provider definitions.

This module imports no provider code and makes no execution-readiness claim.
The installer supplies the measured local artifact pins separately; package
metadata cannot choose the installed wheel, environment or execution grants.

A package's registration (``registration.json`` schema 3,
:class:`~cruxible_client.contracts.provider_registration.PackageRegistration`)
declares its typed **operations** (each with a description, its source, the
capability names it serves, its ``in`` and ``out`` Contracts, effect, typed
refusals and the manifest entrypoint that serves it) and carries every shape
they reach in ``declares``. The install pins each operation Contract to the
digest of the package's own carried copy; it never reads the instance's
Contracts, so a provider installs with no kit present.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.provider_registration import (
    DeclaredOperation,
    ProviderDeclares,
    RegistrationRuntimeRequirement,
    evaluate_declared_operations,
)
from cruxible_client.contracts.providers import (
    NamedProviderBuild,
    ProviderLocalDistributionPin,
    ProviderLocalEnvBackendPin,
    ProviderRuntimeArtifactPayload,
    ProviderRuntimeManifest,
    provider_build_key,
    provider_expected_implementation_records,
    provider_manifest_digest,
)
from cruxible_client.contracts.repairs import HandEditInstruction, HandEditRepair
from cruxible_client.contracts.shapes import ContractRef, contract_digest
from cruxible_core.errors import RequestRefusedError
from cruxible_core.providers.provider_runtime_contract import (
    PROVIDER_RUNTIME_RESERVED_REFUSAL_CODES,
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PackageRegistrationDocumentV1(_Strict):
    """A provider package's registration, as ``package_metadata`` reads it from the wheel.

    The schema-3 registration with its runtime manifest read and parsed.
    ``operations`` name Contracts ``name@N`` (pinned at install to the carried
    copies). Every operation's entrypoint is one manifest implementation's (one
    entrypoint may serve several), every implementation serves at least one
    operation, and no operation declares a refusal code the provider runtime
    reserves. The registration law over ``declares`` is judged at install
    (:meth:`pinned_operations`), where it refuses typed.
    """

    schema_version: Literal[3]
    manifest: ProviderRuntimeManifest
    operations: tuple[DeclaredOperation, ...] = Field(min_length=1)
    declares: ProviderDeclares
    runtime_requirements: tuple[RegistrationRuntimeRequirement, ...] = ()

    @model_validator(mode="after")
    def _coverage(self) -> PackageRegistrationDocumentV1:
        names = [item.name for item in self.operations]
        if len(set(names)) != len(names):
            raise ValueError("a package declares one or more operations, each named once")
        implementations = {item.entrypoint: item for item in self.manifest.implementations}
        used: set[str] = set()
        for operation in self.operations:
            if operation.entrypoint not in implementations:
                raise ValueError(
                    f"operation {operation.name!r} names entrypoint {operation.entrypoint!r}, "
                    "which the manifest does not export"
                )
            reserved = sorted(set(operation.refusals) & PROVIDER_RUNTIME_RESERVED_REFUSAL_CODES)
            if reserved:
                raise ValueError(
                    f"operation {operation.name!r} declares refusal codes the provider "
                    f"runtime reserves: {reserved}"
                )
            used.add(operation.entrypoint)
        if used != set(implementations):
            raise ValueError("every manifest implementation serves at least one operation")
        for requirement in self.runtime_requirements:
            row = implementations.get(requirement.entrypoint)
            if row is None or (
                requirement.kind == "python_extra" and requirement.name not in row.requires_extras
            ):
                raise ValueError("runtime requirement does not match a declared implementation")
        return self

    def pinned_operations(self) -> tuple[DeclaredOperation, ...]:
        """Every operation with each Contract pinned to the digest of its carried copy.

        Refuses with the registration law's own code
        (``cruxible.provider.capability_reserved``,
        ``cruxible.provider.declared_contract_missing``, ...) when the
        operations and ``declares`` break it, counting the problems and naming
        the first. Linear in the registration; the instance is never read.
        """

        problems = evaluate_declared_operations(self.operations, self.declares)
        if problems:
            first = problems[0]
            more = f" ({len(problems) - 1} more)" if len(problems) > 1 else ""
            raise RequestRefusedError(
                first.code,
                f"{self.manifest.distribution.name} cannot be installed: {first.message}{more}",
                repair=HandEditRepair(
                    hand_edit=HandEditInstruction(
                        target=f"the {self.manifest.distribution.name} package's registration.json",
                        required_change=(
                            "Carry exactly the Contracts and CaptureKinds the operations reach, "
                            "each nested Contract pinned by digest, and serve no workspace. "
                            "capability; then rebuild the package."
                        ),
                    )
                ),
            )
        digests = {
            (item.name, item.version): contract_digest(item).tagged
            for item in self.declares.contracts
        }

        def pin(ref: ContractRef) -> ContractRef:
            return ref.model_copy(update={"digest": digests[(ref.name, ref.version)]})

        return tuple(
            operation.model_copy(
                update={
                    "in_": pin(operation.in_),
                    "out": pin(operation.out),
                    "refusals": {
                        code: refusal.model_copy(update={"contract": pin(refusal.contract)})
                        for code, refusal in operation.refusals.items()
                    },
                }
            )
            for operation in sorted(self.operations, key=lambda item: item.name.encode("utf-8"))
        )

    def provider_definition(
        self,
        *,
        distribution: ProviderLocalDistributionPin,
        local_env: ProviderLocalEnvBackendPin,
        control_domain: str,
        operations: tuple[DeclaredOperation, ...],
    ) -> NamedProviderBuild:
        """The provider build declaring ``operations`` (pinned) and carrying ``declares``.

        Its identity names the build (``<provider_id>/<build key>``), so it
        installs beside other builds.
        """

        if {item.name for item in operations} != {item.name for item in self.operations}:
            raise ValueError("a build declares exactly the package's operations")
        payload = ProviderRuntimeArtifactPayload(
            provider_id=self.manifest.provider_id,
            status="accepted",
            manifest=self.manifest,
            manifest_digest=provider_manifest_digest(self.manifest),
            distribution=distribution,
            local_env=local_env,
        )
        return NamedProviderBuild.model_validate(
            {
                "identity": ArtifactIdentity(
                    kind="Provider",
                    name=f"{payload.provider_id}/{provider_build_key(payload).value}",
                ),
                "control_domain": control_domain,
                "signing_keys": (),
                "runtime_artifact": payload,
                "implementations": provider_expected_implementation_records(payload),
                "operations": tuple(sorted(operations, key=lambda item: item.name.encode("utf-8"))),
                "declares": self.declares,
            }
        )


__all__ = ["PackageRegistrationDocumentV1"]
