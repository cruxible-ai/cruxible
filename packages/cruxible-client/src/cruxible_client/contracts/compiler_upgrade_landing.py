"""What `cruxible compiler upgrade` did: landed the upgrade, or why it stopped."""

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from cruxible_client.contracts import (
    ProposalApprovalResult,
    ProposalInspection,
)
from cruxible_client.contracts.projection import AcceptedCoordinate


class CompilerUpgradeLanded(BaseModel):
    """Proposed, signed by the caller and activated in the same command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["landed"] = "landed"
    proposal_id: str
    accepted_coordinate: AcceptedCoordinate


#: Why the command left the upgrade a proposal.
CompilerUpgradeStopReason: TypeAlias = Literal[
    # --no-accept: propose only.
    "accept_never",
    # No signing key was given or found for the caller.
    "signing_key_missing",
    # The approval policy needs a principal other than the proposer.
    "independent_approval_required",
    # The caller's signature was recorded; more approvals are still required.
    "approvals_remaining",
    # The caller's credential cannot activate a compiler upgrade (admin only).
    "activation_not_permitted",
    # Accepted state moved after the proposal was checked.
    "head_moved",
    # The proposal was recompiled at a new head; review the new one.
    "recompiled",
    # The upgrade was evaluated and refused; nothing can be signed.
    "refused",
]


class CompilerUpgradeProposed(BaseModel):
    """The upgrade stays a proposal: the typed reason and the next step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["proposed"] = "proposed"
    proposal_id: str | None = None
    reason: CompilerUpgradeStopReason
    message: str
    next: str | None = None


CompilerUpgradeLanding: TypeAlias = Annotated[
    CompilerUpgradeLanded | CompilerUpgradeProposed, Field(discriminator="status")
]


class CompilerUpgradeSigningKey(BaseModel):
    """The key the command signs with, and where it was found.

    The path stays on the client: the daemon receives only the signature.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    signer_id: str
    private_key_path: str
    source: Literal["option", "environment", "remembered_settings", "default_key_dir"]


class CompilerUpgradeResult(BaseModel):
    """The proposal, the approval the command submitted (if any) and the outcome.

    A preview (``proposal.status`` ``would_propose`` or ``would_block``) has no
    approval and no outcome: nothing was proposed or signed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["cruxible-compiler-upgrade-result-v1"] = "cruxible-compiler-upgrade-result-v1"
    proposal: ProposalInspection
    signing_key: CompilerUpgradeSigningKey | None = None
    approval: ProposalApprovalResult | None = None
    outcome: CompilerUpgradeLanding | None = None


__all__ = [
    "CompilerUpgradeLanded",
    "CompilerUpgradeLanding",
    "CompilerUpgradeProposed",
    "CompilerUpgradeResult",
    "CompilerUpgradeSigningKey",
    "CompilerUpgradeStopReason",
]
