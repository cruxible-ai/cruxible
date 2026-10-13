"""Compiler-owned ``workspace.file`` registration and built-in Provider.

New instances start with these two artifacts at genesis (the
``triggers-4-workspace-file`` genesis seed set in ``ledger/bootstrap.py``): the
ProviderInterface registration over the exact V2 operation definition the package
``cruxible-provider-workspace`` ships (``workspace-file-interface.json``, digest
``faa92552...`` under its retained ``cruxible.interface.stub.v1`` domain), and the
``cruxible-builtin`` Provider whose one implementation is core's in-process adapter
(``cruxible_core.providers.builtin_workspace_file``).

Identity. The implementation digest is the ordinary
``provider_implementation_digest(interface_id, interface_digest, entrypoint,
distribution_sha256)``. A built-in has no wheel, so ``distribution_sha256`` is
``WORKSPACE_FILE_BUILTIN_REVISION``: a compiler-owned constant, frozen like
``WEB_FETCH_INTERFACE_DIGEST``, that names the adapter's behaviour. It is not the
core version and not the module's source bytes; it changes only when the
adapter's output for some input changes, and the behaviour golden in
``tests/test_providers/test_builtin_workspace_file.py`` fails until it does.
The materialization, deployment, environment-manifest and lock digests are
typed constants derived from the implementation digest, so a binding to the
built-in is the same in every daemon.

The registration is the seeded bytes themselves
(``genesis/<set>/provider-interfaces/workspace.file.json``, identical in every
seed set), read and checked once per process. It is a V1 registration, so it
still carries the bucket vocabulary, classifier identity and six conformance
proofs core once ran; that data is inert. Core runs no classifier; the built-in
adapter refuses what it does not structure.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from typing import Literal

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.canonical import (
    Sha256Value,
    canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistration,
    ProviderInterfaceRegistrationV1,
    parse_provider_interface,
    provider_external_interface_definition_digest,
    provider_interface_digest,
    provider_interface_path,
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
from cruxible_client.contracts.workspace_file import WORKSPACE_FILE_INTERFACE_V2_DIGEST

WORKSPACE_FILE_INTERFACE_ID = "workspace.file"
WORKSPACE_FILE_INTERFACE_DOMAIN: Literal["cruxible.interface.stub.v1"] = (
    "cruxible.interface.stub.v1"
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
#: (append-only, guarded by a frozen-keys test), append the new revision's entry,
#: and append a new genesis seed set for the new Provider bytes.
WORKSPACE_FILE_BUILTIN_REVISION = (
    "sha256:9010dd924943e5ba971d5326bfe4cb7673a90873d1a475e7b2651943d0091e0d"
)
#: The one local-environment pin key the built-in Provider advertises.
WORKSPACE_FILE_BUILTIN_ENVIRONMENT_PIN_KEY = "builtin"
WORKSPACE_FILE_CAPTURE_CONTRACT_FAMILY = "workspace.file.capture.v1"

#: The exact V2 operation definition, byte-for-byte the package's
#: ``contracts/workspace.file.json``; its canonical bytes reproduce
#: ``WORKSPACE_FILE_INTERFACE_V2_DIGEST`` (checked at import).
WORKSPACE_FILE_INTERFACE_DEFINITION: dict[str, object] = json.loads(
    files("cruxible_core.governance.seed_artifacts")
    .joinpath("workspace-file-interface.json")
    .read_bytes()
)
if (
    provider_external_interface_definition_digest(
        canonical_bytes(WORKSPACE_FILE_INTERFACE_DEFINITION).hex(),
        domain=WORKSPACE_FILE_INTERFACE_DOMAIN,
    )
    != WORKSPACE_FILE_INTERFACE_V2_DIGEST
):  # pragma: no cover - import-time guard on checked-in bytes
    raise RuntimeError("workspace.file interface definition drifted from its frozen digest")

#: The seeded registration's file (one seed set holds it; every set's bytes are
#: equal) and the sha256 of those bytes, which every genesis seed set pins.
_SEEDED_REGISTRATION = "genesis/triggers-4-workspace-file/provider-interfaces/workspace.file.json"
_SEEDED_REGISTRATION_SHA256 = "f3afe32fd84a09b991edaba2c02110f05b5854bccf6db16c4c7379ebf1d205fe"


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
        interface_id=WORKSPACE_FILE_INTERFACE_ID,
        interface_digest=WORKSPACE_FILE_INTERFACE_V2_DIGEST,
        entrypoint=entrypoint,
        distribution_sha256=revision,
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


@cache
def _seeded_registration() -> ProviderInterfaceRegistrationV1:
    """Read the seeded registration once per process and prove its bytes."""

    content = (
        files("cruxible_core.governance.seed_artifacts").joinpath(_SEEDED_REGISTRATION).read_bytes()
    )
    if hashlib.sha256(content).hexdigest() != _SEEDED_REGISTRATION_SHA256:
        raise RuntimeError(  # pragma: no cover - guard on checked-in bytes
            "the seeded workspace.file registration drifted from its genesis digest"
        )
    registration = parse_provider_interface(
        content, path=provider_interface_path(WORKSPACE_FILE_INTERFACE_ID)
    )
    if (
        type(registration) is not ProviderInterfaceRegistrationV1
        or registration.interface_digest != WORKSPACE_FILE_INTERFACE_V2_DIGEST
        or registration.interface_bytes_hex
        != canonical_bytes(WORKSPACE_FILE_INTERFACE_DEFINITION).hex()
    ):  # pragma: no cover - guard on checked-in bytes
        raise RuntimeError("the seeded workspace.file registration names another definition")
    return registration


def is_builtin_workspace_file_registration(registration: object) -> bool:
    """Whether an accepted interface is the compiler-owned built-in registration."""

    if type(registration) is not ProviderInterfaceRegistrationV1:
        return False
    return registration.model_dump(exclude={"lifecycle"}) == _seeded_registration().model_dump(
        exclude={"lifecycle"}
    )


def workspace_file_interface_registration() -> ProviderInterfaceRegistrationV1:
    """The compiler-owned registration over the exact V2 definition: the seeded bytes."""

    return _seeded_registration()


def workspace_file_builtin_provider(
    *,
    interface_artifact_digest: str,
    lifecycle: ArtifactLifecycle = ArtifactLifecycle(),
) -> Provider:
    """The ``cruxible-builtin`` Provider implementing ``workspace.file`` in-process."""

    # The seeded manifest's bucket claims, kept byte-for-byte as inert data:
    # every selector the seeded registration once proved, none of them read.
    proofs = _seeded_registration().conformance_proofs
    implementation = ProviderImplementationManifest(
        interface_id=WORKSPACE_FILE_INTERFACE_ID,
        interface_digest=WORKSPACE_FILE_INTERFACE_V2_DIGEST,
        entrypoint=WORKSPACE_FILE_BUILTIN_ENTRYPOINT,
        backends=("local_env",),
        declared_input_buckets=tuple(proof.selector for proof in proofs),
        bucket_conformance={proof.selector: proof.fixture_id for proof in proofs},
        declared_endpoints=(),
        capture_contract_families=(WORKSPACE_FILE_CAPTURE_CONTRACT_FAMILY,),
        deterministic=True,
        side_effects=False,
    )
    manifest = ProviderRuntimeManifest(
        provider_id=WORKSPACE_FILE_BUILTIN_PROVIDER_ID,
        distribution=ProviderDistributionRef(name=WORKSPACE_FILE_BUILTIN_PROVIDER_ID, version="1"),
        supported_protocol_majors=(1,),
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
        pins=(
            ArtifactPin(
                role="provider-interface",
                target=ArtifactIdentity(kind="ProviderInterface", name=WORKSPACE_FILE_INTERFACE_ID),
                artifact_digest=interface_artifact_digest,
            ),
        ),
        lifecycle=lifecycle,
        runtime_artifact=runtime_artifact,
        implementations=provider_expected_implementation_records(runtime_artifact),
    )
    (record,) = provider.implementations
    if record.implementation_digest != WORKSPACE_FILE_BUILTIN_IMPLEMENTATION_DIGEST:
        raise RuntimeError("workspace.file built-in implementation digest drifted")
    return provider


def workspace_file_accepted_registration() -> AcceptedProviderInterfaceRegistration:
    """The seeded registration as an accepted artifact."""

    registration = workspace_file_interface_registration()
    return AcceptedProviderInterfaceRegistration(
        path=provider_interface_path(WORKSPACE_FILE_INTERFACE_ID),
        registration=registration,
        artifact_digest=provider_interface_digest(registration).tagged,
    )


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
    "WORKSPACE_FILE_INTERFACE_DEFINITION",
    "WORKSPACE_FILE_INTERFACE_ID",
    "is_builtin_workspace_file_registration",
    "workspace_file_accepted_registration",
    "workspace_file_builtin_identity",
    "workspace_file_builtin_provider",
    "workspace_file_interface_registration",
]
