"""Project a landing's source from its immutable Capture, never from a request label."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.errors import CasError, JournalIntegrityError
from cruxible_client.contracts.source_references import ExternalSourceReference
from cruxible_core.exhaust.payloads import UnavailablePayload
from cruxible_core.storage.capture_metadata import capture_envelope


def landing_logical_source(
    bodies: Any, payload: Mapping[str, object], *, access: BodyAccessContext
) -> str | None | UnavailablePayload:
    """One envelope lookup per fresh landing; warm metadata is cached by content id.

    Non-external Captures have no logical source. Historical synthetic events
    without a Capture digest remain usable only by unrestricted selectors.
    """
    digest = payload.get("capture_digest")
    if digest is None:
        return None
    if not isinstance(digest, str):
        raise JournalIntegrityError("produced Capture payload has an invalid Capture digest")
    try:
        envelope = capture_envelope(bodies, digest, access=access)
    except (CasError, OSError):
        return UnavailablePayload(digest)
    if envelope.capture_contract_digest != payload.get("capture_contract_digest"):
        raise JournalIntegrityError("produced Capture differs from its event contract")
    return (
        envelope.source.source_identity
        if isinstance(envelope.source, ExternalSourceReference)
        else None
    )
