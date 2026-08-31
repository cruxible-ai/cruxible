"""Derived reads over durable bound publication intents.

The AuthoringIntent stream is protocol state, not a second governed truth plane.
Both ``next`` and coverage use this fold so they cannot disagree about whether a
publication was actually confirmed.
"""

from __future__ import annotations

from dataclasses import dataclass

from cruxible_client.contracts.authoring.models import PublicationPreparationV2
from cruxible_client.contracts.errors import PlaybillError
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.instance import PlaybillInstance


@dataclass(frozen=True)
class BoundPublicationRegistration:
    """The exact association established by one confirmed publication."""

    intent_id: str
    claim_identity: str
    claim_statement_digest: str
    preparation: PublicationPreparationV2


def bound_publication_registrations(
    instance: PlaybillInstance,
) -> tuple[BoundPublicationRegistration, ...] | None:
    """Fold latest intent events, or return ``None`` when the fold is unavailable."""

    exhaust_root = instance.root / instance.descriptor.storage.exhaust
    if not (exhaust_root / "authoring-intents").is_dir():
        return ()
    try:
        latest = {
            event.intent.intent_id: event.intent
            for event in AuthoringIntentStore(exhaust_root).events()
        }
    except (OSError, PlaybillError):
        return None
    registrations = [
        BoundPublicationRegistration(
            intent_id=intent.intent_id,
            claim_identity=expectation.claim_identity,
            claim_statement_digest=expectation.claim_statement_digest,
            preparation=expectation.preparation,
        )
        for intent in latest.values()
        if (expectation := intent.insertion_expectation) is not None
        and expectation.state == "bound"
        and expectation.preparation is not None
    ]
    return tuple(
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


__all__ = ["BoundPublicationRegistration", "bound_publication_registrations"]
