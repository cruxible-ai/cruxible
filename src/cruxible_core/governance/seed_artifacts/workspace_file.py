"""Compiler-owned exact inputs for the ``workspace.file`` Provider seed."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Literal

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.canonical import CanonicalValue, canonical_bytes
from cruxible_client.contracts.provider_interfaces import (
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
    provider_external_interface_definition_digest,
)
from cruxible_client.contracts.providers import (
    ProviderDistributionRefV1,
    ProviderImplementationManifestV1,
    ProviderLocalDistributionPinV1,
    ProviderLocalEnvBackendPinV1,
    ProviderRuntimeArtifactPayloadV1,
    ProviderRuntimeManifestV1,
    ProviderV2,
    provider_expected_implementation_records,
    provider_manifest_digest,
)
from cruxible_client.contracts.workspace_file import WORKSPACE_FILE_INTERFACE_DIGEST

WORKSPACE_FILE_INTERFACE_ID = "workspace.file"
WORKSPACE_FILE_PROVIDER_ID = "cruxible-provider-workspace"
WORKSPACE_FILE_INTERFACE_DOMAIN: Literal["cruxible.interface.stub.v1"] = (
    "cruxible.interface.stub.v1"
)
WORKSPACE_FILE_WHEEL_DIGEST = (
    "sha256:e710b00549a8ef8b1b0f6774dd3d801bd6612b3374c304ec87a5ef42bebd1aae"
)
WORKSPACE_FILE_IMPLEMENTATION_DIGEST = (
    "sha256:1dc4e265a2f63985cb7f5b7bbd47dbea601345ed5d1f6fa979134c59547467c7"
)
WORKSPACE_FILE_LOCK_DIGEST = (
    "sha256:b7bb433e6fe67d1142af7a705fd548ebffe7700ac44aba8c59ab9894e9cc38e4"
)
WORKSPACE_FILE_PROTOCOL_FIXTURE_DIGEST = (
    "sha256:56b1d2799515c84f3848d08c79b33a3297280ebef82232701612ccf3ce4488c7"
)
WORKSPACE_FILE_PROVIDER_COMMIT = "8e7436f359dd28c2afdc4b9941fd09e33fa0e470"
WORKSPACE_FILE_CLASSIFIER_IDENTITY = "cruxible.core.workspace.file"
WORKSPACE_FILE_CLASSIFIER_VERSION = 1
WORKSPACE_FILE_ENTRYPOINT = "cruxible_provider_workspace.file:WorkspaceFile"

_DIGEST_SCHEMA = {
    "type": "string",
    "required": True,
    "pattern": "^sha256:[0-9a-f]{64}$",
}
WORKSPACE_FILE_INTERFACE_PREIMAGE: dict[str, object] = {
    "interface_id": WORKSPACE_FILE_INTERFACE_ID,
    "version": 1,
    "effect_class": "pure",
    "input": {
        "logical_source": {"type": "string", "required": True},
        "commitment_digest": _DIGEST_SCHEMA,
        "content_encoding": {"type": "string", "required": True, "enum": ["base64"]},
        "bytes": {"type": "string", "required": True},
        "byte_length": {"type": "integer", "required": True, "minimum": 0},
        "bytes_digest": _DIGEST_SCHEMA,
    },
    "output": {
        "input_bucket": {"type": "string"},
        "source": {
            "type": "object",
            "properties": {
                "logical_source": {"type": "string"},
                "commitment_digest": {"type": "string"},
                "bytes_digest": {"type": "string"},
                "byte_length": {"type": "integer"},
            },
        },
        "content": {
            "type": "object",
            "one_of": [
                {
                    "kind": "text",
                    "encoding": "utf-8",
                    "bom": {"type": "boolean"},
                    "newline": {
                        "type": "string",
                        "enum": ["lf", "crlf", "cr", "mixed", "none"],
                    },
                    "trailing_newline": {"type": "boolean"},
                    "line_count": {"type": "integer"},
                    "character_count": {"type": "integer"},
                    "text": {"type": "string"},
                    "lines": {"type": "array", "items": {"type": "string"}},
                },
                {
                    "kind": "bytes",
                    "encoding": "base64",
                    "byte_length": {"type": "integer"},
                    "bytes": {"type": "string"},
                },
            ],
        },
    },
    "refusals": ["invalid_parameter", "mismatched_lengths", "provider_declined"],
}


def _vocabulary() -> ProviderBucketVocabularyV1:
    return ProviderBucketVocabularyV1(
        interface_id=WORKSPACE_FILE_INTERFACE_ID,
        status="accepted",
        description=(
            "Structure the bytes of one authorized workspace file read into a capture body. "
            "The two dimensions separate the text path (a UTF-8 decode, a line view) from "
            "the opaque-bytes path, and size the payload so a claim over a large file is "
            "visibly a different bucket from a claim over a small one."
        ),
        dimensions=(
            ProviderBucketDimensionV1(
                name="content_kind",
                description="whether the bytes decode as text",
                classes=(
                    ProviderBucketClassV1(
                        id="text",
                        description="strict UTF-8 with no NUL byte; an empty file is text",
                    ),
                    ProviderBucketClassV1(
                        id="binary",
                        description=(
                            "anything that is not strict UTF-8, or that carries a NUL byte"
                        ),
                    ),
                ),
            ),
            ProviderBucketDimensionV1(
                name="byte_size",
                description="length of the decoded bytes",
                classes=(
                    ProviderBucketClassV1(id="tiny", description="at most 4096 bytes (4 KiB)"),
                    ProviderBucketClassV1(id="small", description="4097 to 65536 bytes (64 KiB)"),
                    ProviderBucketClassV1(
                        id="medium", description="65537 to 1048576 bytes (1 MiB)"
                    ),
                    ProviderBucketClassV1(
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


def _content_kind(data: bytes) -> str:
    if b"\x00" in data:
        return "binary"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "binary"
    return "text"


def _byte_size(length: int) -> str:
    for name, ceiling in (("tiny", 4_096), ("small", 65_536), ("medium", 1_048_576)):
        if length <= ceiling:
            return name
    return "large"


def _fixture(fixture_id: str, data: bytes) -> ProviderBucketConformanceFixtureV1:
    bucket = f"content_kind={_content_kind(data)};byte_size={_byte_size(len(data))}"
    return ProviderBucketConformanceFixtureV1(
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
            ProviderBucketConformanceFixtureProofV1(
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
    """Core conformance double; it receives bounded bytes, never a locator."""

    classifier_identity = WORKSPACE_FILE_CLASSIFIER_IDENTITY
    classifier_version = WORKSPACE_FILE_CLASSIFIER_VERSION
    classifier_digest = WORKSPACE_FILE_CLASSIFIER_DIGEST

    def classify(self, canonical_input: CanonicalValue) -> str:
        if not isinstance(canonical_input, dict):
            raise ValueError("workspace.file classifier input must be an object")
        if canonical_input.get("content_encoding") != "base64":
            raise ValueError("workspace.file classifier requires base64 bytes")
        encoded = canonical_input.get("bytes")
        if not isinstance(encoded, str):
            raise ValueError("workspace.file classifier requires bytes")
        try:
            data = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError) as exc:
            raise ValueError("workspace.file classifier requires canonical base64") from exc
        return f"content_kind={_content_kind(data)};byte_size={_byte_size(len(data))}"


@dataclass(frozen=True)
class WorkspaceFileSeedManifestV1:
    materialization_source: Literal["local", "registry"]
    provider_commit: str
    protocol_fixture_digest: str
    wheel_digest: str
    implementation_digest: str
    lock_digest: str
    materialization_digests: tuple[tuple[str, str], ...]


WORKSPACE_FILE_SEED_MANIFEST = WorkspaceFileSeedManifestV1(
    materialization_source="local",
    provider_commit=WORKSPACE_FILE_PROVIDER_COMMIT,
    protocol_fixture_digest=WORKSPACE_FILE_PROTOCOL_FIXTURE_DIGEST,
    wheel_digest=WORKSPACE_FILE_WHEEL_DIGEST,
    implementation_digest=WORKSPACE_FILE_IMPLEMENTATION_DIGEST,
    lock_digest=WORKSPACE_FILE_LOCK_DIGEST,
    materialization_digests=(
        (
            "linux-cp311",
            "sha256:c82171e90b55633d85d5da601ba9ad7125b246917df3a265df5a54a21e676443",
        ),
        (
            "linux-cp312",
            "sha256:1584850eca8b6001f4df174f96137696634e05326128eba76479cdfd26af370c",
        ),
        (
            "macos-arm-cp312",
            "sha256:1684ea14402849ae498001254aa4a3598c78512b59eb7a2e5954e9fc4edf5dea",
        ),
    ),
)


def workspace_file_interface_registration(
    *, lifecycle: ArtifactLifecycle = ArtifactLifecycle()
) -> ProviderInterfaceRegistrationV1:
    interface_bytes = canonical_bytes(WORKSPACE_FILE_INTERFACE_PREIMAGE)
    vocabulary_bytes = canonical_bytes(_vocabulary().model_dump(mode="json"))
    return ProviderInterfaceRegistrationV1(
        identity=ArtifactIdentity(kind="ProviderInterface", name=WORKSPACE_FILE_INTERFACE_ID),
        interface_id=WORKSPACE_FILE_INTERFACE_ID,
        interface_bytes_hex=interface_bytes.hex(),
        interface_digest_domain=WORKSPACE_FILE_INTERFACE_DOMAIN,
        interface_digest=provider_external_interface_definition_digest(
            interface_bytes.hex(), domain=WORKSPACE_FILE_INTERFACE_DOMAIN
        ),
        vocabulary_bytes_hex=vocabulary_bytes.hex(),
        vocabulary_digest=provider_bucket_vocabulary_digest(vocabulary_bytes.hex()),
        classifier_identity=WORKSPACE_FILE_CLASSIFIER_IDENTITY,
        classifier_version=WORKSPACE_FILE_CLASSIFIER_VERSION,
        classifier_digest=WORKSPACE_FILE_CLASSIFIER_DIGEST,
        conformance_fixture_set_digest=WORKSPACE_FILE_FIXTURE_SET_DIGEST,
        conformance_proofs=WORKSPACE_FILE_CONFORMANCE_PROOFS,
        effect_class="none",
        lifecycle=lifecycle,
    )


def workspace_file_provider(
    *,
    interface_artifact_digest: str,
    lifecycle: ArtifactLifecycle = ArtifactLifecycle(),
) -> ProviderV2:
    selectors = tuple(proof.selector for proof in WORKSPACE_FILE_CONFORMANCE_PROOFS)
    implementation = ProviderImplementationManifestV1(
        interface_id=WORKSPACE_FILE_INTERFACE_ID,
        interface_digest=WORKSPACE_FILE_INTERFACE_DIGEST,
        entrypoint=WORKSPACE_FILE_ENTRYPOINT,
        backends=("local_env",),
        declared_input_buckets=selectors,
        bucket_conformance={
            proof.selector: proof.fixture_id for proof in WORKSPACE_FILE_CONFORMANCE_PROOFS
        },
        declared_endpoints=(),
        capture_contract_families=("workspace.file.capture.v1",),
        deterministic=True,
        side_effects=False,
    )
    manifest = ProviderRuntimeManifestV1(
        provider_id=WORKSPACE_FILE_PROVIDER_ID,
        distribution=ProviderDistributionRefV1(name="cruxible-provider-workspace", version="0.1.0"),
        supported_protocol_majors=(1,),
        implementations=(implementation,),
    )
    runtime_artifact = ProviderRuntimeArtifactPayloadV1(
        provider_id=WORKSPACE_FILE_PROVIDER_ID,
        status="accepted",
        manifest=manifest,
        manifest_digest=provider_manifest_digest(manifest),
        distribution=ProviderLocalDistributionPinV1(
            name="cruxible-provider-workspace",
            version="0.1.0",
            filename="cruxible_provider_workspace-0.1.0-py3-none-any.whl",
            sha256=WORKSPACE_FILE_WHEEL_DIGEST,
        ),
        local_env=ProviderLocalEnvBackendPinV1(
            lock_sha256=WORKSPACE_FILE_LOCK_DIGEST,
            materialization_digests=dict(WORKSPACE_FILE_SEED_MANIFEST.materialization_digests),
        ),
    )
    provider = ProviderV2(
        identity=ArtifactIdentity(kind="Provider", name=WORKSPACE_FILE_PROVIDER_ID),
        control_domain=WORKSPACE_FILE_PROVIDER_ID,
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
    if provider.implementations[0].implementation_digest != WORKSPACE_FILE_IMPLEMENTATION_DIGEST:
        raise RuntimeError("workspace.file seed implementation digest drifted")
    return provider


__all__ = [
    "WORKSPACE_FILE_FIXTURES",
    "WORKSPACE_FILE_IMPLEMENTATION_DIGEST",
    "WORKSPACE_FILE_INTERFACE_DIGEST",
    "WORKSPACE_FILE_INTERFACE_ID",
    "WORKSPACE_FILE_PROTOCOL_FIXTURE_DIGEST",
    "WORKSPACE_FILE_PROVIDER_ID",
    "WORKSPACE_FILE_SEED_MANIFEST",
    "WorkspaceFileBucketClassifier",
    "workspace_file_interface_registration",
    "workspace_file_provider",
]
