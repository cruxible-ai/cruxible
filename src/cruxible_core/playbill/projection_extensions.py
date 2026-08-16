"""Versioned additive projection facts and their frozen value normalization."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_core.playbill.artifacts import parse_artifact_identity
from cruxible_core.playbill.canonical import (
    CanonicalValue,
    Sha256Value,
    normalize_ledger_path,
)
from cruxible_core.playbill.errors import ProjectionFormatError

ProjectionFactClassification = Literal["semantic", "presentation"]

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")


class _StrictProjectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _normalized_text(value: str, *, label: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProjectionFormatError(f"{label} contains a non-Unicode surrogate") from exc
    return normalized


def _normalized_identifier(value: str, *, label: str) -> str:
    normalized = _normalized_text(value, label=label)
    if normalized != value or not _IDENTIFIER_RE.fullmatch(value):
        raise ProjectionFormatError(f"{label} must be a canonical lowercase identifier")
    return value


def _normalized_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ProjectionFormatError("non-finite decimal values are forbidden")
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if rendered in {"", "-0"}:
        rendered = "0"
    return rendered


def _normalized_timestamp(value: datetime | str) -> str:
    if isinstance(value, str):
        raw = value
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ProjectionFormatError("timestamp wrapper is not RFC 3339") from exc
    else:
        parsed = value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProjectionFormatError("timestamps must carry an explicit UTC offset")
    utc = parsed.astimezone(timezone.utc)
    timespec = "microseconds" if utc.microsecond else "seconds"
    return utc.isoformat(timespec=timespec).replace("+00:00", "Z")


def normalize_projection_value(value: object, *, location: str = "$") -> CanonicalValue:
    """Normalize the reference assembler's closed, language-neutral value set.

    JSON-native values retain their types. Decimal, timestamp, digest, path, and
    name values use explicit one-key wrappers so YAML/JSON parser coercion can
    never silently change their meaning. Floats are always refused.
    """

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):  # gives the same refusal for every float spelling
            raise ProjectionFormatError(f"{location}: floating-point values are forbidden")
        raise ProjectionFormatError(f"{location}: floating-point values are forbidden")
    if isinstance(value, Decimal):
        return {"$decimal": _normalized_decimal(value)}
    if isinstance(value, datetime):
        return {"$timestamp": _normalized_timestamp(value)}
    if isinstance(value, str):
        return _normalized_text(value, label=location)
    if isinstance(value, bytes):
        raise ProjectionFormatError(f"{location}: raw bytes are forbidden in projection facts")
    if isinstance(value, list):
        return [
            normalize_projection_value(item, location=f"{location}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        raw_items = list(value.items())
        if any(not isinstance(key, str) for key, _item in raw_items):
            raise ProjectionFormatError(f"{location}: mapping keys must be strings")
        string_items = [(str(key), item) for key, item in raw_items]
        if len(string_items) == 1 and string_items[0][0].startswith("$"):
            wrapper, wrapped = string_items[0]
            if wrapper == "$decimal":
                if not isinstance(wrapped, str):
                    raise ProjectionFormatError(f"{location}: decimal wrapper must contain text")
                try:
                    decimal = Decimal(wrapped)
                except Exception as exc:
                    raise ProjectionFormatError(f"{location}: invalid decimal wrapper") from exc
                return {"$decimal": _normalized_decimal(decimal)}
            if wrapper == "$timestamp":
                if not isinstance(wrapped, str):
                    raise ProjectionFormatError(f"{location}: timestamp wrapper must contain text")
                return {"$timestamp": _normalized_timestamp(wrapped)}
            if wrapper == "$digest":
                if not isinstance(wrapped, str):
                    raise ProjectionFormatError(f"{location}: digest wrapper must contain text")
                try:
                    parsed = Sha256Value.from_tagged(wrapped)
                except ValueError as exc:
                    raise ProjectionFormatError(f"{location}: invalid digest wrapper") from exc
                return {"$digest": parsed.tagged}
            if wrapper == "$path":
                if not isinstance(wrapped, str):
                    raise ProjectionFormatError(f"{location}: path wrapper must contain text")
                try:
                    path = normalize_ledger_path(wrapped)
                except Exception as exc:
                    raise ProjectionFormatError(f"{location}: invalid path wrapper") from exc
                return {"$path": path}
            if wrapper == "$name":
                if not isinstance(wrapped, str):
                    raise ProjectionFormatError(f"{location}: name wrapper must contain text")
                return {"$name": _normalized_identifier(wrapped, label=location)}
            raise ProjectionFormatError(f"{location}: unknown typed wrapper {wrapper!r}")
        if any(key.startswith("$") for key, _item in string_items):
            raise ProjectionFormatError(f"{location}: reserved wrapper keys cannot be mixed")
        normalized: list[tuple[str, CanonicalValue]] = []
        seen: set[str] = set()
        for raw_key, item in string_items:
            key = _normalized_text(raw_key, label=f"{location} key")
            if key in seen:
                raise ProjectionFormatError(
                    f"{location}: mapping keys collide after Unicode normalization"
                )
            seen.add(key)
            normalized.append((key, normalize_projection_value(item, location=f"{location}.{key}")))
        normalized.sort(key=lambda item: item[0].encode("utf-8"))
        return dict(normalized)
    if isinstance(value, Sequence):
        raise ProjectionFormatError(f"{location}: sequences must be concrete JSON arrays")
    raise ProjectionFormatError(
        f"{location}: unsupported projection value type {type(value).__name__}"
    )


class ProjectionFactDeclaration(_StrictProjectionModel):
    """One compiler-owned extension schema declaration."""

    schema_id: str
    schema_version: int = Field(ge=1)
    classification: ProjectionFactClassification
    constraints: tuple[str, ...] = ()

    @field_validator("schema_id")
    @classmethod
    def _schema_id(cls, value: str) -> str:
        return _normalized_identifier(value, label="projection fact schema_id")

    @field_validator("constraints")
    @classmethod
    def _constraints(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))) != value:
            raise ValueError("projection fact constraints must be sorted and unique")
        return value


class ProjectionFact(_StrictProjectionModel):
    """One typed normalized fact emitted for a semantic subject."""

    schema_id: str
    schema_version: int = Field(ge=1)
    subject_identity: str
    fact_key: str
    value: object

    @field_validator("schema_id")
    @classmethod
    def _schema_id(cls, value: str) -> str:
        return _normalized_identifier(value, label="projection fact schema_id")

    @field_validator("subject_identity")
    @classmethod
    def _subject_identity(cls, value: str) -> str:
        normalized = _normalized_text(value, label="projection fact subject_identity")
        if normalized != value:
            raise ValueError("projection fact subject_identity must be canonical")
        if _IDENTIFIER_RE.fullmatch(value):
            return value
        try:
            parsed = parse_artifact_identity(value)
        except ValueError as exc:
            raise ValueError(
                "projection fact subject_identity must be canonical and kind-qualified"
            ) from exc
        if parsed.qualified != value:
            raise ValueError("projection fact subject_identity must be canonically rendered")
        return value

    @field_validator("fact_key")
    @classmethod
    def _fact_key(cls, value: str) -> str:
        return _normalized_identifier(value, label="projection fact fact_key")

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object) -> CanonicalValue:
        return normalize_projection_value(value)


class ProjectionExtensionRegistry:
    """Closed registry that prevents fact schemas from being invented by input."""

    def __init__(self, declarations: Iterable[ProjectionFactDeclaration]) -> None:
        self._declarations: dict[tuple[str, int], ProjectionFactDeclaration] = {}
        self._versions: dict[str, set[int]] = {}
        for declaration in declarations:
            key = (declaration.schema_id, declaration.schema_version)
            if key in self._declarations:
                raise ProjectionFormatError(
                    "duplicate projection fact schema declaration: "
                    f"{declaration.schema_id}@{declaration.schema_version}"
                )
            self._declarations[key] = declaration
            self._versions.setdefault(declaration.schema_id, set()).add(declaration.schema_version)

    def declarations(
        self,
        classification: ProjectionFactClassification,
    ) -> tuple[ProjectionFactDeclaration, ...]:
        return tuple(
            sorted(
                (
                    declaration
                    for declaration in self._declarations.values()
                    if declaration.classification == classification
                ),
                key=lambda item: (item.schema_id.encode("utf-8"), item.schema_version),
            )
        )

    def supports(
        self,
        schema_id: str,
        schema_version: int,
        *,
        classification: ProjectionFactClassification,
    ) -> bool:
        declaration = self._declarations.get((schema_id, schema_version))
        return declaration is not None and declaration.classification == classification

    def validate(
        self,
        facts: Iterable[ProjectionFact],
        *,
        classification: ProjectionFactClassification,
    ) -> tuple[ProjectionFact, ...]:
        validated: list[ProjectionFact] = []
        seen: set[tuple[str, int, str, str]] = set()
        for fact in facts:
            expected_versions = self._versions.get(fact.schema_id)
            if expected_versions is None:
                raise ProjectionFormatError(f"undeclared projection fact schema: {fact.schema_id}")
            if fact.schema_version not in expected_versions:
                versions = ", ".join(str(version) for version in sorted(expected_versions))
                raise ProjectionFormatError(
                    f"projection fact schema version mismatch for {fact.schema_id}: "
                    f"expected one of {versions}, got {fact.schema_version}"
                )
            declaration = self._declarations[(fact.schema_id, fact.schema_version)]
            if declaration.classification != classification:
                raise ProjectionFormatError(
                    f"projection fact {fact.schema_id} is declared {declaration.classification}, "
                    f"not {classification}"
                )
            key = (
                fact.schema_id,
                fact.schema_version,
                fact.subject_identity,
                fact.fact_key,
            )
            if key in seen:
                raise ProjectionFormatError(
                    "duplicate projection fact: " + ":".join(str(part) for part in key)
                )
            seen.add(key)
            validated.append(fact)
        return tuple(
            sorted(
                validated,
                key=lambda item: (
                    item.schema_id.encode("utf-8"),
                    item.schema_version,
                    item.subject_identity.encode("utf-8"),
                    item.fact_key.encode("utf-8"),
                ),
            )
        )


def fixture_extension_registry() -> ProjectionExtensionRegistry:
    """Return PB-B's minimal frozen extension registry."""

    return ProjectionExtensionRegistry(
        (
            ProjectionFactDeclaration(
                schema_id="playbill.fixture.fact",
                schema_version=1,
                classification="semantic",
                constraints=("unique(subject_identity,fact_key)",),
            ),
            ProjectionFactDeclaration(
                schema_id="playbill.fixture.label",
                schema_version=1,
                classification="presentation",
                constraints=("unique(subject_identity,fact_key)",),
            ),
        )
    )


def playbill_extension_registry() -> ProjectionExtensionRegistry:
    """Return the additive PB-C registry, preserving every PB-B declaration."""

    fixture = fixture_extension_registry().declarations("semantic")
    presentation = fixture_extension_registry().declarations("presentation")
    document = tuple(
        ProjectionFactDeclaration(
            schema_id=schema_id,
            schema_version=1,
            classification="semantic",
            constraints=("unique(subject_identity,fact_key)",),
        )
        for schema_id in (
            "playbill.document.metadata",
            "playbill.document.references",
            "playbill.document.source_mapping",
            "playbill.document.subject",
        )
    )
    return ProjectionExtensionRegistry((*fixture, *document, *presentation))


def playbill_governance_extension_registry() -> ProjectionExtensionRegistry:
    """Return PB-D's additive accepted-governance explanation schemas."""

    pb_c_semantic = playbill_extension_registry().declarations("semantic")
    presentation = playbill_extension_registry().declarations("presentation")
    explanation = tuple(
        ProjectionFactDeclaration(
            schema_id=schema_id,
            schema_version=1,
            classification="semantic",
            constraints=("unique(subject_identity,fact_key)",),
        )
        for schema_id in (
            "playbill.document.attestation_coverage",
            "playbill.document.governance",
            "playbill.document.history",
            "playbill.document.provenance",
        )
    )
    return ProjectionExtensionRegistry((*pb_c_semantic, *explanation, *presentation))


def playbill_subject_extension_registry() -> ProjectionExtensionRegistry:
    """Return PC-A1's additive Subject and Subject-explanation schemas."""

    prior = playbill_governance_extension_registry()
    subject = tuple(
        ProjectionFactDeclaration(
            schema_id=schema_id,
            schema_version=1,
            classification="semantic",
            constraints=("unique(subject_identity,fact_key)",),
        )
        for schema_id in (
            "playbill.subject.attestation_coverage",
            "playbill.subject.governance",
            "playbill.subject.history",
            "playbill.subject.identity",
            "playbill.subject.lifecycle",
            "playbill.subject.provenance",
            "playbill.subject.references",
        )
    )
    return ProjectionExtensionRegistry(
        (*prior.declarations("semantic"), *subject, *prior.declarations("presentation"))
    )


__all__ = [
    "ProjectionExtensionRegistry",
    "ProjectionFact",
    "ProjectionFactClassification",
    "ProjectionFactDeclaration",
    "fixture_extension_registry",
    "normalize_projection_value",
    "playbill_extension_registry",
    "playbill_governance_extension_registry",
    "playbill_subject_extension_registry",
]
