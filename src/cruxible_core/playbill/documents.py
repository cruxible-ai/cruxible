"""Document v1 envelopes, frozen digest dispatch, and acceptance law."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cruxible_core.playbill.canonical import (
    ArtifactDigest,
    CasDigest,
    canonical_bytes,
    normalize_ledger_path,
    typed_digest,
)
from cruxible_core.playbill.diagnostics import CompilerDiagnostic
from cruxible_core.playbill.errors import (
    CanonicalEncodingError,
    DocumentFormatError,
    PlaybillCasError,
)
from cruxible_core.playbill.semantic import SemanticAddress

PermissionTier = Literal["governed_write", "graph_write", "admin"]
DocumentApprovalRole = Literal["owner", "reviewer"]
DocumentActivationPolicy = Literal["snapshot"]

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_KIND_QUALIFIED_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}:[a-z][a-z0-9_.-]{0,255}$")
_MEDIA_TYPE_RE = re.compile(r"^[a-z][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$")
_SCOPE_RE = re.compile(r"^[a-z][a-z0-9_.:/-]{0,255}$")


class _StrictDocumentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _identifier(value: str, *, label: str) -> str:
    if unicodedata.normalize("NFC", value) != value or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{label} must be a canonical lowercase identifier")
    return value


def _kind_qualified(value: str, *, label: str) -> str:
    if unicodedata.normalize("NFC", value) != value or not _KIND_QUALIFIED_RE.fullmatch(value):
        raise ValueError(f"{label} must be a canonical kind-qualified identity")
    return value


class DocumentLink(_StrictDocumentModel):
    relation: str
    target_identity: str

    @field_validator("relation")
    @classmethod
    def _relation(cls, value: str) -> str:
        return _identifier(value, label="document link relation")

    @field_validator("target_identity")
    @classmethod
    def _target_identity(cls, value: str) -> str:
        return _kind_qualified(value, label="document link target")


class DocumentPin(_StrictDocumentModel):
    role: str
    target_identity: str
    target_digest: str

    @field_validator("role")
    @classmethod
    def _role(cls, value: str) -> str:
        return _identifier(value, label="document pin role")

    @field_validator("target_identity")
    @classmethod
    def _target_identity(cls, value: str) -> str:
        return _kind_qualified(value, label="document pin target")

    @field_validator("target_digest")
    @classmethod
    def _target_digest(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value


class DocumentAuthority(_StrictDocumentModel):
    required_tier: PermissionTier = "governed_write"
    approval_roles: tuple[DocumentApprovalRole, ...] = ("owner",)

    @field_validator("approval_roles")
    @classmethod
    def _approval_roles(
        cls, value: tuple[DocumentApprovalRole, ...]
    ) -> tuple[DocumentApprovalRole, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("document approval_roles must be sorted and unique")
        if not value:
            raise ValueError("document authority requires at least one approval role")
        return value


class DocumentLifecycle(_StrictDocumentModel):
    revision: int = Field(ge=1, le=2**63 - 1)
    status: Literal["active"] = "active"
    activation_policy: DocumentActivationPolicy = "snapshot"


class DocumentShell(_StrictDocumentModel):
    """Canonical light envelope; body bytes remain outside the ledger."""

    tag: Literal["playbill-document-v1"] = "playbill-document-v1"
    format_version: Literal[1] = 1
    kind: Literal["document"] = "document"
    identity: str
    document_kind: str
    title: str
    media_type: str
    body_digest: str
    links: tuple[DocumentLink, ...] = ()
    pins: tuple[DocumentPin, ...] = ()
    authority: DocumentAuthority = DocumentAuthority()
    governance_scope: tuple[str, ...]
    predecessor_digest: str | None = None
    lifecycle: DocumentLifecycle

    @field_validator("identity")
    @classmethod
    def _identity(cls, value: str) -> str:
        value = _kind_qualified(value, label="document identity")
        if not value.startswith("document:"):
            raise ValueError("Document identity must use the 'document:' kind")
        return value

    @field_validator("document_kind")
    @classmethod
    def _document_kind(cls, value: str) -> str:
        return _identifier(value, label="document_kind")

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        if unicodedata.normalize("NFC", value) != value or not value.strip():
            raise ValueError("document title must be nonblank and NFC-normalized")
        has_control = any(ord(character) < 32 and character not in "\t" for character in value)
        if len(value) > 1024 or has_control:
            raise ValueError("document title is too long or contains control characters")
        return value

    @field_validator("media_type")
    @classmethod
    def _media_type(cls, value: str) -> str:
        if not _MEDIA_TYPE_RE.fullmatch(value):
            raise ValueError("media_type must be a canonical lowercase type/subtype")
        return value

    @field_validator("body_digest")
    @classmethod
    def _body_digest(cls, value: str) -> str:
        CasDigest.from_tagged(value)
        return value

    @field_validator("predecessor_digest")
    @classmethod
    def _predecessor_digest(cls, value: str | None) -> str | None:
        if value is not None:
            ArtifactDigest.from_tagged(value)
        return value

    @field_validator("governance_scope")
    @classmethod
    def _governance_scope(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))) != value:
            raise ValueError("governance_scope must be nonempty, UTF-8-sorted, and unique")
        if any(not _SCOPE_RE.fullmatch(item) for item in value):
            raise ValueError("governance_scope entries must be canonical scope identifiers")
        return value

    @field_validator("links")
    @classmethod
    def _links(cls, value: tuple[DocumentLink, ...]) -> tuple[DocumentLink, ...]:
        def key(item: DocumentLink) -> tuple[bytes, bytes]:
            return item.relation.encode("utf-8"), item.target_identity.encode("utf-8")

        if tuple(sorted(value, key=key)) != value or len({key(item) for item in value}) != len(
            value
        ):
            raise ValueError("document links must be sorted and unique")
        return value

    @field_validator("pins")
    @classmethod
    def _pins(cls, value: tuple[DocumentPin, ...]) -> tuple[DocumentPin, ...]:
        def key(item: DocumentPin) -> tuple[bytes, bytes]:
            return item.role.encode("utf-8"), item.target_identity.encode("utf-8")

        if tuple(sorted(value, key=key)) != value or len({key(item) for item in value}) != len(
            value
        ):
            raise ValueError("document pins must be sorted and unique")
        return value

    @property
    def document_id(self) -> str:
        return self.identity.partition(":")[2]


def document_path(document_id: str) -> str:
    return f"documents/{_identifier(document_id, label='document_id')}.yaml"


def validate_document_path(shell: DocumentShell, path: str) -> str:
    try:
        normalized = normalize_ledger_path(path)
    except CanonicalEncodingError as exc:
        raise DocumentFormatError("Document path is not a canonical ledger path") from exc
    expected = document_path(shell.document_id)
    if normalized != path or path != expected:
        raise DocumentFormatError(
            f"Document identity/path disagreement: {shell.identity!r} requires {expected!r}"
        )
    return path


def render_document(shell: DocumentShell) -> bytes:
    return canonical_bytes(shell.model_dump(mode="json")) + b"\n"


def parse_document(content: bytes, *, path: str) -> DocumentShell:
    """Dispatch by explicit format and refuse unknown or noncanonical wire forms."""

    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise DocumentFormatError("Document shell is not strict JSON") from exc
    if not isinstance(payload, dict) or payload.get("tag") != "playbill-document-v1":
        declared = payload.get("tag") if isinstance(payload, dict) else None
        raise DocumentFormatError(f"unsupported Document artifact format: {declared!r}")
    try:
        shell = DocumentShell.model_validate(payload)
    except ValidationError as exc:
        raise DocumentFormatError("Document shell failed strict v1 validation") from exc
    if render_document(shell) != content:
        raise DocumentFormatError("Document shell is not in canonical wire form")
    validate_document_path(shell, path)
    return shell


def _document_digest_v1(shell: DocumentShell) -> ArtifactDigest:
    payload = shell.model_dump(mode="json")
    artifact_format = str(payload.pop("tag"))
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        {"artifact_format": artifact_format, **payload},
    )


DOCUMENT_DIGEST_FUNCTIONS: dict[int, Callable[[DocumentShell], ArtifactDigest]] = {
    1: _document_digest_v1,
}
"""Frozen digest dispatcher; new Document formats add entries, never mutate v1."""


def document_digest(shell: DocumentShell) -> ArtifactDigest:
    return DOCUMENT_DIGEST_FUNCTIONS[shell.format_version](shell)


class BodyVerifierProtocol(Protocol):
    def verify(self, digest: str) -> bool: ...


class AcceptedDocument(_StrictDocumentModel):
    path: str
    shell: DocumentShell
    envelope_digest: str

    @field_validator("envelope_digest")
    @classmethod
    def _envelope_digest(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value


class DocumentLawResult(_StrictDocumentModel):
    verdict: Literal["accepted", "refused"]
    envelope_digest: str | None = None
    required_tier: PermissionTier | None = None
    approval_scope: tuple[DocumentApprovalRole, ...] = ()
    activation_policy: DocumentActivationPolicy | None = None
    diagnostics: tuple[CompilerDiagnostic, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> "DocumentLawResult":
        if self.verdict == "accepted":
            if (
                self.envelope_digest is None
                or self.required_tier is None
                or self.activation_policy is None
                or self.diagnostics
            ):
                raise ValueError("accepted Document law result is incomplete")
        elif self.envelope_digest is not None:
            raise ValueError("refused Document law result cannot carry an envelope digest")
        return self


def _diagnostic(code: str, message: str, *, path: str) -> CompilerDiagnostic:
    return CompilerDiagnostic(
        code=code,
        severity="error",
        message=message,
        subject=SemanticAddress.whole_artifact(path),
    )


def evaluate_document_law(
    shell: DocumentShell,
    *,
    path: str,
    bodies: BodyVerifierProtocol,
    predecessor: AcceptedDocument | None,
) -> DocumentLawResult:
    """Evaluate the complete dependency-free Family-1 Document acceptance law."""

    try:
        validate_document_path(shell, path)
    except DocumentFormatError as exc:
        return DocumentLawResult(
            verdict="refused",
            diagnostics=(
                _diagnostic("playbill.document.identity_path_mismatch", str(exc), path=path),
            ),
        )

    try:
        body_present = bodies.verify(shell.body_digest)
    except PlaybillCasError:
        body_present = False
    if not body_present:
        return DocumentLawResult(
            verdict="refused",
            diagnostics=(
                _diagnostic(
                    "playbill.document.body_missing",
                    "The exact content-addressed Document body is unavailable.",
                    path=path,
                ),
            ),
        )

    digest = document_digest(shell).tagged
    if predecessor is None:
        if shell.predecessor_digest is not None or shell.lifecycle.revision != 1:
            return DocumentLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.document.unexpected_predecessor",
                        "A new Document must begin at revision 1 without a predecessor.",
                        path=path,
                    ),
                ),
            )
    else:
        if predecessor.shell.identity != shell.identity or predecessor.path != path:
            return DocumentLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.document.predecessor_identity_mismatch",
                        "The live predecessor has a different Document identity.",
                        path=path,
                    ),
                ),
            )
        if shell.predecessor_digest != predecessor.envelope_digest:
            return DocumentLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.document.stale_predecessor",
                        "The proposed Document does not name the exact live predecessor digest.",
                        path=path,
                    ),
                ),
            )
        if shell.lifecycle.revision != predecessor.shell.lifecycle.revision + 1:
            return DocumentLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.document.revision_mismatch",
                        "Document supersession must advance the predecessor revision by one.",
                        path=path,
                    ),
                ),
            )
        if digest == predecessor.envelope_digest:
            return DocumentLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.document.no_semantic_change",
                        "Document supersession must produce a new envelope digest.",
                        path=path,
                    ),
                ),
            )

    return DocumentLawResult(
        verdict="accepted",
        envelope_digest=digest,
        required_tier=shell.authority.required_tier,
        approval_scope=shell.authority.approval_roles,
        activation_policy=shell.lifecycle.activation_policy,
    )


__all__ = [
    "AcceptedDocument",
    "BodyVerifierProtocol",
    "DOCUMENT_DIGEST_FUNCTIONS",
    "DocumentActivationPolicy",
    "DocumentApprovalRole",
    "DocumentAuthority",
    "DocumentLawResult",
    "DocumentLifecycle",
    "DocumentLink",
    "DocumentPin",
    "DocumentShell",
    "PermissionTier",
    "document_digest",
    "document_path",
    "evaluate_document_law",
    "parse_document",
    "render_document",
    "validate_document_path",
]
