"""PB-B's minimal registered artifact formats and normalized intermediate rows."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cruxible_core.playbill.bootstrap import render_principal
from cruxible_core.playbill.canonical import ArtifactDigest, canonical_bytes, file_digest
from cruxible_core.playbill.errors import ProjectionFormatError
from cruxible_core.playbill.projection_extensions import (
    ProjectionExtensionRegistry,
    ProjectionFact,
)
from cruxible_core.playbill.types import PrincipalRecord

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_REGISTERED_PATHS = (
    (re.compile(r"^principals/[a-z][a-z0-9_.-]{0,127}\.yaml$"), "principal"),
    (re.compile(r"^artifacts/fixtures/[a-z][a-z0-9_.-]{0,255}\.yaml$"), "fixture"),
    (
        re.compile(r"^presentation/fixtures/[a-z][a-z0-9_.-]{0,255}\.json$"),
        "presentation",
    ),
)

RegisteredPathKind = Literal["principal", "fixture", "presentation"]


class _StrictArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _identifier(value: str, *, label: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if normalized != value or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{label} must be a canonical lowercase identifier")
    return value


class FixturePin(_StrictArtifactModel):
    target_identity: str
    target_digest: str

    @field_validator("target_identity")
    @classmethod
    def _target_identity(cls, value: str) -> str:
        return _identifier(value, label="pin target_identity")

    @field_validator("target_digest")
    @classmethod
    def _target_digest(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value


class FixtureArtifact(_StrictArtifactModel):
    """Minimal semantic envelope used only to prove the PB-B compiler contract."""

    tag: Literal["playbill-fixture-v1"] = "playbill-fixture-v1"
    kind: Literal["fixture"] = "fixture"
    artifact_id: str
    revision: int = Field(ge=1, le=2**63 - 1)
    predecessor_digest: str | None = None
    pins: tuple[FixturePin, ...] = ()
    extension_facts: tuple[ProjectionFact, ...] = ()

    @field_validator("artifact_id")
    @classmethod
    def _artifact_id(cls, value: str) -> str:
        return _identifier(value, label="fixture artifact_id")

    @field_validator("predecessor_digest")
    @classmethod
    def _predecessor_digest(cls, value: str | None) -> str | None:
        if value is not None:
            ArtifactDigest.from_tagged(value)
        return value

    @field_validator("pins")
    @classmethod
    def _pins(cls, value: tuple[FixturePin, ...]) -> tuple[FixturePin, ...]:
        ordered = tuple(
            sorted(
                value,
                key=lambda item: (
                    item.target_identity.encode("utf-8"),
                    item.target_digest.encode("ascii"),
                ),
            )
        )
        if value != ordered or len({item.target_identity for item in value}) != len(value):
            raise ValueError("fixture pins must be sorted and unique by target_identity")
        return value

    @field_validator("extension_facts")
    @classmethod
    def _extension_facts(cls, value: tuple[ProjectionFact, ...]) -> tuple[ProjectionFact, ...]:
        def key(fact: ProjectionFact) -> tuple[bytes, int, bytes, bytes]:
            return (
                fact.schema_id.encode("utf-8"),
                fact.schema_version,
                fact.subject_identity.encode("utf-8"),
                fact.fact_key.encode("utf-8"),
            )

        ordered = tuple(sorted(value, key=key))
        if value != ordered or len({key(fact) for fact in value}) != len(value):
            raise ValueError("fixture extension facts must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _fact_subjects(self) -> "FixtureArtifact":
        if any(fact.subject_identity != self.artifact_id for fact in self.extension_facts):
            raise ValueError("fixture extension facts must name their containing artifact")
        return self


class FixturePresentation(_StrictArtifactModel):
    """Disposable rendered/cache content excluded from the canonical logical export."""

    tag: Literal["playbill-fixture-presentation-v1"] = "playbill-fixture-presentation-v1"
    subject_identity: str
    label: str

    @field_validator("subject_identity")
    @classmethod
    def _subject_identity(cls, value: str) -> str:
        return _identifier(value, label="fixture presentation subject_identity")


@dataclass(frozen=True)
class ArtifactEnvelopeRow:
    identity: str
    kind: str
    format_tag: str
    path: str
    artifact_digest: str
    predecessor_digest: str | None
    revision: int


@dataclass(frozen=True)
class PinRow:
    source_identity: str
    target_identity: str
    target_digest: str


@dataclass(frozen=True)
class ParsedProjectionTree:
    envelopes: tuple[ArtifactEnvelopeRow, ...]
    pins: tuple[PinRow, ...]
    semantic_facts: tuple[ProjectionFact, ...]
    presentation_facts: tuple[ProjectionFact, ...]


def registered_path_kind(path: str) -> RegisteredPathKind:
    for pattern, kind in _REGISTERED_PATHS:
        if pattern.fullmatch(path):
            return cast(RegisteredPathKind, kind)
    raise ProjectionFormatError(f"ledger path has no registered PB-B format: {path}")


def _pairs_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    normalized: set[str] = set()
    for key, value in pairs:
        normalized_key = unicodedata.normalize("NFC", key)
        if normalized_key in normalized:
            raise ProjectionFormatError("artifact object has duplicate normalized keys")
        normalized.add(normalized_key)
        result[key] = value
    return result


def _load_object(content: bytes, *, path: str) -> dict[str, object]:
    try:
        decoded = content.decode("utf-8")
        payload = json.loads(decoded, object_pairs_hook=_pairs_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectionFormatError(
            f"registered artifact must use canonical JSON-compatible YAML: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise ProjectionFormatError(f"registered artifact must be an object: {path}")
    return payload


def _canonical_model_bytes(model: BaseModel) -> bytes:
    return canonical_bytes(model.model_dump(mode="json")) + b"\n"


def parse_projection_tree(
    blobs: dict[str, bytes],
    *,
    registry: ProjectionExtensionRegistry,
) -> ParsedProjectionTree:
    """Parse all registered blobs and produce one sorted, typed row stream."""

    envelopes: list[ArtifactEnvelopeRow] = []
    pins: list[PinRow] = []
    semantic_facts: list[ProjectionFact] = []
    presentation_facts: list[ProjectionFact] = []
    identities: dict[str, str] = {}

    for path in sorted(blobs, key=lambda item: item.encode("utf-8")):
        content = blobs[path]
        kind = registered_path_kind(path)
        payload = _load_object(content, path=path)
        try:
            if kind == "principal":
                principal = PrincipalRecord.model_validate(payload)
                if render_principal(principal) != content:
                    raise ProjectionFormatError(f"principal artifact is not canonical: {path}")
                continue
            if kind == "fixture":
                artifact = FixtureArtifact.model_validate(payload)
                if _canonical_model_bytes(artifact) != content:
                    raise ProjectionFormatError(f"fixture artifact is not canonical: {path}")
                previous = identities.get(artifact.artifact_id)
                if previous is not None:
                    raise ProjectionFormatError(
                        f"duplicate semantic identity {artifact.artifact_id!r}: "
                        f"{previous} and {path}"
                    )
                identities[artifact.artifact_id] = path
                digest = file_digest(content).tagged
                envelopes.append(
                    ArtifactEnvelopeRow(
                        identity=artifact.artifact_id,
                        kind=artifact.kind,
                        format_tag=artifact.tag,
                        path=path,
                        artifact_digest=digest,
                        predecessor_digest=artifact.predecessor_digest,
                        revision=artifact.revision,
                    )
                )
                pins.extend(
                    PinRow(
                        source_identity=artifact.artifact_id,
                        target_identity=pin.target_identity,
                        target_digest=pin.target_digest,
                    )
                    for pin in artifact.pins
                )
                semantic_facts.extend(artifact.extension_facts)
                continue

            presentation = FixturePresentation.model_validate(payload)
            if _canonical_model_bytes(presentation) != content:
                raise ProjectionFormatError(f"presentation artifact is not canonical: {path}")
            presentation_facts.append(
                ProjectionFact(
                    schema_id="playbill.fixture.label",
                    schema_version=1,
                    subject_identity=presentation.subject_identity,
                    fact_key="label",
                    value=presentation.label,
                )
            )
        except ValidationError as exc:
            raise ProjectionFormatError(
                f"registered artifact failed strict validation: {path}"
            ) from exc

    validated_semantic = registry.validate(semantic_facts, classification="semantic")
    validated_presentation = registry.validate(
        presentation_facts,
        classification="presentation",
    )
    return ParsedProjectionTree(
        envelopes=tuple(sorted(envelopes, key=lambda item: item.identity.encode("utf-8"))),
        pins=tuple(
            sorted(
                pins,
                key=lambda item: (
                    item.source_identity.encode("utf-8"),
                    item.target_identity.encode("utf-8"),
                ),
            )
        ),
        semantic_facts=validated_semantic,
        presentation_facts=validated_presentation,
    )


__all__ = [
    "ArtifactEnvelopeRow",
    "FixtureArtifact",
    "FixturePin",
    "FixturePresentation",
    "ParsedProjectionTree",
    "PinRow",
    "parse_projection_tree",
    "registered_path_kind",
]
