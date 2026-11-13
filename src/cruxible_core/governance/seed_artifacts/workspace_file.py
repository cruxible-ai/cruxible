"""Compiler-owned workspace reads and the built-in Provider that serves them.

New instances start with the ``cruxible-builtin`` Provider at genesis
(``GENESIS_SEEDS`` in ``ledger/bootstrap.py``). Its two operations,
``workspace.file`` (one path, one Capture) and ``workspace.files`` (a glob, a
sorted bounded list of Captures), serve the reserved capabilities of the same
names (ruling O2) through core's in-process adapter
(``cruxible_core.providers.builtin_workspace_file``). Like any provider build it
carries its own shapes in ``declares``: the handoff Contract the daemon fills
with the bytes it read (core's internal shape, never a Procedure's request),
its copies of the base kit's two result Contracts, byte for byte, and its three
refusal Contracts (``cruxible_client.contracts.workspace_file``). Its
operation pins resolve inside the build, so it needs no instance Contract. The
capabilities themselves, with the authored request and result Contracts a
Procedure is typed against, arrive with the base kit (``cruxible_core.kits``).

Identity. The implementation digest is the ordinary
``provider_implementation_digest(entrypoint, distribution_sha256)``
(``cruxible.provider.implementation.v2``) over the runtime manifest's one
entrypoint row. A built-in has no wheel, so ``distribution_sha256`` is
``WORKSPACE_FILE_BUILTIN_REVISION``: a compiler-owned constant that names the
adapter's behaviour. It is not the core version and not the module's source
bytes; it changes only when the adapter's output for some input changes, and the
behaviour golden in ``tests/test_providers/test_builtin_workspace_file.py``
fails until it does. The materialization, deployment, environment-manifest and
lock digests are typed constants derived from the implementation digest, so a
binding to the built-in is the same in every daemon. The built-in speaks
provider runtime protocol 2.0, like any provider.
"""

from __future__ import annotations

from dataclasses import dataclass

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.provider_registration import (
    DeclaredOperation,
    ProviderDeclares,
)
from cruxible_client.contracts.providers import (
    Provider,
    ProviderDistributionRef,
    ProviderImplementationManifest,
    ProviderLocalDistributionPin,
    ProviderLocalEnvBackendPin,
    ProviderRuntimeArtifactPayload,
    ProviderRuntimeManifest,
    provider_expected_implementation_records,
    provider_implementation_digest,
    provider_manifest_digest,
)
from cruxible_client.contracts.shapes import Contract, contract_digest
from cruxible_client.contracts.workspace_file import (
    WORKSPACE_FILE_OPERATION,
    WORKSPACE_FILES_OPERATION,
    workspace_file_contracts,
    workspace_file_refusal_contracts,
)

WORKSPACE_FILE_BUILTIN_PROVIDER_ID = "cruxible-builtin"
WORKSPACE_FILE_BUILTIN_ENTRYPOINT = "cruxible_core.providers.builtin_workspace_file:WorkspaceFile"
#: The built-in adapter's current behaviour revision. Bump only on a semantic
#: change (some input is answered or refused differently), together with the
#: behaviour golden; never for a refactor, a version bump or a docstring.
#:
#: Once a release is published a bump never replaces the old revision: every
#: instance seeded under it pins its implementation digest forever. Freeze the
#: old adapter, keep its entry in
#: ``providers.builtin_runtime.BUILTIN_IMPLEMENTATIONS`` (append-only, guarded by
#: a frozen-keys test) and append the new revision's entry. New Provider bytes
#: change ``GENESIS_SEEDS``, which every instance's generation zero must match.
#:
#: Revision 2 (2026-11-13, contract batch slice 8): the adapter checks the bytes
#: core read against their declared length and digest and returns an empty
#: output, core committing the bytes as the call's capture field; it no longer
#: structures text, and its 1 MiB guard is gone (the CaptureKind's byte budget
#: bounds the read). Nothing was published under revision 1, so it is replaced.
WORKSPACE_FILE_BUILTIN_REVISION = (
    "sha256:04922994e7747b10f7a84726512d7b537d89d37655ca63a926bbb216dfdc25a5"
)
#: The one local-environment pin key the built-in Provider advertises.
WORKSPACE_FILE_BUILTIN_ENVIRONMENT_PIN_KEY = "builtin"


@dataclass(frozen=True)
class BuiltinProviderIdentity:
    """The compiler-owned digests of one built-in implementation revision.

    The implementation digest is the ordinary one over the revision constant;
    the rest are typed digests derived from it, so a revision's binding is the
    same constant in every daemon and stays derivable after a later revision.
    """

    revision: str
    entrypoint: str
    implementation_digest: str
    materialization_digest: str
    deployment_digest: str
    environment_manifest_digest: str
    lock_digest: str


def workspace_file_builtin_identity(
    revision: str, *, entrypoint: str = WORKSPACE_FILE_BUILTIN_ENTRYPOINT
) -> BuiltinProviderIdentity:
    implementation_digest = provider_implementation_digest(
        entrypoint=entrypoint, distribution_sha256=revision
    )

    def derived(role: str) -> str:
        return typed_digest(
            Sha256Value,
            f"cruxible-builtin-provider-{role}-v1",
            {"implementation_digest": implementation_digest},
        ).tagged

    return BuiltinProviderIdentity(
        revision=revision,
        entrypoint=entrypoint,
        implementation_digest=implementation_digest,
        materialization_digest=derived("materialization"),
        deployment_digest=derived("deployment"),
        environment_manifest_digest=derived("environment-manifest"),
        lock_digest=derived("lock"),
    )


WORKSPACE_FILE_BUILTIN_IDENTITY = workspace_file_builtin_identity(WORKSPACE_FILE_BUILTIN_REVISION)
WORKSPACE_FILE_BUILTIN_IMPLEMENTATION_DIGEST = WORKSPACE_FILE_BUILTIN_IDENTITY.implementation_digest
WORKSPACE_FILE_BUILTIN_MATERIALIZATION_DIGEST = (
    WORKSPACE_FILE_BUILTIN_IDENTITY.materialization_digest
)
WORKSPACE_FILE_BUILTIN_DEPLOYMENT_DIGEST = WORKSPACE_FILE_BUILTIN_IDENTITY.deployment_digest
WORKSPACE_FILE_BUILTIN_ENVIRONMENT_MANIFEST_DIGEST = (
    WORKSPACE_FILE_BUILTIN_IDENTITY.environment_manifest_digest
)
WORKSPACE_FILE_BUILTIN_LOCK_DIGEST = WORKSPACE_FILE_BUILTIN_IDENTITY.lock_digest


#: Where the builtin's code is, as its operation's ``source`` records it.
WORKSPACE_FILE_BUILTIN_SOURCE = {
    "path": "src/cruxible_core/providers/builtin_workspace_file.py",
    "repository": "https://github.com/cruxible-ai/cruxible",
}
WORKSPACE_FILE_BUILTIN_DESCRIPTION = (
    "Reads one workspace file in the daemon's own process and returns its exact bytes as "
    "one Capture. Core's built-in; no other provider may serve a workspace. capability."
)
WORKSPACE_FILES_BUILTIN_DESCRIPTION = (
    "Reads every workspace file a glob matches in the daemon's own process and returns "
    "their exact bytes as a list of Captures, sorted by name, past the request's max_files "
    "refused before any file is read. Core's built-in; no other provider may serve a "
    "workspace. capability."
)


def workspace_file_declares() -> ProviderDeclares:
    """The shapes the builtin carries: its handoff, two result and three refusal Contracts."""

    contracts = (*workspace_file_contracts(), *workspace_file_refusal_contracts())
    assert all(isinstance(item, Contract) for item in contracts)
    return ProviderDeclares(
        contracts=tuple(
            sorted(
                (item for item in contracts if isinstance(item, Contract)),
                key=lambda item: (item.name.encode("utf-8"), item.version),
            )
        )
    )


def _pin(contract: object) -> dict[str, object]:
    assert isinstance(contract, Contract)
    return {
        "name": contract.name,
        "version": contract.version,
        "digest": contract_digest(contract).tagged,
    }


def workspace_file_operations() -> tuple[DeclaredOperation, DeclaredOperation]:
    """The builtin's two operations with their exact Contract pins, sorted by name.

    Each takes the handoff the daemon fills with the bytes it read for one file
    and returns its copy of the base kit's result. Both declare the built-in's
    own typed refusals (``providers/builtin_workspace_file.py``'s shape, length
    and digest guards), so they surface as typed node refusals instead of an
    undeclared-code protocol violation.
    """

    handoff, file_result, files_result = workspace_file_contracts()
    declined, invalid, mismatched = workspace_file_refusal_contracts()
    refusals = {
        "provider_declined": {"contract": _pin(declined), "absorbable": True},
        "invalid_parameter": {"contract": _pin(invalid), "absorbable": True},
        "mismatched_lengths": {"contract": _pin(mismatched), "absorbable": True},
    }

    def operation(name: str, description: str, result: object) -> DeclaredOperation:
        return DeclaredOperation.model_validate(
            {
                "name": name,
                "description": description,
                "source": WORKSPACE_FILE_BUILTIN_SOURCE,
                "serves": [name],
                "in": _pin(handoff),
                "out": _pin(result),
                "effect": "pure",
                "refusals": refusals,
                "entrypoint": WORKSPACE_FILE_BUILTIN_ENTRYPOINT,
            }
        )

    return (
        operation(WORKSPACE_FILE_OPERATION, WORKSPACE_FILE_BUILTIN_DESCRIPTION, file_result),
        operation(WORKSPACE_FILES_OPERATION, WORKSPACE_FILES_BUILTIN_DESCRIPTION, files_result),
    )


def workspace_file_builtin_provider(
    *,
    lifecycle: ArtifactLifecycle = ArtifactLifecycle(),
) -> Provider:
    """The ``cruxible-builtin`` Provider serving the workspace reads in-process."""

    implementation = ProviderImplementationManifest(
        entrypoint=WORKSPACE_FILE_BUILTIN_ENTRYPOINT,
        backends=("local_env",),
        declared_endpoints=(),
        deterministic=True,
    )
    manifest = ProviderRuntimeManifest(
        provider_id=WORKSPACE_FILE_BUILTIN_PROVIDER_ID,
        distribution=ProviderDistributionRef(name=WORKSPACE_FILE_BUILTIN_PROVIDER_ID, version="1"),
        supported_protocol_majors=(2,),
        implementations=(implementation,),
    )
    runtime_artifact = ProviderRuntimeArtifactPayload(
        provider_id=WORKSPACE_FILE_BUILTIN_PROVIDER_ID,
        status="accepted",
        manifest=manifest,
        manifest_digest=provider_manifest_digest(manifest),
        distribution=ProviderLocalDistributionPin(
            name=WORKSPACE_FILE_BUILTIN_PROVIDER_ID,
            version="1",
            filename="cruxible-builtin-workspace-file",
            sha256=WORKSPACE_FILE_BUILTIN_REVISION,
        ),
        local_env=ProviderLocalEnvBackendPin(
            lock_sha256=WORKSPACE_FILE_BUILTIN_LOCK_DIGEST,
            materialization_digests={
                WORKSPACE_FILE_BUILTIN_ENVIRONMENT_PIN_KEY: (
                    WORKSPACE_FILE_BUILTIN_MATERIALIZATION_DIGEST
                )
            },
        ),
    )
    provider = Provider(
        identity=ArtifactIdentity(kind="Provider", name=WORKSPACE_FILE_BUILTIN_PROVIDER_ID),
        control_domain=WORKSPACE_FILE_BUILTIN_PROVIDER_ID,
        signing_keys=(),
        capture_contract_digests=(),
        lifecycle=lifecycle,
        runtime_artifact=runtime_artifact,
        implementations=provider_expected_implementation_records(runtime_artifact),
        operations=workspace_file_operations(),
        declares=workspace_file_declares(),
    )
    (record,) = provider.implementations
    if record.implementation_digest != WORKSPACE_FILE_BUILTIN_IMPLEMENTATION_DIGEST:
        raise RuntimeError("workspace.file built-in implementation digest drifted")
    return provider


__all__ = [
    "BuiltinProviderIdentity",
    "WORKSPACE_FILE_BUILTIN_DEPLOYMENT_DIGEST",
    "WORKSPACE_FILE_BUILTIN_IDENTITY",
    "WORKSPACE_FILE_BUILTIN_ENTRYPOINT",
    "WORKSPACE_FILE_BUILTIN_ENVIRONMENT_MANIFEST_DIGEST",
    "WORKSPACE_FILE_BUILTIN_ENVIRONMENT_PIN_KEY",
    "WORKSPACE_FILE_BUILTIN_IMPLEMENTATION_DIGEST",
    "WORKSPACE_FILE_BUILTIN_LOCK_DIGEST",
    "WORKSPACE_FILE_BUILTIN_MATERIALIZATION_DIGEST",
    "WORKSPACE_FILE_BUILTIN_PROVIDER_ID",
    "WORKSPACE_FILE_BUILTIN_REVISION",
    "workspace_file_builtin_identity",
    "WORKSPACE_FILE_BUILTIN_DESCRIPTION",
    "WORKSPACE_FILES_BUILTIN_DESCRIPTION",
    "WORKSPACE_FILE_BUILTIN_SOURCE",
    "workspace_file_builtin_provider",
    "workspace_file_declares",
    "workspace_file_operations",
]
