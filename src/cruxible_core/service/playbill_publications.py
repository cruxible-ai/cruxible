"""Derived reads over durable bound publication intents.

The fold itself lives in ``cruxible_core.playbill.authoring.registrations`` so
claim lowering can read it; it is re-exported here for every service-layer
reader that already imports it from this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cruxible_client.contracts import (
    PlaybillAcceptedCoordinate,
    PlaybillBlockDepublishResultV1,
)
from cruxible_client.contracts.errors import PlaybillError, PlaybillFormatError
from cruxible_core.playbill.authoring.registrations import (
    BoundPublicationRegistration,
    bound_publication_registrations,
    reset_bound_publication_registration_memo,
)
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedCoordinate

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.playbill.proposals import AuthenticatedActor


def service_depublish_playbill_block(
    instance: PlaybillInstance,
    *,
    coordinator: "AuthoringIntentCoordinator",
    actor: "AuthenticatedActor",
    source_id: str,
    block_id: str,
) -> PlaybillBlockDepublishResultV1:
    """Release the bound publication registration that demands one page block.

    A registration is folded from a `bound` insertion expectation and nothing
    ever released it, so `next` demanded the frame for a block a later ruling
    had removed, and the repair it named was to restore it. Abandoning the
    expectation is the transition out; the expectation keeps its preparation, so
    the record still says which block was published and which was taken down.

    Idempotent by construction: a released registration no longer folds, so a
    second call finds the expectation it already released and says so instead of
    minting an identity or refusing.
    """

    coordinate = PlaybillAcceptedCoordinate.model_validate(
        AcceptedCoordinate.from_internal(instance.accepted_coordinate()).model_dump(mode="json")
    )
    registrations = bound_publication_registrations(instance)
    if registrations is None:
        raise PlaybillFormatError(
            "playbill.block.publication_registry_unavailable: the durable publication "
            "stream cannot be read; repair: restore the instance exhaust and retry"
        )
    matched = tuple(
        item
        for item in registrations
        if item.preparation.source_id == source_id and item.preparation.block_id == block_id
    )
    if not matched:
        released = _released_publication_expectation(instance, source_id, block_id)
        if released is None:
            raise PlaybillFormatError(
                f"playbill.block.publication_not_registered: no bound publication registers "
                f"{source_id}#{block_id}; repair: read the registered blocks with "
                "`cruxible playbill next` before depublishing one"
            )
        intent_id, expectation_id, claim_identity = released
        return PlaybillBlockDepublishResultV1(
            source_id=source_id,
            block_id=block_id,
            intent_id=intent_id,
            expectation_id=expectation_id,
            outcome="already_depublished",
            claim_identity=claim_identity,
            coordinate=coordinate,
        )
    if len(matched) > 1:
        raise PlaybillFormatError(
            f"playbill.block.publication_registration_ambiguous: {len(matched)} bound "
            f"publications register {source_id}#{block_id}; repair: abandon each intent "
            "through `cruxible playbill authoring abandon-insertion`"
        )
    registration = matched[0]
    result = coordinator.abandon_insertion(
        registration.intent_id,
        actor=actor,
        expectation_id=registration.preparation.expectation_id,
    )
    return PlaybillBlockDepublishResultV1(
        source_id=source_id,
        block_id=block_id,
        intent_id=registration.intent_id,
        expectation_id=result.expectation.expectation_id,
        outcome="depublished",
        claim_identity=registration.claim_identity,
        coordinate=coordinate,
    )


def _released_publication_expectation(
    instance: PlaybillInstance,
    source_id: str,
    block_id: str,
) -> tuple[str, str, str] | None:
    """Find an expectation that once published this block and no longer registers it."""

    exhaust_root = instance.root / instance.descriptor.storage.exhaust
    try:
        latest = {
            event.intent.intent_id: event.intent
            for event in AuthoringIntentStore(exhaust_root, read_only=True).events()
        }
    except (OSError, PlaybillError):
        return None
    for intent in latest.values():
        for expectation in intent.insertion_expectations:
            preparation = expectation.preparation
            if preparation is None:
                continue
            if preparation.source_id != source_id or preparation.block_id != block_id:
                continue
            if expectation.state == "bound":
                continue
            return (intent.intent_id, expectation.expectation_id, expectation.claim_identity)
    return None


__all__ = [
    "BoundPublicationRegistration",
    "bound_publication_registrations",
    "reset_bound_publication_registration_memo",
    "service_depublish_playbill_block",
]
