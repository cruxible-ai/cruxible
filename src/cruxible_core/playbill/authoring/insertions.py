"""State reduction over the publication expectations an instance already holds.

Nothing mints one any more. Publishing a Claim as its own page text was the
mint-era overlap the two-block-kinds law refuses, and the authoring door now
refuses `insertion_target` outright, so the mint, the preparation and the
confirmation are gone with it. What an instance that published before the
ruling still needs is here: the accepted-Claim transition its protocol refresh
drives, and the terminal transitions -- expiry, currency loss, and the
abandonment that `block depublish` performs.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import NoReturn

from cruxible_client.contracts.authoring.models import (
    AuthoringIntentV1,
    InsertionExpectationV2,
    build_insertion_terminal_tombstone_v2,
    insertion_result_key,
    update_insertion_expectation_v2,
)
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import ensure_utc

DEFAULT_INSERTION_TOMBSTONE_HORIZON = timedelta(days=30)


class InsertionProtocolError(PlaybillError):
    """An insertion transition is invalid at the durable expectation state."""

    code = "playbill.authoring.publication_transition_invalid"


class PublicationTerminalStateRefused(InsertionProtocolError):
    code = "playbill.authoring.publication_terminal_state"


def _raise(error: type[InsertionProtocolError], message: str) -> NoReturn:
    raise error(f"{error.code}: {message}")


def mark_publication_claim_accepted(
    expectation: InsertionExpectationV2,
    *,
    accepted_coordinate: object,
) -> InsertionExpectationV2:

    coordinate = AcceptedCoordinate.model_validate(accepted_coordinate)
    if expectation.state == "pending":
        return expectation
    if expectation.state != "awaiting_claim_acceptance":
        raise InsertionProtocolError("only an awaiting publication can become pending")
    return update_insertion_expectation_v2(
        expectation,
        state="pending",
        accepted_claim_coordinate=coordinate,
    )


def _terminal_v2(
    intent: AuthoringIntentV1,
    expectation: InsertionExpectationV2,
    *,
    state: str,
    finalized_at: datetime,
    horizon: timedelta = DEFAULT_INSERTION_TOMBSTONE_HORIZON,
) -> InsertionExpectationV2:
    finalized = ensure_utc(finalized_at)
    # The tombstone commits source bytes only for `bound`, because only a bound
    # publication asserts that the block landed. A depublication does not lose
    # which block it took down: the expectation keeps its own `preparation`
    # across the transition, and that is what names the source and the block
    # afterwards.
    preparation = expectation.preparation if state == "bound" else None
    tombstone = build_insertion_terminal_tombstone_v2(
        result_key=insertion_result_key(
            instance_id=intent.instance_id,
            actor_id=intent.actor_id,
            intent_id=intent.intent_id,
            expectation_id=expectation.expectation_id,
        ),
        intent_id=intent.intent_id,
        expectation_id=expectation.expectation_id,
        final_state=state,
        preparation_digest=(None if preparation is None else preparation.preparation_digest),
        source_id=None if preparation is None else preparation.source_id,
        block_id=None if preparation is None else preparation.block_id,
        accepted_claim_identity=expectation.claim_identity,
        accepted_claim_artifact_digest=expectation.original_claim_artifact_digest,
        accepted_claim_coordinate=expectation.accepted_claim_coordinate,
        finalized_at=finalized,
        retain_until=finalized + horizon,
    )
    return update_insertion_expectation_v2(
        expectation,
        state=state,
        terminal_tombstone=tombstone,
    )


def mark_publication_terminal(
    intent: AuthoringIntentV1,
    expectation: InsertionExpectationV2,
    *,
    state: str,
    finalized_at: datetime,
) -> InsertionExpectationV2:
    if expectation.state == state:
        return expectation
    # `bound` is terminal for everything except being taken down. It was
    # terminal for that too, and the consequence was a lifecycle with no exit:
    # publish once and that page carries that block, with that id, forever. A
    # governed page could not be re-modelled, a mistaken publication could not
    # be withdrawn, and a ruling that retired the backing Claim did not release
    # the marker -- `next` kept demanding a frame whose repair was to restore
    # the block the ruling had told you to delete. Abandonment is the one
    # transition out, and it keeps the preparation, so the record still says
    # which block was published and which was taken down.
    if expectation.state == "bound" and state != "abandoned":
        _raise(PublicationTerminalStateRefused, "publication is already terminal")
    if expectation.state in {"expired", "abandoned", "claim_currency_changed"}:
        _raise(PublicationTerminalStateRefused, "publication is already terminal")
    if state == "expired" and ensure_utc(finalized_at) < expectation.expires_at:
        raise InsertionProtocolError("publication has not reached expires_at")
    return _terminal_v2(intent, expectation, state=state, finalized_at=finalized_at)


__all__ = [
    "DEFAULT_INSERTION_TOMBSTONE_HORIZON",
    "InsertionProtocolError",
    "PublicationTerminalStateRefused",
    "mark_publication_claim_accepted",
    "mark_publication_terminal",
]
