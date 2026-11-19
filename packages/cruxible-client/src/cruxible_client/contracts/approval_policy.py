"""Governed singleton controlling ordinary-candidate approval requirements."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.errors import FormatError

APPROVAL_POLICY_PATH = "governance/approval-policy.json"
APPROVAL_POLICY_IDENTITY = "ApprovalPolicy:instance"
ApprovalPolicyMode = Literal[
    "self_approval_allowed",
    "independent_approval_required",
]


class ApprovalPolicyFormatError(FormatError):
    """The governed approval-policy singleton is absent or malformed."""


class ApprovalPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-approval-policy-v1"] = "playbill-approval-policy-v1"
    mode: ApprovalPolicyMode


def render_approval_policy(policy: ApprovalPolicy) -> bytes:
    return pretty_canonical_bytes(policy.model_dump(mode="json"))


def parse_approval_policy(
    content: bytes,
    *,
    path: str,
) -> ApprovalPolicy:
    if APPROVAL_POLICY_PATH != path:
        raise ApprovalPolicyFormatError("approval policy must use its singleton path")
    try:
        payload = json.loads(content)
        policy = ApprovalPolicy.model_validate(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ApprovalPolicyFormatError("approval policy failed strict validation") from exc
    if render_approval_policy(policy) != content:
        raise ApprovalPolicyFormatError("approval policy is not canonical")
    return policy


def approval_policy_digest(policy: ApprovalPolicy) -> ArtifactDigest:
    payload = policy.model_dump(mode="json")
    artifact_format = str(payload.pop("tag"))
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        {"artifact_format": artifact_format, **payload},
    )


__all__ = [
    "APPROVAL_POLICY_IDENTITY",
    "APPROVAL_POLICY_PATH",
    "ApprovalPolicyFormatError",
    "ApprovalPolicyMode",
    "ApprovalPolicy",
    "approval_policy_digest",
    "parse_approval_policy",
    "render_approval_policy",
]
