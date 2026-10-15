"""Governed Provider interface registrations.

A registration (v3) holds one interface contract and nothing else: the
definition bytes, their digest domain and digest, and the effect class. It is
named by that contract (``ProviderInterface:<id>/<contract key>`` at
``provider-interfaces/<id>/<contract key>.json``), so every build implementing
one contract binds the same registration, a changed contract adds a
registration beside the old one, and none is ever replaced.

V1 and V2 registrations (one per interface id, at
``provider-interfaces/<id>.json``) stay readable. They carry a bucket
vocabulary, classifier identity (V2: its package code and fixtures) and
conformance proofs. Core no longer classifies any input, so that data is
inert. A registration is read as bytes whose digests reproduce -- the
interface, vocabulary, fixture-set and classifier digests over the exact bytes
it carries -- and nothing reads the data for meaning: no selector is parsed or
matched, no measured bucket is checked against a vocabulary, and no proof or
fixture is required for, or checked against, any other.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.canonical import (
    CURRENT_ARTIFACT_CODEC,
    ArtifactCodec,
    ArtifactDigest,
    artifact_bytes_for_path,
    artifact_path_matches,
    canonical_bytes,
    normalize_canonical,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.content_memo import ParseMemo, sealed_memo
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.semantic import SemanticAddress

_INTERFACE_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_CLASSIFIER_ID_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,255}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX_RE = re.compile(r"^(?:[0-9a-f]{2})+$")
_CONTRACT_KEY_RE = re.compile(r"^[0-9a-f]{64}$")

ProviderEffectClass: TypeAlias = Literal["none", "external_read", "external_mutation"]


class ProviderInterfaceFormatError(FormatError):
    """A ProviderInterface artifact, vocabulary, or proof is invalid."""


class _StrictInterfaceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _digest(value: str) -> str:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError("Provider interface digest must be canonical sha256:<lowercase-hex>")
    return value


def _content_bytes(content_hex: str, *, label: str) -> bytes:
    if not _HEX_RE.fullmatch(content_hex):
        raise ValueError(f"{label} must be nonempty lowercase even-length hex")
    content = bytes.fromhex(content_hex)
    try:
        parsed = json.loads(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"{label} must encode canonical JSON bytes") from exc
    if canonical_bytes(parsed) != content:
        raise ValueError(f"{label} is not exact canonical JSON")
    return content


class ProviderBucketClass(_StrictInterfaceModel):
    id: str
    description: str

    @field_validator("id")
    @classmethod
    def _class_id(cls, value: str) -> str:
        if not value or ";" in value or "=" in value or value == "*":
            raise ValueError("invalid Provider bucket class id")
        return value


class ProviderBucketDimension(_StrictInterfaceModel):
    name: str
    description: str
    classes: tuple[ProviderBucketClass, ...]

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not value or ";" in value or "=" in value:
            raise ValueError("invalid Provider bucket dimension name")
        return value

    @field_validator("classes")
    @classmethod
    def _classes(
        cls,
        value: tuple[ProviderBucketClass, ...],
    ) -> tuple[ProviderBucketClass, ...]:
        ids = tuple(item.id for item in value)
        if not value or len(ids) != len(set(ids)):
            raise ValueError("Provider bucket classes must be nonempty and unique")
        return value


class ProviderBucketVocabulary(_StrictInterfaceModel):
    interface_id: str
    version: int = 1
    status: Literal["draft", "accepted"] = "draft"
    description: str = ""
    dimensions: tuple[ProviderBucketDimension, ...]

    @field_validator("interface_id")
    @classmethod
    def _interface_id(cls, value: str) -> str:
        if not _INTERFACE_ID_RE.fullmatch(value):
            raise ValueError("Provider bucket vocabulary interface_id is not canonical")
        return value

    @field_validator("dimensions")
    @classmethod
    def _dimensions(
        cls,
        value: tuple[ProviderBucketDimension, ...],
    ) -> tuple[ProviderBucketDimension, ...]:
        names = tuple(item.name for item in value)
        if not value or len(names) != len(set(names)):
            raise ValueError("Provider bucket dimensions must be nonempty and unique")
        return value


def provider_vocabulary_document(value: object) -> dict[str, object]:
    """One registration's bucket vocabulary as plain data, without judging it.

    A vocabulary every past install accepted reads through the model and comes
    back exactly as its model dump, so accepted bytes and projected facts keep
    their exact shape. Any other mapping is taken as written, with the model's
    defaults filled: nothing about a vocabulary decides whether a registration
    is read, installed or accepted.
    """

    if not isinstance(value, dict):
        raise ValueError("a bucket vocabulary must be a JSON object")
    try:
        return ProviderBucketVocabulary.model_validate(value).model_dump(mode="json")
    except ValueError:
        return {"version": 1, "status": "draft", "description": "", **value}


class ProviderBucketConformanceFixture(_StrictInterfaceModel):
    tag: Literal["playbill-provider-bucket-conformance-fixture-v1"] = (
        "playbill-provider-bucket-conformance-fixture-v1"
    )
    fixture_id: str
    canonical_input: object
    measured_bucket_id: str

    @field_validator("fixture_id")
    @classmethod
    def _fixture_id(cls, value: str) -> str:
        if not _CLASSIFIER_ID_RE.fullmatch(value):
            raise ValueError("Provider fixture id must be canonical")
        return value

    @field_validator("canonical_input", mode="before")
    @classmethod
    def _input(cls, value: object) -> object:
        normalized = normalize_canonical(value)
        if not isinstance(normalized, dict):
            raise ValueError("Provider classifier fixture input must be a canonical object")
        return normalized


def provider_bucket_fixture_digest(fixture: ProviderBucketConformanceFixture) -> str:
    """Hash compiler-shipped fixture bytes without minting another authority domain."""

    return f"sha256:{hashlib.sha256(canonical_bytes(fixture.model_dump(mode='json'))).hexdigest()}"


class ProviderBucketConformanceFixtureProof(_StrictInterfaceModel):
    tag: Literal["playbill-provider-bucket-conformance-fixture-proof-v1"] = (
        "playbill-provider-bucket-conformance-fixture-proof-v1"
    )
    selector: str
    fixture_id: str
    fixture_digest: str
    measured_bucket_id: str

    _fixture_digest = field_validator("fixture_digest")(_digest)


def _proof_key(proof: ProviderBucketConformanceFixtureProof) -> tuple[bytes, bytes]:
    return proof.selector.encode("utf-8"), proof.fixture_id.encode("utf-8")


def provider_interface_definition_digest(content_hex: str) -> str:
    _content_bytes(content_hex, label="Provider interface bytes")
    return typed_digest(
        ArtifactDigest,
        "playbill-provider-interface-definition-v1",
        {"content_hex": content_hex},
    ).tagged


def provider_external_interface_definition_digest(content_hex: str, *, domain: str) -> str:
    """Reproduce the Provider runtime's reviewed external stub digest domain."""

    content = _content_bytes(content_hex, label="Provider interface bytes")
    if domain != "cruxible.interface.stub.v1":
        raise ValueError("unsupported external Provider interface digest domain")
    digest = hashlib.sha256()
    digest.update(domain.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def provider_bucket_vocabulary_digest(content_hex: str) -> str:
    _content_bytes(content_hex, label="Provider bucket vocabulary bytes")
    return typed_digest(
        ArtifactDigest,
        "playbill-provider-bucket-vocabulary-v1",
        {"content_hex": content_hex},
    ).tagged


def provider_bucket_fixture_set_digest(
    proofs: tuple[ProviderBucketConformanceFixtureProof, ...],
) -> str:
    return typed_digest(
        ArtifactDigest,
        "playbill-provider-bucket-conformance-fixture-set-v1",
        {"proofs": [proof.model_dump(mode="json", exclude={"tag"}) for proof in proofs]},
    ).tagged


def provider_bucket_classifier_digest(
    *,
    classifier_identity: str,
    classifier_version: int,
    conformance_fixture_set_digest: str,
) -> str:
    _digest(conformance_fixture_set_digest)
    return typed_digest(
        ArtifactDigest,
        "playbill-provider-bucket-classifier-v1",
        {
            "classifier_identity": classifier_identity,
            "classifier_version": classifier_version,
            "conformance_fixture_set_digest": conformance_fixture_set_digest,
        },
    ).tagged


def _pin_key(pin: ArtifactPin) -> tuple[bytes, bytes, bytes]:
    return (
        pin.role.encode("utf-8"),
        pin.target.qualified.encode("utf-8"),
        pin.artifact_digest.encode("ascii"),
    )


class ProviderInterfaceRegistrationV1(_StrictInterfaceModel):
    artifact_format: Literal["playbill-provider-interface-v1"] = "playbill-provider-interface-v1"
    identity: ArtifactIdentity
    interface_id: str
    interface_bytes_hex: str
    interface_digest_domain: Literal[
        "playbill-provider-interface-definition-v1", "cruxible.interface.stub.v1"
    ] = Field(
        default="playbill-provider-interface-definition-v1",
        exclude_if=lambda value: value == "playbill-provider-interface-definition-v1",
    )
    interface_digest: str
    vocabulary_bytes_hex: str
    vocabulary_digest: str
    classifier_identity: str
    classifier_version: int
    classifier_digest: str
    conformance_fixture_set_digest: str
    conformance_proofs: tuple[ProviderBucketConformanceFixtureProof, ...]
    effect_class: ProviderEffectClass
    pins: tuple[ArtifactPin, ...] = ()
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    _digests = field_validator(
        "interface_digest",
        "vocabulary_digest",
        "classifier_digest",
        "conformance_fixture_set_digest",
    )(_digest)

    @field_validator("interface_id")
    @classmethod
    def _interface_id(cls, value: str) -> str:
        if not _INTERFACE_ID_RE.fullmatch(value):
            raise ValueError("Provider interface id must be canonical")
        return value

    @field_validator("classifier_identity")
    @classmethod
    def _classifier_identity(cls, value: str) -> str:
        if not _CLASSIFIER_ID_RE.fullmatch(value):
            raise ValueError("Provider classifier identity must be canonical")
        return value

    @field_validator("classifier_version")
    @classmethod
    def _classifier_version(cls, value: int) -> int:
        if value < 1:
            raise ValueError("Provider classifier version must be positive")
        return value

    @field_validator("interface_bytes_hex")
    @classmethod
    def _interface_bytes(cls, value: str) -> str:
        _content_bytes(value, label="Provider interface bytes")
        return value

    @field_validator("vocabulary_bytes_hex")
    @classmethod
    def _vocabulary_bytes(cls, value: str) -> str:
        _content_bytes(value, label="Provider bucket vocabulary bytes")
        return value

    @field_validator("conformance_proofs")
    @classmethod
    def _proofs(
        cls,
        value: tuple[ProviderBucketConformanceFixtureProof, ...],
    ) -> tuple[ProviderBucketConformanceFixtureProof, ...]:
        # Canonical order only, so the fixture-set digest is one function of the
        # proofs. Proofs are inert: none is required, and none is checked
        # against the vocabulary or another proof.
        keys = tuple(_proof_key(item) for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("Provider conformance proofs must be sorted and unique")
        return value

    @field_validator("pins")
    @classmethod
    def _pins(cls, value: tuple[ArtifactPin, ...]) -> tuple[ArtifactPin, ...]:
        if value != tuple(sorted(value, key=_pin_key)):
            raise ValueError("Provider interface pins must be canonically sorted")
        keys = tuple((item.role, item.target.qualified) for item in value)
        if len(keys) != len(set(keys)):
            raise ValueError("Provider interface pins must be role/target unique")
        return value

    @model_validator(mode="after")
    def _correspondence(self) -> "ProviderInterfaceRegistrationV1":
        if self.identity.kind != "ProviderInterface" or self.identity.name != self.interface_id:
            raise ValueError("Provider interface identity must match its interface_id")
        expected_interface_digest = (
            provider_interface_definition_digest(self.interface_bytes_hex)
            if self.interface_digest_domain == "playbill-provider-interface-definition-v1"
            else provider_external_interface_definition_digest(
                self.interface_bytes_hex, domain=self.interface_digest_domain
            )
        )
        if self.interface_digest != expected_interface_digest:
            raise ValueError("Provider interface digest does not reproduce exact bytes")
        if self.vocabulary_digest != provider_bucket_vocabulary_digest(self.vocabulary_bytes_hex):
            raise ValueError("Provider vocabulary digest does not reproduce exact bytes")
        if self.conformance_fixture_set_digest != provider_bucket_fixture_set_digest(
            self.conformance_proofs
        ):
            raise ValueError("Provider conformance fixture-set digest does not reproduce")
        if self.classifier_digest != self.expected_classifier_digest:
            raise ValueError("Provider classifier digest does not reproduce")
        return self

    @property
    def expected_classifier_digest(self) -> str:
        return provider_bucket_classifier_digest(
            classifier_identity=self.classifier_identity,
            classifier_version=self.classifier_version,
            conformance_fixture_set_digest=self.conformance_fixture_set_digest,
        )

    @property
    def vocabulary_document(self) -> dict[str, object]:
        """The registration's (inert) bucket vocabulary as plain data."""

        return provider_vocabulary_document(json.loads(bytes.fromhex(self.vocabulary_bytes_hex)))


class ProviderClassifierCode(_StrictInterfaceModel):
    """Package-owned classifier entry point, verified inside the pinned environment."""

    entrypoint: str
    source_digest: str

    _source_digest = field_validator("source_digest")(_digest)

    @field_validator("entrypoint")
    @classmethod
    def _entrypoint(cls, value: str) -> str:
        module, separator, member = value.partition(":")
        if (
            not separator
            or not member.isidentifier()
            or not all(part.isidentifier() for part in module.split("."))
        ):
            raise ValueError("classifier entrypoint must be module:callable")
        return value


def provider_package_classifier_digest(
    *,
    classifier_identity: str,
    classifier_version: int,
    conformance_fixture_set_digest: str,
    code: ProviderClassifierCode,
) -> str:
    return typed_digest(
        ArtifactDigest,
        "playbill-provider-bucket-classifier-v2",
        {
            "classifier_identity": classifier_identity,
            "classifier_version": classifier_version,
            "conformance_fixture_set_digest": conformance_fixture_set_digest,
            "code": code.model_dump(mode="json"),
        },
    ).tagged


class ProviderInterfaceRegistration(ProviderInterfaceRegistrationV1):
    artifact_format: Literal["playbill-provider-interface-v2"] = "playbill-provider-interface-v2"  # type: ignore[assignment]
    classifier_code: ProviderClassifierCode
    conformance_fixtures: tuple[ProviderBucketConformanceFixture, ...]

    @property
    def expected_classifier_digest(self) -> str:
        return provider_package_classifier_digest(
            classifier_identity=self.classifier_identity,
            classifier_version=self.classifier_version,
            conformance_fixture_set_digest=self.conformance_fixture_set_digest,
            code=self.classifier_code,
        )

    @model_validator(mode="after")
    def _fixture_order(self) -> "ProviderInterfaceRegistration":
        # Canonical order only. The fixtures and proofs are inert: no fixture
        # is required for a proof or a proof for a fixture, and neither is
        # checked against the other. The registration's artifact digest still
        # commits every byte of both.
        ids = tuple(item.fixture_id for item in self.conformance_fixtures)
        if ids != tuple(sorted(set(ids), key=str.encode)):
            raise ValueError("package fixtures must be sorted and unique")
        return self


ProviderInterfaceDigestDomain: TypeAlias = Literal[
    "playbill-provider-interface-definition-v1", "cruxible.interface.stub.v1"
]


def provider_interface_contract_key(
    *,
    interface_id: str,
    interface_digest_domain: ProviderInterfaceDigestDomain,
    interface_digest: str,
    effect_class: ProviderEffectClass,
) -> ArtifactDigest:
    """The key one interface contract is registered under."""

    return typed_digest(
        ArtifactDigest,
        "cruxible-provider-interface-contract-key-v1",
        {
            "interface_id": interface_id,
            "interface_digest_domain": interface_digest_domain,
            "interface_digest": interface_digest,
            "effect_class": effect_class,
        },
    )


class ProviderInterfaceContract(_StrictInterfaceModel):
    """One interface contract's registration: the contract and nothing else.

    Named by its contract key, so every build implementing this contract binds
    this one registration; it carries no package data, so a build that changes
    only its code leaves it byte-identical.
    """

    artifact_format: Literal["cruxible-provider-interface-v3"] = "cruxible-provider-interface-v3"
    identity: ArtifactIdentity
    interface_id: str
    interface_bytes_hex: str
    interface_digest_domain: ProviderInterfaceDigestDomain
    interface_digest: str
    effect_class: ProviderEffectClass
    pins: tuple[ArtifactPin, ...] = ()
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    _digests = field_validator("interface_digest")(_digest)

    @field_validator("interface_id")
    @classmethod
    def _interface_id(cls, value: str) -> str:
        if not _INTERFACE_ID_RE.fullmatch(value):
            raise ValueError("Provider interface id must be canonical")
        return value

    @field_validator("interface_bytes_hex")
    @classmethod
    def _interface_bytes(cls, value: str) -> str:
        _content_bytes(value, label="Provider interface bytes")
        return value

    @field_validator("pins")
    @classmethod
    def _pins(cls, value: tuple[ArtifactPin, ...]) -> tuple[ArtifactPin, ...]:
        return ProviderInterfaceRegistrationV1._pins(value)

    @model_validator(mode="after")
    def _correspondence(self) -> "ProviderInterfaceContract":
        expected_interface_digest = (
            provider_interface_definition_digest(self.interface_bytes_hex)
            if self.interface_digest_domain == "playbill-provider-interface-definition-v1"
            else provider_external_interface_definition_digest(
                self.interface_bytes_hex, domain=self.interface_digest_domain
            )
        )
        if self.interface_digest != expected_interface_digest:
            raise ValueError("Provider interface digest does not reproduce exact bytes")
        if (
            self.identity.kind != "ProviderInterface"
            or self.identity.name != f"{self.interface_id}/{self.contract_key}"
        ):
            raise ValueError("Provider interface identity must name its interface and contract")
        return self

    @property
    def contract_key(self) -> str:
        return provider_interface_contract_key(
            interface_id=self.interface_id,
            interface_digest_domain=self.interface_digest_domain,
            interface_digest=self.interface_digest,
            effect_class=self.effect_class,
        ).value


def provider_interface_contract(
    *,
    interface_id: str,
    interface_bytes_hex: str,
    interface_digest_domain: ProviderInterfaceDigestDomain,
    interface_digest: str,
    effect_class: ProviderEffectClass,
) -> ProviderInterfaceContract:
    """The registration of one contract, named by its key."""

    key = provider_interface_contract_key(
        interface_id=interface_id,
        interface_digest_domain=interface_digest_domain,
        interface_digest=interface_digest,
        effect_class=effect_class,
    )
    return ProviderInterfaceContract(
        identity=ArtifactIdentity(kind="ProviderInterface", name=f"{interface_id}/{key.value}"),
        interface_id=interface_id,
        interface_bytes_hex=interface_bytes_hex,
        interface_digest_domain=interface_digest_domain,
        interface_digest=interface_digest,
        effect_class=effect_class,
    )


ProviderInterfaceRegistrationAny: TypeAlias = Annotated[
    ProviderInterfaceRegistrationV1 | ProviderInterfaceRegistration | ProviderInterfaceContract,
    Field(discriminator="artifact_format"),
]
#: Every registration format, for isinstance checks (v3 is not a V1 subclass:
#: it carries none of the inert bucket data).
PROVIDER_INTERFACE_REGISTRATION_TYPES = (ProviderInterfaceRegistrationV1, ProviderInterfaceContract)
_INTERFACE_ADAPTER: TypeAdapter[ProviderInterfaceRegistrationAny] = TypeAdapter(
    ProviderInterfaceRegistrationAny
)


def provider_interface_id_of(identity_name: str) -> str:
    """The interface id a registration identity name names (``id`` or ``id/<key>``)."""

    return identity_name.partition("/")[0]


def provider_interface_path(identity_name: str) -> str:
    """The path of the registration named ``identity_name``: ``<id>`` (v1/v2) or
    ``<id>/<contract key>`` (v3)."""

    interface_id, slash, key = identity_name.partition("/")
    if not _INTERFACE_ID_RE.fullmatch(interface_id) or (
        slash and not _CONTRACT_KEY_RE.fullmatch(key)
    ):
        raise ProviderInterfaceFormatError("Provider interface id is not path-addressable")
    if slash:
        return f"provider-interfaces/{interface_id}/{key}.json"
    return f"provider-interfaces/{interface_id}.json"


def provider_interface_registrations_prefix(interface_id: str) -> str:
    """The tree prefix holding every contract registration of ``interface_id``."""

    if not _INTERFACE_ID_RE.fullmatch(interface_id):
        raise ProviderInterfaceFormatError("Provider interface id is not path-addressable")
    return f"provider-interfaces/{interface_id}/"


def render_provider_interface(
    registration: ProviderInterfaceRegistrationV1 | ProviderInterfaceContract,
) -> bytes:
    return pretty_canonical_bytes(registration.model_dump(mode="json"))


_INTERFACE_PARSES: ParseMemo[ProviderInterfaceRegistrationAny] = ParseMemo(
    capacity=256, max_bytes=8 * 1024 * 1024
)


def parse_provider_interface(
    content: bytes,
    *,
    path: str,
    codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC,
) -> ProviderInterfaceRegistrationAny:
    """Parse immutable registration bytes once per exact content, path and codec."""
    return _INTERFACE_PARSES.parse(
        content, path, codec, lambda: _parse_provider_interface(content, path=path, codec=codec)
    )


def _parse_provider_interface(
    content: bytes,
    *,
    path: str,
    codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC,
) -> ProviderInterfaceRegistrationAny:
    try:
        registration = _INTERFACE_ADAPTER.validate_python(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProviderInterfaceFormatError(
            "Provider interface failed strict versioned validation"
        ) from exc
    if not artifact_path_matches(
        provider_interface_path(registration.identity.name),
        path,
        codec=codec,
    ):
        raise ProviderInterfaceFormatError("Provider interface identity/path disagreement")
    rendered = artifact_bytes_for_path(
        render_provider_interface(registration),
        path,
        codec=codec,
    )
    if rendered != content:
        raise ProviderInterfaceFormatError("Provider interface is not in canonical wire form")
    return registration


@sealed_memo
def provider_interface_digest(
    registration: ProviderInterfaceRegistrationV1 | ProviderInterfaceContract,
) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        registration.artifact_format,
        registration.model_dump(mode="json"),
    )


class AcceptedProviderInterfaceRegistration(_StrictInterfaceModel):
    path: str
    registration: ProviderInterfaceRegistrationAny
    artifact_digest: str

    @model_validator(mode="after")
    def _binding(self) -> "AcceptedProviderInterfaceRegistration":
        if self.path != provider_interface_path(self.registration.identity.name):
            raise ValueError("accepted Provider interface path does not reproduce")
        if self.artifact_digest != provider_interface_digest(self.registration).tagged:
            raise ValueError("accepted Provider interface digest does not reproduce")
        return self


class ProviderInterfaceLawResult(_StrictInterfaceModel):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    required_tier: PermissionTier | None = None
    approval_scope: tuple[str, ...] = ()
    diagnostics: tuple[CompilerDiagnostic, ...] = ()


def _refusal(code: str, message: str, *, path: str) -> ProviderInterfaceLawResult:
    return ProviderInterfaceLawResult(
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


def evaluate_provider_interface_law(
    registration: ProviderInterfaceRegistrationV1 | ProviderInterfaceContract,
    *,
    path: str,
    predecessor: AcceptedProviderInterfaceRegistration | None,
) -> ProviderInterfaceLawResult:
    """Identity, path and succession. No bucket or proof reproduction: a
    registration's classifier data is inert, so nothing here re-proves it. A
    contract registration (v3) never changes its contract: a successor only
    retires it."""

    if path != provider_interface_path(registration.identity.name):
        return _refusal(
            "cruxible.provider_interface.path_mismatch",
            "Provider interface identity/path disagreement.",
            path=path,
        )
    if predecessor is None:
        if registration.lifecycle.predecessor_digest is not None:
            return _refusal(
                "cruxible.provider_interface.predecessor_missing",
                "A new Provider interface cannot name a predecessor.",
                path=path,
            )
    else:
        if registration.identity != predecessor.registration.identity:
            return _refusal(
                "cruxible.provider_interface.stable_identity_changed",
                "A Provider interface successor must retain stable identity.",
                path=path,
            )
        if registration.lifecycle.predecessor_digest != predecessor.artifact_digest:
            return _refusal(
                "cruxible.provider_interface.predecessor_mismatch",
                "Provider interface successor does not pin its exact predecessor.",
                path=path,
            )
        if isinstance(registration, ProviderInterfaceContract) and registration.model_dump(
            exclude={"lifecycle"}
        ) != predecessor.registration.model_dump(exclude={"lifecycle"}):
            return _refusal(
                "cruxible.provider_interface.contract_immutable",
                "A contract registration never changes; a changed contract registers beside it.",
                path=path,
            )
    return ProviderInterfaceLawResult(
        verdict="accepted",
        artifact_digest=provider_interface_digest(registration).tagged,
        required_tier="governed_write",
    )


__all__ = [
    "AcceptedProviderInterfaceRegistration",
    "PROVIDER_INTERFACE_REGISTRATION_TYPES",
    "ProviderInterfaceContract",
    "ProviderInterfaceDigestDomain",
    "ProviderBucketClass",
    "ProviderBucketConformanceFixtureProof",
    "ProviderBucketConformanceFixture",
    "ProviderBucketDimension",
    "ProviderBucketVocabulary",
    "ProviderEffectClass",
    "ProviderInterfaceFormatError",
    "ProviderInterfaceLawResult",
    "ProviderInterfaceRegistrationV1",
    "ProviderInterfaceRegistration",
    "ProviderInterfaceRegistrationAny",
    "ProviderClassifierCode",
    "provider_package_classifier_digest",
    "provider_vocabulary_document",
    "evaluate_provider_interface_law",
    "parse_provider_interface",
    "provider_bucket_classifier_digest",
    "provider_bucket_fixture_digest",
    "provider_bucket_fixture_set_digest",
    "provider_bucket_vocabulary_digest",
    "provider_interface_definition_digest",
    "provider_external_interface_definition_digest",
    "provider_interface_digest",
    "provider_interface_contract",
    "provider_interface_contract_key",
    "provider_interface_id_of",
    "provider_interface_path",
    "provider_interface_registrations_prefix",
    "render_provider_interface",
]
