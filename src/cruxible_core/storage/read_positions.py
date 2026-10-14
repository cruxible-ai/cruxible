"""The log positions one read answers as of: a point-in-time read.

A read records, once at its start, where the logs outside the accepted
coordinate stand -- the body store's landing log and the re-observation
journal -- and answers as of exactly those positions. Writes that land while it
runs are not in its answer; it never waits for them, retries or refuses.

Within a read (``reading_at``) a Capture body's presence is the landing log's
answer at the read's landing position (``body_present``), never the file's;
a body's bytes are read only when the reader needs them, and only if the body
had landed by then (``read_body``). Re-observations count through the read's
journal sequence (``reobservations_through``).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.errors import CasError


@dataclass(frozen=True)
class ReadPositions:
    """Where a read stands in each log it reads outside the accepted coordinate."""

    #: The landing log's identity and the last sequence the read counts.
    landing_log: str
    landing: int
    #: The re-observation journal's last sequence the read counts.
    reobs: int


_CURRENT: ContextVar[ReadPositions | None] = ContextVar("cruxible_read_positions", default=None)


@contextmanager
def reading_at(positions: ReadPositions) -> Iterator[ReadPositions]:
    """Answer every read inside as of ``positions``."""

    token = _CURRENT.set(positions)
    try:
        yield positions
    finally:
        _CURRENT.reset(token)


def current_positions() -> ReadPositions | None:
    """The positions of the read in progress, or None outside one."""

    return _CURRENT.get()


def reobservations_through() -> int | None:
    """The last re-observation journal sequence the read in progress counts (None: all)."""

    positions = _CURRENT.get()
    return None if positions is None else positions.reobs


def body_present(store: Any, digest: str) -> bool | None:
    """Whether ``digest`` had landed by the read's landing position.

    None outside a positioned read, or for a store that keeps no landing log.
    """

    positions = _CURRENT.get()
    landings = getattr(store, "landings", None)
    if positions is None or landings is None:
        return None
    return bool(
        landings.present(digest, identity=positions.landing_log, sequence=positions.landing)
    )


def read_body(store: Any, digest: str, *, access: BodyAccessContext) -> bytes:
    """A body's verified bytes, read only if it had landed by the read's position."""

    if body_present(store, digest) is False:
        raise CasError("CAS object is missing")
    return bytes(store.read(digest, access=access))


class PositionedBodies:
    """A body store whose presence is the landing log's at the read's position."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def verify(self, digest: str) -> bool:
        present = body_present(self._store, digest)
        return bool(self._store.verify(digest)) if present is None else present

    def read(self, digest: str, *, access: BodyAccessContext) -> bytes:
        return read_body(self._store, digest, access=access)


__all__ = [
    "PositionedBodies",
    "ReadPositions",
    "body_present",
    "current_positions",
    "read_body",
    "reading_at",
    "reobservations_through",
]
