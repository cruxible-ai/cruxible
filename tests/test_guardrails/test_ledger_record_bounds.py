"""The advertised ledger ceiling and the bound that actually fires stay equal."""

from __future__ import annotations

from cruxible_client.contracts.proposal_models import ProposalReceiveLimits
from cruxible_core.playbill.projection_tree import TreeReadLimits


def test_the_advertised_record_ceiling_is_the_ceiling_that_fires() -> None:
    """One number, declared twice, with nothing making the copies agree.

    `ProposalReceiveLimits.max_change_set_record_bytes` is what a caller reads
    to learn how large a change set may be, and what preflight refuses against.
    The bound that actually stops an oversized change-set record from being
    written is `TreeReadLimits.max_blob_bytes`, a separate default in a
    different package -- the client package cannot import core, so the
    advertisement is a hand copy.

    Moving either one alone breaks the advertisement in the PERMISSIVE
    direction: preflight would admit a set the ledger then refuses at
    activation, which is the whole of card 110. This is the only thing holding
    the copies together.
    """

    assert ProposalReceiveLimits().max_change_set_record_bytes == TreeReadLimits().max_blob_bytes
