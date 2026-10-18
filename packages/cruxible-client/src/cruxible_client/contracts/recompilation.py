"""A new candidate produced by recompiling retained input at the accepted head."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cruxible_client.contracts.projection import AcceptedCoordinate


RECOMPILED_MEMBER_PATH_LIMIT = 50


class RecompiledMemberChange(BaseModel):
    """One own-member byte change between the stale and fresh candidates."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str
    change: Literal["added", "replaced", "removed"]


class ProposalRecompiled(BaseModel):
    """Review the new proposal normally; this call recorded no approval for it."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    tag: Literal["cruxible-proposal-recompiled-v1"] = "cruxible-proposal-recompiled-v1"
    status: Literal["pending_review", "ready_to_activate"]
    source_proposal_id: str
    proposal_id: str
    candidate_digest: str
    members_identical: bool
    changed_members: tuple[RecompiledMemberChange, ...] = Field(
        default=(), max_length=RECOMPILED_MEMBER_PATH_LIMIT
    )
    changed_members_truncated: bool = False
    accepted_coordinate: AcceptedCoordinate
