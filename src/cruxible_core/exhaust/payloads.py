"""Best-effort exhaust reads: an authenticated record can outlive its body."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import structlog

from cruxible_client.contracts.canonical import CanonicalValue
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.errors import CasError
from cruxible_core.exhaust.records import parse_journal_payload


@dataclass(frozen=True)
class UnavailablePayload:
    """A record's payload could not be produced; the journal frame is still valid."""

    digest: str
    state: Literal["unavailable"] = "unavailable"


def read_payload(
    bodies: Any, digest: str, *, access: BodyAccessContext
) -> CanonicalValue | UnavailablePayload:
    """Read exhaust metadata, returning typed unavailability on lost/damaged bytes.

    Direct body-byte reads continue to use ``bodies.read`` and refuse. Invalid
    journal framing or noncanonical payloads whose digest *does* match remain
    integrity failures, rather than being mistaken for crash loss.
    """
    raw = read_payload_bytes(bodies, digest, access=access)
    return raw if isinstance(raw, UnavailablePayload) else parse_journal_payload(raw)


def read_payload_bytes(
    bodies: Any, digest: str, *, access: BodyAccessContext
) -> bytes | UnavailablePayload:
    """Verify a cached record's availability without parsing its immutable bytes again."""
    try:
        return bytes(bodies.read(digest, access=access))
    except (CasError, OSError):
        structlog.get_logger(__name__).warning("exhaust_payload_unavailable", digest=digest)
        return UnavailablePayload(digest)
