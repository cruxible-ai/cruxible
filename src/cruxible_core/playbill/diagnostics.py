"""Typed compiler diagnostics that carry evidence but never authority."""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_core.playbill.canonical import Sha256Value, canonical_bytes
from cruxible_core.playbill.semantic import ContentSpan, SemanticAddress

DiagnosticSeverity = Literal["info", "warning", "error"]
GovernedOperation = Literal["check", "compile", "propose"]

_CODE_RE = re.compile(r"^playbill\.[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_DRAFT_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


class _StrictDiagnosticModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LocalDraftEdit(_StrictDiagnosticModel):
    """An edit against exact unaccepted client bytes, never accepted storage."""

    tag: Literal["playbill-local-draft-edit-v1"] = "playbill-local-draft-edit-v1"
    draft_id: str
    content_digest: str
    start_byte: int
    end_byte: int
    replacement_text: str

    @field_validator("draft_id")
    @classmethod
    def _draft_id(cls, value: str) -> str:
        if not _DRAFT_ID_RE.fullmatch(value):
            raise ValueError("draft_id must be a canonical client-local identifier")
        return value

    @field_validator("content_digest")
    @classmethod
    def _content_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("replacement_text")
    @classmethod
    def _replacement_text(cls, value: str) -> str:
        normalized = unicodedata.normalize("NFC", value)
        if normalized != value:
            raise ValueError("replacement_text must already be NFC-normalized")
        return value

    @model_validator(mode="after")
    def _range(self) -> "LocalDraftEdit":
        if self.start_byte < 0 or self.end_byte < self.start_byte:
            raise ValueError("local draft edit range is invalid")
        return self


class GovernedOperationReference(_StrictDiagnosticModel):
    """An invitation to an ordinary operation, not an embedded affordance."""

    tag: Literal["playbill-governed-operation-ref-v1"] = "playbill-governed-operation-ref-v1"
    operation: GovernedOperation
    subject: SemanticAddress | None = None


class CompilerDiagnostic(_StrictDiagnosticModel):
    """Stable diagnostic identity with evolvable human wording."""

    tag: Literal["playbill-diagnostic-v1"] = "playbill-diagnostic-v1"
    code: str
    severity: DiagnosticSeverity
    message: str
    subject: SemanticAddress | None = None
    span: ContentSpan | None = None
    related_subjects: tuple[SemanticAddress, ...] = ()
    local_edits: tuple[LocalDraftEdit, ...] = ()
    operation_references: tuple[GovernedOperationReference, ...] = ()

    @field_validator("code")
    @classmethod
    def _code(cls, value: str) -> str:
        if not _CODE_RE.fullmatch(value):
            raise ValueError("diagnostic code must be a stable namespaced identifier")
        return value

    @field_validator("message")
    @classmethod
    def _message(cls, value: str) -> str:
        normalized = unicodedata.normalize("NFC", value)
        if normalized != value or not value.strip():
            raise ValueError("diagnostic message must be nonblank and NFC-normalized")
        return value

    @field_validator("related_subjects")
    @classmethod
    def _related_subjects(cls, value: tuple[SemanticAddress, ...]) -> tuple[SemanticAddress, ...]:
        ordered = tuple(
            sorted(value, key=lambda item: canonical_bytes(item.model_dump(mode="json")))
        )
        identities = {canonical_bytes(item.model_dump(mode="json")) for item in value}
        if value != ordered or len(identities) != len(value):
            raise ValueError("related subjects must be sorted and unique")
        return value

    def without_protected_body_metadata(self) -> "CompilerDiagnostic":
        """Remove exact spans while preserving stable refusal identity."""

        return self.model_copy(update={"span": None})


__all__ = [
    "CompilerDiagnostic",
    "DiagnosticSeverity",
    "GovernedOperation",
    "GovernedOperationReference",
    "LocalDraftEdit",
]
