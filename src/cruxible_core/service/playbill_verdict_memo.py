"""A per-process memo for the per-Claim verdict and resolution-status map.

`orient` and `next` are READS of accepted state, and both begin by deriving
every live Claim's verdict and resolution status at one coordinate. At a few
hundred Claims that derivation crossed the client's own three-minute default
timeout, and at seven hundred it ran for minutes at a time -- so the first
thing every SDK session does could fail against a healthy instance.

The derivation is a pure function of things that do not move between two reads
at the same coordinate, so this remembers it. It is a cache and nothing else:

* it is per-process and cold after a restart, so no answer depends on it;
* it is keyed on everything the derivation reads -- the instance root, the
  accepted coordinate, the exact Claim set, and a fingerprint of the two stores
  a verdict consults BESIDES the accepted tree: the content-addressed body
  store, whose contents decide whether a capture can be replayed now, and the
  principal-authored attestation ledger;
* the evaluation instant is NOT in the key, because every real surface stamps a
  fresh `utc_now()` and a wall-clock key can therefore never be hit twice. A
  verdict is a step function of time whose only breakpoints are the instants it
  compares against -- a Claim's effective interval, a capture's observation and
  expiry, an attestation's validity window -- so the entry carries the interval
  between the two breakpoints straddling the instant it was derived at, and is
  served for any instant inside it. Crossing a breakpoint misses;
* it is bounded, so a long-lived daemon reading many coordinates does not grow
  without limit.

It is deliberately NOT a projection table. Nothing here is accepted state,
nothing is served as a fact, and the compiler does not know it exists.
"""

from __future__ import annotations

import hashlib
import os
from collections import OrderedDict
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from cruxible_core.playbill.claim_attestation_store import (
    STORE_DIRECTORY as CLAIM_ATTESTATION_STORE_DIRECTORY,
)

MEMO_CAPACITY = 4

_STORE_FINGERPRINT_UNREADABLE = "unreadable"


def _directory_fingerprint(root: Path, *, depth: int) -> str:
    """Fingerprint a store's shape without reading a single object.

    Only directory metadata is read: a shard's mtime moves when an object lands
    in it or leaves it, which is exactly the event that can change a replay
    availability answer. Walking the objects themselves would cost more than
    the derivation this memo exists to skip -- which is why the CAS is
    fingerprinted at ``depth=0``, on the shard directories alone. A CAS object
    is named by its own content, so it is never rewritten in place: every
    arrival and every removal renames an entry in a shard and moves that
    shard's mtime. A store whose files ARE rewritten in place -- the attestation
    ledger's partitions -- is fingerprinted one level deeper, on the files.
    """

    digest = hashlib.sha256()
    try:
        stack = [(root, 0)]
        while stack:
            directory, level = stack.pop()
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
            for entry in entries:
                metadata = entry.stat(follow_symlinks=False)
                digest.update(entry.name.encode("utf-8"))
                digest.update(
                    f"{metadata.st_mode}:{metadata.st_size}:{metadata.st_mtime_ns}".encode()
                )
                if entry.is_dir(follow_symlinks=False) and level < depth:
                    stack.append((Path(entry.path), level + 1))
    except OSError:
        return _STORE_FINGERPRINT_UNREADABLE
    return digest.hexdigest()


def verdict_input_fingerprint(instance: object) -> str:
    """Fingerprint every input a Claim verdict reads besides the accepted tree.

    An unreadable store fingerprints as unreadable rather than as empty, and an
    unreadable fingerprint never equals the readable one it replaced, so a
    transient fault misses the memo instead of serving a stale answer from it.
    """

    root = getattr(instance, "root", None)
    descriptor = getattr(instance, "descriptor", None)
    if not isinstance(root, Path) or descriptor is None:
        return _STORE_FINGERPRINT_UNREADABLE
    storage = getattr(descriptor, "storage", None)
    if storage is None:
        return _STORE_FINGERPRINT_UNREADABLE
    cas = root / getattr(storage, "cas", "cas")
    exhaust = root / getattr(storage, "exhaust", "exhaust")
    # The CAS is sharded one level deep and its shard mtimes are the store's own
    # change signal; the attestation ledger rewrites partition files in place,
    # so those files are read.
    return "-".join(
        (
            _directory_fingerprint(cas, depth=0),
            _directory_fingerprint(exhaust / CLAIM_ATTESTATION_STORE_DIRECTORY, depth=1),
        )
    )


def memo_key(
    *,
    instance_root: str,
    coordinate_digest: str,
    claim_set_digest: str,
    input_fingerprint: str,
) -> tuple[str, str, str, str]:
    """Name one derivation. The instance is part of it: one process serves many."""

    return (instance_root, coordinate_digest, claim_set_digest, input_fingerprint)


def invariance_interval(
    boundaries: Iterable[datetime],
    *,
    evaluation_time: datetime,
) -> tuple[datetime | None, datetime | None]:
    """The half-open interval around ``evaluation_time`` on which a verdict holds.

    Every time comparison a verdict makes is half-open at its boundary -- an
    interval is entered at ``effective_from`` and left at ``effective_until``,
    a capture is current from ``observed_at`` until it expires, an attestation
    holds until ``valid_until``. So the answer is constant on
    ``[b_i, b_{i+1})`` for consecutive boundaries, and ``None`` on either side
    means the answer holds back to, or forward to, forever.
    """

    lower: datetime | None = None
    upper: datetime | None = None
    for boundary in boundaries:
        if boundary <= evaluation_time:
            if lower is None or boundary > lower:
                lower = boundary
        elif upper is None or boundary < upper:
            upper = boundary
    return lower, upper


def interval_holds(
    interval: tuple[datetime | None, datetime | None],
    *,
    evaluation_time: datetime,
) -> bool:
    """Whether a remembered answer still stands at this instant."""

    lower, upper = interval
    if lower is not None and evaluation_time < lower:
        return False
    return upper is None or evaluation_time < upper


def claim_set_digest(identities: tuple[str, ...]) -> str:
    """A stable name for exactly the Claims one derivation was asked about."""

    digest = hashlib.sha256()
    for identity in sorted(identities, key=lambda item: item.encode("utf-8")):
        digest.update(identity.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def bounded_memo() -> "OrderedDict[tuple[str, str, str, str], object]":
    return OrderedDict()


__all__ = [
    "MEMO_CAPACITY",
    "bounded_memo",
    "claim_set_digest",
    "interval_holds",
    "invariance_interval",
    "memo_key",
    "verdict_input_fingerprint",
]
