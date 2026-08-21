"""Pure PC-G2 insertion expectation construction and state reduction."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from cruxible_core.playbill.authoring.models import (
    AuthoringIntentV1,
    ClaimAuthoringPayloadV1,
    InsertionConfirmationObservationV1,
    InsertionExpectationV1,
    SelfSourceBodyV1,
    build_insertion_expectation,
    build_insertion_patch_envelope,
    build_insertion_terminal_tombstone,
    insertion_expectation_id,
    insertion_result_key,
    insertion_target_digest,
    update_insertion_expectation,
)
from cruxible_core.playbill.canonical import CasDigest
from cruxible_core.playbill.errors import PlaybillError
from cruxible_core.temporal import ensure_utc

DEFAULT_INSERTION_TOMBSTONE_HORIZON = timedelta(days=30)


class InsertionProtocolError(PlaybillError):
    """An insertion transition is invalid at the durable expectation state."""


def mint_insertion_expectation(
    intent: AuthoringIntentV1,
    *,
    original_claim_artifact_digest: str,
    claim_statement_digest: str,
    expires_at: datetime,
) -> InsertionExpectationV1:
    payload = intent.payload
    if not isinstance(payload, ClaimAuthoringPayloadV1):
        raise InsertionProtocolError("only a Claim intent can mint an insertion expectation")
    if payload.insertion_target is None:
        raise InsertionProtocolError("insertion expectation requires a frozen target")
    if not isinstance(payload.source, SelfSourceBodyV1):
        raise InsertionProtocolError("insertion expectation requires a Flow-B self-source")
    body = payload.source.content
    # CasDigest deliberately shares the ordinary SHA-256 spelling; deriving it
    # locally commits the exact retained body without claiming filesystem truth.
    body_digest_value = CasDigest(hashlib.sha256(body).hexdigest()).tagged
    target = payload.insertion_target
    patch = build_insertion_patch_envelope(
        source_id=target.source_id,
        preimage_digest=target.preimage_digest,
        preimage_byte_length=target.coordinate.source_byte_length,
        selector=target.selector,
        operation=target.operation,
        body_digest=body_digest_value,
        body_byte_length=len(body),
        postimage_digest=target.postimage_digest,
        postimage_byte_length=target.postimage_byte_length,
        target_digest=insertion_target_digest(target),
        expires_at=ensure_utc(expires_at),
    )
    return build_insertion_expectation(
        expectation_id=insertion_expectation_id(
            instance_id=intent.instance_id,
            intent_id=intent.intent_id,
            intent_revision=intent.intent_revision,
        ),
        state="awaiting_claim_acceptance",
        claim_identity=intent.semantic_identity,
        original_claim_artifact_digest=original_claim_artifact_digest,
        claim_statement_digest=claim_statement_digest,
        patch=patch,
    )


def mark_claim_accepted(expectation: InsertionExpectationV1) -> InsertionExpectationV1:
    if expectation.state == "pending":
        return expectation
    if expectation.state != "awaiting_claim_acceptance":
        raise InsertionProtocolError("only an awaiting expectation can become pending")
    return update_insertion_expectation(expectation, state="pending")


def mark_confirming(
    expectation: InsertionExpectationV1,
    *,
    observation: InsertionConfirmationObservationV1,
    citation_id: str,
    proposal_id: str,
    candidate_ref: str,
    candidate_digest: str | None,
) -> InsertionExpectationV1:
    if expectation.state == "confirming":
        if expectation.confirmation_observation == observation:
            return expectation
        raise InsertionProtocolError("insertion expectation already has another confirmation")
    if expectation.state != "pending":
        raise InsertionProtocolError("only a pending insertion can begin confirmation")
    return update_insertion_expectation(
        expectation,
        state="confirming",
        confirmation_observation=observation,
        citation_id=citation_id,
        successor_proposal_id=proposal_id,
        successor_candidate_ref=candidate_ref,
        successor_candidate_digest=candidate_digest,
    )


def _terminal(
    intent: AuthoringIntentV1,
    expectation: InsertionExpectationV1,
    *,
    state: str,
    finalized_at: datetime,
    citation_id: str | None = None,
    horizon: timedelta = DEFAULT_INSERTION_TOMBSTONE_HORIZON,
) -> InsertionExpectationV1:
    finalized = ensure_utc(finalized_at)
    tombstone = build_insertion_terminal_tombstone(
        result_key=insertion_result_key(
            instance_id=intent.instance_id,
            actor_id=intent.actor_id,
            intent_id=intent.intent_id,
            expectation_id=expectation.expectation_id,
        ),
        intent_id=intent.intent_id,
        expectation_id=expectation.expectation_id,
        final_state=state,
        final_result=state,
        citation_id=citation_id,
        successor_candidate_ref=expectation.successor_candidate_ref,
        finalized_at=finalized,
        retain_until=finalized + horizon,
        patch_envelope_digest=expectation.patch.envelope_digest,
    )
    return update_insertion_expectation(
        expectation,
        state=state,
        citation_id=citation_id,
        terminal_tombstone=tombstone,
    )


def mark_bound(
    intent: AuthoringIntentV1,
    expectation: InsertionExpectationV1,
    *,
    finalized_at: datetime,
) -> InsertionExpectationV1:
    if expectation.state == "bound":
        return expectation
    if expectation.state != "confirming" or expectation.citation_id is None:
        raise InsertionProtocolError("only a confirmed insertion successor can become bound")
    return _terminal(
        intent,
        expectation,
        state="bound",
        finalized_at=finalized_at,
        citation_id=expectation.citation_id,
    )


def mark_expired(
    intent: AuthoringIntentV1,
    expectation: InsertionExpectationV1,
    *,
    evaluation_time: datetime,
) -> InsertionExpectationV1:
    evaluated = ensure_utc(evaluation_time)
    if expectation.state == "expired":
        return expectation
    if expectation.state not in {"awaiting_claim_acceptance", "pending"}:
        raise InsertionProtocolError("confirmation or a terminal result won before expiry")
    if evaluated < expectation.patch.expires_at:
        raise InsertionProtocolError("insertion expectation has not reached expires_at")
    return _terminal(intent, expectation, state="expired", finalized_at=evaluated)


def mark_abandoned(
    intent: AuthoringIntentV1,
    expectation: InsertionExpectationV1,
    *,
    finalized_at: datetime,
) -> InsertionExpectationV1:
    if expectation.state == "abandoned":
        return expectation
    if expectation.state not in {"awaiting_claim_acceptance", "pending"}:
        raise InsertionProtocolError("confirmed or terminal insertion cannot be abandoned")
    return _terminal(intent, expectation, state="abandoned", finalized_at=finalized_at)


def mark_claim_currency_changed(
    intent: AuthoringIntentV1,
    expectation: InsertionExpectationV1,
    *,
    finalized_at: datetime,
) -> InsertionExpectationV1:
    if expectation.state == "claim_currency_changed":
        return expectation
    if expectation.state in {"bound", "expired", "abandoned"}:
        raise InsertionProtocolError("terminal insertion cannot change currency again")
    return _terminal(
        intent,
        expectation,
        state="claim_currency_changed",
        finalized_at=finalized_at,
    )


__all__ = [
    "DEFAULT_INSERTION_TOMBSTONE_HORIZON",
    "InsertionProtocolError",
    "mark_abandoned",
    "mark_bound",
    "mark_claim_accepted",
    "mark_claim_currency_changed",
    "mark_confirming",
    "mark_expired",
    "mint_insertion_expectation",
]
