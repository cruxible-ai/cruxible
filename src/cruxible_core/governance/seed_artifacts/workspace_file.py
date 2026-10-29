"""Compiler-owned ``workspace.file`` operation and built-in Provider.

New instances start with these artifacts at genesis (``GENESIS_SEEDS`` in
``ledger/bootstrap.py``): the core-owned Contracts the operation's
signature pins (``cruxible_client.contracts.workspace_file``), and the
``cruxible-builtin`` Provider whose one operation, ``workspace.file``, is core's
in-process adapter (``cruxible_core.providers.builtin_workspace_file``). Core
recognizes the operation by its exact signature, never by name.

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
from cruxible_client.contracts.provider_operations import ProviderOperation
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
    workspace_file_contracts,
    workspace_file_refusal_contracts,
)

WORKSPACE_FILE_BUILTIN_PROVIDER_ID = "cruxible-builtin"
WORKSPACE_FILE_BUILTIN_ENTRYPOINT = "cruxible_core.providers.builtin_workspace_file:WorkspaceFile"
#: The built-in adapter's current behaviour revision. Bump only on a semantic
#: change (some input structures or refuses differently), together with the
#: behaviour golden; never for a refactor, a version bump or a docstring.
#:
#: A bump never replaces the old revision: every instance seeded under it pins
#: its implementation digest forever. Freeze the old adapter (its own module or
#: class), keep its entry in ``providers.builtin_runtime.BUILTIN_IMPLEMENTATIONS``
#: (append-only, guarded by a frozen-keys test) and append the new revision's
#: entry. New Provider bytes change ``GENESIS_SEEDS``, which every instance's
#: generation zero must match exactly.
WORKSPACE_FILE_BUILTIN_REVISION = (
    "sha256:9010dd924943e5ba971d5326bfe4cb7673a90873d1a475e7b2651943d0091e0d"
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


def workspace_file_operation() -> ProviderOperation:
    """The ``workspace.file`` operation with its exact Contract pins.

    Declares the built-in's own typed refusals (``providers/
    builtin_workspace_file.py``'s digest, length and size guards) so they
    surface through the executor as typed node refusals (F10's
    ``_typed_refusal``) instead of an undeclared-code protocol violation.
    """

    _source, _content, request, result = workspace_file_contracts()
    assert isinstance(request, Contract) and isinstance(result, Contract)
    declined, invalid, mismatched = workspace_file_refusal_contracts()
    assert (
        isinstance(declined, Contract)
        and isinstance(invalid, Contract)
        and isinstance(mismatched, Contract)
    )
    return ProviderOperation.model_validate(
        {
            "name": WORKSPACE_FILE_OPERATION,
            "in": {
                "name": request.name,
                "version": request.version,
                "digest": contract_digest(request).tagged,
            },
            "out": {
                "name": result.name,
                "version": result.version,
                "digest": contract_digest(result).tagged,
            },
            "effect": "pure",
            "refusals": {
                "provider_declined": {
                    "contract": {
                        "name": declined.name,
                        "version": declined.version,
                        "digest": contract_digest(declined).tagged,
                    },
                    "absorbable": True,
                },
                "invalid_parameter": {
                    "contract": {
                        "name": invalid.name,
                        "version": invalid.version,
                        "digest": contract_digest(invalid).tagged,
                    },
                    "absorbable": True,
                },
                "mismatched_lengths": {
                    "contract": {
                        "name": mismatched.name,
                        "version": mismatched.version,
                        "digest": contract_digest(mismatched).tagged,
                    },
                    "absorbable": True,
                },
            },
            "entrypoint": WORKSPACE_FILE_BUILTIN_ENTRYPOINT,
        }
    )


def workspace_file_builtin_provider(
    *,
    lifecycle: ArtifactLifecycle = ArtifactLifecycle(),
) -> Provider:
    """The ``cruxible-builtin`` Provider serving ``workspace.file`` in-process."""

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
        operations=(workspace_file_operation(),),
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
    "workspace_file_builtin_provider",
    "workspace_file_operation",
]
