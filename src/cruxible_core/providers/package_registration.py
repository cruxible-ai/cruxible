"""Lower verified package metadata into ordinary governed Provider definitions.

This module imports no provider code and makes no execution-readiness claim.
The installer supplies the measured local artifact pins separately; package
metadata cannot choose the installed wheel, environment or execution grants.

A package's registration (``registration.json`` schema 2) declares its typed
**operations**: each names its input and output Contracts (``name@N``), its
effect, its typed refusals and the manifest entrypoint that serves it. The
install pins every Contract to the exact accepted version; the provider law
refuses a pin that does not resolve.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.provider_operations import ProviderOperation
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
from cruxible_client.contracts.shapes import ContractRef
from cruxible_core.errors import RequestRefusedError

#: Resolve one Contract pin by name and version at the install base: its digest,
#: or None when that version is not accepted there.
ContractDigests = Callable[[ContractRef], str | None]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PackageRuntimeRequirementV1(_Strict):
    #: The manifest implementation row the requirement belongs to (its id).
    interface_id: str
    kind: Literal["python_extra", "runtime_resource"]
    name: str
    description: str
    probe_entrypoint: str | None = None


class PackageRegistrationDocumentV1(_Strict):
    """A provider package's registration, as ``package_metadata`` reads it from the wheel.

    ``operations`` are the package's typed operations, Contracts named
    ``name@N`` (unpinned until install). Every operation's entrypoint is one
    manifest implementation's, every implementation serves at least one
    operation, and an operation's effect agrees with the side effects its
    implementation declares.
    """

    schema_version: Literal[2]
    manifest: ProviderRuntimeManifest
    operations: tuple[ProviderOperation, ...]
    runtime_requirements: tuple[PackageRuntimeRequirementV1, ...] = ()

    @model_validator(mode="after")
    def _coverage(self) -> PackageRegistrationDocumentV1:
        names = [item.name for item in self.operations]
        if not names or len(set(names)) != len(names):
            raise ValueError("a package declares one or more operations, each named once")
        by_entrypoint: dict[str, bool] = {}
        for item in self.manifest.implementations:
            if item.entrypoint in by_entrypoint:
                raise ValueError("each manifest implementation has its own entrypoint")
            by_entrypoint[item.entrypoint] = item.side_effects
        used: set[str] = set()
        for operation in self.operations:
            side_effects = by_entrypoint.get(operation.entrypoint)
            if side_effects is None:
                raise ValueError(
                    f"operation {operation.name!r} names entrypoint {operation.entrypoint!r}, "
                    "which the manifest does not export"
                )
            if side_effects != (operation.effect == "external_mutation"):
                raise ValueError(
                    f"operation {operation.name!r} effect disagrees with its implementation"
                )
            used.add(operation.entrypoint)
        if used != set(by_entrypoint):
            raise ValueError("every manifest implementation serves at least one operation")
        implementations = {item.interface_id: item for item in self.manifest.implementations}
        for requirement in self.runtime_requirements:
            row = implementations.get(requirement.interface_id)
            if row is None or (
                requirement.kind == "python_extra" and requirement.name not in row.requires_extras
            ):
                raise ValueError("runtime requirement does not match a declared implementation")
        return self

    def pinned_operations(self, digests: ContractDigests) -> tuple[ProviderOperation, ...]:
        """Every operation with each Contract pinned to its accepted digest.

        Refuses ``cruxible.provider.operation_contract_unresolved`` naming the
        first Contract version not accepted at the install base.
        """

        pinned: list[ProviderOperation] = []
        for operation in sorted(self.operations, key=lambda item: item.name.encode("utf-8")):

            def pin(ref: ContractRef, *, owner: str = operation.name) -> ContractRef:
                digest = digests(ref)
                if digest is None:
                    raise RequestRefusedError(
                        "cruxible.provider.operation_contract_unresolved",
                        f"Operation {owner!r} pins {ref.spelling}, which is not accepted here.",
                        repair=HandEditRepair(
                            hand_edit=HandEditInstruction(
                                target=f"Contract {ref.spelling}",
                                required_change=(
                                    "Accept the Contract versions the package's operations "
                                    "pin before installing it (a kit ships them)."
                                ),
                            )
                        ),
                    )
                if ref.digest is not None and ref.digest != digest:
                    raise RequestRefusedError(
                        "cruxible.provider.operation_contract_unresolved",
                        f"Operation {owner!r} pins {ref.spelling} at {ref.digest}; "
                        f"the accepted version is {digest}.",
                        repair=HandEditRepair(
                            hand_edit=HandEditInstruction(
                                target=f"Contract {ref.spelling}",
                                required_change="Rebuild the package against the accepted one.",
                            )
                        ),
                    )
                return ref.model_copy(update={"digest": digest})

            pinned.append(
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
            )
        return tuple(pinned)

    def provider_definition(
        self,
        *,
        distribution: ProviderLocalDistributionPin,
        local_env: ProviderLocalEnvBackendPin,
        control_domain: str,
        operations: tuple[ProviderOperation, ...],
    ) -> NamedProviderBuild:
        """The provider build declaring ``operations`` (pinned). Its identity names
        the build (``<provider_id>/<build key>``), so it installs beside other builds."""

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
            }
        )


__all__ = [
    "ContractDigests",
    "PackageRegistrationDocumentV1",
    "PackageRuntimeRequirementV1",
]
