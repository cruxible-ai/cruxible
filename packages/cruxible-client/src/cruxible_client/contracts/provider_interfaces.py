"""Governed Provider interface, bucket vocabulary, and classifier authority."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

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
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.semantic import SemanticAddress

_INTERFACE_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_CLASSIFIER_ID_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,255}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX_RE = re.compile(r"^(?:[0-9a-f]{2})+$")

ProviderEffectClassV1: TypeAlias = Literal["none", "external_read", "external_mutation"]


class ProviderInterfaceFormatError(PlaybillFormatError):
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


class ProviderBucketClassV1(_StrictInterfaceModel):
    id: str
    description: str

    @field_validator("id")
    @classmethod
    def _class_id(cls, value: str) -> str:
        if not value or ";" in value or "=" in value or value == "*":
            raise ValueError("invalid Provider bucket class id")
        return value


class ProviderBucketDimensionV1(_StrictInterfaceModel):
    name: str
    description: str
    classes: tuple[ProviderBucketClassV1, ...]

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
        value: tuple[ProviderBucketClassV1, ...],
    ) -> tuple[ProviderBucketClassV1, ...]:
        ids = tuple(item.id for item in value)
        if not value or len(ids) != len(set(ids)):
            raise ValueError("Provider bucket classes must be nonempty and unique")
        return value


class ProviderBucketVocabularyV1(_StrictInterfaceModel):
    interface_id: str
    version: int = 1
    status: Literal["draft", "accepted"] = "draft"
    description: str = ""
    dimensions: tuple[ProviderBucketDimensionV1, ...]

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
        value: tuple[ProviderBucketDimensionV1, ...],
    ) -> tuple[ProviderBucketDimensionV1, ...]:
        names = tuple(item.name for item in value)
        if not value or len(names) != len(set(names)):
            raise ValueError("Provider bucket dimensions must be nonempty and unique")
        return value

    def parse_selector(
        self,
        selector: str,
        *,
        allow_star: bool = True,
    ) -> tuple[tuple[str, str], ...]:
        assignment = _parse_pairs(selector)
        result: list[tuple[str, str]] = []
        for dimension in self.dimensions:
            if dimension.name not in assignment:
                raise ValueError(f"bucket expression misses dimension {dimension.name!r}")
            member = assignment.pop(dimension.name)
            class_ids = {item.id for item in dimension.classes}
            if member not in class_ids and not (allow_star and member == "*"):
                raise ValueError(
                    f"bucket expression names unregistered class {member!r} for "
                    f"dimension {dimension.name!r}"
                )
            result.append((dimension.name, member))
        if assignment:
            raise ValueError(f"bucket expression names extra dimensions: {sorted(assignment)}")
        return tuple(result)

    def validate_bucket(self, bucket_id: str) -> tuple[tuple[str, str], ...]:
        return self.parse_selector(bucket_id, allow_star=False)

    def selector_matches(self, selector: str, bucket_id: str) -> bool:
        expected = self.parse_selector(selector)
        actual = dict(self.validate_bucket(bucket_id))
        return all(member == "*" or actual[name] == member for name, member in expected)


def _parse_pairs(value: str) -> dict[str, str]:
    if not value:
        raise ValueError("bucket expression must be nonempty")
    result: dict[str, str] = {}
    for part in value.split(";"):
        name, separator, member = part.partition("=")
        if not separator or not name or not member or name in result:
            raise ValueError("bucket expression is malformed or repeats a dimension")
        result[name] = member
    return result


class ProviderBucketConformanceFixtureV1(_StrictInterfaceModel):
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


def provider_bucket_fixture_digest(fixture: ProviderBucketConformanceFixtureV1) -> str:
    """Hash compiler-shipped fixture bytes without minting another authority domain."""

    return f"sha256:{hashlib.sha256(canonical_bytes(fixture.model_dump(mode='json'))).hexdigest()}"


class ProviderBucketConformanceFixtureProofV1(_StrictInterfaceModel):
    tag: Literal["playbill-provider-bucket-conformance-fixture-proof-v1"] = (
        "playbill-provider-bucket-conformance-fixture-proof-v1"
    )
    selector: str
    fixture_id: str
    fixture_digest: str
    measured_bucket_id: str

    _fixture_digest = field_validator("fixture_digest")(_digest)


def _proof_key(proof: ProviderBucketConformanceFixtureProofV1) -> tuple[bytes, bytes]:
    return proof.selector.encode("utf-8"), proof.fixture_id.encode("utf-8")


def provider_interface_definition_digest(content_hex: str) -> str:
    _content_bytes(content_hex, label="Provider interface bytes")
    return typed_digest(
        ArtifactDigest,
        "playbill-provider-interface-definition-v1",
        {"content_hex": content_hex},
    ).tagged


def provider_bucket_vocabulary_digest(content_hex: str) -> str:
    _content_bytes(content_hex, label="Provider bucket vocabulary bytes")
    return typed_digest(
        ArtifactDigest,
        "playbill-provider-bucket-vocabulary-v1",
        {"content_hex": content_hex},
    ).tagged


def provider_bucket_fixture_set_digest(
    proofs: tuple[ProviderBucketConformanceFixtureProofV1, ...],
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
    interface_digest: str
    vocabulary_bytes_hex: str
    vocabulary_digest: str
    classifier_identity: str
    classifier_version: int
    classifier_digest: str
    conformance_fixture_set_digest: str
    conformance_proofs: tuple[ProviderBucketConformanceFixtureProofV1, ...]
    effect_class: ProviderEffectClassV1
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
        value: tuple[ProviderBucketConformanceFixtureProofV1, ...],
    ) -> tuple[ProviderBucketConformanceFixtureProofV1, ...]:
        if not value or value != tuple(sorted(value, key=_proof_key)):
            raise ValueError("Provider conformance proofs must be nonempty and sorted")
        selectors = tuple(item.selector for item in value)
        fixture_ids = tuple(item.fixture_id for item in value)
        if len(selectors) != len(set(selectors)) or len(fixture_ids) != len(set(fixture_ids)):
            raise ValueError("Provider conformance proofs must be selector/fixture unique")
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
        if self.interface_digest != provider_interface_definition_digest(self.interface_bytes_hex):
            raise ValueError("Provider interface digest does not reproduce exact bytes")
        if self.vocabulary_digest != provider_bucket_vocabulary_digest(self.vocabulary_bytes_hex):
            raise ValueError("Provider vocabulary digest does not reproduce exact bytes")
        vocabulary = self.vocabulary
        if vocabulary.interface_id != self.interface_id or vocabulary.status != "accepted":
            raise ValueError("Provider accepted vocabulary names another interface or status")
        for proof in self.conformance_proofs:
            vocabulary.parse_selector(proof.selector)
            vocabulary.validate_bucket(proof.measured_bucket_id)
            if not vocabulary.selector_matches(proof.selector, proof.measured_bucket_id):
                raise ValueError("Provider conformance result does not match its selector")
        if self.conformance_fixture_set_digest != provider_bucket_fixture_set_digest(
            self.conformance_proofs
        ):
            raise ValueError("Provider conformance fixture-set digest does not reproduce")
        if self.classifier_digest != provider_bucket_classifier_digest(
            classifier_identity=self.classifier_identity,
            classifier_version=self.classifier_version,
            conformance_fixture_set_digest=self.conformance_fixture_set_digest,
        ):
            raise ValueError("Provider classifier digest does not reproduce")
        return self

    @property
    def vocabulary(self) -> ProviderBucketVocabularyV1:
        return ProviderBucketVocabularyV1.model_validate(
            json.loads(bytes.fromhex(self.vocabulary_bytes_hex))
        )


def provider_interface_path(interface_id: str) -> str:
    if not _INTERFACE_ID_RE.fullmatch(interface_id):
        raise ProviderInterfaceFormatError("Provider interface id is not path-addressable")
    return f"provider-interfaces/{interface_id}.json"


def render_provider_interface(registration: ProviderInterfaceRegistrationV1) -> bytes:
    return pretty_canonical_bytes(registration.model_dump(mode="json"))


def parse_provider_interface(
    content: bytes,
    *,
    path: str,
    codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC,
) -> ProviderInterfaceRegistrationV1:
    try:
        registration = ProviderInterfaceRegistrationV1.model_validate(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProviderInterfaceFormatError(
            "Provider interface failed strict v1 validation"
        ) from exc
    if not artifact_path_matches(
        provider_interface_path(registration.interface_id),
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


def provider_interface_digest(registration: ProviderInterfaceRegistrationV1) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-provider-interface-v1",
        registration.model_dump(mode="json"),
    )


class AcceptedProviderInterfaceRegistrationV1(_StrictInterfaceModel):
    path: str
    registration: ProviderInterfaceRegistrationV1
    artifact_digest: str

    @model_validator(mode="after")
    def _binding(self) -> "AcceptedProviderInterfaceRegistrationV1":
        if self.path != provider_interface_path(self.registration.interface_id):
            raise ValueError("accepted Provider interface path does not reproduce")
        if self.artifact_digest != provider_interface_digest(self.registration).tagged:
            raise ValueError("accepted Provider interface digest does not reproduce")
        return self


class ProviderInterfaceLawResultV1(_StrictInterfaceModel):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    required_tier: PermissionTier | None = None
    approval_scope: tuple[str, ...] = ()
    diagnostics: tuple[CompilerDiagnostic, ...] = ()


def _refusal(code: str, message: str, *, path: str) -> ProviderInterfaceLawResultV1:
    return ProviderInterfaceLawResultV1(
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
    registration: ProviderInterfaceRegistrationV1,
    *,
    path: str,
    predecessor: AcceptedProviderInterfaceRegistrationV1 | None,
    conformance_fixtures: Mapping[str, ProviderBucketConformanceFixtureV1],
) -> ProviderInterfaceLawResultV1:
    if path != provider_interface_path(registration.interface_id):
        return _refusal(
            "playbill.provider_interface.path_mismatch",
            "Provider interface identity/path disagreement.",
            path=path,
        )
    if predecessor is None:
        if registration.lifecycle.predecessor_digest is not None:
            return _refusal(
                "playbill.provider_interface.predecessor_missing",
                "A new Provider interface cannot name a predecessor.",
                path=path,
            )
    else:
        if registration.identity != predecessor.registration.identity:
            return _refusal(
                "playbill.provider_interface.stable_identity_changed",
                "A Provider interface successor must retain stable identity.",
                path=path,
            )
        if registration.lifecycle.predecessor_digest != predecessor.artifact_digest:
            return _refusal(
                "playbill.provider_interface.predecessor_mismatch",
                "Provider interface successor does not pin its exact predecessor.",
                path=path,
            )
    for proof in registration.conformance_proofs:
        fixture = conformance_fixtures.get(proof.fixture_id)
        if fixture is None:
            return _refusal(
                "playbill.provider_interface.bucket_fixture_missing",
                f"Compiler conformance fixture {proof.fixture_id!r} is unavailable.",
                path=path,
            )
        if (
            provider_bucket_fixture_digest(fixture) != proof.fixture_digest
            or fixture.measured_bucket_id != proof.measured_bucket_id
        ):
            return _refusal(
                "playbill.provider_interface.classifier_digest_mismatch",
                f"Conformance proof {proof.fixture_id!r} diverges from compiler bytes.",
                path=path,
            )
    return ProviderInterfaceLawResultV1(
        verdict="accepted",
        artifact_digest=provider_interface_digest(registration).tagged,
        required_tier="governed_write",
    )


class ProviderBucketClassifierInstallationResultV1(_StrictInterfaceModel):
    fixture_id: str
    fixture_digest: str
    measured_bucket_id: str

    _fixture_digest = field_validator("fixture_digest")(_digest)


class ProviderBucketClassifierInstallationV1(_StrictInterfaceModel):
    tag: Literal["playbill-provider-bucket-classifier-installation-v1"] = (
        "playbill-provider-bucket-classifier-installation-v1"
    )
    classifier_identity: str
    classifier_version: int
    classifier_digest: str
    conformance_fixture_set_digest: str
    results: tuple[ProviderBucketClassifierInstallationResultV1, ...]

    _digests = field_validator(
        "classifier_digest",
        "conformance_fixture_set_digest",
    )(_digest)

    @field_validator("results")
    @classmethod
    def _results(
        cls,
        value: tuple[ProviderBucketClassifierInstallationResultV1, ...],
    ) -> tuple[ProviderBucketClassifierInstallationResultV1, ...]:
        ids = tuple(item.fixture_id for item in value)
        if ids != tuple(sorted(set(ids), key=lambda item: item.encode("utf-8"))):
            raise ValueError("Classifier installation results must be sorted and unique")
        return value


__all__ = [
    "AcceptedProviderInterfaceRegistrationV1",
    "ProviderBucketClassV1",
    "ProviderBucketClassifierInstallationResultV1",
    "ProviderBucketClassifierInstallationV1",
    "ProviderBucketConformanceFixtureProofV1",
    "ProviderBucketConformanceFixtureV1",
    "ProviderBucketDimensionV1",
    "ProviderBucketVocabularyV1",
    "ProviderEffectClassV1",
    "ProviderInterfaceFormatError",
    "ProviderInterfaceLawResultV1",
    "ProviderInterfaceRegistrationV1",
    "evaluate_provider_interface_law",
    "parse_provider_interface",
    "provider_bucket_classifier_digest",
    "provider_bucket_fixture_digest",
    "provider_bucket_fixture_set_digest",
    "provider_bucket_vocabulary_digest",
    "provider_interface_definition_digest",
    "provider_interface_digest",
    "provider_interface_path",
    "render_provider_interface",
]
