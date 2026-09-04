"""Derived reads over durable bound publication intents.

The AuthoringIntent stream is protocol state, not a second governed truth plane.
Both ``next`` and coverage use this fold so they cannot disagree about whether a
publication was actually confirmed.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from cruxible_client.contracts import (
    PlaybillAcceptedCoordinate,
    PlaybillBlockDepublishResultV1,
)
from cruxible_client.contracts.authoring.models import PublicationPreparationV2
from cruxible_client.contracts.errors import PlaybillError, PlaybillFormatError
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.memo import memo_get, memo_put
from cruxible_core.playbill.projection import AcceptedCoordinate

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.playbill.proposals import AuthenticatedActor


@dataclass(frozen=True)
class BoundPublicationRegistration:
    """The exact association established by one confirmed publication."""

    intent_id: str
    claim_identity: str
    claim_statement_digest: str
    preparation: PublicationPreparationV2


# The fold reads and parses every durable event file. On a worked instance that
# is hundreds of megabytes, and one `block sync --check` asks the same question
# once per block. The identity below names the exact stream that was folded:
# every event file's directory, name, inode, size and both timestamps. Any
# append or rewrite moves it, so a changed stream is folded again.
_REGISTRATION_MEMO_CAPACITY = 4
_REGISTRATION_MEMO: "OrderedDict[tuple[object, ...], tuple[BoundPublicationRegistration, ...]]" = (
    OrderedDict()
)


def _intent_stream_identity(root: Path) -> tuple[object, ...] | None:
    """Name the durable event stream without reading a single event."""

    entries: list[tuple[object, ...]] = []
    try:
        for directory in sorted(root.glob("AIT-*"), key=lambda item: item.name):
            events = directory / "events"
            if not events.is_dir():
                continue
            for path in sorted(events.glob("*.json"), key=lambda item: item.name):
                metadata = os.lstat(path)
                entries.append(
                    (
                        directory.name,
                        path.name,
                        metadata.st_mode,
                        metadata.st_dev,
                        metadata.st_ino,
                        metadata.st_size,
                        metadata.st_mtime_ns,
                        metadata.st_ctime_ns,
                    )
                )
    except OSError:
        return None
    return (str(root), tuple(entries))


def reset_bound_publication_registration_memo() -> None:
    """Forget every in-process publication fold."""

    _REGISTRATION_MEMO.clear()


def bound_publication_registrations(
    instance: PlaybillInstance,
) -> tuple[BoundPublicationRegistration, ...] | None:
    """Fold latest intent events, or return ``None`` when the fold is unavailable."""

    exhaust_root = instance.root / instance.descriptor.storage.exhaust
    intent_root = exhaust_root / "authoring-intents"
    if not intent_root.is_dir():
        return ()
    identity = _intent_stream_identity(intent_root)
    if identity is not None:
        memoized = memo_get(_REGISTRATION_MEMO, identity)
        if memoized is not None:
            return memoized
    try:
        latest = {
            event.intent.intent_id: event.intent
            for event in AuthoringIntentStore(exhaust_root, read_only=True).events()
        }
    except (OSError, PlaybillError):
        return None
    # Every expectation the intent owns, not just the singular mirror: one intent
    # is one changeset, so a set that published three Claims registers three
    # blocks, and the two it did not fold read back as orphan markers.
    registrations = [
        BoundPublicationRegistration(
            intent_id=intent.intent_id,
            claim_identity=expectation.claim_identity,
            claim_statement_digest=expectation.claim_statement_digest,
            preparation=expectation.preparation,
        )
        for intent in latest.values()
        for expectation in intent.insertion_expectations
        if expectation.state == "bound" and expectation.preparation is not None
    ]
    folded = tuple(
        sorted(
            registrations,
            key=lambda item: (
                item.preparation.source_id.encode("utf-8"),
                item.preparation.block_id.encode("ascii"),
                item.claim_identity.encode("ascii"),
                item.intent_id.encode("ascii"),
            ),
        )
    )
    if identity is not None:
        memo_put(_REGISTRATION_MEMO, identity, folded, capacity=_REGISTRATION_MEMO_CAPACITY)
    return folded


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
