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
    assert TreeReadLimits().max_blob_bytes == 64 * 1024 * 1024


def test_the_advertised_member_budget_fits_under_the_record_ceiling() -> None:
    """The budget a caller is told, and the budget that settles, are one number.

    At the 4 MiB ceiling these two were different numbers for one thing:
    admission advertised `max_changed_members: 5000` while the ledger's own
    record of a change set, at 11,264 bytes per lowered entry, held 372 of them.
    A caller who believed the advertised budget authored a set that passed every
    receive bound and then could not be recorded.

    The ceiling is 64 MiB, so a submission of the whole advertised budget
    projects to 56,320,000 bytes -- about 53.7 MiB -- and fits. Moving either
    number back into disagreement is what this refuses.
    """

    limits = ProposalReceiveLimits()

    assert limits.projected_change_set_record_bytes(limits.max_changed_members) == 56_320_000
    assert (
        limits.projected_change_set_record_bytes(limits.max_changed_members)
        <= limits.max_change_set_record_bytes
    )
    assert limits.max_change_set_members >= limits.max_changed_members


def test_a_member_receive_admits_is_a_member_the_ledger_can_read_back() -> None:
    """Receive's per-file ceiling never exceeds the per-blob read ceiling.

    Receive takes a single member of up to `max_file_bytes`, and every blob it
    accepts is later read back through `TreeReadLimits.max_blob_bytes`. While
    the read ceiling was 4 MiB and the receive ceiling 8 MiB, a captured source
    between them was admitted and then unreadable -- accepted state no reader
    could open. The read ceiling is now 64 MiB, well above the receive one, so
    a 5 MiB capture is both admissible and citable.
    """

    receive = ProposalReceiveLimits()
    read = TreeReadLimits()

    assert receive.max_file_bytes <= read.max_blob_bytes
    assert 5 * 1024 * 1024 <= receive.max_file_bytes
    assert 5 * 1024 * 1024 <= read.max_blob_bytes
