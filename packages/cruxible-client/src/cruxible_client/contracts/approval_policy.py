"""Governed singleton controlling ordinary-candidate approval requirements."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    artifact_bytes_for_path,
    artifact_path_matches,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.errors import PlaybillFormatError

APPROVAL_POLICY_PATH = "governance/approval-policy.json"
APPROVAL_POLICY_IDENTITY = "ApprovalPolicy:instance"
ApprovalPolicyMode = Literal[
    "self_approval_allowed",
    "independent_approval_required",
]


class ApprovalPolicyFormatError(PlaybillFormatError):
    """The governed approval-policy singleton is absent or malformed."""


class ApprovalPolicyV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-approval-policy-v1"] = "playbill-approval-policy-v1"
    mode: ApprovalPolicyMode


def render_approval_policy(policy: ApprovalPolicyV1) -> bytes:
    return pretty_canonical_bytes(policy.model_dump(mode="json"))


def parse_approval_policy(content: bytes, *, path: str) -> ApprovalPolicyV1:
    if not artifact_path_matches(APPROVAL_POLICY_PATH, path):
        raise ApprovalPolicyFormatError("approval policy must use its singleton path")
    try:
        payload = json.loads(content)
        policy = ApprovalPolicyV1.model_validate(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ApprovalPolicyFormatError("approval policy failed strict validation") from exc
    if artifact_bytes_for_path(render_approval_policy(policy), path) != content:
        raise ApprovalPolicyFormatError("approval policy is not canonical")
    return policy


def approval_policy_digest(policy: ApprovalPolicyV1) -> ArtifactDigest:
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
    "ApprovalPolicyV1",
    "approval_policy_digest",
    "parse_approval_policy",
    "render_approval_policy",
]
