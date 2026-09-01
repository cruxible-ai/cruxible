"""Accepted provider identities and their externally held signing keys."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Literal, TypeAlias
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import (
    CURRENT_ARTIFACT_CODEC,
    ArtifactCodec,
    ArtifactDigest,
    artifact_bytes_for_path,
    artifact_path_matches,
    canonical_bytes,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.semantic import SemanticAddress

_PROVIDER_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_KEY_ID_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_PUBLIC_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_DYNAMIC_ENDPOINT_FORMS = frozenset({"dynamic:target-from-run-input"})


class ProviderFormatError(PlaybillFormatError):
    """A Provider artifact or canonical path is invalid."""


class _StrictProviderModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderSigningKeyV1(_StrictProviderModel):
    """One public key interval; private custody is never a Provider field."""

    tag: Literal["playbill-provider-signing-key-v1"] = "playbill-provider-signing-key-v1"
    key_id: str
    algorithm: Literal["ed25519"] = "ed25519"
    public_key: str
    valid_from: datetime
    valid_until: datetime | None = None
    status: Literal["active", "revoked"] = "active"

    @field_validator("key_id")
    @classmethod
    def _key_id(cls, value: str) -> str:
        if not _KEY_ID_RE.fullmatch(value):
            raise ValueError("Provider key_id must be a canonical identifier")
        return value

    @field_validator("public_key")
    @classmethod
    def _public_key(cls, value: str) -> str:
        if not _PUBLIC_KEY_RE.fullmatch(value):
            raise ValueError("Provider public_key must contain 32 bytes of lowercase hex")
        return value

    @model_validator(mode="after")
    def _interval(self) -> "ProviderSigningKeyV1":
        if self.valid_from.tzinfo is None or self.valid_from.utcoffset() is None:
            raise ValueError("Provider key validity must be timezone-aware")
        if self.valid_until is not None:
            if self.valid_until.tzinfo is None or self.valid_until.utcoffset() is None:
                raise ValueError("Provider key validity must be timezone-aware")
            if self.valid_until <= self.valid_from:
                raise ValueError("Provider key validity interval must be increasing")
        return self

    def active_at(self, moment: datetime) -> bool:
        return (
            self.status == "active"
            and moment >= self.valid_from
            and (self.valid_until is None or moment < self.valid_until)
        )


def _pin_key(pin: ArtifactPin) -> tuple[bytes, bytes]:
    return pin.role.encode("utf-8"), pin.target.qualified.encode("utf-8")


class ProviderV1(_StrictProviderModel):
    """Governed provider identity, control domain, and verification material."""

    artifact_format: Literal["playbill-provider-v1"] = "playbill-provider-v1"
    identity: ArtifactIdentity
    control_domain: str
    upstream_provenance: tuple[ArtifactIdentity, ...] = ()
    signing_keys: tuple[ProviderSigningKeyV1, ...]
    capture_contract_digests: tuple[str, ...]
    pins: tuple[ArtifactPin, ...] = ()
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @field_validator("control_domain")
    @classmethod
    def _control_domain(cls, value: str) -> str:
        if not _PROVIDER_NAME_RE.fullmatch(value):
            raise ValueError("Provider control_domain must be a canonical identifier")
        return value

    @field_validator("upstream_provenance")
    @classmethod
    def _upstream(cls, value: tuple[ArtifactIdentity, ...]) -> tuple[ArtifactIdentity, ...]:
        keys = tuple(item.qualified for item in value)
        if keys != tuple(sorted(set(keys), key=lambda item: item.encode("utf-8"))):
            raise ValueError("Provider upstream provenance must be sorted and unique")
        return value

    @field_validator("signing_keys")
    @classmethod
    def _keys(cls, value: tuple[ProviderSigningKeyV1, ...]) -> tuple[ProviderSigningKeyV1, ...]:
        ids = tuple(item.key_id for item in value)
        if not value or ids != tuple(sorted(set(ids), key=lambda item: item.encode("utf-8"))):
            raise ValueError("Provider signing keys must be nonempty, sorted, and unique")
        public_keys = tuple(item.public_key for item in value)
        if len(public_keys) != len(set(public_keys)):
            raise ValueError("Provider public keys must be unique")
        return value

    @field_validator("capture_contract_digests")
    @classmethod
    def _contracts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("ascii"))):
            raise ValueError("Provider CaptureContract digests must be sorted and unique")
        for digest in value:
            ArtifactDigest.from_tagged(digest)
        return value

    @field_validator("pins")
    @classmethod
    def _pins(cls, value: tuple[ArtifactPin, ...]) -> tuple[ArtifactPin, ...]:
        if value != tuple(sorted(value, key=_pin_key)):
            raise ValueError("Provider pins must be canonically sorted")
        keys = tuple((item.role, item.target.qualified) for item in value)
        if len(keys) != len(set(keys)):
            raise ValueError("Provider pins must be unique by role and target")
        return value

    @model_validator(mode="after")
    def _identity(self) -> "ProviderV1":
        if self.identity.kind != "Provider" or not _PROVIDER_NAME_RE.fullmatch(self.identity.name):
            raise ValueError("Provider identity must be path-addressable")
        return self

    def require_key(self, key_id: str, *, at: datetime) -> ProviderSigningKeyV1:
        for key in self.signing_keys:
            if key.key_id == key_id and key.active_at(at):
                return key
        raise ProviderFormatError("Provider signing key is absent, expired, or revoked")


def _sha256(value: str, *, label: str = "digest") -> str:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"Provider {label} must be canonical sha256:<lowercase-hex>")
    return value


def _external_domain_digest(domain: str, payload: Mapping[str, object]) -> str:
    """Reproduce the provider-runtime domain || NUL || canonical-JSON rule."""

    hasher = hashlib.sha256()
    hasher.update(domain.encode("utf-8"))
    hasher.update(b"\x00")
    hasher.update(canonical_bytes(dict(payload)))
    return f"sha256:{hasher.hexdigest()}"


class ProviderDistributionRefV1(_StrictProviderModel):
    name: str
    version: str


class ProviderImplementationManifestV1(_StrictProviderModel):
    interface_id: str
    interface_digest: str
    entrypoint: str
    backends: tuple[Literal["local_env", "container"], ...]
    declared_input_buckets: tuple[str, ...]
    bucket_conformance: dict[str, str] = Field(default_factory=dict)
    declared_endpoints: tuple[str, ...] = ()
    capture_contract_families: tuple[str, ...] = ()
    requires_extras: tuple[str, ...] = ()
    deterministic: bool
    side_effects: bool

    _interface_digest = field_validator("interface_digest")(_sha256)

    @field_validator("entrypoint")
    @classmethod
    def _entrypoint(cls, value: str) -> str:
        module, separator, object_name = value.partition(":")
        if not separator or not module or not object_name:
            raise ValueError("Provider entrypoint must be module:object")
        return value

    @field_validator("backends")
    @classmethod
    def _backends(
        cls,
        value: tuple[Literal["local_env", "container"], ...],
    ) -> tuple[Literal["local_env", "container"], ...]:
        if not value or len(set(value)) != len(value):
            raise ValueError("Provider implementation backends must be nonempty and unique")
        return value

    @field_validator("declared_input_buckets")
    @classmethod
    def _buckets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("Provider implementation must declare input buckets")
        return value

    @field_validator("requires_extras")
    @classmethod
    def _extras(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(not item or item.strip() != item for item in value):
            raise ValueError("Provider implementation extras must be nonempty and unique")
        return value

    @field_validator("declared_endpoints")
    @classmethod
    def _endpoints(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for entry in value:
            if entry in _DYNAMIC_ENDPOINT_FORMS:
                continue
            if entry.startswith("dynamic:"):
                raise ValueError("Provider manifest declares an unknown dynamic endpoint form")
            candidate = entry.strip()
            if "//" not in candidate:
                candidate = f"https://{candidate}"
            parts = urlsplit(candidate)
            if not parts.hostname:
                raise ValueError("Provider manifest endpoint has no host")
            # Accessing the parsed port reproduces the runtime's rejection of
            # malformed and out-of-range port spellings.
            _ = parts.port
        return value


class ProviderRuntimeManifestV1(_StrictProviderModel):
    schema_version: Literal[1] = 1
    provider_id: str
    distribution: ProviderDistributionRefV1
    entrypoint_group: Literal["cruxible.providers"] = "cruxible.providers"
    supported_protocol_majors: tuple[int, ...]
    implementations: tuple[ProviderImplementationManifestV1, ...]

    @field_validator("supported_protocol_majors")
    @classmethod
    def _protocols(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value:
            raise ValueError("Provider manifest must declare a protocol major")
        return value

    @field_validator("implementations")
    @classmethod
    def _implementations(
        cls,
        value: tuple[ProviderImplementationManifestV1, ...],
    ) -> tuple[ProviderImplementationManifestV1, ...]:
        keys = tuple((item.interface_id, item.entrypoint) for item in value)
        if not value or len(keys) != len(set(keys)):
            raise ValueError("Provider manifest implementations must be nonempty and unique")
        return value


def provider_manifest_digest(manifest: ProviderRuntimeManifestV1) -> str:
    return _external_domain_digest(
        "cruxible.provider.manifest.v1",
        manifest.model_dump(mode="json"),
    )


class ProviderDistributionPinV1(_StrictProviderModel):
    name: str
    version: str
    filename: str
    sha256: str
    index_url: str
    url: str

    _distribution_digest = field_validator("sha256")(_sha256)

    @field_validator("filename")
    @classmethod
    def _filename(cls, value: str) -> str:
        if (
            not value
            or value in {".", ".."}
            or value.startswith(".")
            or any(character in value for character in ("/", "\\", ":", "\x00"))
        ):
            raise ValueError("Provider distribution filename must be a plain filename")
        return value


class ProviderImageProvenanceV1(_StrictProviderModel):
    provider_artifact_digest: str
    materialization_digest: str
    base_image_digest: str
    builder_identity: str

    _digests = field_validator(
        "provider_artifact_digest",
        "materialization_digest",
        "base_image_digest",
    )(_sha256)


class ProviderContainerBackendPinV1(_StrictProviderModel):
    image_reference: str
    image_digest: str
    provenance: ProviderImageProvenanceV1

    _image_digest = field_validator("image_digest")(_sha256)


class ProviderLocalEnvBackendPinV1(_StrictProviderModel):
    lock_sha256: str
    materialization_digests: dict[str, str]

    _lock_digest = field_validator("lock_sha256")(_sha256)

    @field_validator("materialization_digests")
    @classmethod
    def _materializations(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError("Provider local environment pin must cover an environment")
        for pin_key, digest in value.items():
            if not pin_key:
                raise ValueError("Provider environment pin key must be nonempty")
            _sha256(digest, label=f"materialization digest for {pin_key!r}")
        return value


class ProviderRuntimeArtifactPayloadV1(_StrictProviderModel):
    schema_version: Literal[1] = 1
    provider_id: str
    status: Literal["proposed", "accepted"] = "proposed"
    manifest: ProviderRuntimeManifestV1
    manifest_digest: str
    distribution: ProviderDistributionPinV1
    local_env: ProviderLocalEnvBackendPinV1 | None = None
    container: ProviderContainerBackendPinV1 | None = None

    _manifest_digest = field_validator("manifest_digest")(_sha256)


def provider_runtime_artifact_digest(payload: ProviderRuntimeArtifactPayloadV1) -> str:
    document = payload.model_dump(mode="json")
    document.pop("status", None)
    container = document.get("container")
    if isinstance(container, dict):
        provenance = container.get("provenance")
        if isinstance(provenance, dict):
            provenance.pop("provider_artifact_digest", None)
    return _external_domain_digest("cruxible.provider.artifact.v1", document)


def provider_implementation_digest(
    *,
    interface_id: str,
    interface_digest: str,
    entrypoint: str,
    distribution_sha256: str,
) -> str:
    _sha256(interface_digest, label="interface digest")
    _sha256(distribution_sha256, label="distribution digest")
    return _external_domain_digest(
        "cruxible.provider.implementation.v1",
        {
            "interface_id": interface_id,
            "interface_digest": interface_digest,
            "entrypoint": entrypoint,
            "distribution_sha256": distribution_sha256,
        },
    )


class ProviderLocalMaterializationReferenceV1(_StrictProviderModel):
    tag: Literal["playbill-provider-local-materialization-reference-v1"] = (
        "playbill-provider-local-materialization-reference-v1"
    )
    kind: Literal["local_env"] = "local_env"
    environment_pin_key: str
    materialization_digest: str

    _materialization_digest = field_validator("materialization_digest")(_sha256)


class ProviderContainerMaterializationReferenceV1(_StrictProviderModel):
    tag: Literal["playbill-provider-container-materialization-reference-v1"] = (
        "playbill-provider-container-materialization-reference-v1"
    )
    kind: Literal["container"] = "container"
    image_reference: str
    image_digest: str
    materialization_digest: str

    _digests = field_validator("image_digest", "materialization_digest")(_sha256)

    @model_validator(mode="after")
    def _container_identity(self) -> "ProviderContainerMaterializationReferenceV1":
        if self.materialization_digest != self.image_digest:
            raise ValueError("Provider container materialization is its exact image digest")
        return self


ProviderMaterializationReferenceV1: TypeAlias = Annotated[
    ProviderLocalMaterializationReferenceV1 | ProviderContainerMaterializationReferenceV1,
    Field(discriminator="kind"),
]


def _backend_key(value: str) -> int:
    return {"local_env": 0, "container": 1}[value]


def _materialization_key(
    reference: ProviderMaterializationReferenceV1,
) -> tuple[int, bytes, bytes]:
    name = (
        reference.environment_pin_key
        if isinstance(reference, ProviderLocalMaterializationReferenceV1)
        else reference.image_reference
    )
    return (
        _backend_key(reference.kind),
        name.encode("utf-8"),
        reference.materialization_digest.encode("ascii"),
    )


class ProviderImplementationRecordV1(_StrictProviderModel):
    tag: Literal["playbill-provider-implementation-v1"] = "playbill-provider-implementation-v1"
    interface_id: str
    interface_digest: str
    entrypoint: str
    implementation_digest: str
    backend_kinds: tuple[Literal["local_env", "container"], ...]
    materialization_references: tuple[ProviderMaterializationReferenceV1, ...]

    _digests = field_validator("interface_digest", "implementation_digest")(_sha256)

    @field_validator("backend_kinds")
    @classmethod
    def _backend_kinds(
        cls,
        value: tuple[Literal["local_env", "container"], ...],
    ) -> tuple[Literal["local_env", "container"], ...]:
        if value != tuple(sorted(set(value), key=_backend_key)) or not value:
            raise ValueError("Provider backend kinds must be canonically sorted and unique")
        return value

    @field_validator("materialization_references")
    @classmethod
    def _references(
        cls,
        value: tuple[ProviderMaterializationReferenceV1, ...],
    ) -> tuple[ProviderMaterializationReferenceV1, ...]:
        if value != tuple(sorted(value, key=_materialization_key)):
            raise ValueError("Provider materialization references must be canonically sorted")
        keys = tuple((item.kind, _materialization_key(item)[1]) for item in value)
        if len(keys) != len(set(keys)):
            raise ValueError("Provider materialization references must be unique")
        return value


def _eligible_local_pin_keys(
    local_env: ProviderLocalEnvBackendPinV1,
    *,
    extras: tuple[str, ...],
) -> tuple[str, ...]:
    expected = tuple(sorted(extras, key=lambda item: item.encode("utf-8")))
    return tuple(
        sorted(
            (
                pin_key
                for pin_key in local_env.materialization_digests
                if tuple(sorted(pin_key.split("+")[1:], key=lambda item: item.encode("utf-8")))
                == expected
            ),
            key=lambda item: item.encode("utf-8"),
        )
    )


def provider_expected_implementation_records(
    payload: ProviderRuntimeArtifactPayloadV1,
) -> tuple[ProviderImplementationRecordV1, ...]:
    records: list[ProviderImplementationRecordV1] = []
    for manifest in payload.manifest.implementations:
        implementation_digest = provider_implementation_digest(
            interface_id=manifest.interface_id,
            interface_digest=manifest.interface_digest,
            entrypoint=manifest.entrypoint,
            distribution_sha256=payload.distribution.sha256,
        )
        references: list[ProviderMaterializationReferenceV1] = []
        if "local_env" in manifest.backends:
            if payload.local_env is None:
                raise ValueError("backend_pin_missing: local_env")
            pin_keys = _eligible_local_pin_keys(payload.local_env, extras=manifest.requires_extras)
            if not pin_keys:
                raise ValueError("materialization_reference_missing: local_env")
            references.extend(
                ProviderLocalMaterializationReferenceV1(
                    environment_pin_key=pin_key,
                    materialization_digest=payload.local_env.materialization_digests[pin_key],
                )
                for pin_key in pin_keys
            )
        if "container" in manifest.backends:
            if payload.container is None:
                raise ValueError("backend_pin_missing: container")
            references.append(
                ProviderContainerMaterializationReferenceV1(
                    image_reference=payload.container.image_reference,
                    image_digest=payload.container.image_digest,
                    materialization_digest=payload.container.image_digest,
                )
            )
        records.append(
            ProviderImplementationRecordV1(
                interface_id=manifest.interface_id,
                interface_digest=manifest.interface_digest,
                entrypoint=manifest.entrypoint,
                implementation_digest=implementation_digest,
                backend_kinds=tuple(sorted(manifest.backends, key=_backend_key)),
                materialization_references=tuple(sorted(references, key=_materialization_key)),
            )
        )
    return tuple(
        sorted(
            records,
            key=lambda item: (
                item.interface_id.encode("utf-8"),
                item.implementation_digest.encode("ascii"),
            ),
        )
    )


class ProviderV2(ProviderV1):
    """Governed Provider successor carrying the exact runtime mirror and rows."""

    artifact_format: Literal["playbill-provider-v2"] = "playbill-provider-v2"  # type: ignore[assignment]
    runtime_artifact: ProviderRuntimeArtifactPayloadV1
    implementations: tuple[ProviderImplementationRecordV1, ...]

    @field_validator("implementations")
    @classmethod
    def _implementation_order(
        cls,
        value: tuple[ProviderImplementationRecordV1, ...],
    ) -> tuple[ProviderImplementationRecordV1, ...]:
        expected = tuple(
            sorted(
                value,
                key=lambda item: (
                    item.interface_id.encode("utf-8"),
                    item.implementation_digest.encode("ascii"),
                ),
            )
        )
        if value != expected or len({item.implementation_digest for item in value}) != len(value):
            raise ValueError("Provider implementations must be canonically sorted and unique")
        return value

    @model_validator(mode="after")
    def _runtime_correspondence(self) -> "ProviderV2":
        payload = self.runtime_artifact
        if (
            payload.provider_id != self.identity.name
            or payload.manifest.provider_id != self.identity.name
        ):
            raise ValueError(
                "manifest_divergence: Provider identity does not match runtime payload"
            )
        if payload.manifest_digest != provider_manifest_digest(payload.manifest):
            raise ValueError("manifest_divergence: manifest digest does not reproduce")
        if (
            payload.distribution.name != payload.manifest.distribution.name
            or payload.distribution.version != payload.manifest.distribution.version
        ):
            raise ValueError("manifest_divergence: distribution identity does not reproduce")
        expected = provider_expected_implementation_records(payload)
        if self.implementations != expected:
            raise ValueError("implementation_digest_mismatch: implementation table diverges")
        if payload.container is not None:
            external_digest = provider_runtime_artifact_digest(payload)
            provenance = payload.container.provenance
            if provenance.provider_artifact_digest != external_digest:
                raise ValueError("manifest_divergence: external Provider artifact digest diverges")
            if provenance.materialization_digest != payload.container.image_digest:
                raise ValueError("materialization_reference_missing: container provenance")
        return self


ProviderAny: TypeAlias = Annotated[
    ProviderV1 | ProviderV2,
    Field(discriminator="artifact_format"),
]
_PROVIDER_ADAPTER: TypeAdapter[ProviderAny] = TypeAdapter(ProviderAny)


def provider_path(name: str) -> str:
    if not _PROVIDER_NAME_RE.fullmatch(name):
        raise ProviderFormatError("Provider identity is not path-addressable")
    return f"providers/{name}.json"


def render_provider(provider: ProviderAny) -> bytes:
    return pretty_canonical_bytes(provider.model_dump(mode="json"))


def parse_provider(
    content: bytes,
    *,
    path: str,
    codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC,
) -> ProviderAny:
    try:
        provider = _PROVIDER_ADAPTER.validate_python(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProviderFormatError("Provider failed strict versioned validation") from exc
    if not artifact_path_matches(provider_path(provider.identity.name), path, codec=codec):
        raise ProviderFormatError("Provider identity/path disagreement")
    if artifact_bytes_for_path(render_provider(provider), path, codec=codec) != content:
        raise ProviderFormatError("Provider is not in canonical wire form")
    return provider


def provider_digest(provider: ProviderAny) -> ArtifactDigest:
    if isinstance(provider, ProviderV2):
        return typed_digest(
            ArtifactDigest,
            "playbill-provider-v2",
            provider.model_dump(mode="json"),
        )
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        provider.model_dump(mode="json"),
    )


class AcceptedProviderV1(_StrictProviderModel):
    path: str
    provider: ProviderAny
    artifact_digest: str

    @model_validator(mode="after")
    def _correspondence(self) -> "AcceptedProviderV1":
        if self.path != provider_path(self.provider.identity.name):
            raise ValueError("accepted Provider path does not reproduce")
        if self.artifact_digest != provider_digest(self.provider).tagged:
            raise ValueError("accepted Provider digest does not reproduce")
        return self


class ProviderLawResultV1(_StrictProviderModel):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    required_tier: PermissionTier | None = None
    approval_scope: tuple[str, ...] = ()
    diagnostics: tuple[CompilerDiagnostic, ...] = ()


def _law_refusal(code: str, message: str, *, path: str) -> ProviderLawResultV1:
    return ProviderLawResultV1(
        verdict="refused",
        diagnostics=(
            CompilerDiagnostic(
                code=code,
                severity="error",
                message=message,
                subject=SemanticAddress.whole_artifact(path),
            ),
        ),
    )


def evaluate_provider_law(
    provider: ProviderAny,
    *,
    path: str,
    predecessor: AcceptedProviderV1 | None,
    interface_registrations: Mapping[str, object] | None = None,
) -> ProviderLawResultV1:
    if path != provider_path(provider.identity.name):
        return _law_refusal(
            "playbill.provider.path_mismatch",
            "Provider identity/path disagreement.",
            path=path,
        )
    if predecessor is None:
        if provider.lifecycle.predecessor_digest is not None:
            return _law_refusal(
                "playbill.provider.predecessor_missing",
                "A new Provider cannot name a predecessor.",
                path=path,
            )
    else:
        if provider.identity != predecessor.provider.identity or (
            provider.control_domain != predecessor.provider.control_domain
        ):
            return _law_refusal(
                "playbill.provider.stable_identity_changed",
                "Provider identity and ultimate control domain are immutable in v1.",
                path=path,
            )
        if provider.lifecycle.predecessor_digest != predecessor.artifact_digest:
            return _law_refusal(
                "playbill.provider.predecessor_mismatch",
                "Provider successor does not pin the exact predecessor.",
                path=path,
            )
        if isinstance(predecessor.provider, ProviderV2) and not isinstance(
            provider,
            ProviderV2,
        ):
            return _law_refusal(
                "playbill.provider.wire_downgrade",
                "A Provider v2 lineage cannot be succeeded by the historical v1 wire.",
                path=path,
            )
    pinned_contracts = {
        pin.artifact_digest for pin in provider.pins if pin.role == "capture-contract"
    }
    if not set(provider.capture_contract_digests).issubset(pinned_contracts):
        return _law_refusal(
            "playbill.provider.capture_contract_pin_missing",
            "Provider CaptureContract declarations require exact governed pins.",
            path=path,
        )
    if isinstance(provider, ProviderV2):
        registrations = {} if interface_registrations is None else interface_registrations
        interface_pins = {
            pin.target.qualified: pin for pin in provider.pins if pin.role == "provider-interface"
        }
        for implementation in provider.runtime_artifact.manifest.implementations:
            identity = f"ProviderInterface:{implementation.interface_id}"
            accepted = registrations.get(identity) or registrations.get(implementation.interface_id)
            registration = getattr(accepted, "registration", accepted)
            if registration is None:
                return _law_refusal(
                    "playbill.provider.unknown_interface",
                    f"Provider interface {implementation.interface_id!r} is not accepted.",
                    path=path,
                )
            accepted_digest = getattr(accepted, "artifact_digest", None)
            interface_pin = interface_pins.get(identity)
            if interface_pin is None or interface_pin.artifact_digest != accepted_digest:
                return _law_refusal(
                    "playbill.provider.interface_pin_missing",
                    "Provider interface declarations require exact governed registration pins.",
                    path=path,
                )
            if getattr(registration, "interface_digest", None) != implementation.interface_digest:
                return _law_refusal(
                    "playbill.provider.interface_digest_mismatch",
                    "Provider manifest and governed interface digest disagree.",
                    path=path,
                )
            proof_by_selector = {
                proof.selector: proof for proof in getattr(registration, "conformance_proofs", ())
            }
            for selector in implementation.declared_input_buckets:
                proof = proof_by_selector.get(selector)
                if (
                    proof is None
                    or implementation.bucket_conformance.get(selector) != proof.fixture_id
                ):
                    return _law_refusal(
                        "playbill.provider.bucket_fixture_missing",
                        f"Provider selector {selector!r} lacks its governed conformance proof.",
                        path=path,
                    )
            expected_side_effects = getattr(registration, "effect_class", None) == (
                "external_mutation"
            )
            if implementation.side_effects != expected_side_effects:
                return _law_refusal(
                    "playbill.provider.effect_declaration_mismatch",
                    "Provider side_effects must equal the governed interface effect class.",
                    path=path,
                )
    return ProviderLawResultV1(
        verdict="accepted",
        artifact_digest=provider_digest(provider).tagged,
        required_tier="governed_write",
        approval_scope=(),
    )


__all__ = [
    "AcceptedProviderV1",
    "ProviderAny",
    "ProviderContainerBackendPinV1",
    "ProviderContainerMaterializationReferenceV1",
    "ProviderDistributionPinV1",
    "ProviderDistributionRefV1",
    "ProviderFormatError",
    "ProviderImageProvenanceV1",
    "ProviderImplementationManifestV1",
    "ProviderImplementationRecordV1",
    "ProviderLocalEnvBackendPinV1",
    "ProviderLocalMaterializationReferenceV1",
    "ProviderMaterializationReferenceV1",
    "ProviderRuntimeArtifactPayloadV1",
    "ProviderRuntimeManifestV1",
    "ProviderSigningKeyV1",
    "ProviderV1",
    "ProviderV2",
    "ProviderLawResultV1",
    "evaluate_provider_law",
    "parse_provider",
    "provider_digest",
    "provider_expected_implementation_records",
    "provider_implementation_digest",
    "provider_manifest_digest",
    "provider_path",
    "provider_runtime_artifact_digest",
    "render_provider",
]
