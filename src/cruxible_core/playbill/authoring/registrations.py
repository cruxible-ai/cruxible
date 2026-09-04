"""The registration fold over durable publication intents.

The AuthoringIntent stream is protocol state, not a second governed truth plane.
``next``, coverage, the detach refusal and claim lowering all read this one fold
so they cannot disagree about which blocks an instance registers. It lives under
``playbill/authoring`` rather than the service layer because lowering -- which
may not import a service module -- has to ask it which sources carry projection
blocks before it admits a citation into one.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from cruxible_client.contracts.authoring.models import PublicationPreparationV2
from cruxible_client.contracts.errors import PlaybillError
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.memo import memo_get, memo_put


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


__all__ = [
    "BoundPublicationRegistration",
    "bound_publication_registrations",
    "reset_bound_publication_registration_memo",
]
