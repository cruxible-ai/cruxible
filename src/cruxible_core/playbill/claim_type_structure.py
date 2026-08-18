"""Non-wire ClaimType structural primitives used before policy activation."""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from cruxible_core.playbill.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_core.playbill.diagnostics import CompilerDiagnostic
from cruxible_core.playbill.errors import CanonicalEncodingError

CLAIM_TYPE_STRUCTURAL_SIGNATURE_DOMAIN = "playbill-claim-type-structural-signature-v1"

ClaimRole = Literal["normative", "observation", "environment_binding", "derivation"]
ClaimObjectKind = Literal["literal", "subject", "exact_content"]
ClaimCardinality = Literal["one", "many"]
ReferentSensitivity = Literal["identity", "shell"]

_SEMANTIC_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})*$")


class _StrictClaimTypeStructureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _semantic_kind(value: str, *, label: str) -> str:
    if unicodedata.normalize("NFC", value) != value or not _SEMANTIC_KIND_RE.fullmatch(value):
        raise ValueError(f"{label} is not a canonical semantic kind")
    return value


class ClaimTypeStructure(_StrictClaimTypeStructureModel):
    """Policy-free ClaimType shape; deliberately not a governed artifact format."""

    predicate: str
    allowed_subject_kinds: tuple[str, ...]
    object_kind: ClaimObjectKind
    literal_schema: dict[str, object] | None = None
    allowed_object_subject_kinds: tuple[str, ...] = ()
    cardinality: ClaimCardinality
    permitted_roles: tuple[ClaimRole, ...]
    referent_sensitivity: ReferentSensitivity = "identity"

    @field_validator("predicate")
    @classmethod
    def _predicate(cls, value: str) -> str:
        return _semantic_kind(value, label="ClaimType predicate")

    @field_validator("allowed_subject_kinds", "allowed_object_subject_kinds")
    @classmethod
    def _kinds(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))) != value:
            raise ValueError("ClaimType semantic kinds must be sorted and unique")
        for item in value:
            _semantic_kind(item, label="ClaimType semantic kind")
        return value

    @field_validator("permitted_roles")
    @classmethod
    def _roles(cls, value: tuple[ClaimRole, ...]) -> tuple[ClaimRole, ...]:
        if not value or tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))) != value:
            raise ValueError("ClaimType roles must be nonempty, sorted, and unique")
        return value

    @field_validator("literal_schema")
    @classmethod
    def _literal_schema(
        cls,
        value: dict[str, object] | None,
    ) -> dict[str, object] | None:
        if value is None:
            return None
        # This proves the schema is an exact Playbill canonical value rather
        # than a Python object whose floats or keys could canonicalize loosely.
        try:
            canonical_bytes(value)
        except CanonicalEncodingError as exc:
            raise ValueError("literal_schema must be exact canonical JSON") from exc
        if not value:
            raise ValueError("literal_schema must be an explicit nonempty JSON Schema")
        schema_uri = value.get("$schema")
        if schema_uri is not None and schema_uri != "https://json-schema.org/draft/2020-12/schema":
            raise ValueError("literal_schema must use JSON Schema draft 2020-12 when declared")
        declared_type = value.get("type")
        if declared_type is None and "enum" not in value and "const" not in value:
            raise ValueError("literal_schema must constrain type, enum, or const")
        if declared_type is not None and declared_type not in {
            "array",
            "boolean",
            "integer",
            "null",
            "number",
            "object",
            "string",
        }:
            raise ValueError("literal_schema contains an unsupported exact type")
        return value

    @model_validator(mode="after")
    def _object_contract(self) -> "ClaimTypeStructure":
        if not self.allowed_subject_kinds:
            raise ValueError("ClaimType requires at least one allowed subject kind")
        if self.object_kind == "literal":
            if self.literal_schema is None:
                raise ValueError("literal ClaimType requires an exact literal_schema")
            if self.allowed_object_subject_kinds:
                raise ValueError("literal ClaimType cannot admit object Subject kinds")
        elif self.object_kind == "subject":
            if self.literal_schema is not None:
                raise ValueError("subject-valued ClaimType cannot carry literal_schema")
            if not self.allowed_object_subject_kinds:
                raise ValueError("subject-valued ClaimType requires allowed object Subject kinds")
        elif self.literal_schema is not None or self.allowed_object_subject_kinds:
            raise ValueError(
                "exact-content ClaimType cannot carry literal or object-Subject schema"
            )
        return self

    def literal_schema_bytes(self) -> bytes | None:
        """Return exact canonical schema bytes without creating an artifact digest."""

        if self.literal_schema is None:
            return None
        return canonical_bytes(self.literal_schema)


class ClaimTypeStructuralCheck(_StrictClaimTypeStructureModel):
    """Local compiler coverage that explicitly makes no acceptance claim."""

    tag: Literal["playbill-claim-type-structural-check-v1"] = (
        "playbill-claim-type-structural-check-v1"
    )
    coverage: Literal["local_only"] = "local_only"
    status: Literal["valid", "invalid"]
    structure: ClaimTypeStructure | None = None
    diagnostics: tuple[CompilerDiagnostic, ...] = ()

    @model_validator(mode="after")
    def _status_shape(self) -> "ClaimTypeStructuralCheck":
        if self.status == "valid" and (self.structure is None or self.diagnostics):
            raise ValueError("valid structural check requires a structure and no diagnostics")
        if self.status == "invalid" and (self.structure is not None or not self.diagnostics):
            raise ValueError("invalid structural check requires diagnostics only")
        return self


def claim_type_structural_signature(structure: ClaimTypeStructure) -> str:
    """Return the shape-only signature two ClaimTypes share when they mean the same.

    Write-time reuse checking and read-time discovery must agree on structural
    identity, so both derive it here instead of from private local copies.
    """

    return typed_digest(
        Sha256Value,
        CLAIM_TYPE_STRUCTURAL_SIGNATURE_DOMAIN,
        structure.model_dump(mode="json"),
    ).tagged


def check_claim_type_structure(value: object) -> ClaimTypeStructuralCheck:
    """Validate a local draft without registering, digesting, or accepting it."""

    try:
        structure = ClaimTypeStructure.model_validate(value)
    except ValidationError as exc:
        locations = sorted(
            {
                ".".join(str(part) for part in error["loc"]) or "$"
                for error in exc.errors(include_url=False)
            }
        )
        return ClaimTypeStructuralCheck(
            status="invalid",
            diagnostics=(
                CompilerDiagnostic(
                    code="playbill.claim_type.structure_invalid",
                    severity="error",
                    message=("Local ClaimType structure is invalid at: " + ", ".join(locations)),
                ),
            ),
        )
    return ClaimTypeStructuralCheck(status="valid", structure=structure)


__all__ = [
    "CLAIM_TYPE_STRUCTURAL_SIGNATURE_DOMAIN",
    "ClaimCardinality",
    "ClaimObjectKind",
    "ClaimRole",
    "ClaimTypeStructuralCheck",
    "ClaimTypeStructure",
    "ReferentSensitivity",
    "check_claim_type_structure",
    "claim_type_structural_signature",
]
