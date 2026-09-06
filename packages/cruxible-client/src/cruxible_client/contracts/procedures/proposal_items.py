"""What one `propose_change_set` terminal item is allowed to say.

A Procedure's `candidate_templates` are canonical objects the graph resolves
at run time. Once resolved, each one has to be exactly one of the shapes
below before it can become a change-set member; anything else refuses the
terminal, typed to the item, before lowering runs.

The item names a Claim STATEMENT and the author's rationale, and nothing
about its evidence. Evidence is never caller-supplied: the daemon attaches the
produced Capture that sits in the item's own dependency closure, so a computed
interpretation cites exactly the observation the run made and cannot borrow,
invent, or omit one.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from cruxible_client.contracts.authoring.models import AuthoringClaimStatementV1
from cruxible_client.contracts.claims import claim_path


class _StrictProposalItemModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProcedureClaimProposalItemV1(_StrictProposalItemModel):
    """One Claim a Procedure proposes: its statement, its rationale, its lineage."""

    tag: Literal["playbill-procedure-claim-proposal-item-v1"] = (
        "playbill-procedure-claim-proposal-item-v1"
    )
    statement: AuthoringClaimStatementV1
    rationale: str
    revises: str | None = None

    @field_validator("rationale")
    @classmethod
    def _rationale(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("proposal item rationale must not be empty")
        return value

    @field_validator("revises")
    @classmethod
    def _revises(cls, value: str | None) -> str | None:
        if value is not None:
            claim_path(value)
        return value


__all__ = ["ProcedureClaimProposalItemV1"]
