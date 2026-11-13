"""Versioned additive projection facts and their frozen value normalization."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import (
    CanonicalValue,
    Sha256Value,
    normalize_ledger_path,
)
from cruxible_client.contracts.content_memo import FrozenDict, FrozenList
from cruxible_client.contracts.errors import ProjectionFormatError

ProjectionFactClassification = Literal["semantic"]

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


@dataclass(frozen=True, slots=True, init=False)
class _NormalizedProjectionValue:
    """An owned, normalized subtree; its constructor never trusts raw values."""

    value: CanonicalValue

    def __init__(self, value: object) -> None:
        object.__setattr__(self, "value", _freeze_normalized(normalize_projection_value(value)))

    def __copy__(self) -> _NormalizedProjectionValue:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> _NormalizedProjectionValue:
        memo[id(self)] = self
        return self

    def __reduce__(self) -> tuple[type[_NormalizedProjectionValue], tuple[CanonicalValue]]:
        # FrozenDict deliberately unpickles as a mutable dict. Re-enter the
        # validating constructor, so an unpickled carrier owns frozen values.
        return (_NormalizedProjectionValue, (self.value,))


def _freeze_normalized(value: CanonicalValue) -> CanonicalValue:
    if isinstance(value, dict):
        return FrozenDict({key: _freeze_normalized(item) for key, item in value.items()})
    if isinstance(value, list):
        return FrozenList(_freeze_normalized(item) for item in value)
    return value


def normalized_projection_value(value: object) -> _NormalizedProjectionValue:
    """Normalize and own a subtree once, for composition into compiler rows.

    ProjectionFact still validates each row's schema, key and identity. Only
    this constructor can introduce a carried value, after the ordinary cold
    normalizer accepts it; no mutable caller-owned container is retained.
    """
    if type(value) is _NormalizedProjectionValue:
        return value
    return _NormalizedProjectionValue(value)


def normalize_projection_value(value: object, *, location: str = "$") -> CanonicalValue:
    """Normalize the reference assembler's closed, language-neutral value set.

    JSON-native values retain their types. Decimal, timestamp, digest, path, and
    name values use explicit one-key wrappers so YAML/JSON parser coercion can
    never silently change their meaning. Floats are always refused.
    """

    if type(value) is _NormalizedProjectionValue:
        return value.value
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


@lru_cache(maxsize=16384)
def _qualified_subject_identity(value: str) -> str:
    # A fact needs the qualified string, not a second ArtifactIdentity model.
    # Use the very same field validators as that model (and the same first
    # colon split as parse_artifact_identity); keep no parallel grammar.
    kind, separator, name = value.partition(":")
    if not separator:
        raise ValueError("artifact identity must be kind-qualified")
    ArtifactIdentity._kind(kind)
    ArtifactIdentity._name(name)
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
            _qualified_subject_identity(value)
        except ValueError as exc:
            raise ValueError(
                "projection fact subject_identity must be canonical and kind-qualified"
            ) from exc
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


#: Every semantic fact schema the current compiler projects, at version 1.
_CURRENT_FACT_SCHEMA_IDS = (
    "cruxible.capture_contract.contract",
    "cruxible.capture_contract.references",
    "cruxible.citation_relation.capture_contract",
    "cruxible.citation_relation.external_use",
    "cruxible.citation_relation.retired_conflict",
    "cruxible.citation_relation.source_use",
    "cruxible.citation_relation.use",
    "cruxible.claim.attestation_coverage",
    "cruxible.claim.backing",
    "cruxible.claim.current_verdict",
    "cruxible.claim.evidence_basis",
    "cruxible.claim.governance",
    "cruxible.claim.history",
    "cruxible.claim.identity",
    "cruxible.claim.lifecycle",
    "cruxible.claim.provenance",
    "cruxible.claim.source_mapping",
    "cruxible.claim.statement",
    "cruxible.claim_type.attestation_coverage",
    "cruxible.claim_type.governance",
    "cruxible.claim_type.history",
    "cruxible.claim_type.identity",
    "cruxible.claim_type.policies",
    "cruxible.claim_type.provenance",
    "cruxible.claim_type.references",
    "cruxible.document.attestation_coverage",
    "cruxible.document.governance",
    "cruxible.document.history",
    "cruxible.document.metadata",
    "cruxible.document.provenance",
    "cruxible.document.references",
    "cruxible.document.source_mapping",
    "cruxible.document.subject",
    "cruxible.exhaust_promotion.basis",
    "cruxible.line.attestation_coverage",
    "cruxible.line.governance",
    "cruxible.line.history",
    "cruxible.line.provenance",
    "cruxible.line.source_mapping",
    "cruxible.line.spec",
    "cruxible.line.track_record",
    "cruxible.procedure.attestation_coverage",
    "cruxible.procedure.definition",
    "cruxible.procedure.governance",
    "cruxible.procedure.graph",
    "cruxible.procedure.history",
    "cruxible.procedure.provenance",
    "cruxible.procedure.resolution_activation",
    "cruxible.procedure.source_mapping",
    "cruxible.procedure.track_record",
    "cruxible.procedure_mandate.authority",
    "cruxible.provider.identity",
    "cruxible.provider.implementations",
    "cruxible.provider.keys",
    "cruxible.provider.provenance",
    "cruxible.provider.runtime",
    "cruxible.query_definition.attestation_coverage",
    "cruxible.query_definition.definition",
    "cruxible.query_definition.governance",
    "cruxible.query_definition.history",
    "cruxible.query_definition.policy",
    "cruxible.query_definition.provenance",
    "cruxible.query_definition.references",
    "cruxible.source_acquisition_policy.policy",
    "cruxible.subject.attestation_coverage",
    "cruxible.subject.governance",
    "cruxible.subject.history",
    "cruxible.subject.identity",
    "cruxible.subject.lifecycle",
    "cruxible.subject.provenance",
    "cruxible.subject.references",
)


def current_extension_registry() -> ProjectionExtensionRegistry:
    """Return the current compiler's closed fact-schema registry."""

    declarations = tuple(
        ProjectionFactDeclaration(
            schema_id=schema_id,
            schema_version=version,
            classification="semantic",
            constraints=("unique(subject_identity,fact_key)",),
        )
        for schema_id, version in (
            *((schema_id, 1) for schema_id in _CURRENT_FACT_SCHEMA_IDS),
            ("cruxible.claim_type.identity", 2),
        )
    )
    return ProjectionExtensionRegistry(declarations)


__all__ = [
    "ProjectionExtensionRegistry",
    "ProjectionFact",
    "ProjectionFactClassification",
    "ProjectionFactDeclaration",
    "current_extension_registry",
    "normalize_projection_value",
]
