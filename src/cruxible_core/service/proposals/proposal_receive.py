"""One daemon's operational ceiling on how many members a proposal may change."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cruxible_client.contracts.errors import PlaybillExecutionError
from cruxible_client.contracts.proposal_models import ProposalReceiveLimits

PROPOSAL_RECEIVE_CONFIG_PATH = Path("daemon/proposal-receive.json")


class _StrictProposalReceiveModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProposalReceiveOperationalConfigV1(_StrictProposalReceiveModel):
    """An operator's admission ceiling on one submission's changed members.

    The changed-member limit is an ADMISSION knob, never a product ceiling: one
    authoring intent is one changeset and may carry any mix of members, so the
    only thing this bounds is how large a single submission one daemon is
    willing to receive. It lives in the daemon's own state root beside the other
    operational bounds, where no caller can read or move it, and its default is
    exactly the ratified `ProposalReceiveLimits` default.
    """

    tag: Literal["cruxible-proposal-receive-operational-config-v1"] = (
        "cruxible-proposal-receive-operational-config-v1"
    )
    max_changed_members: int = Field(
        default=ProposalReceiveLimits().max_changed_members,
        ge=1,
        le=1_000_000,
        description="Reads ADMISSION CEILING.",
    )

    def limits(self, base: ProposalReceiveLimits | None = None) -> ProposalReceiveLimits:
        """Apply this daemon's ceiling to the ratified receive limits."""

        return (base or ProposalReceiveLimits()).model_copy(
            update={"max_changed_members": self.max_changed_members}
        )


def load_proposal_receive_config(state_root: Path) -> ProposalReceiveOperationalConfigV1:
    """Read one daemon state root's receive ceiling, or the ratified default.

    An absent file is the default configuration. A file that exists and cannot
    be read as one is a daemon fault, not a caller's: it refuses loudly rather
    than falling back, or an operator's typo would silently restore a bound the
    operator meant to move.
    """

    path = state_root / PROPOSAL_RECEIVE_CONFIG_PATH
    if not path.exists():
        return ProposalReceiveOperationalConfigV1()
    if path.is_symlink() or not path.is_file():
        raise PlaybillExecutionError(
            f"daemon proposal-receive config is not a regular file: {PROPOSAL_RECEIVE_CONFIG_PATH}"
        )
    try:
        return ProposalReceiveOperationalConfigV1.model_validate(json.loads(path.read_bytes()))
    except (OSError, ValueError, ValidationError) as exc:
        raise PlaybillExecutionError(
            f"daemon proposal-receive config is malformed: {PROPOSAL_RECEIVE_CONFIG_PATH}"
        ) from exc


__all__ = [
    "PROPOSAL_RECEIVE_CONFIG_PATH",
    "ProposalReceiveOperationalConfigV1",
    "load_proposal_receive_config",
]
