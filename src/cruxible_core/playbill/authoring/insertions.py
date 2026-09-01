"""Pure PC-G2 insertion expectation construction and state reduction."""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta
from typing import NoReturn, overload

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import (
    AuthoringIntentV1,
    ClaimAuthoringPayloadV1,
    InsertionConfirmationObservationV2,
    InsertionExpectationV2,
    InsertionTargetV2,
    PublicationPreparationV2,
    PublicationSourceObservationV2,
    SelfSourceBodyV1,
    build_insertion_expectation_v2,
    build_insertion_terminal_tombstone_v2,
    build_publication_preparation_v2,
    insertion_expectation_id,
    insertion_result_key,
    insertion_target_v2_digest,
    publication_block_id,
    update_insertion_expectation_v2,
)
from cruxible_client.contracts.declared_blocks import (
    ProjectionBlockStampV1,
    ProjectionBootstrapUnstampedError,
    ProjectionClaimBackingV1,
    ProjectionMarkerError,
    frame_projection_block,
    parse_projection_blocks,
)
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import ensure_utc

DEFAULT_INSERTION_TOMBSTONE_HORIZON = timedelta(days=30)
MAX_PUBLICATION_PREPARATION_REVISIONS = 16
PUBLICATION_EXPECTATION_EXPIRY = timedelta(days=7)


class InsertionProtocolError(PlaybillError):
    """An insertion transition is invalid at the durable expectation state."""

    code = "playbill.authoring.publication_transition_invalid"


class PublicationClaimNotAccepted(InsertionProtocolError):
    code = "playbill.authoring.publication_claim_not_accepted"


class PublicationNotPrepared(InsertionProtocolError):
    code = "playbill.authoring.publication_not_prepared"


class PublicationPreparationStale(InsertionProtocolError):
    code = "playbill.authoring.publication_preparation_stale"


class PublicationRevisionLimitExceeded(InsertionProtocolError):
    code = "playbill.authoring.publication_revision_limit"


class PublicationAnchorStale(InsertionProtocolError):
    code = "playbill.authoring.publication_anchor_stale"


class PublicationAnchorAmbiguous(InsertionProtocolError):
    code = "playbill.authoring.publication_anchor_ambiguous"


class PublicationBodyNotMarkerCompatible(InsertionProtocolError):
    code = "playbill.authoring.publication_body_not_marker_compatible"


class PublicationSourceHasUnrepinnedBlock(InsertionProtocolError):
    code = "playbill.authoring.source_has_unrepinned_block"


class PublicationConfirmationMismatch(InsertionProtocolError):
    code = "playbill.authoring.publication_confirmation_mismatch"


class PublicationTerminalStateRefused(InsertionProtocolError):
    code = "playbill.authoring.publication_terminal_state"


def _raise(error: type[InsertionProtocolError], message: str) -> NoReturn:
    raise error(f"{error.code}: {message}")


def _raise_marker_refusal(exc: ProjectionMarkerError) -> NoReturn:
    if isinstance(exc, ProjectionBootstrapUnstampedError):
        raise PublicationSourceHasUnrepinnedBlock(
            f"{PublicationSourceHasUnrepinnedBlock.code}: run playbill block repin before "
            "publishing into this source"
        ) from exc
    raise PublicationBodyNotMarkerCompatible(
        f"{PublicationBodyNotMarkerCompatible.code}: {exc}"
    ) from exc


def mint_insertion_expectation_v2(
    intent: AuthoringIntentV1,
    *,
    original_claim_artifact_digest: str,
    claim_statement_digest: str,
    expires_at: datetime,
) -> InsertionExpectationV2:
    payload = intent.payload
    if not isinstance(payload, ClaimAuthoringPayloadV1):
        raise InsertionProtocolError("only a Claim intent can mint an insertion expectation")
    if not isinstance(payload.insertion_target, InsertionTargetV2):
        raise InsertionProtocolError("publication v2 requires its frozen v2 target")
    if not isinstance(payload.source, SelfSourceBodyV1):
        raise InsertionProtocolError("publication v2 requires a Flow-B self-source")
    return build_insertion_expectation_v2(
        expectation_id=insertion_expectation_id(
            instance_id=intent.instance_id,
            intent_id=intent.intent_id,
            intent_revision=intent.intent_revision,
        ),
        state="awaiting_claim_acceptance",
        claim_identity=intent.semantic_identity,
        original_claim_artifact_digest=original_claim_artifact_digest,
        claim_statement_digest=claim_statement_digest,
        target=payload.insertion_target,
        expires_at=ensure_utc(expires_at),
    )


def mark_publication_claim_accepted(
    expectation: InsertionExpectationV2,
    *,
    accepted_coordinate: object,
) -> InsertionExpectationV2:
    from cruxible_client.contracts.projection import AcceptedCoordinate

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


def _overlapping_offsets(content: bytes, needle: bytes) -> tuple[int, ...]:
    if not needle:
        return ()
    offsets: list[int] = []
    start = 0
    while True:
        found = content.find(needle, start)
        if found < 0:
            return tuple(offsets)
        offsets.append(found)
        start = found + 1


@overload
def build_publication_preparation(
    expectation: PublicationPreparationV2,
    *,
    body: bytes,
) -> bytes: ...


@overload
def build_publication_preparation(
    expectation: InsertionExpectationV2,
    *,
    observation: PublicationSourceObservationV2,
    body: bytes,
    accepted_coordinate: AcceptedCoordinate,
    accepted_generation: int,
) -> PublicationPreparationV2: ...


def build_publication_preparation(
    expectation: InsertionExpectationV2 | PublicationPreparationV2,
    *,
    body: bytes,
    observation: PublicationSourceObservationV2 | None = None,
    accepted_coordinate: AcceptedCoordinate | None = None,
    accepted_generation: int | None = None,
) -> PublicationPreparationV2 | bytes:
    """Build one deterministic full-file postimage from fresh observed bytes."""

    if isinstance(expectation, PublicationPreparationV2):
        framed = frame_projection_block(stamp=expectation.stamp, body=body)
        if (
            len(framed) != expectation.inserted_block_byte_length
            or "sha256:" + hashlib.sha256(framed).hexdigest() != expectation.inserted_block_digest
        ):
            _raise(
                InsertionProtocolError,
                "rendered publication block differs from its preparation",
            )
        return framed
    if observation is None or accepted_coordinate is None or accepted_generation is None:
        raise TypeError("publication preparation requires an observation and accepted coordinate")

    if expectation.state not in {"pending", "prepared"}:
        if expectation.state == "awaiting_claim_acceptance":
            _raise(PublicationClaimNotAccepted, "the governed Claim is not accepted")
        _raise(PublicationTerminalStateRefused, "publication is not preparable")
    if observation.source_id != expectation.target.source_id:
        _raise(PublicationPreparationStale, "source observation names another target")
    if expectation.accepted_claim_coordinate != accepted_coordinate:
        _raise(PublicationPreparationStale, "accepted Claim coordinate changed")
    prior = expectation.preparation
    if prior is not None:
        try:
            blocks = parse_projection_blocks(observation.content, source_id=observation.source_id)
        except ProjectionMarkerError as exc:
            _raise_marker_refusal(exc)
        matches = tuple(block for block in blocks if block.block_id == prior.block_id)
        if (
            len(matches) == 1
            and matches[0].stamp == prior.stamp
            and matches[0].body_digest == prior.body_digest
        ):
            return prior

    block_id = (
        prior.block_id if prior is not None else publication_block_id(expectation.expectation_id)
    )
    marker_token = f"playbill:block:{block_id}".encode("ascii")
    if marker_token in observation.content:
        _raise(PublicationPreparationStale, "publication block ID already has non-exact bytes")

    target = expectation.target
    if target.operation == "append":
        start = end = offset = len(observation.content)
        anchor = b""
    else:
        anchor = target.selector.content
        offsets = _overlapping_offsets(observation.content, anchor)
        if not offsets:
            _raise(PublicationAnchorStale, "publication anchor is absent from the fresh source")
        if len(offsets) != 1:
            _raise(
                PublicationAnchorAmbiguous,
                "publication anchor occurs more than once in the fresh source",
            )
        start = offsets[0]
        end = start + len(anchor)
        offset = start if target.operation in {"insert_before", "replace_window"} else end
    selector = target.selector.model_copy(
        update={
            "anchor_content_base64": base64.b64encode(anchor).decode("ascii"),
            "anchor_bytes_digest": "sha256:" + hashlib.sha256(anchor).hexdigest(),
            "start_byte": start,
            "end_byte": end,
            "insertion_offset": offset,
            "observed_occurrence_count": 1,
        }
    )
    body_digest = "sha256:" + hashlib.sha256(body).hexdigest()
    stamp = ProjectionBlockStampV1(
        source_id=target.source_id,
        block_id=block_id,
        declared_generation=accepted_generation,
        declared_coordinate=accepted_coordinate,
        backing=(
            ProjectionClaimBackingV1(
                identity=ArtifactIdentity(kind="Claim", name=expectation.claim_identity),
                statement_digest=expectation.claim_statement_digest,
            ),
        ),
        body_digest=body_digest,
    )
    try:
        framed = frame_projection_block(stamp=stamp, body=body)
    except ProjectionMarkerError as exc:
        _raise_marker_refusal(exc)
    if target.operation == "replace_window":
        final = observation.content[:start] + framed + observation.content[end:]
        block_start = start
    else:
        final = observation.content[:offset] + framed + observation.content[offset:]
        block_start = offset
    try:
        blocks = parse_projection_blocks(final, source_id=target.source_id)
    except ProjectionMarkerError as exc:
        _raise_marker_refusal(exc)
    matches = tuple(block for block in blocks if block.block_id == block_id)
    if len(matches) != 1 or matches[0].stamp != stamp or matches[0].body_digest != body_digest:
        _raise(
            PublicationBodyNotMarkerCompatible,
            "prospective source does not contain exactly the intended Claim-backed block",
        )
    parsed = matches[0]
    if parsed.opening_start != block_start:
        _raise(PublicationBodyNotMarkerCompatible, "prospective block span does not reproduce")
    preparation = build_publication_preparation_v2(
        expectation_id=expectation.expectation_id,
        revision=1 if prior is None else prior.revision + 1,
        accepted_coordinate=accepted_coordinate,
        accepted_generation=accepted_generation,
        source_id=target.source_id,
        rebased_selector=selector,
        operation=target.operation,
        body_digest=body_digest,
        body_byte_length=len(body),
        block_id=block_id,
        stamp=stamp,
        inserted_block_digest="sha256:" + hashlib.sha256(framed).hexdigest(),
        inserted_block_byte_length=len(framed),
        block_start_byte=parsed.opening_start,
        block_end_byte=parsed.closing_end,
        body_start_byte=parsed.body_start,
        body_end_byte=parsed.body_end,
        target_digest=insertion_target_v2_digest(target),
        expires_at=expectation.expires_at,
    )
    if prior is not None and preparation.model_dump(
        exclude={"revision", "preparation_digest"}
    ) == prior.model_dump(exclude={"revision", "preparation_digest"}):
        return prior
    if prior is not None and prior.revision >= MAX_PUBLICATION_PREPARATION_REVISIONS:
        _raise(
            PublicationRevisionLimitExceeded,
            f"publication reached its {MAX_PUBLICATION_PREPARATION_REVISIONS}-revision limit; "
            f"confirm the revision-{MAX_PUBLICATION_PREPARATION_REVISIONS} postimage or wait "
            f"for the expectation's {PUBLICATION_EXPECTATION_EXPIRY.days}-day expiry",
        )
    return preparation


def mark_publication_prepared(
    expectation: InsertionExpectationV2,
    *,
    preparation: PublicationPreparationV2,
) -> InsertionExpectationV2:
    if preparation.revision > MAX_PUBLICATION_PREPARATION_REVISIONS:
        _raise(
            PublicationRevisionLimitExceeded,
            f"publication exceeds its {MAX_PUBLICATION_PREPARATION_REVISIONS}-revision limit",
        )
    if expectation.state == "prepared":
        if expectation.preparation == preparation:
            return expectation
        prior = expectation.preparation
        if (
            prior is not None
            and preparation.expectation_id == expectation.expectation_id
            and preparation.target_digest == insertion_target_v2_digest(expectation.target)
            and preparation.block_id == prior.block_id
            and preparation.revision == prior.revision + 1
        ):
            return update_insertion_expectation_v2(
                expectation,
                state="prepared",
                preparation=preparation,
            )
        _raise(PublicationPreparationStale, "unrelated durable preparation cannot replace prior")
    if expectation.state != "pending":
        if expectation.state == "awaiting_claim_acceptance":
            _raise(PublicationClaimNotAccepted, "the governed Claim is not accepted")
        _raise(PublicationTerminalStateRefused, "publication is not pending")
    if preparation.expectation_id != expectation.expectation_id:
        _raise(PublicationPreparationStale, "preparation names another expectation")
    if preparation.target_digest != insertion_target_v2_digest(expectation.target):
        _raise(PublicationPreparationStale, "preparation names another insertion target")
    if preparation.revision != 1:
        _raise(PublicationPreparationStale, "initial preparation must have revision 1")
    return update_insertion_expectation_v2(
        expectation,
        state="prepared",
        preparation=preparation,
    )


def publication_confirmation_matches(
    expectation: InsertionExpectationV2,
    observation: InsertionConfirmationObservationV2,
    *,
    intent_id: str,
) -> bool:
    preparation = expectation.preparation
    if preparation is None:
        return False
    summary = observation.marker_summary
    return (
        observation.intent_id == intent_id
        and observation.expectation_id == expectation.expectation_id
        and observation.preparation_digest == preparation.preparation_digest
        and observation.source_id == preparation.source_id
        and observation.observed_occurrence_count == 1
        and summary.stamp == preparation.stamp
        and summary.observed_body_digest == preparation.body_digest
    )


def publication_confirmation_from_source(
    *,
    intent_id: str,
    expectation: InsertionExpectationV2,
    observation: PublicationSourceObservationV2,
) -> InsertionConfirmationObservationV2 | None:
    preparation = expectation.preparation
    if preparation is None or observation.source_id != preparation.source_id:
        return None
    try:
        blocks = parse_projection_blocks(observation.content, source_id=observation.source_id)
    except ProjectionMarkerError:
        return None
    matches = tuple(block for block in blocks if block.block_id == preparation.block_id)
    # parse_projection_blocks already refuses repeated block identities, so a
    # successful parse has cardinality zero or one for this exact block ID.
    if not matches:
        return None
    (match,) = matches
    return InsertionConfirmationObservationV2(
        intent_id=intent_id,
        expectation_id=expectation.expectation_id,
        preparation_digest=preparation.preparation_digest,
        source_id=observation.source_id,
        marker_summary=match.summary(),
        observed_occurrence_count=1,
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


def mark_publication_bound(
    intent: AuthoringIntentV1,
    expectation: InsertionExpectationV2,
    *,
    observation: InsertionConfirmationObservationV2,
    finalized_at: datetime,
) -> InsertionExpectationV2:
    if expectation.state == "bound":
        return expectation
    if expectation.state != "prepared":
        _raise(PublicationNotPrepared, "publication has no durable preparation")
    if not publication_confirmation_matches(
        expectation,
        observation,
        intent_id=intent.intent_id,
    ):
        _raise(PublicationConfirmationMismatch, "observation differs from exact preparation")
    return _terminal_v2(intent, expectation, state="bound", finalized_at=finalized_at)


def mark_publication_terminal(
    intent: AuthoringIntentV1,
    expectation: InsertionExpectationV2,
    *,
    state: str,
    finalized_at: datetime,
) -> InsertionExpectationV2:
    if expectation.state == state:
        return expectation
    if expectation.state in {"bound", "expired", "abandoned", "claim_currency_changed"}:
        _raise(PublicationTerminalStateRefused, "publication is already terminal")
    if state == "expired" and ensure_utc(finalized_at) < expectation.expires_at:
        raise InsertionProtocolError("publication has not reached expires_at")
    return _terminal_v2(intent, expectation, state=state, finalized_at=finalized_at)


__all__ = [
    "DEFAULT_INSERTION_TOMBSTONE_HORIZON",
    "InsertionProtocolError",
    "MAX_PUBLICATION_PREPARATION_REVISIONS",
    "PUBLICATION_EXPECTATION_EXPIRY",
    "PublicationAnchorAmbiguous",
    "PublicationAnchorStale",
    "PublicationBodyNotMarkerCompatible",
    "PublicationClaimNotAccepted",
    "PublicationConfirmationMismatch",
    "PublicationNotPrepared",
    "PublicationPreparationStale",
    "PublicationRevisionLimitExceeded",
    "PublicationSourceHasUnrepinnedBlock",
    "PublicationTerminalStateRefused",
    "build_publication_preparation",
    "mark_publication_bound",
    "mark_publication_claim_accepted",
    "mark_publication_prepared",
    "mark_publication_terminal",
    "mint_insertion_expectation_v2",
    "publication_confirmation_matches",
    "publication_confirmation_from_source",
]
