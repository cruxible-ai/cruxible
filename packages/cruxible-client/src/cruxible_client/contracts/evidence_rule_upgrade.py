"""What the identity-rule conversion inside `claim-type upgrade` reports per ClaimType."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")


class EvidenceRuleConversion(_Model):
    claim_type: str
    #: Accepted contract versions the converted rules admit that the exact rules did not.
    widened_versions: tuple[str, ...] = ()


class EvidenceRuleRefusal(_Model):
    claim_type: str
    reason: str


__all__ = [
    "EvidenceRuleConversion",
    "EvidenceRuleRefusal",
]
