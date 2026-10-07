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

The bucket classifier is compiler-owned too: ``WorkspaceFileBucketClassifier``
measures the same two dimensions the package classifier does, re-proven against
the same six conformance fixtures (``WORKSPACE_FILE_FIXTURES``, byte-equal to the
package's registration fixtures).
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from typing import TYPE_CHECKING, Literal

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.canonical import (
    CanonicalValue,
    Sha256Value,
    canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistration,
    ProviderBucketClass,
    ProviderBucketConformanceFixture,
    ProviderBucketConformanceFixtureProof,
    ProviderBucketDimension,
    ProviderBucketVocabulary,
    ProviderInterfaceRegistration,
    ProviderInterfaceRegistrationV1,
    provider_bucket_classifier_digest,
    provider_bucket_fixture_digest,
    provider_bucket_fixture_set_digest,
    provider_bucket_vocabulary_digest,
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
from cruxible_core.providers.builtin_workspace_file import (
    byte_size_class,
    content_kind_class,
    decode_declared_bytes,
)

if TYPE_CHECKING:
    from cruxible_core.providers.provider_local_runtime import ProviderSpawnDeadline

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
WORKSPACE_FILE_CLASSIFIER_IDENTITY = "cruxible.core.workspace.file"
WORKSPACE_FILE_CLASSIFIER_VERSION = 1
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


def _vocabulary() -> ProviderBucketVocabulary:
    return ProviderBucketVocabulary(
        interface_id=WORKSPACE_FILE_INTERFACE_ID,
        status="accepted",
        description=(
            "Structure the bytes of one authorized workspace file read into a capture body. "
            "The two dimensions separate the text path (a UTF-8 decode, a line view) from "
            "the opaque-bytes path, and size the payload so a claim over a large file is "
            "visibly a different bucket from a claim over a small one."
        ),
        dimensions=(
            ProviderBucketDimension(
                name="content_kind",
                description="whether the bytes decode as text",
                classes=(
                    ProviderBucketClass(
                        id="text",
                        description="strict UTF-8 with no NUL byte; an empty file is text",
                    ),
                    ProviderBucketClass(
                        id="binary",
                        description=(
                            "anything that is not strict UTF-8, or that carries a NUL byte"
                        ),
                    ),
                ),
            ),
            ProviderBucketDimension(
                name="byte_size",
                description="length of the decoded bytes",
                classes=(
                    ProviderBucketClass(id="tiny", description="at most 4096 bytes (4 KiB)"),
                    ProviderBucketClass(id="small", description="4097 to 65536 bytes (64 KiB)"),
                    ProviderBucketClass(id="medium", description="65537 to 1048576 bytes (1 MiB)"),
                    ProviderBucketClass(
                        id="large",
                        description=("more than 1048576 bytes (1 MiB); unclaimed by the built-in"),
                    ),
                ),
            ),
        ),
    )


def _text_lines(count: int, *, newline: str, trailing: bool, bom: bool = False) -> bytes:
    lines = [
        f"line {index:05d}: the quick brown fox jumps over the lazy dog" for index in range(count)
    ]
    text = newline.join(lines) + (newline if trailing else "")
    return ("\ufeff" if bom else "").encode() + text.encode()


def _pseudo_random(length: int, *, seed: str) -> bytes:
    output = bytearray()
    block = seed.encode()
    while len(output) < length:
        block = hashlib.sha256(block).digest()
        output.extend(block)
    return bytes(output[:length])


_FIXTURE_BYTES: dict[str, bytes] = {
    "workspace-file-binary-medium": _pseudo_random(70_000, seed="workspace-file-binary-medium"),
    "workspace-file-binary-small": _pseudo_random(5_000, seed="workspace-file-binary-small"),
    "workspace-file-binary-tiny": bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000100000001008060000001ff3ff61"
    ),
    "workspace-file-text-medium": _text_lines(1_200, newline="\n", trailing=False, bom=True),
    "workspace-file-text-small": _text_lines(600, newline="\r\n", trailing=True),
    "workspace-file-text-tiny": (
        "# Reach readings\n\nUpper reach: 4.1 mg/l nitrate — see the tide-gauge report.\n"
    ).encode(),
}


def _fixture(fixture_id: str, data: bytes) -> ProviderBucketConformanceFixture:
    bucket = f"content_kind={content_kind_class(data)};byte_size={byte_size_class(len(data))}"
    return ProviderBucketConformanceFixture(
        fixture_id=fixture_id,
        canonical_input={
            "logical_source": f"fixtures/{fixture_id}",
            "commitment_digest": f"sha256:{'c0' * 32}",
            "content_encoding": "base64",
            "bytes": base64.b64encode(data).decode("ascii"),
            "byte_length": len(data),
            "bytes_digest": f"sha256:{hashlib.sha256(data).hexdigest()}",
        },
        measured_bucket_id=bucket,
    )


WORKSPACE_FILE_FIXTURES = tuple(
    _fixture(fixture_id, data)
    for fixture_id, data in sorted(_FIXTURE_BYTES.items(), key=lambda item: item[0].encode())
)
WORKSPACE_FILE_CONFORMANCE_PROOFS = tuple(
    sorted(
        (
            ProviderBucketConformanceFixtureProof(
                selector=fixture.measured_bucket_id,
                fixture_id=fixture.fixture_id,
                fixture_digest=provider_bucket_fixture_digest(fixture),
                measured_bucket_id=fixture.measured_bucket_id,
            )
            for fixture in WORKSPACE_FILE_FIXTURES
        ),
        key=lambda proof: (proof.selector.encode(), proof.fixture_id.encode()),
    )
)
WORKSPACE_FILE_FIXTURE_SET_DIGEST = provider_bucket_fixture_set_digest(
    WORKSPACE_FILE_CONFORMANCE_PROOFS
)
WORKSPACE_FILE_CLASSIFIER_DIGEST = provider_bucket_classifier_digest(
    classifier_identity=WORKSPACE_FILE_CLASSIFIER_IDENTITY,
    classifier_version=WORKSPACE_FILE_CLASSIFIER_VERSION,
    conformance_fixture_set_digest=WORKSPACE_FILE_FIXTURE_SET_DIGEST,
)


class WorkspaceFileBucketClassifier:
    """Compiler-owned classifier; it receives bounded bytes, never a locator."""

    classifier_identity = WORKSPACE_FILE_CLASSIFIER_IDENTITY
    classifier_version = WORKSPACE_FILE_CLASSIFIER_VERSION
    classifier_digest = WORKSPACE_FILE_CLASSIFIER_DIGEST

    def classify(
        self, canonical_input: CanonicalValue, *, deadline: ProviderSpawnDeadline | None
    ) -> str:
        if not isinstance(canonical_input, dict):
            raise ValueError("workspace.file classifier input must be an object")
        if canonical_input.get("content_encoding") != "base64":
            raise ValueError("workspace.file classifier requires base64 bytes")
        if not isinstance(canonical_input.get("bytes"), str):
            raise ValueError("workspace.file classifier requires bytes")
        data = decode_declared_bytes(canonical_input)
        if data is None:
            raise ValueError("workspace.file classifier requires canonical base64")
        return f"content_kind={content_kind_class(data)};byte_size={byte_size_class(len(data))}"


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


def is_builtin_workspace_file_registration(registration: object) -> bool:
    """Whether an accepted interface is the compiler-owned built-in registration."""

    return (
        isinstance(registration, ProviderInterfaceRegistrationV1)
        and not isinstance(registration, ProviderInterfaceRegistration)
        and registration.interface_id == WORKSPACE_FILE_INTERFACE_ID
        and registration.classifier_digest == WORKSPACE_FILE_CLASSIFIER_DIGEST
    )


def workspace_file_interface_registration(
    *, lifecycle: ArtifactLifecycle = ArtifactLifecycle()
) -> ProviderInterfaceRegistrationV1:
    """The compiler-owned registration over the exact V2 definition bytes."""

    interface_bytes = canonical_bytes(WORKSPACE_FILE_INTERFACE_DEFINITION)
    vocabulary_bytes = canonical_bytes(_vocabulary().model_dump(mode="json"))
    return ProviderInterfaceRegistrationV1(
        identity=ArtifactIdentity(kind="ProviderInterface", name=WORKSPACE_FILE_INTERFACE_ID),
        interface_id=WORKSPACE_FILE_INTERFACE_ID,
        interface_bytes_hex=interface_bytes.hex(),
        interface_digest_domain=WORKSPACE_FILE_INTERFACE_DOMAIN,
        interface_digest=WORKSPACE_FILE_INTERFACE_V2_DIGEST,
        vocabulary_bytes_hex=vocabulary_bytes.hex(),
        vocabulary_digest=provider_bucket_vocabulary_digest(vocabulary_bytes.hex()),
        classifier_identity=WORKSPACE_FILE_CLASSIFIER_IDENTITY,
        classifier_version=WORKSPACE_FILE_CLASSIFIER_VERSION,
        classifier_digest=WORKSPACE_FILE_CLASSIFIER_DIGEST,
        conformance_fixture_set_digest=WORKSPACE_FILE_FIXTURE_SET_DIGEST,
        conformance_proofs=WORKSPACE_FILE_CONFORMANCE_PROOFS,
        # The definition spells a no-effect operation "pure"; governed, it is "none".
        effect_class="none",
        lifecycle=lifecycle,
    )


def workspace_file_builtin_provider(
    *,
    interface_artifact_digest: str,
    lifecycle: ArtifactLifecycle = ArtifactLifecycle(),
) -> Provider:
    """The ``cruxible-builtin`` Provider implementing ``workspace.file`` in-process."""

    selectors = tuple(proof.selector for proof in WORKSPACE_FILE_CONFORMANCE_PROOFS)
    implementation = ProviderImplementationManifest(
        interface_id=WORKSPACE_FILE_INTERFACE_ID,
        interface_digest=WORKSPACE_FILE_INTERFACE_V2_DIGEST,
        entrypoint=WORKSPACE_FILE_BUILTIN_ENTRYPOINT,
        backends=("local_env",),
        declared_input_buckets=selectors,
        bucket_conformance={
            proof.selector: proof.fixture_id for proof in WORKSPACE_FILE_CONFORMANCE_PROOFS
        },
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
    """The seeded registration as an accepted artifact (its classifier's install key)."""

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
    "WORKSPACE_FILE_CLASSIFIER_DIGEST",
    "WORKSPACE_FILE_FIXTURES",
    "WORKSPACE_FILE_INTERFACE_DEFINITION",
    "WORKSPACE_FILE_INTERFACE_ID",
    "WorkspaceFileBucketClassifier",
    "is_builtin_workspace_file_registration",
    "workspace_file_accepted_registration",
    "workspace_file_builtin_identity",
    "workspace_file_builtin_provider",
    "workspace_file_interface_registration",
]
