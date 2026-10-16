"""Immutable Capture metadata, with presence supplied by the landing index.

Parsing an envelope is a byte read on a cold miss. Thereafter metadata readers
reuse its sealed value by content id; a damage/erase record still makes it
unavailable. Direct body reads deliberately do not use this metadata memo.
"""

from __future__ import annotations

from typing import Any

from cruxible_client.contracts.captures import CaptureEnvelopeAny, parse_capture_envelope
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.errors import CasError
from cruxible_core.derived.derived_runtime import BoundedCache
from cruxible_core.storage.read_positions import body_present, read_body

_ENVELOPES: BoundedCache[CaptureEnvelopeAny] = BoundedCache(
    max_entries=16384, max_bytes=32 * 1024 * 1024
)


def capture_envelope(store: Any, digest: str, *, access: BodyAccessContext) -> CaptureEnvelopeAny:
    """Metadata of one present Capture; cold bytes are verified before parsing."""

    if not access.can_read_body:
        raise CasError("body access is denied")
    if body_present(store, digest) is False:
        raise CasError("CAS object is missing")
    root = getattr(store, "root", None)
    key = None if root is None else (str(root), digest)
    known = None if key is None else _ENVELOPES.get(key)
    if known is not None:
        return known
    content = read_body(store, digest, access=access)
    envelope = parse_capture_envelope(content)
    if key is not None:
        _ENVELOPES.put(key, envelope, weight=len(content) * 6 + len(digest))
    return envelope
