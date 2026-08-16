"""Typed compatibility records for historical resolution evidence."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from cruxible_core.graph.evidence import EvidenceRef
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.primitives import canonical_json


class LegacyResolutionAttestationV1(BaseModel):
    """Minimum immutable record needed to replay an old resolution measurement."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attestation_id: str
    relationship_type: str
    from_type: str
    from_id: str
    to_type: str
    to_id: str
    edge_key: int | None = None
    claim_id: str | None = None
    claim_content_digest: str
    claim_state_at_record: str
    stance: Literal["support", "contradict", "unsure"]
    evidence_refs: tuple[EvidenceRef, ...] = ()
    observed_at: datetime
    recorded_at: datetime
    actor_context: GovernedActorContext

    def claim_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.relationship_type,
            self.from_type,
            self.from_id,
            self.to_type,
            self.to_id,
        )


class LegacyResolutionDispositionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    disposition_id: str
    attestation_id: str
    verdict: Literal["upheld", "corrected", "invalidated"]
    reviewer_actor_context: GovernedActorContext
    recorded_at: datetime


def compute_claim_content_digest(
    relationship_type: str,
    from_type: str,
    from_id: str,
    to_type: str,
    to_id: str,
    properties: dict[str, Any],
) -> str:
    payload = {
        "relationship_type": relationship_type,
        "from_type": from_type,
        "from_id": from_id,
        "to_type": to_type,
        "to_id": to_id,
        "properties": properties,
    }
    return f"sha256:{hashlib.sha256(canonical_json(payload).encode()).hexdigest()}"


__all__ = [
    "LegacyResolutionAttestationV1",
    "LegacyResolutionDispositionV1",
    "compute_claim_content_digest",
]
