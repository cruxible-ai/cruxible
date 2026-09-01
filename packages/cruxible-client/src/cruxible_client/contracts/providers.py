"""Accepted provider identities and their externally held signing keys."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

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


def provider_path(name: str) -> str:
    if not _PROVIDER_NAME_RE.fullmatch(name):
        raise ProviderFormatError("Provider identity is not path-addressable")
    return f"providers/{name}.json"


def render_provider(provider: ProviderV1) -> bytes:
    return pretty_canonical_bytes(provider.model_dump(mode="json"))


def parse_provider(
    content: bytes,
    *,
    path: str,
    codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC,
) -> ProviderV1:
    try:
        provider = ProviderV1.model_validate(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProviderFormatError("Provider failed strict v1 validation") from exc
    if not artifact_path_matches(provider_path(provider.identity.name), path, codec=codec):
        raise ProviderFormatError("Provider identity/path disagreement")
    if artifact_bytes_for_path(render_provider(provider), path, codec=codec) != content:
        raise ProviderFormatError("Provider is not in canonical wire form")
    return provider


def provider_digest(provider: ProviderV1) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        provider.model_dump(mode="json"),
    )


class AcceptedProviderV1(_StrictProviderModel):
    path: str
    provider: ProviderV1
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
    provider: ProviderV1,
    *,
    path: str,
    predecessor: AcceptedProviderV1 | None,
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
    pinned_contracts = {
        pin.artifact_digest for pin in provider.pins if pin.role == "capture-contract"
    }
    if not set(provider.capture_contract_digests).issubset(pinned_contracts):
        return _law_refusal(
            "playbill.provider.capture_contract_pin_missing",
            "Provider CaptureContract declarations require exact governed pins.",
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
    "ProviderFormatError",
    "ProviderSigningKeyV1",
    "ProviderV1",
    "ProviderLawResultV1",
    "evaluate_provider_law",
    "parse_provider",
    "provider_digest",
    "provider_path",
    "render_provider",
]
