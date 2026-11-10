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
from cruxible_client.contracts.repairs import (
    HandEditInstruction,
    HandEditRepair,
    RepairOperation,
)
from cruxible_client.contracts.shapes import ContractRef
from cruxible_core.errors import RequestRefusedError
from cruxible_core.providers.provider_runtime_contract import (
    PROVIDER_RUNTIME_RESERVED_REFUSAL_CODES,
)

#: Resolve one Contract pin by name and version at the install base: its digest,
#: or None when that version is not accepted there.
ContractDigests = Callable[[ContractRef], str | None]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PackageRuntimeRequirementV1(_Strict):
    #: The manifest implementation row the requirement belongs to: its entrypoint.
    entrypoint: str
    kind: Literal["python_extra", "runtime_resource"]
    name: str
    description: str
    probe_entrypoint: str | None = None


class PackageRegistrationDocumentV1(_Strict):
    """A provider package's registration, as ``package_metadata`` reads it from the wheel.

    ``operations`` are the package's typed operations, Contracts named
    ``name@N`` (unpinned until install). Every operation's entrypoint is one
    manifest implementation's (one entrypoint may serve several), every
    implementation serves at least one operation, and no operation declares a
    refusal code the provider runtime reserves.
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

    def pinned_operations(self, digests: ContractDigests) -> tuple[ProviderOperation, ...]:
        """Every operation with each Contract pinned to its accepted digest.

        Refuses ``cruxible.provider.operation_contract_unresolved`` when any
        Contract version the operations pin is not accepted at the install base
        (counting them and naming the first), or when a pinned digest differs
        from the accepted one.
        """

        self._refuse_unaccepted_contracts(digests)
        pinned: list[ProviderOperation] = []
        for operation in sorted(self.operations, key=lambda item: item.name.encode("utf-8")):

            def pin(ref: ContractRef, *, owner: str = operation.name) -> ContractRef:
                digest = digests(ref)
                if digest is None:  # pragma: no cover - refused by the closure check above
                    raise AssertionError(f"{ref.spelling} is unaccepted after the closure check")
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

    def _refuse_unaccepted_contracts(self, digests: ContractDigests) -> None:
        """Refuse once, counting every pinned Contract version not accepted here.

        A provider's Contracts arrive with a kit, so the usual cause is installing
        the package by name on an instance where no kit that uses it was added.
        """

        refs = {
            ref.spelling: ref
            for operation in self.operations
            for ref in (
                operation.in_,
                operation.out,
                *(refusal.contract for refusal in operation.refusals.values()),
            )
        }
        missing = sorted(
            (spelling for spelling, ref in refs.items() if digests(ref) is None),
            key=lambda spelling: spelling.encode("utf-8"),
        )
        if not missing:
            return
        raise RequestRefusedError(
            "cruxible.provider.operation_contract_unresolved",
            f"{self.manifest.distribution.name} cannot be installed here yet: "
            f"{len(missing)} of the {len(refs)} Contract versions its operations use are "
            f"not accepted on this instance (first: {missing[0]}). A provider's Contracts "
            "arrive with a kit: add a kit that uses this provider (cruxible kit add), which "
            "installs the provider too, or install the provider after such a kit is added.",
            repair=RepairOperation(operation="cruxible.kit.add"),
        )

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
