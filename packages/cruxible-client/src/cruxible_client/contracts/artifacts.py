"""Shared value objects and fail-closed registries for governed artifacts."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_validator

from cruxible_client.contracts.canonical import ArtifactDigest, normalize_ledger_path
from cruxible_client.contracts.errors import ProjectionFormatError

_KIND_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,383}$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


class _StrictArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _nfc(value: str, *, label: str) -> str:
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{label} must already be NFC-normalized")
    return value


class ArtifactIdentity(_StrictArtifactModel):
    """Kind-qualified stable identity shared by new Playbill artifact families."""

    kind: str
    name: str

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        _nfc(value, label="artifact identity kind")
        if not _KIND_RE.fullmatch(value):
            raise ValueError("artifact identity kind is not canonical")
        return value

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        _nfc(value, label="artifact identity name")
        if not _NAME_RE.fullmatch(value) or "//" in value or value.endswith(("/", ":")):
            raise ValueError("artifact identity name is not canonical")
        return value

    @property
    def qualified(self) -> str:
        return f"{self.kind}:{self.name}"


def parse_artifact_identity(value: str) -> ArtifactIdentity:
    """Parse the generic kind-qualified identity grammar without guessing a kind."""

    kind, separator, name = value.partition(":")
    if not separator:
        raise ValueError("artifact identity must be kind-qualified")
    return ArtifactIdentity(kind=kind, name=name)


class ArtifactPin(_StrictArtifactModel):
    role: str
    target: ArtifactIdentity
    artifact_digest: str

    @field_validator("role")
    @classmethod
    def _role(cls, value: str) -> str:
        _nfc(value, label="artifact pin role")
        if not _ROLE_RE.fullmatch(value):
            raise ValueError("artifact pin role is not canonical")
        return value

    @field_validator("artifact_digest")
    @classmethod
    def _artifact_digest(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value


class ArtifactLifecycle(_StrictArtifactModel):
    state: Literal["live", "retired"] = "live"
    predecessor_digest: str | None = None

    @field_validator("state")
    @classmethod
    def _state(cls, value: str) -> str:
        if value not in {"live", "retired"}:
            raise ValueError("artifact lifecycle state must be live or retired")
        return value

    @field_validator("predecessor_digest")
    @classmethod
    def _predecessor_digest(cls, value: str | None) -> str | None:
        if value is not None:
            ArtifactDigest.from_tagged(value)
        return value


@runtime_checkable
class GovernedArtifactProtocol(Protocol):
    """Structural kernel used by new kinds without imposing a shared wire base."""

    @property
    def artifact_format(self) -> str: ...

    @property
    def identity(self) -> ArtifactIdentity: ...

    @property
    def pins(self) -> tuple[ArtifactPin, ...]: ...

    @property
    def lifecycle(self) -> ArtifactLifecycle: ...


@dataclass(frozen=True)
class ArtifactPathKind:
    kind: str
    pattern: re.Pattern[str]


class ArtifactKindRegistry:
    """Closed path-kind registry: a ledger path resolves to one registered kind or refuses."""

    def __init__(self, entries: tuple[ArtifactPathKind, ...]) -> None:
        self._entries: dict[str, ArtifactPathKind] = {}
        patterns: set[str] = set()
        for entry in entries:
            if entry.kind in self._entries or entry.pattern.pattern in patterns:
                raise ValueError("duplicate artifact kind or path-pattern registration")
            if not _ROLE_RE.fullmatch(entry.kind):
                raise ValueError("artifact path kind must be a canonical lowercase identifier")
            self._entries[entry.kind] = entry
            patterns.add(entry.pattern.pattern)

    def entries(self) -> tuple[ArtifactPathKind, ...]:
        """Return the frozen registrations in canonical kind order."""

        return tuple(sorted(self._entries.values(), key=lambda entry: entry.kind.encode("utf-8")))

    def resolve_path(self, path: str) -> str:
        try:
            normalized = normalize_ledger_path(path)
        except Exception as exc:
            raise ProjectionFormatError("artifact path is not a canonical ledger path") from exc
        if normalized != path:
            raise ProjectionFormatError("artifact path must already be canonical")
        matches = tuple(entry for entry in self._entries.values() if entry.pattern.fullmatch(path))
        if len(matches) != 1:
            raise ProjectionFormatError(
                f"ledger path has no registered format or is ambiguous: {path}"
            )
        return matches[0].kind


__all__ = [
    "ArtifactIdentity",
    "ArtifactKindRegistry",
    "ArtifactLifecycle",
    "ArtifactPathKind",
    "ArtifactPin",
    "GovernedArtifactProtocol",
    "parse_artifact_identity",
]
