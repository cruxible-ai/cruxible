"""Family-neutral governance contracts shared by Playbill acceptance laws."""

from __future__ import annotations

import re
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_client.contracts.canonical import AcceptanceLawDigest

PermissionTier = Literal["governed_write", "graph_write", "admin"]
ActivationPolicy = Literal["abort", "drain", "epoch-check", "snapshot"]
MutationDisposition = Literal[
    "generated-successor",
    "hand-authored-successor",
    "invalidation",
    "replacement",
]

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
PLAYBILL_FIXED_INDEPENDENT_APPROVALS: Final = 1
PLAYBILL_INDEPENDENT_APPROVAL_ROLE: Final = "independent-principal"


class _StrictGovernanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def governance_identifier(value: str, *, label: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{label} must be a canonical lowercase identifier")
    return value


class ApprovalRequirement(_StrictGovernanceModel):
    """One independently satisfied approval role in an evaluated candidate."""

    role: str
    minimum_distinct_signers: int = Field(default=1, ge=1, le=32)

    @field_validator("role")
    @classmethod
    def _role(cls, value: str) -> str:
        return governance_identifier(value, label="approval role")


class AcceptanceLawCoordinate(_StrictGovernanceModel):
    """An installed historical acceptance law selected by accepted artifact state."""

    identifier: str
    digest: str

    @field_validator("identifier")
    @classmethod
    def _identifier(cls, value: str) -> str:
        return governance_identifier(value, label="acceptance-law identifier")

    @field_validator("digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        AcceptanceLawDigest.from_tagged(value)
        return value


__all__ = [
    "AcceptanceLawCoordinate",
    "ActivationPolicy",
    "ApprovalRequirement",
    "MutationDisposition",
    "PermissionTier",
    "PLAYBILL_FIXED_INDEPENDENT_APPROVALS",
    "PLAYBILL_INDEPENDENT_APPROVAL_ROLE",
    "governance_identifier",
]
