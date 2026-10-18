"""A new candidate produced by recompiling retained input at the accepted head."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.projection import AcceptedCoordinate


class ProposalRecompiled(BaseModel):
    """Review the new proposal normally; this call recorded no approval for it."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    tag: Literal["cruxible-proposal-recompiled-v1"] = "cruxible-proposal-recompiled-v1"
    status: Literal["pending_review", "ready_to_activate"]
    source_proposal_id: str
    proposal_id: str
    candidate_digest: str
    members_identical: bool
    accepted_coordinate: AcceptedCoordinate
