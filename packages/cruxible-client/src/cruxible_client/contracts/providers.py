"""Accepted provider identities and their externally held signing keys."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, ClassVar, Literal, TypeAlias
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializeAsAny,
    TypeAdapter,
    field_validator,
    model_validator,
)

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactNameRef,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    canonical_bytes,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.content_memo import ParseMemo, ValueMemo, sealed_memo
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.provider_operations import ProviderOperation, validate_operations
from cruxible_client.contracts.semantic import SemanticAddress

_PROVIDER_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_BUILD_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_DISTRIBUTION_SEPARATORS_RE = re.compile(r"[-_.]+")
#: The format of one provider build: its own artifact at
#: ``providers/<name>/<build>.json``, identity ``Provider:<name>/<build>``.
PROVIDER_BUILD_FORMAT = "cruxible-provider-v4"
_KEY_ID_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_PUBLIC_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_DYNAMIC_ENDPOINT_FORMS = frozenset({"dynamic:target-from-run-input"})


class ProviderFormatError(FormatError):
    """A Provider artifact or canonical path is invalid."""


class _StrictProviderModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderSigningKey(_StrictProviderModel):
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
    def _interval(self) -> "ProviderSigningKey":
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
    signing_keys: tuple[ProviderSigningKey, ...]
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
    def _keys(cls, value: tuple[ProviderSigningKey, ...]) -> tuple[ProviderSigningKey, ...]:
        ids = tuple(item.key_id for item in value)
        if ids != tuple(sorted(set(ids), key=lambda item: item.encode("utf-8"))):
            raise ValueError("Provider signing keys must be sorted and unique")
        public_keys = tuple(item.public_key for item in value)
        if len(public_keys) != len(set(public_keys)):
            raise ValueError("Provider public keys must be unique")
        return value

    @field_validator("capture_contract_digests", check_fields=False)
    @classmethod
    def _contracts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("ascii"))):
            raise ValueError("Provider CaptureContract digests must be sorted and unique")
        for digest in value:
            ArtifactDigest.from_tagged(digest)
        return value

    @field_validator("pins", check_fields=False)
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
        # A build names its provider and build (``name/<64 hex>``); every earlier
        # format is the single artifact of its name. ``/`` is outside the name
        # grammar, so the two spellings never collide.
        name, slash, build = self.identity.name.partition("/")
        built = self.artifact_format in {PROVIDER_BUILD_FORMAT, "cruxible-provider-v6"}
        if (
            self.identity.kind != "Provider"
            or not _PROVIDER_NAME_RE.fullmatch(name)
            or bool(slash) != built
            or (built and not _BUILD_KEY_RE.fullmatch(build))
        ):
            raise ValueError("Provider identity must be path-addressable")
        if self.artifact_format == "playbill-provider-v1" and not self.signing_keys:
            raise ValueError("Provider v1 signing keys must be nonempty")
        return self

    @property
    def provider_name(self) -> str:
        """The provider this artifact is a build of: its identity without a build key."""

        return self.identity.name.partition("/")[0]

    @property
    def build_key(self) -> str | None:
        """The 64-hex build key a build is named by; None for a single-path Provider."""

        return self.identity.name.partition("/")[2] or None

    def require_key(self, key_id: str, *, at: datetime) -> ProviderSigningKey:
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


class ProviderDistributionRef(_StrictProviderModel):
    name: str
    version: str


class ProviderImplementationManifestV1(_StrictProviderModel):
    interface_id: str
    interface_digest: str
    entrypoint: str
    backends: tuple[Literal["local_env", "container"], ...]
    # The provider-runtime 0.2.x manifest's bucket claims: inert. Core admits
    # every input the interface's contract admits and reads none of these.
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
    distribution: ProviderDistributionRef
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


class ProviderImplementationManifest(ProviderImplementationManifestV1):
    """Current endpoint vocabulary; the V1 reader remains frozen."""

    @field_validator("declared_endpoints")
    @classmethod
    def _endpoints(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        ProviderImplementationManifestV1._endpoints(
            tuple(item for item in value if item != "dynamic:target-from-configuration")
        )
        return value


class ProviderRuntimeManifest(ProviderRuntimeManifestV1):
    implementations: tuple[ProviderImplementationManifest, ...]


def provider_manifest_digest(manifest: ProviderRuntimeManifestV1) -> str:
    return _external_domain_digest(
        "cruxible.provider.manifest.v1",
        manifest.model_dump(mode="json"),
    )


class ProviderDistributionPin(_StrictProviderModel):
    materialization_source: Literal["registry"] = Field(
        default="registry",
        exclude_if=lambda value: value == "registry",
    )
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


class ProviderLocalDistributionPin(_StrictProviderModel):
    """Exact wheel identity whose materialization comes from operator custody."""

    materialization_source: Literal["local"] = "local"
    name: str
    version: str
    filename: str
    sha256: str

    _distribution_digest = field_validator("sha256")(_sha256)

    @field_validator("filename")
    @classmethod
    def _filename(cls, value: str) -> str:
        return ProviderDistributionPin._filename(value)


class ProviderImageProvenance(_StrictProviderModel):
    provider_artifact_digest: str
    materialization_digest: str
    base_image_digest: str
    builder_identity: str

    _digests = field_validator(
        "provider_artifact_digest",
        "materialization_digest",
        "base_image_digest",
    )(_sha256)


class ProviderContainerBackendPin(_StrictProviderModel):
    image_reference: str
    image_digest: str
    provenance: ProviderImageProvenance

    _image_digest = field_validator("image_digest")(_sha256)


class ProviderLocalEnvBackendPin(_StrictProviderModel):
    lock_sha256: str
    materialization_digests: dict[str, str]

    _lock_digest = field_validator("lock_sha256")(_sha256)

    @field_validator("materialization_digests")
    @classmethod
    def _materializations(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError("Provider local environment pin must cover an environment")
        for pin_key, digest in value.items():
            parts = pin_key.split("+")
            extras = tuple(parts[1:])
            if not all(re.fullmatch(r"[a-z0-9][a-z0-9._-]*", part) for part in parts):
                raise ValueError("Provider environment pin key has invalid grammar")
            if extras != tuple(sorted(set(extras), key=lambda item: item.encode("utf-8"))):
                raise ValueError("Provider environment pin key extras must be sorted and unique")
            _sha256(digest, label=f"materialization digest for {pin_key!r}")
        return value


class ProviderRuntimeArtifactPayloadV1(_StrictProviderModel):
    schema_version: Literal[1] = 1
    provider_id: str
    status: Literal["proposed", "accepted"] = "proposed"
    manifest: ProviderRuntimeManifestV1
    manifest_digest: str
    distribution: ProviderDistributionPin | ProviderLocalDistributionPin
    local_env: ProviderLocalEnvBackendPin | None = None
    container: ProviderContainerBackendPin | None = None

    _manifest_digest = field_validator("manifest_digest")(_sha256)

    @field_validator("distribution", mode="before")
    @classmethod
    def _explicit_local_distribution(cls, value: object) -> object:
        if isinstance(value, Mapping) and "materialization_source" not in value:
            # Missing discriminators are legacy registry payloads. Local source
            # custody is never selected merely because a registry URL is absent.
            return {**value, "materialization_source": "registry"}
        return value


class ProviderRuntimeArtifactPayload(ProviderRuntimeArtifactPayloadV1):
    """Advertised manifest with pins only for prepared execution backends."""

    schema_version: Literal[2] = 2  # type: ignore[assignment]
    manifest: ProviderRuntimeManifest


def provider_runtime_artifact_digest(payload: ProviderRuntimeArtifactPayloadV1) -> str:
    document = payload.model_dump(mode="json")
    document.pop("status", None)
    container = document.get("container")
    if isinstance(container, dict):
        provenance = container.get("provenance")
        if isinstance(provenance, dict):
            provenance.pop("provider_artifact_digest", None)
    return _external_domain_digest(
        "cruxible.provider.artifact.v2"
        if isinstance(payload, ProviderRuntimeArtifactPayload)
        else "cruxible.provider.artifact.v1",
        document,
    )


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


class ProviderLocalMaterializationReference(_StrictProviderModel):
    tag: Literal["playbill-provider-local-materialization-reference-v1"] = (
        "playbill-provider-local-materialization-reference-v1"
    )
    kind: Literal["local_env"] = "local_env"
    environment_pin_key: str
    materialization_digest: str

    _materialization_digest = field_validator("materialization_digest")(_sha256)


class ProviderContainerMaterializationReference(_StrictProviderModel):
    tag: Literal["playbill-provider-container-materialization-reference-v1"] = (
        "playbill-provider-container-materialization-reference-v1"
    )
    kind: Literal["container"] = "container"
    image_reference: str
    image_digest: str
    materialization_digest: str

    _digests = field_validator("image_digest", "materialization_digest")(_sha256)

    @model_validator(mode="after")
    def _container_identity(self) -> "ProviderContainerMaterializationReference":
        if self.materialization_digest != self.image_digest:
            raise ValueError("Provider container materialization is its exact image digest")
        return self


ProviderMaterializationReference: TypeAlias = Annotated[
    ProviderLocalMaterializationReference | ProviderContainerMaterializationReference,
    Field(discriminator="kind"),
]


def _backend_key(value: str) -> int:
    return {"local_env": 0, "container": 1}[value]


def _materialization_key(
    reference: ProviderMaterializationReference,
) -> tuple[int, bytes, bytes]:
    name = (
        reference.environment_pin_key
        if isinstance(reference, ProviderLocalMaterializationReference)
        else reference.image_reference
    )
    return (
        _backend_key(reference.kind),
        name.encode("utf-8"),
        reference.materialization_digest.encode("ascii"),
    )


class ProviderImplementationRecord(_StrictProviderModel):
    tag: Literal["playbill-provider-implementation-v1"] = "playbill-provider-implementation-v1"
    interface_id: str
    interface_digest: str
    entrypoint: str
    implementation_digest: str
    backend_kinds: tuple[Literal["local_env", "container"], ...]
    materialization_references: tuple[ProviderMaterializationReference, ...]

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
        value: tuple[ProviderMaterializationReference, ...],
    ) -> tuple[ProviderMaterializationReference, ...]:
        if value != tuple(sorted(value, key=_materialization_key)):
            raise ValueError("Provider materialization references must be canonically sorted")
        keys = tuple((item.kind, _materialization_key(item)[1]) for item in value)
        if len(keys) != len(set(keys)):
            raise ValueError("Provider materialization references must be unique")
        return value


def _eligible_local_pin_keys(
    local_env: ProviderLocalEnvBackendPin,
    *,
    extras: tuple[str, ...],
    allow_superset: bool = False,
) -> tuple[str, ...]:
    expected = tuple(sorted(extras, key=lambda item: item.encode("utf-8")))
    return tuple(
        sorted(
            (
                pin_key
                for pin_key in local_env.materialization_digests
                if (
                    set(expected) <= set(pin_key.split("+")[1:])
                    if allow_superset
                    else tuple(
                        sorted(pin_key.split("+")[1:], key=lambda item: item.encode("utf-8"))
                    )
                    == expected
                )
            ),
            key=lambda item: item.encode("utf-8"),
        )
    )


def provider_expected_implementation_records(
    payload: ProviderRuntimeArtifactPayloadV1,
) -> tuple[ProviderImplementationRecord, ...]:
    records: list[ProviderImplementationRecord] = []
    for manifest in payload.manifest.implementations:
        implementation_digest = provider_implementation_digest(
            interface_id=manifest.interface_id,
            interface_digest=manifest.interface_digest,
            entrypoint=manifest.entrypoint,
            distribution_sha256=payload.distribution.sha256,
        )
        references: list[ProviderMaterializationReference] = []
        available_only = isinstance(payload, ProviderRuntimeArtifactPayload)
        if "local_env" in manifest.backends and (
            payload.local_env is not None or not available_only
        ):
            if payload.local_env is None:
                raise ValueError("backend_pin_missing: local_env")
            pin_keys = _eligible_local_pin_keys(
                payload.local_env, extras=manifest.requires_extras, allow_superset=available_only
            )
            if not pin_keys and not available_only:
                raise ValueError("materialization_reference_missing: local_env")
            references.extend(
                ProviderLocalMaterializationReference(
                    environment_pin_key=pin_key,
                    materialization_digest=payload.local_env.materialization_digests[pin_key],
                )
                for pin_key in pin_keys
            )
        if "container" in manifest.backends and (
            payload.container is not None or not available_only
        ):
            if payload.container is None:
                raise ValueError("backend_pin_missing: container")
            references.append(
                ProviderContainerMaterializationReference(
                    image_reference=payload.container.image_reference,
                    image_digest=payload.container.image_digest,
                    materialization_digest=payload.container.image_digest,
                )
            )
        if available_only and not references:
            continue
        records.append(
            ProviderImplementationRecord(
                interface_id=manifest.interface_id,
                interface_digest=manifest.interface_digest,
                entrypoint=manifest.entrypoint,
                implementation_digest=implementation_digest,
                backend_kinds=tuple(
                    sorted(
                        {ref.kind for ref in references} if available_only else manifest.backends,
                        key=_backend_key,
                    )
                ),
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
    implementations: tuple[ProviderImplementationRecord, ...]
    #: What this build serves: typed operations, sorted by name. Each names a
    #: manifest entrypoint; a Line binds a Procedure slot to one of them.
    operations: tuple[ProviderOperation, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )

    _operations = field_validator("operations")(validate_operations)

    @field_validator("implementations")
    @classmethod
    def _implementation_order(
        cls,
        value: tuple[ProviderImplementationRecord, ...],
    ) -> tuple[ProviderImplementationRecord, ...]:
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
        if not self.signing_keys and not isinstance(
            payload.distribution, ProviderLocalDistributionPin
        ):
            raise ValueError("keyless Provider v2 is restricted to local materialization")
        if (
            payload.provider_id != self.provider_name
            or payload.manifest.provider_id != self.provider_name
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
        entrypoints = {item.entrypoint for item in payload.manifest.implementations}
        for operation in self.operations:
            if operation.entrypoint not in entrypoints:
                raise ValueError(
                    f"operation_entrypoint_unknown: {operation.name} names "
                    f"{operation.entrypoint!r}, which the manifest does not export"
                )
        if payload.container is not None:
            external_digest = provider_runtime_artifact_digest(payload)
            provenance = payload.container.provenance
            if provenance.provider_artifact_digest != external_digest:
                raise ValueError("manifest_divergence: external Provider artifact digest diverges")
            if provenance.materialization_digest != payload.container.image_digest:
                raise ValueError("materialization_reference_missing: container provenance")
        return self


class Provider(ProviderV2):
    """Package registration separates advertised capabilities from prepared ones."""

    artifact_format: Literal["playbill-provider-v3"] = "playbill-provider-v3"  # type: ignore[assignment]
    runtime_artifact: ProviderRuntimeArtifactPayload


def normalized_distribution_name(name: str) -> str:
    """A distribution name as PEP 503 compares it: one name however it is spelled."""

    return _DISTRIBUTION_SEPARATORS_RE.sub("-", name).lower()


def provider_build_key_of(
    *,
    provider_id: str,
    distribution_name: str,
    distribution_version: str,
    distribution_sha256: str,
    lock_sha256: str | None,
    image_digest: str | None = None,
) -> ArtifactDigest:
    """The key one build of ``provider_id`` is named by.

    It covers what the build runs -- the exact wheel and the lock (or image) it
    was built with -- and nothing a host decides: the environment a host
    materializes the lock into is left out, so one wheel and lock name the same
    build on every host, as a kit names it from its manifest.
    """

    return typed_digest(
        ArtifactDigest,
        "cruxible-provider-build-key-v1",
        {
            "provider_id": provider_id,
            "distribution": {
                "name": normalized_distribution_name(distribution_name),
                "version": distribution_version,
                "sha256": distribution_sha256,
            },
            "lock_sha256": lock_sha256,
            "image_digest": image_digest,
        },
    )


def provider_build_key(runtime: ProviderRuntimeArtifactPayloadV1) -> ArtifactDigest:
    """The build key of one runtime artifact (``provider_build_key_of``)."""

    return provider_build_key_of(
        provider_id=runtime.provider_id,
        distribution_name=runtime.distribution.name,
        distribution_version=runtime.distribution.version,
        distribution_sha256=runtime.distribution.sha256,
        lock_sha256=None if runtime.local_env is None else runtime.local_env.lock_sha256,
        image_digest=None if runtime.container is None else runtime.container.image_digest,
    )


class ProviderBuild(Provider):
    """One build of a provider: several live side by side under one name.

    Each build is its own artifact, named ``Provider:<name>/<build key>`` and
    held at ``providers/<name>/<build key>.json``, so installing a new build adds
    an artifact rather than replacing one, and every Procedure keeps the exact
    build it pins. A build's runtime never changes: a successor only retires it
    or changes its signing keys and capture declarations.
    """

    artifact_format: Literal["cruxible-provider-v4"] = "cruxible-provider-v4"  # type: ignore[assignment]

    @model_validator(mode="after")
    def _build_key_reproduces(self) -> "ProviderBuild":
        if self.build_key != provider_build_key(self.runtime_artifact).value:
            raise ValueError(
                "build_key_mismatch: the identity's build key is not the key of the "
                "runtime artifact it carries"
            )
        return self


class NamedProviderBuild(ProviderBuild):
    """A provider build as revision 36 accepts it: named references and typed operations."""

    artifact_format: Literal["cruxible-provider-v6"] = "cruxible-provider-v6"  # type: ignore[assignment]
    capture_contracts: tuple[ArtifactNameRef, ...] = ()
    capture_contract_digests: ClassVar[tuple[str, ...]] = ()  # type: ignore[misc]
    pins: ClassVar[tuple[ArtifactPin, ...]] = ()  # type: ignore[misc]

    @field_validator("capture_contracts")
    @classmethod
    def _named_captures(cls, value: tuple[ArtifactNameRef, ...]) -> tuple[ArtifactNameRef, ...]:
        names = tuple(item.target.qualified for item in value)
        if any(item.target.kind != "CaptureContract" for item in value) or names != tuple(
            sorted(set(names))
        ):
            raise ValueError("Provider capture declarations must name sorted unique contracts")
        return value


class NamedProviderIdentity(ProviderV1):
    """A signing Provider identity without a package runtime."""

    artifact_format: Literal["cruxible-provider-identity-v3"] = "cruxible-provider-identity-v3"  # type: ignore[assignment]
    capture_contracts: tuple[ArtifactNameRef, ...] = ()
    capture_contract_digests: ClassVar[tuple[str, ...]] = ()  # type: ignore[misc]
    pins: ClassVar[tuple[ArtifactPin, ...]] = ()  # type: ignore[misc]

    @model_validator(mode="after")
    def _declarations(self) -> NamedProviderIdentity:
        if not self.signing_keys:
            raise ValueError("a signing Provider requires signing keys")
        names = tuple(ref.target.qualified for ref in self.capture_contracts)
        if any(ref.target.kind != "CaptureContract" for ref in self.capture_contracts) or (
            names != tuple(sorted(set(names)))
        ):
            raise ValueError("Provider declarations must name sorted unique artifacts")
        return self


def provider_declares_capture_contract(provider: ProviderV1, identity: ArtifactIdentity) -> bool:
    if isinstance(provider, NamedProviderBuild | NamedProviderIdentity):
        return any(ref.target == identity for ref in provider.capture_contracts)
    return any(
        pin.role == "capture-contract"
        and pin.target == identity
        and pin.artifact_digest in provider.capture_contract_digests
        for pin in provider.pins
    )


ProviderAny: TypeAlias = Annotated[
    ProviderV1 | ProviderV2 | Provider | ProviderBuild | NamedProviderBuild | NamedProviderIdentity,
    Field(discriminator="artifact_format"),
]
_PROVIDER_ADAPTER: TypeAdapter[ProviderAny] = TypeAdapter(ProviderAny)


class ProviderBuildName(_StrictProviderModel):
    """A Provider identity name split into the provider and, for a build, its key."""

    name: str
    #: The 64-hex build key; None names a single-path Provider (a built-in, or
    #: one registered before builds), which is itself one build of its name.
    build: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _PROVIDER_NAME_RE.fullmatch(value):
            raise ValueError("Provider name must be a canonical identifier")
        return value

    @field_validator("build")
    @classmethod
    def _build(cls, value: str | None) -> str | None:
        if value is not None and not _BUILD_KEY_RE.fullmatch(value):
            raise ValueError("a build key is 64 lowercase hex characters")
        return value

    @property
    def identity_name(self) -> str:
        return self.name if self.build is None else f"{self.name}/{self.build}"


_PROVIDER_BUILD_NAMES: ValueMemo[ProviderBuildName] = ValueMemo(capacity=4096)


def parse_provider_build_name(identity_name: str) -> ProviderBuildName:
    """Parse the immutable identity string once; malformed names are never retained."""
    return _PROVIDER_BUILD_NAMES.derive(
        identity_name, lambda: _parse_provider_build_name(identity_name)
    )


def _parse_provider_build_name(identity_name: str) -> ProviderBuildName:
    name, slash, build = identity_name.partition("/")
    try:
        return ProviderBuildName(name=name, build=build if slash else None)
    except ValueError as exc:
        raise ProviderFormatError("Provider identity is not path-addressable") from exc


def provider_path(name: str) -> str:
    """The path of the Provider whose identity name is ``name`` (a build or single path)."""

    parsed = parse_provider_build_name(name)
    if parsed.build is None:
        return f"providers/{parsed.name}.json"
    return f"providers/{parsed.name}/{parsed.build}.json"


def provider_builds_prefix(name: str) -> str:
    """The tree prefix holding every build of ``name`` (its single path aside)."""

    return f"providers/{parse_provider_build_name(name).name}/"


def render_provider(provider: ProviderAny) -> bytes:
    return pretty_canonical_bytes(provider.model_dump(mode="json"))


_PROVIDER_PARSES: ParseMemo[ProviderAny] = ParseMemo(capacity=256, max_bytes=8 * 1024 * 1024)


def parse_provider(
    content: bytes,
    *,
    path: str,
) -> ProviderAny:
    """Parse once per exact (path, bytes); see ``content_memo``."""
    return _PROVIDER_PARSES.parse(content, path, None, lambda: _parse_provider(content, path=path))


def _parse_provider(
    content: bytes,
    *,
    path: str,
) -> ProviderAny:
    try:
        provider = _PROVIDER_ADAPTER.validate_python(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProviderFormatError("Provider failed strict versioned validation") from exc
    if provider_path(provider.identity.name) != path:
        raise ProviderFormatError("Provider identity/path disagreement")
    if render_provider(provider) != content:
        raise ProviderFormatError("Provider is not in canonical wire form")
    return provider


@sealed_memo
def provider_digest(provider: ProviderAny) -> ArtifactDigest:
    if isinstance(provider, ProviderV2):
        return typed_digest(
            ArtifactDigest,
            provider.artifact_format,
            provider.model_dump(mode="json"),
        )
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        provider.model_dump(mode="json"),
    )


class AcceptedProvider(_StrictProviderModel):
    path: str
    provider: SerializeAsAny[ProviderAny]
    artifact_digest: str

    @model_validator(mode="after")
    def _correspondence(self) -> "AcceptedProvider":
        if self.path != provider_path(self.provider.identity.name):
            raise ValueError("accepted Provider path does not reproduce")
        if self.artifact_digest != provider_digest(self.provider).tagged:
            raise ValueError("accepted Provider digest does not reproduce")
        return self


class ProviderLawResult(_StrictProviderModel):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    required_tier: PermissionTier | None = None
    approval_scope: tuple[str, ...] = ()
    diagnostics: tuple[CompilerDiagnostic, ...] = ()


def _law_refusal(code: str, message: str, *, path: str) -> ProviderLawResult:
    return ProviderLawResult(
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


def _build_runtime(provider: ProviderBuild, *, by_name: bool = False) -> dict[str, object]:
    """What a build runs: fixed for its whole lineage."""

    result: dict[str, object] = {
        "control_domain": provider.control_domain,
        "runtime_artifact": provider.runtime_artifact.model_dump(mode="json"),
        "implementations": [item.model_dump(mode="json") for item in provider.implementations],
        "operations": [item.model_dump(mode="json") for item in provider.operations],
    }
    del by_name
    return result


def evaluate_provider_law(
    provider: SerializeAsAny[ProviderAny],
    *,
    path: str,
    predecessor: AcceptedProvider | None,
    contracts: Mapping[tuple[str, int], str] | None = None,
    refusal_codes: frozenset[str] | None = None,
    sibling_control_domains: frozenset[str] = frozenset(),
    by_name: bool = False,
) -> ProviderLawResult:
    """Identity, path, succession and the operation law.

    ``contracts`` maps each accepted Contract ``(name, version)`` to its
    digest: every operation pin must resolve to it exactly. ``refusal_codes``
    is the closed vocabulary a provider may refuse with.
    ``sibling_control_domains`` are the control domains of the other builds of
    this provider name: a new build joins its name's one control domain.
    """

    by_name = by_name or isinstance(provider, NamedProviderBuild | NamedProviderIdentity)
    if path != provider_path(provider.identity.name):
        return _law_refusal(
            "cruxible.provider.path_mismatch",
            "Provider identity/path disagreement.",
            path=path,
        )
    if predecessor is None:
        if provider.lifecycle.predecessor_digest is not None:
            return _law_refusal(
                "cruxible.provider.predecessor_missing",
                "A new Provider cannot name a predecessor.",
                path=path,
            )
        if any(domain != provider.control_domain for domain in sibling_control_domains):
            return _law_refusal(
                "cruxible.provider.control_domain_differs",
                f"Every build of {provider.provider_name} shares one control domain; this "
                f"build names {provider.control_domain!r}, another build names "
                f"{min(sibling_control_domains - {provider.control_domain})!r}.",
                path=path,
            )
    else:
        if provider.identity != predecessor.provider.identity or (
            provider.control_domain != predecessor.provider.control_domain
        ):
            return _law_refusal(
                "cruxible.provider.stable_identity_changed",
                "Provider identity and ultimate control domain are immutable in v1.",
                path=path,
            )
        if provider.lifecycle.predecessor_digest != predecessor.artifact_digest:
            return _law_refusal(
                "cruxible.provider.predecessor_mismatch",
                "Provider successor does not pin the exact predecessor.",
                path=path,
            )
        if isinstance(predecessor.provider, ProviderV2) and not isinstance(
            provider,
            ProviderV2,
        ):
            return _law_refusal(
                "cruxible.provider.wire_downgrade",
                "A Provider v2 lineage cannot be succeeded by the historical v1 wire.",
                path=path,
            )
        if isinstance(provider, ProviderBuild) and (
            not isinstance(predecessor.provider, ProviderBuild)
            or _build_runtime(provider, by_name=by_name)
            != _build_runtime(predecessor.provider, by_name=by_name)
        ):
            return _law_refusal(
                "cruxible.provider.build_immutable",
                "A provider build never changes what it runs: a successor may only retire "
                "it or change its signing keys and capture declarations. Install the new "
                "build alongside it.",
                path=path,
            )
    pinned_contracts = {
        pin.artifact_digest for pin in provider.pins if pin.role == "capture-contract"
    }
    if not set(provider.capture_contract_digests).issubset(pinned_contracts):
        return _law_refusal(
            "cruxible.provider.capture_contract_pin_missing",
            "Provider CaptureContract declarations require exact governed pins.",
            path=path,
        )
    if isinstance(provider, ProviderV2):
        problem = operation_law_problem(
            provider, contracts=contracts or {}, refusal_codes=refusal_codes
        )
        if problem is not None:
            return _law_refusal(problem[0], problem[1], path=path)
    return ProviderLawResult(
        verdict="accepted",
        artifact_digest=provider_digest(provider).tagged,
        required_tier="governed_write",
        approval_scope=(),
    )


def operation_law_problem(
    provider: ProviderV2,
    *,
    contracts: Mapping[tuple[str, int], str],
    refusal_codes: frozenset[str] | None,
) -> tuple[str, str] | None:
    """The install law for one build's operations: ``(code, message)`` or ``None``.

    Every pin resolves to the exact accepted Contract; names are unique (the
    model enforces it); every refusal code has a Contract and is a code the
    provider runtime can return; every entrypoint is one the manifest exports
    (the model enforces it); and an operation's effect agrees with the side
    effects its manifest implementation declares.
    """

    by_entrypoint = {
        item.entrypoint: item for item in provider.runtime_artifact.manifest.implementations
    }
    for operation in provider.operations:
        for ref in operation.contract_refs():
            accepted = contracts.get((ref.name, ref.version))
            if accepted is None:
                return (
                    "cruxible.provider.operation_contract_unresolved",
                    f"Operation {operation.name!r} pins {ref.spelling}, which is not accepted.",
                )
            if ref.digest != accepted:
                return (
                    "cruxible.provider.operation_contract_unresolved",
                    f"Operation {operation.name!r} must pin {ref.spelling} at {accepted}.",
                )
        if refusal_codes is not None:
            unknown = sorted(set(operation.refusals) - refusal_codes)
            if unknown:
                return (
                    "cruxible.provider.operation_refusal_unknown",
                    f"Operation {operation.name!r} declares refusal codes the provider "
                    f"runtime cannot return: {unknown}.",
                )
        implementation = by_entrypoint.get(operation.entrypoint)
        if implementation is None:
            return (
                "cruxible.provider.operation_entrypoint_unknown",
                f"Operation {operation.name!r} names an entrypoint the manifest lacks.",
            )
        if implementation.side_effects != (operation.effect == "external_mutation"):
            return (
                "cruxible.provider.effect_declaration_mismatch",
                f"Operation {operation.name!r} is {operation.effect}, but its entrypoint "
                f"declares side_effects={implementation.side_effects}.",
            )
    return None


__all__ = [
    "AcceptedProvider",
    "PROVIDER_BUILD_FORMAT",
    "ProviderAny",
    "ProviderBuild",
    "ProviderBuildName",
    "ProviderContainerBackendPin",
    "ProviderContainerMaterializationReference",
    "ProviderDistributionPin",
    "ProviderDistributionRef",
    "ProviderFormatError",
    "ProviderImageProvenance",
    "ProviderImplementationManifestV1",
    "ProviderImplementationRecord",
    "ProviderLocalDistributionPin",
    "ProviderLocalEnvBackendPin",
    "ProviderLocalMaterializationReference",
    "ProviderMaterializationReference",
    "ProviderRuntimeArtifactPayloadV1",
    "ProviderRuntimeManifestV1",
    "ProviderSigningKey",
    "ProviderV1",
    "ProviderV2",
    "Provider",
    "ProviderRuntimeArtifactPayload",
    "ProviderRuntimeManifest",
    "ProviderImplementationManifest",
    "ProviderLawResult",
    "evaluate_provider_law",
    "normalized_distribution_name",
    "parse_provider",
    "parse_provider_build_name",
    "provider_build_key",
    "provider_build_key_of",
    "provider_builds_prefix",
    "provider_digest",
    "operation_law_problem",
    "provider_expected_implementation_records",
    "provider_implementation_digest",
    "provider_manifest_digest",
    "provider_path",
    "provider_runtime_artifact_digest",
    "render_provider",
]
