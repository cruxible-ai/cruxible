"""Build the publication records an instance that published before the ruling holds.

Nothing in the product mints a publication expectation any more: a Claim
projected as its own page text was the mint-era overlap the two-block-kinds law
refuses, and `insertion_target` refuses at the authoring door. The records
themselves did not disappear -- an instance that published under the old road
still folds them, still reports their markers, and still has to be able to
depublish them -- so the tests that hold the daemon to that keep a way to write
one. It lives here, in the tests, because no product code may build one again.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from cruxible_client.authoring.insertions import PlaybillInsertionApplyError
from cruxible_client.contracts.authoring.models import (
    InsertionExpectationV2,
)
from cruxible_client.contracts.declared_blocks import (
    ProjectionMarkerError,
    assert_projection_block_frame,
    frame_projection_block,
)


@dataclass(frozen=True)
class PlaybillInsertionApplication:
    outcome: Literal["applied", "already_applied"]
    content: bytes
    observation: dict[str, Any]


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PlaybillInsertionApplyError(f"{label} is not an object")
    return value


def apply_playbill_publication(
    content: bytes,
    *,
    intent_id: str,
    expectation: Mapping[str, Any],
    retained_body: bytes,
) -> PlaybillInsertionApplication:
    """Apply one durable v2 preparation or recognize its exact stamped postimage."""

    typed_expectation = InsertionExpectationV2.model_validate(expectation)
    preparation = typed_expectation.preparation
    if preparation is None:
        raise PlaybillInsertionApplyError("publication has no durable preparation")
    if (
        _digest(retained_body) != preparation.body_digest
        or len(retained_body) != preparation.body_byte_length
    ):
        raise PlaybillInsertionApplyError(
            "retained accepted body does not reproduce the publication preparation"
        )
    framed = frame_projection_block(stamp=preparation.stamp, body=retained_body)
    if (
        _digest(framed) != preparation.inserted_block_digest
        or len(framed) != preparation.inserted_block_byte_length
    ):
        raise PlaybillInsertionApplyError(
            "retained accepted body does not reproduce the publication preparation"
        )

    try:
        assert_projection_block_frame(
            content,
            source_id=preparation.source_id,
            block_id=preparation.block_id,
            stamp=preparation.stamp,
            body_digest=preparation.body_digest,
        )
        updated = content
        outcome: Literal["applied", "already_applied"] = "already_applied"
    except ProjectionMarkerError:
        if f"playbill:block:{preparation.block_id}".encode("ascii") in content:
            raise PlaybillInsertionApplyError(
                "local source contains a conflicting publication block"
            )
        selector = preparation.rebased_selector
        anchor = selector.content
        empty_append = (
            preparation.operation == "append"
            and not anchor
            and selector.start_byte == selector.end_byte == selector.insertion_offset
        )
        if content[selector.start_byte : selector.end_byte] != anchor or (
            not empty_append and content.count(anchor) != 1
        ):
            raise PlaybillInsertionApplyError("publication anchor is stale or ambiguous")
        if preparation.operation == "replace_window":
            updated = content[: selector.start_byte] + framed + content[selector.end_byte :]
        else:
            offset = selector.insertion_offset
            updated = content[:offset] + framed + content[offset:]
        outcome = "applied"
    try:
        match = assert_projection_block_frame(
            updated,
            source_id=preparation.source_id,
            block_id=preparation.block_id,
            stamp=preparation.stamp,
            body_digest=preparation.body_digest,
        )
    except ProjectionMarkerError as exc:
        raise PlaybillInsertionApplyError(
            "final publication does not reproduce its exact declared block"
        ) from exc
    observation = {
        "tag": "playbill-insertion-confirmation-observation-v2",
        "intent_id": intent_id,
        "expectation_id": typed_expectation.expectation_id,
        "preparation_digest": preparation.preparation_digest,
        "source_id": preparation.source_id,
        "marker_summary": match.summary().model_dump(mode="json"),
        "observed_occurrence_count": 1,
    }
    return PlaybillInsertionApplication(
        outcome=outcome,
        content=updated,
        observation=observation,
    )


# ---------------------------------------------------------------------------
# The removed publication road, kept only so a test can write the records an
# instance that already published holds. Copied verbatim from
# cruxible_core.playbill.authoring.insertions at the commit that deleted
# them; no product code may call any of it again.
# ---------------------------------------------------------------------------

import base64  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
from typing import NoReturn, overload  # noqa: E402

from cruxible_client.contracts.artifacts import ArtifactIdentity  # noqa: E402
from cruxible_client.contracts.authoring.models import (  # noqa: E402
    AuthoringIntentV1,
    ClaimAuthoringPayloadV1,
    InsertionConfirmationObservationV2,
    InsertionTargetV2,
    PublicationPreparationV2,
    PublicationSourceObservationV2,
    SelfSourceBodyV1,
    authoring_create_fingerprint,
    authoring_payload_digest,
    build_insertion_expectation_v2,
    build_publication_preparation_v2,
    insertion_expectation_id,
    insertion_target_v2_digest,
    publication_block_id,
    update_insertion_expectation_v2,
)
from cruxible_client.contracts.declared_blocks import (  # noqa: E402
    ProjectionBlockStampV1,
    ProjectionBootstrapUnstampedError,
    ProjectionClaimBackingV1,
    parse_projection_blocks,
)
from cruxible_client.contracts.projection import AcceptedCoordinate  # noqa: E402
from cruxible_client.contracts.temporal import ensure_utc  # noqa: E402
from cruxible_core.playbill.authoring.insertions import (  # noqa: E402
    InsertionProtocolError,
    _raise,
    _terminal_v2,
)

MAX_PUBLICATION_PREPARATION_REVISIONS = 16
PUBLICATION_EXPECTATION_EXPIRY = timedelta(days=7)


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


class PublicationClaimProjectedAsItself(InsertionProtocolError):
    code = "playbill.authoring.publication_claim_projected_as_itself"


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
    payload: ClaimAuthoringPayloadV1 | None = None,
    claim_identity: str | None = None,
    member_identity: str | None = None,
) -> InsertionExpectationV2:
    """Mint one Claim's publication expectation, singular intent or set member.

    `payload`, `claim_identity` and `member_identity` name the publishing member
    when the intent is a change set. Omitted, they fall back to the singular
    Claim intent's own payload and identity, whose expectation ID preimage is
    exactly the one it has always had.
    """

    authored = intent.payload if payload is None else payload
    if not isinstance(authored, ClaimAuthoringPayloadV1):
        raise InsertionProtocolError("only a Claim intent can mint an insertion expectation")
    if not isinstance(authored.insertion_target, InsertionTargetV2):
        raise InsertionProtocolError("publication v2 requires its frozen v2 target")
    if not isinstance(authored.source, SelfSourceBodyV1):
        raise InsertionProtocolError("publication v2 requires a Flow-B self-source")
    return build_insertion_expectation_v2(
        expectation_id=insertion_expectation_id(
            instance_id=intent.instance_id,
            intent_id=intent.intent_id,
            intent_revision=intent.intent_revision,
            member_identity=member_identity,
        ),
        state="awaiting_claim_acceptance",
        claim_identity=intent.semantic_identity if claim_identity is None else claim_identity,
        original_claim_artifact_digest=original_claim_artifact_digest,
        claim_statement_digest=claim_statement_digest,
        target=authored.insertion_target,
        expires_at=ensure_utc(expires_at),
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


def register_legacy_publication(
    store: Any,
    intent_id: str,
    *,
    actor_id: str,
    target: InsertionTargetV2,
    preimage: bytes,
    body: bytes,
    accepted_coordinate: AcceptedCoordinate,
    accepted_generation: int,
    claim_artifact_digest: str,
    claim_statement_digest: str,
    observed_at: datetime,
) -> tuple[InsertionExpectationV2, bytes]:
    """Write the bound publication record an old instance holds, and return the page.

    Runs the whole removed road -- mint, prepare, apply, confirm -- over one
    already-submitted singular Claim intent, and lands the result in the durable
    event stream exactly as the coordinator used to. This is the only way a test
    can produce a registration now, and that is the point: the fold, the marker
    checks, the detach refusal and `block depublish` all have to keep working on
    records nothing can create again.
    """

    intent = store.get(intent_id, actor_id=actor_id)
    payload = intent.payload
    assert isinstance(payload, ClaimAuthoringPayloadV1)
    published = payload.model_copy(update={"insertion_target": target})
    # An intent that published carried its target on the payload itself, and the
    # payload digest and create fingerprint are both derived from it, so putting
    # the target back means recomputing both -- an intent whose digest does not
    # reproduce is refused by the store, and an expectation whose intent has no
    # target is refused by the intent model.
    republished = {
        "payload": published,
        "payload_digest": authoring_payload_digest(published),
        "create_fingerprint": authoring_create_fingerprint(
            instance_id=intent.instance_id,
            actor_id=intent.actor_id,
            payload=published,
        ),
    }
    with_target = intent.model_copy(update=republished)
    expectation = mint_insertion_expectation_v2(
        with_target,
        original_claim_artifact_digest=claim_artifact_digest,
        claim_statement_digest=claim_statement_digest,
        expires_at=observed_at + PUBLICATION_EXPECTATION_EXPIRY,
    )
    expectation = update_insertion_expectation_v2(
        expectation,
        state="pending",
        accepted_claim_coordinate=accepted_coordinate,
    )
    observation = PublicationSourceObservationV2(
        source_id=target.source_id,
        content_base64=base64.b64encode(preimage).decode("ascii"),
        content_digest="sha256:" + hashlib.sha256(preimage).hexdigest(),
        byte_length=len(preimage),
    )
    preparation = build_publication_preparation(
        expectation,
        observation=observation,
        body=body,
        accepted_coordinate=accepted_coordinate,
        accepted_generation=accepted_generation,
    )
    expectation = mark_publication_prepared(expectation, preparation=preparation)
    landed = apply_playbill_publication(
        preimage,
        intent_id=intent_id,
        expectation=expectation.model_dump(mode="json"),
        retained_body=body,
    )
    confirmation = publication_confirmation_from_source(
        intent_id=intent_id,
        expectation=expectation,
        observation=PublicationSourceObservationV2(
            source_id=target.source_id,
            content_base64=base64.b64encode(landed.content).decode("ascii"),
            content_digest="sha256:" + hashlib.sha256(landed.content).hexdigest(),
            byte_length=len(landed.content),
        ),
    )
    assert confirmation is not None
    bound = mark_publication_bound(
        with_target,
        expectation,
        observation=confirmation,
        finalized_at=observed_at,
    )

    def bind(current: AuthoringIntentV1) -> AuthoringIntentV1:
        if current.intent_revision != intent.intent_revision:
            raise InsertionProtocolError("intent was revised while the publication was registered")
        return current.model_copy(
            update={
                **republished,
                "insertion_expectation": bound,
                "insertion_expectations": (bound,),
            }
        )

    store.transition(
        intent_id,
        actor_id=actor_id,
        operation_key="sha256:"
        + hashlib.sha256(b"legacy-publication:" + intent_id.encode()).hexdigest(),
        transform=bind,
    )
    return bound, landed.content
