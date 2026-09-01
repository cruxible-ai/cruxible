"""Governed, exact-Procedure authority grants for effectful terminals."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.canonical import (
    CURRENT_ARTIFACT_CODEC,
    ArtifactCodec,
    ArtifactDigest,
    Sha256Value,
    artifact_bytes_for_path,
    artifact_path_matches,
    normalize_ledger_path,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedureV1
from cruxible_client.contracts.procedures.models import ProcedureHardCapsV3
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.temporal import ensure_utc, format_datetime

_MANDATE_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")


class ProcedureMandateError(PlaybillFormatError):
    """A ProcedureMandate is malformed or cannot authorize an invocation."""


class _StrictProcedureMandateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _namespace_key(value: str) -> bytes:
    return value.encode("utf-8")


class ProcedureMandateV1(_StrictProcedureMandateModel):
    """One finite grant pinned to one exact accepted Procedure artifact."""

    artifact_format: Literal["playbill-procedure-mandate-v1"] = "playbill-procedure-mandate-v1"
    identity: ArtifactIdentity
    procedure: ArtifactPin
    rung: Literal[2, 3]
    authority_ceiling: ProcedureHardCapsV3
    namespace: tuple[str, ...]
    valid_from: datetime
    expires_at: datetime
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @field_validator("namespace")
    @classmethod
    def _namespace(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or value != tuple(sorted(set(value), key=_namespace_key)):
            raise ValueError("ProcedureMandate namespace must be nonempty, sorted, and unique")
        for member in value:
            if normalize_ledger_path(member) != member:
                raise ValueError("ProcedureMandate namespace must use canonical ledger prefixes")
        return value

    @field_validator("valid_from", "expires_at")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("valid_from", "expires_at", when_used="json")
    def _serialize_time(self, value: datetime) -> str | None:
        return format_datetime(value)

    @model_validator(mode="after")
    def _shape(self) -> "ProcedureMandateV1":
        if self.identity.kind != "ProcedureMandate" or not _MANDATE_NAME_RE.fullmatch(
            self.identity.name
        ):
            raise ValueError("ProcedureMandate identity is not path-addressable")
        if self.procedure.role != "procedure" or self.procedure.target.kind != "Procedure":
            raise ValueError("ProcedureMandate must pin one exact Procedure")
        if self.expires_at <= self.valid_from:
            raise ValueError("ProcedureMandate requires a finite increasing interval")
        return self

    @property
    def pins(self) -> tuple[ArtifactPin, ...]:
        return (self.procedure,)


def procedure_mandate_path(name: str) -> str:
    if not _MANDATE_NAME_RE.fullmatch(name):
        raise ProcedureMandateError("ProcedureMandate identity is not path-addressable")
    return f"procedure-mandates/{name}.json"


def render_procedure_mandate(mandate: ProcedureMandateV1) -> bytes:
    return pretty_canonical_bytes(mandate.model_dump(mode="json"))


def parse_procedure_mandate(
    content: bytes,
    *,
    path: str,
    codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC,
) -> ProcedureMandateV1:
    try:
        mandate = ProcedureMandateV1.model_validate(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProcedureMandateError("ProcedureMandate failed strict v1 validation") from exc
    if not artifact_path_matches(procedure_mandate_path(mandate.identity.name), path, codec=codec):
        raise ProcedureMandateError("ProcedureMandate identity/path disagreement")
    if artifact_bytes_for_path(render_procedure_mandate(mandate), path, codec=codec) != content:
        raise ProcedureMandateError("ProcedureMandate is not in canonical wire form")
    return mandate


def procedure_mandate_digest(mandate: ProcedureMandateV1) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        mandate.model_dump(mode="json"),
    )


class AcceptedProcedureMandateV1(_StrictProcedureMandateModel):
    path: str
    mandate: ProcedureMandateV1
    artifact_digest: str

    @model_validator(mode="after")
    def _binding(self) -> "AcceptedProcedureMandateV1":
        if self.path != procedure_mandate_path(self.mandate.identity.name) or (
            self.artifact_digest != procedure_mandate_digest(self.mandate).tagged
        ):
            raise ValueError("accepted ProcedureMandate does not reproduce")
        return self


class ProcedureMandateLawResultV1(_StrictProcedureMandateModel):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    required_tier: PermissionTier | None = None
    approval_scope: tuple[str, ...] = ()
    diagnostics: tuple[CompilerDiagnostic, ...] = ()


def _law_refusal(code: str, message: str, *, path: str) -> ProcedureMandateLawResultV1:
    return ProcedureMandateLawResultV1(
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


def _ceiling_within(
    ceiling: ProcedureHardCapsV3,
    hard_caps: ProcedureHardCapsV3,
) -> bool:
    return (
        ceiling.max_wall_clock.microseconds <= hard_caps.max_wall_clock.microseconds
        and ceiling.max_provider_calls <= hard_caps.max_provider_calls
        and ceiling.max_capture_bytes <= hard_caps.max_capture_bytes
        and ceiling.max_items <= hard_caps.max_items
        and ceiling.max_repeat_attempts <= hard_caps.max_repeat_attempts
    )


def evaluate_procedure_mandate_law(
    mandate: ProcedureMandateV1,
    *,
    path: str,
    predecessor: AcceptedProcedureMandateV1 | None,
    procedure: AcceptedProcedureV1,
) -> ProcedureMandateLawResultV1:
    if path != procedure_mandate_path(mandate.identity.name):
        return _law_refusal(
            "playbill.procedure_mandate.path_mismatch",
            "ProcedureMandate identity/path disagreement.",
            path=path,
        )
    if mandate.procedure.target != procedure.procedure.identity or (
        mandate.procedure.artifact_digest != procedure.artifact_digest
    ):
        return _law_refusal(
            "playbill.procedure_mandate.procedure_mismatch",
            "ProcedureMandate must pin the exact candidate Procedure artifact.",
            path=path,
        )
    if not _ceiling_within(mandate.authority_ceiling, procedure.procedure.definition.hard_caps):
        return _law_refusal(
            "playbill.procedure_mandate.authority_ceiling_widens_procedure",
            "ProcedureMandate authority_ceiling may narrow but never widen Procedure hard caps.",
            path=path,
        )
    if predecessor is None and mandate.lifecycle.predecessor_digest is not None:
        return _law_refusal(
            "playbill.procedure_mandate.predecessor_missing",
            "A new ProcedureMandate cannot name a predecessor.",
            path=path,
        )
    if predecessor is not None and (
        mandate.identity != predecessor.mandate.identity
        or mandate.lifecycle.predecessor_digest != predecessor.artifact_digest
    ):
        return _law_refusal(
            "playbill.procedure_mandate.predecessor_mismatch",
            "ProcedureMandate successor identity or predecessor differs.",
            path=path,
        )
    return ProcedureMandateLawResultV1(
        verdict="accepted",
        artifact_digest=procedure_mandate_digest(mandate).tagged,
        required_tier="governed_write",
        approval_scope=(),
    )


class ProcedureMandateInvocationV1(_StrictProcedureMandateModel):
    tag: Literal["playbill-procedure-mandate-invocation-v1"] = (
        "playbill-procedure-mandate-invocation-v1"
    )
    procedure_identity: ArtifactIdentity
    procedure_artifact_digest: str
    requested_rung: Literal[2, 3]
    requested_authority: ProcedureHardCapsV3
    target_paths: tuple[str, ...]
    evaluation_time: datetime
    accepted_mandate_digest: str

    @field_validator("procedure_artifact_digest", "accepted_mandate_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("target_paths")
    @classmethod
    def _target_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or value != tuple(sorted(set(value), key=_namespace_key)):
            raise ValueError("ProcedureMandate target paths must be nonempty, sorted, and unique")
        for path in value:
            if normalize_ledger_path(path) != path:
                raise ValueError("ProcedureMandate target paths must be canonical ledger paths")
        return value

    @field_validator("evaluation_time")
    @classmethod
    def _evaluation_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("evaluation_time", when_used="json")
    def _serialize_evaluation_time(self, value: datetime) -> str | None:
        return format_datetime(value)


class ProcedureMandateEvaluationV1(_StrictProcedureMandateModel):
    tag: Literal["playbill-procedure-mandate-evaluation-v1"] = (
        "playbill-procedure-mandate-evaluation-v1"
    )
    verdict: Literal["permitted", "refused"]
    mandate_digest: str
    refusal_codes: tuple[str, ...] = ()


def procedure_mandate_evaluation_digest(evaluation: ProcedureMandateEvaluationV1) -> str:
    payload = evaluation.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(
        Sha256Value,
        "playbill-procedure-mandate-evaluation-v1",
        payload,
    ).tagged


def _path_is_in_namespace(path: str, namespace: tuple[str, ...]) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in namespace)


def evaluate_procedure_mandate(
    mandate: ProcedureMandateV1,
    invocation: ProcedureMandateInvocationV1,
) -> ProcedureMandateEvaluationV1:
    refusals: set[str] = set()
    digest = procedure_mandate_digest(mandate).tagged
    if invocation.accepted_mandate_digest != digest or mandate.lifecycle.state != "live":
        refusals.add("procedure_mandate_superseded")
    if not (mandate.valid_from <= invocation.evaluation_time < mandate.expires_at):
        refusals.add("procedure_mandate_expired")
    if (
        invocation.procedure_identity != mandate.procedure.target
        or invocation.procedure_artifact_digest != mandate.procedure.artifact_digest
    ):
        refusals.add("procedure_mandate_procedure_mismatch")
    if invocation.requested_rung > mandate.rung:
        refusals.add("procedure_mandate_rung_insufficient")
    if not _ceiling_within(invocation.requested_authority, mandate.authority_ceiling):
        refusals.add("procedure_mandate_authority_ceiling_insufficient")
    if any(not _path_is_in_namespace(path, mandate.namespace) for path in invocation.target_paths):
        refusals.add("procedure_mandate_namespace_mismatch")
    return ProcedureMandateEvaluationV1(
        verdict="refused" if refusals else "permitted",
        mandate_digest=digest,
        refusal_codes=tuple(sorted(refusals)),
    )


__all__ = [
    "AcceptedProcedureMandateV1",
    "ProcedureMandateError",
    "ProcedureMandateEvaluationV1",
    "ProcedureMandateInvocationV1",
    "ProcedureMandateLawResultV1",
    "ProcedureMandateV1",
    "evaluate_procedure_mandate",
    "evaluate_procedure_mandate_law",
    "parse_procedure_mandate",
    "procedure_mandate_digest",
    "procedure_mandate_evaluation_digest",
    "procedure_mandate_path",
    "render_procedure_mandate",
]
