"""The log positions one read answers as of: a point-in-time read.

A read records, once at its start, where the logs outside the accepted
coordinate stand -- the body store's landing log and the re-observation
journal -- and answers as of exactly those positions. Writes that land while it
runs are not in its answer; it never waits for them, retries or refuses.

Within a read (``reading_at``) a Capture body's presence is the landing log's
answer at the read's landing position (``body_present``), never the file's: it
had landed at or before that position and was not recorded damaged (missing or
corrupt, ``LandingLog.note``) at or before it since. Integrity is the evidence
sweep's job, and its damage record is what changes later reads' answer; no read
stats or hashes a body to decide presence. A body's bytes are read only when
the reader needs them, and only if the body was present by then
(``read_body``). Re-observations count through the read's journal sequence
(``reobservations_through``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any, ParamSpec, TypeVar, cast

from cruxible_client.contracts.cas_contracts import BodyAccessContext, CasObjectMetadata
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


def read_positions(instance: Any) -> ReadPositions:
    """Record the operational positions once for a read of accepted state."""

    from cruxible_core.evidence.claim_reobservations import ClaimReobservationStore

    # Recovery's accepted-prefix source has a body store but no operational
    # journal or instance descriptor. Its evidence still uses landing presence.
    reobs = (
        0
        if getattr(instance, "descriptor", None) is None
        else ClaimReobservationStore(instance).head_sequence()
    )
    landing = instance.body_store().landings.head()
    return ReadPositions(landing.identity, landing.sequence, reobs)


@contextmanager
def read_request(instance: Any) -> Iterator[None]:
    """One operational position across a service read or an in-memory preview."""

    if (
        instance is None
        or current_positions() is not None
        or not hasattr(instance.body_store(), "landings")
    ):
        yield
    else:
        with reading_at(read_positions(instance)):
            yield


_P = ParamSpec("_P")
_R = TypeVar("_R")


def positioned_read(operation: Callable[_P, _R]) -> Callable[_P, _R]:
    """Pin one service read; nested reads inherit the caller's positions."""

    @wraps(operation)
    def positioned(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        instance = args[0] if args else kwargs["instance"]
        with read_request(instance):
            return operation(*args, **kwargs)

    return positioned


def unpositioned_operation(operation: Callable[_P, _R]) -> Callable[_P, _R]:
    """A write or worker operation owns live state, even when invoked inside a read.

    This matters for a concurrent operation emulated synchronously by a caller:
    its new bodies must not inherit the enclosing reader's older positions.
    Returning restores the enclosing read's snapshot.
    """

    @wraps(operation)
    def live(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        token = _CURRENT.set(None)
        try:
            return operation(*args, **kwargs)
        finally:
            _CURRENT.reset(token)

    return live


_OBSERVE_IDENTITIES: ContextVar[bool] = ContextVar(
    "cruxible_observe_body_identities", default=False
)


@contextmanager
def recording_body_identities() -> Iterator[None]:
    """Collect sweep baselines while writing facts or explicitly rebuilding them.

    Ordinary reads never inspect files for these integrity observations. Only
    the changed Claims' dependencies are observed by a worker; a full rebuild
    observes the whole population only as the existing explicit repair does.
    """

    token = _OBSERVE_IDENTITIES.set(True)
    try:
        yield
    finally:
        _OBSERVE_IDENTITIES.reset(token)


def body_identity(store: Any, digest: str) -> tuple[int, int, int, int, int] | None:
    """Sweep baseline, independent of the index's authoritative presence answer."""

    if body_present(store, digest) is False:
        return None
    observe = getattr(
        store, "file_identity" if _OBSERVE_IDENTITIES.get() else "verified_identity", None
    )
    return None if observe is None else observe(digest)


def recording_identities() -> bool:
    """Whether this operation is collecting physical sweep baselines."""

    return _OBSERVE_IDENTITIES.get()


def body_present(store: Any, digest: str) -> bool | None:
    """Whether ``digest`` was present at the read's landing position (landed, not damaged).

    Outside a positioned read, use the current landing head. None only for a
    store that keeps no landing log (in-memory protocol implementations).
    """

    from cruxible_core.storage.cas import DryRunBodyStore

    if isinstance(store, PositionedBodies):
        return body_present(store._store, digest)
    if isinstance(store, DryRunBodyStore) and store.holds(digest):
        return True
    positions = _CURRENT.get()
    landings = getattr(store, "landings", None)
    if landings is None:
        return None
    if positions is None:
        head = landings.head()
        identity, sequence = head.identity, head.sequence
    else:
        identity, sequence = positions.landing_log, positions.landing
    return bool(landings.present(digest, identity=identity, sequence=sequence))


def read_body(store: Any, digest: str, *, access: BodyAccessContext) -> bytes:
    """A body's verified bytes, read only if it had landed by the read's position."""

    if body_present(store, digest) is False:
        raise CasError("CAS object is missing")
    return bytes(store.read(digest, access=access))


class PositionedBodies:
    """A body store whose presence is the landing log's at the read's position.

    ``verify`` hashes nothing: an object damaged in place still verifies until a
    damage record at or before the read's position says otherwise.
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    def store(self, content: bytes) -> CasObjectMetadata:
        # Explicit for runtime-checkable CaptureObjectStoreProtocol: Python
        # checks declared members, not __getattr__ forwarding.
        return cast(CasObjectMetadata, self._store.store(content))

    def verify(self, digest: str) -> bool:
        present = body_present(self._store, digest)
        return bool(self._store.verify(digest)) if present is None else present

    def read(self, digest: str, *, access: BodyAccessContext) -> bytes:
        return read_body(self._store, digest, access=access)

    def metadata(self, digest: str, *, access: BodyAccessContext) -> CasObjectMetadata:
        present = body_present(self._store, digest)
        if present is None:
            return cast(CasObjectMetadata, self._store.metadata(digest, access=access))
        size = None
        if present and access.can_read_body:
            # Size is immutable for a content id. Use the last verified
            # observation; a cold miss reads bytes because it needs their size.
            observe = getattr(self._store, "verified_identity", None)
            identity = None if observe is None else observe(digest)
            size = identity[2] if identity is not None else len(self.read(digest, access=access))
        return CasObjectMetadata(
            digest=digest, present=present, byte_length=size, redacted=not access.can_read_body
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


__all__ = [
    "PositionedBodies",
    "ReadPositions",
    "body_present",
    "body_identity",
    "recording_body_identities",
    "recording_identities",
    "unpositioned_operation",
    "current_positions",
    "read_body",
    "reading_at",
    "read_positions",
    "read_request",
    "positioned_read",
    "reobservations_through",
]
