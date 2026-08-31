"""Governed singleton controlling deterministic Procedure runtime ceilings."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cruxible_client.contracts.canonical import ArtifactDigest, canonical_bytes, typed_digest
from cruxible_client.contracts.errors import PlaybillFormatError

PROCEDURE_RUNTIME_POLICY_PATH = "governance/procedure-runtime-policy.yaml"
PROCEDURE_RUNTIME_POLICY_IDENTITY = "ProcedureRuntimePolicy:instance"


class ProcedureRuntimePolicyFormatError(PlaybillFormatError):
    """The governed Procedure-runtime-policy singleton is absent or malformed."""


class ProcedureRuntimePolicyV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-procedure-runtime-policy-v1"] = "playbill-procedure-runtime-policy-v1"
    provider_output_bytes_cap: int = Field(ge=1)


def render_procedure_runtime_policy(policy: ProcedureRuntimePolicyV1) -> bytes:
    return canonical_bytes(policy.model_dump(mode="json")) + b"\n"


def parse_procedure_runtime_policy(
    content: bytes,
    *,
    path: str,
) -> ProcedureRuntimePolicyV1:
    if path != PROCEDURE_RUNTIME_POLICY_PATH:
        raise ProcedureRuntimePolicyFormatError(
            "Procedure runtime policy must use its singleton path"
        )
    try:
        payload = json.loads(content)
        policy = ProcedureRuntimePolicyV1.model_validate(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProcedureRuntimePolicyFormatError(
            "Procedure runtime policy failed strict validation"
        ) from exc
    if render_procedure_runtime_policy(policy) != content:
        raise ProcedureRuntimePolicyFormatError("Procedure runtime policy is not canonical")
    return policy


def procedure_runtime_policy_digest(policy: ProcedureRuntimePolicyV1) -> ArtifactDigest:
    payload = policy.model_dump(mode="json")
    artifact_format = str(payload.pop("tag"))
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        {"artifact_format": artifact_format, **payload},
    )


__all__ = [
    "PROCEDURE_RUNTIME_POLICY_IDENTITY",
    "PROCEDURE_RUNTIME_POLICY_PATH",
    "ProcedureRuntimePolicyFormatError",
    "ProcedureRuntimePolicyV1",
    "parse_procedure_runtime_policy",
    "procedure_runtime_policy_digest",
    "render_procedure_runtime_policy",
]
