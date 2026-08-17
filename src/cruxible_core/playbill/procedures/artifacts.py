"""Governed ``playbill-procedure-v1`` envelope and acceptance law."""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_core.playbill.artifacts import (
    ArtifactAuthority,
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_core.playbill.canonical import ArtifactDigest, canonical_bytes, typed_digest
from cruxible_core.playbill.diagnostics import CompilerDiagnostic
from cruxible_core.playbill.errors import PlaybillFormatError
from cruxible_core.playbill.governance import PermissionTier
from cruxible_core.playbill.procedures.graph import compute_procedure_definition_digest_v3
from cruxible_core.playbill.procedures.models import (
    ProcedureDefinitionV3,
    ProcedurePinSlotRefV1,
    iter_pin_bindings,
)
from cruxible_core.playbill.semantic import SemanticAddress

_PROCEDURE_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")


class ProcedureFormatError(PlaybillFormatError):
    """A Procedure artifact or canonical path is invalid."""


class _StrictProcedureArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _pin_key(pin: ArtifactPin) -> tuple[bytes, bytes, bytes]:
    return (
        pin.role.encode("utf-8"),
        pin.target.qualified.encode("utf-8"),
        pin.artifact_digest.encode("ascii"),
    )


class ProcedureArtifactV1(_StrictProcedureArtifactModel):
    artifact_format: Literal["playbill-procedure-v1"] = "playbill-procedure-v1"
    identity: ArtifactIdentity
    definition: ProcedureDefinitionV3
    definition_digest: str
    authority: ArtifactAuthority
    pins: tuple[ArtifactPin, ...]
    activation_policy: Literal["drain", "abort", "snapshot", "epoch-check"]
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @field_validator("definition_digest")
    @classmethod
    def _definition_digest(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value

    @field_validator("pins")
    @classmethod
    def _pins(cls, value: tuple[ArtifactPin, ...]) -> tuple[ArtifactPin, ...]:
        if value != tuple(sorted(value, key=_pin_key)):
            raise ValueError("Procedure pins must be canonically sorted")
        keys = tuple((pin.role, pin.target.qualified) for pin in value)
        if len(set(keys)) != len(keys):
            raise ValueError("Procedure pins must be unique by role and target")
        return value

    @model_validator(mode="after")
    def _correspondence(self) -> "ProcedureArtifactV1":
        if self.identity.kind != "Procedure" or not _PROCEDURE_NAME_RE.fullmatch(
            self.identity.name
        ):
            raise ValueError("Procedure identity must be path-addressable")
        if self.definition.name != self.identity.name:
            raise ValueError("Procedure definition name must match stable artifact identity")
        expected = compute_procedure_definition_digest_v3(self.definition).tagged
        if self.definition_digest != expected:
            raise ValueError("Procedure definition_digest does not reproduce graph-format v3")
        declared_exact = {
            (pin.role, pin.target.qualified, pin.artifact_digest) for pin in self.pins
        }
        referenced_exact = {
            (binding.role, binding.target.qualified, binding.artifact_digest)
            for binding in iter_pin_bindings(self.definition)
            if isinstance(binding, ArtifactPin)
        }
        if not referenced_exact.issubset(declared_exact):
            raise ValueError("Procedure definition contains exact pins absent from its envelope")
        declared_slots = {slot.slot_name for slot in self.definition.pin_slots}
        referenced_slots = {
            binding.slot_name
            for binding in iter_pin_bindings(self.definition)
            if isinstance(binding, ProcedurePinSlotRefV1)
        }
        if not referenced_slots.issubset(declared_slots):
            raise ValueError("Procedure definition references undeclared slots")
        return self

    @property
    def directly_runnable(self) -> bool:
        return not any(
            isinstance(binding, ProcedurePinSlotRefV1)
            for binding in iter_pin_bindings(self.definition)
        )


def procedure_path(name: str) -> str:
    if not _PROCEDURE_NAME_RE.fullmatch(name):
        raise ProcedureFormatError("Procedure identity is not path-addressable")
    return f"procedures/{name}.yaml"


def render_procedure(procedure: ProcedureArtifactV1) -> bytes:
    return canonical_bytes(procedure.model_dump(mode="json", by_alias=True)) + b"\n"


def parse_procedure(content: bytes, *, path: str) -> ProcedureArtifactV1:
    try:
        procedure = ProcedureArtifactV1.model_validate(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProcedureFormatError(
            "Procedure failed strict playbill-procedure-v1 validation"
        ) from exc
    if path != procedure_path(procedure.identity.name):
        raise ProcedureFormatError("Procedure identity/path disagreement")
    if render_procedure(procedure) != content:
        raise ProcedureFormatError("Procedure is not in canonical wire form")
    return procedure


def procedure_artifact_digest(procedure: ProcedureArtifactV1) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        procedure.model_dump(mode="json", by_alias=True),
    )


class AcceptedProcedureV1(_StrictProcedureArtifactModel):
    path: str
    procedure: ProcedureArtifactV1
    artifact_digest: str

    @model_validator(mode="after")
    def _binding(self) -> "AcceptedProcedureV1":
        if self.path != procedure_path(self.procedure.identity.name):
            raise ValueError("accepted Procedure path does not reproduce")
        if self.artifact_digest != procedure_artifact_digest(self.procedure).tagged:
            raise ValueError("accepted Procedure digest does not reproduce")
        return self


class ProcedureLawResultV1(_StrictProcedureArtifactModel):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    required_tier: PermissionTier | None = None
    approval_scope: tuple[str, ...] = ()
    diagnostics: tuple[CompilerDiagnostic, ...] = ()


def _refusal(code: str, message: str, *, path: str) -> ProcedureLawResultV1:
    return ProcedureLawResultV1(
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


def evaluate_procedure_law(
    procedure: ProcedureArtifactV1,
    *,
    path: str,
    actor_roles: tuple[str, ...],
    predecessor: AcceptedProcedureV1 | None,
) -> ProcedureLawResultV1:
    """Evaluate stable identity, predecessor, author authority, and exact closure."""

    if path != procedure_path(procedure.identity.name):
        return _refusal(
            "playbill.procedure.path_mismatch",
            "Procedure identity/path disagreement.",
            path=path,
        )
    if predecessor is None:
        if procedure.lifecycle.predecessor_digest is not None:
            return _refusal(
                "playbill.procedure.predecessor_missing",
                "A new Procedure cannot name a predecessor.",
                path=path,
            )
        required_roles = procedure.authority.propose_roles
    else:
        if procedure.identity != predecessor.procedure.identity:
            return _refusal(
                "playbill.procedure.stable_identity_changed",
                "A Procedure successor must retain stable identity.",
                path=path,
            )
        if procedure.lifecycle.predecessor_digest != predecessor.artifact_digest:
            return _refusal(
                "playbill.procedure.predecessor_mismatch",
                "Procedure successor does not pin its exact predecessor.",
                path=path,
            )
        required_roles = predecessor.procedure.authority.propose_roles
    if not set(actor_roles).intersection(required_roles):
        return _refusal(
            "playbill.procedure.proposer_authority_missing",
            "Actor lacks a required Procedure proposer role.",
            path=path,
        )
    return ProcedureLawResultV1(
        verdict="accepted",
        artifact_digest=procedure_artifact_digest(procedure).tagged,
        required_tier="governed_write",
        approval_scope=procedure.authority.approve_roles,
    )


__all__ = [
    "AcceptedProcedureV1",
    "ProcedureArtifactV1",
    "ProcedureFormatError",
    "ProcedureLawResultV1",
    "evaluate_procedure_law",
    "parse_procedure",
    "procedure_artifact_digest",
    "procedure_path",
    "render_procedure",
]
