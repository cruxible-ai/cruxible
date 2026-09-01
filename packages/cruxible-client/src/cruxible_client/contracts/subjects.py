"""Identity-only Subject shells, frozen digest dispatch, and acceptance law."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import (
    CURRENT_ARTIFACT_CODEC,
    ArtifactCodec,
    ArtifactDigest,
    Sha256Value,
    artifact_bytes_for_path,
    artifact_path_for_codec,
    artifact_path_matches,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import SubjectFormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.semantic import SemanticAddress

SUBJECT_REUSE_SIGNATURE_DOMAIN = "playbill-subject-reuse-signature-v1"

_SUBJECT_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})*$")
_SUBJECT_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")


class _StrictSubjectModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical(value: str, *, pattern: re.Pattern[str], label: str) -> str:
    if unicodedata.normalize("NFC", value) != value or not pattern.fullmatch(value):
        raise ValueError(f"{label} is not canonical")
    return value


class SubjectShell(_StrictSubjectModel):
    """A stable referent with no mutable properties or free metadata."""

    artifact_format: Literal["playbill-subject-v1"] = "playbill-subject-v1"
    identity: ArtifactIdentity
    subject_kind: str
    subject_id: str
    pins: tuple[ArtifactPin, ...] = ()
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @field_validator("subject_kind")
    @classmethod
    def _subject_kind(cls, value: str) -> str:
        return _canonical(value, pattern=_SUBJECT_KIND_RE, label="subject_kind")

    @field_validator("subject_id")
    @classmethod
    def _subject_id(cls, value: str) -> str:
        return _canonical(value, pattern=_SUBJECT_ID_RE, label="subject_id")

    @field_validator("pins")
    @classmethod
    def _pins(cls, value: tuple[ArtifactPin, ...]) -> tuple[ArtifactPin, ...]:
        def key(pin: ArtifactPin) -> tuple[bytes, bytes]:
            return pin.role.encode("utf-8"), pin.target.qualified.encode("utf-8")

        if tuple(sorted(value, key=key)) != value or len(
            {pin.target.qualified for pin in value}
        ) != len(value):
            raise ValueError("Subject pins must be sorted and unique by target identity")
        return value

    @model_validator(mode="after")
    def _identity_matches_referent(self) -> "SubjectShell":
        expected = ArtifactIdentity(
            kind="Subject",
            name=f"{self.subject_kind}/{self.subject_id}",
        )
        if self.identity != expected:
            raise ValueError("Subject identity must equal Subject:<subject_kind>/<subject_id>")
        return self

    @property
    def qualified_identity(self) -> str:
        return self.identity.qualified


def subject_path(subject_kind: str, subject_id: str) -> str:
    kind = _canonical(subject_kind, pattern=_SUBJECT_KIND_RE, label="subject_kind")
    identifier = _canonical(subject_id, pattern=_SUBJECT_ID_RE, label="subject_id")
    return f"subjects/{kind}/{identifier}.json"


def validate_subject_path(
    shell: SubjectShell,
    path: str,
    *,
    codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC,
) -> str:
    expected = subject_path(shell.subject_kind, shell.subject_id)
    if not artifact_path_matches(expected, path, codec=codec):
        raise SubjectFormatError(
            "Subject identity/path disagreement: "
            f"{shell.qualified_identity!r} requires {artifact_path_for_codec(expected, codec)!r}"
        )
    return path


def render_subject(shell: SubjectShell) -> bytes:
    return pretty_canonical_bytes(shell.model_dump(mode="json"))


def parse_subject(
    content: bytes,
    *,
    path: str,
    codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC,
) -> SubjectShell:
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise SubjectFormatError("Subject shell is not strict JSON") from exc
    if not isinstance(payload, dict) or payload.get("artifact_format") != "playbill-subject-v1":
        declared = payload.get("artifact_format") if isinstance(payload, dict) else None
        raise SubjectFormatError(f"unsupported Subject artifact format: {declared!r}")
    try:
        shell = SubjectShell.model_validate(payload)
    except ValidationError as exc:
        raise SubjectFormatError("Subject shell failed strict v1 validation") from exc
    validate_subject_path(shell, path, codec=codec)
    if artifact_bytes_for_path(render_subject(shell), path, codec=codec) != content:
        raise SubjectFormatError("Subject shell is not in canonical wire form")
    return shell


def _subject_digest_v1(shell: SubjectShell) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        shell.model_dump(mode="json"),
    )


SUBJECT_DIGEST_FUNCTIONS: dict[str, Callable[[SubjectShell], ArtifactDigest]] = {
    "playbill-subject-v1": _subject_digest_v1,
}


def subject_digest(shell: SubjectShell) -> ArtifactDigest:
    return SUBJECT_DIGEST_FUNCTIONS[shell.artifact_format](shell)


def subject_reuse_signature(identity: ArtifactIdentity) -> str:
    """Return the Subject signature shared by write-time reuse and read-time discovery.

    Subject-kind similarity is not evidence that two instances are duplicates, so
    the signature commits the stable identity rather than the shape.
    """

    return typed_digest(
        Sha256Value,
        SUBJECT_REUSE_SIGNATURE_DOMAIN,
        {"identity": identity.qualified},
    ).tagged


class AcceptedSubject(_StrictSubjectModel):
    path: str
    shell: SubjectShell
    artifact_digest: str

    @model_validator(mode="after")
    def _correspondence(self) -> "AcceptedSubject":
        validate_subject_path(self.shell, self.path)
        if self.artifact_digest != subject_digest(self.shell).tagged:
            raise ValueError("accepted Subject digest differs from its exact shell")
        return self


class SubjectLawResult(_StrictSubjectModel):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    required_tier: PermissionTier | None = None
    approval_scope: tuple[str, ...] = ()
    diagnostics: tuple[CompilerDiagnostic, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> "SubjectLawResult":
        if self.verdict == "accepted":
            if self.artifact_digest is None or self.required_tier is None:
                raise ValueError("accepted Subject law result is incomplete")
            if self.diagnostics:
                raise ValueError("accepted Subject law result cannot carry errors")
        elif self.artifact_digest is not None or self.required_tier is not None:
            raise ValueError("refused Subject law result cannot carry acceptance fields")
        return self


def _diagnostic(code: str, message: str, *, path: str) -> CompilerDiagnostic:
    return CompilerDiagnostic(
        code=code,
        severity="error",
        message=message,
        subject=SemanticAddress.whole_artifact(path),
    )


def evaluate_subject_law(
    shell: SubjectShell,
    *,
    path: str,
    predecessor: AcceptedSubject | None,
) -> SubjectLawResult:
    """Evaluate Subject identity and lifecycle under the installed law."""

    try:
        validate_subject_path(shell, path)
    except SubjectFormatError as exc:
        return SubjectLawResult(
            verdict="refused",
            diagnostics=(_diagnostic("playbill.subject.path_mismatch", str(exc), path=path),),
        )

    digest = subject_digest(shell).tagged
    if predecessor is None:
        if shell.lifecycle.predecessor_digest is not None or shell.lifecycle.state != "live":
            return SubjectLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.subject.unexpected_predecessor",
                        "A new Subject must begin live without a predecessor.",
                        path=path,
                    ),
                ),
            )
    else:
        previous = predecessor.shell
        if previous.identity != shell.identity or predecessor.path != path:
            return SubjectLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.subject.predecessor_identity_mismatch",
                        "The live predecessor has a different Subject identity.",
                        path=path,
                    ),
                ),
            )
        if shell.lifecycle.predecessor_digest != predecessor.artifact_digest:
            return SubjectLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.subject.stale_predecessor",
                        "The proposed Subject does not name the exact live predecessor digest.",
                        path=path,
                    ),
                ),
            )
        if previous.lifecycle.state == "retired":
            return SubjectLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.subject.lifecycle_invalid",
                        "A retired Subject cannot be revived or revised.",
                        path=path,
                    ),
                ),
            )
        if digest == predecessor.artifact_digest:
            return SubjectLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.subject.no_semantic_change",
                        "Subject succession must produce a new artifact digest.",
                        path=path,
                    ),
                ),
            )
    return SubjectLawResult(
        verdict="accepted",
        artifact_digest=digest,
        required_tier="governed_write",
        approval_scope=(),
    )


__all__ = [
    "AcceptedSubject",
    "SUBJECT_DIGEST_FUNCTIONS",
    "SUBJECT_REUSE_SIGNATURE_DOMAIN",
    "SubjectLawResult",
    "SubjectShell",
    "evaluate_subject_law",
    "parse_subject",
    "render_subject",
    "subject_digest",
    "subject_path",
    "subject_reuse_signature",
    "validate_subject_path",
]
