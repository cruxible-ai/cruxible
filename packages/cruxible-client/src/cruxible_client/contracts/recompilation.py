"""A new candidate produced by recompiling retained input at the accepted head."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cruxible_client.contracts.projection import AcceptedCoordinate

RECOMPILED_MEMBER_PATH_LIMIT = 50


class RecompiledMemberChange(BaseModel):
    """One own-member byte change between the stale and fresh candidates."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str
    change: Literal["added", "replaced", "removed"]


class ProposalRecompiled(BaseModel):
    """Review the new proposal normally; this call recorded no approval for it.

    ``already_live``: every change the stale proposal carried is already live at
    the head, so nothing is left to propose. The source is closed as answered,
    no new proposal exists (``proposal_id`` and ``candidate_digest`` are absent),
    and nothing needs review.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    tag: Literal["cruxible-proposal-recompiled-v1"] = "cruxible-proposal-recompiled-v1"
    status: Literal["pending_review", "ready_to_activate", "already_live"]
    source_proposal_id: str
    proposal_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    candidate_digest: str | None = Field(default=None, exclude_if=lambda value: value is None)
    members_identical: bool
    changed_members: tuple[RecompiledMemberChange, ...] = Field(
        default=(), max_length=RECOMPILED_MEMBER_PATH_LIMIT
    )
    changed_members_truncated: bool = False
    accepted_coordinate: AcceptedCoordinate

    @model_validator(mode="after")
    def _successor_shape(self) -> "ProposalRecompiled":
        answered = self.status == "already_live"
        if answered != (self.proposal_id is None) or answered != (self.candidate_digest is None):
            raise ValueError(
                "a recompiled proposal names its successor exactly when one was proposed"
            )
        return self
