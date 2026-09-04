"""A per-process memo for the per-Claim verdict and resolution-status map.

`orient` and `next` are READS of accepted state, and both begin by deriving
every live Claim's verdict and resolution status at one coordinate. At a few
hundred Claims that derivation crossed the client's own three-minute default
timeout, and at seven hundred it ran for minutes at a time -- so the first
thing every SDK session does could fail against a healthy instance.

The derivation is a pure function of things that do not move between two reads
at the same coordinate, so this remembers it. It is a cache and nothing else:

* it is per-process and cold after a restart, so no answer depends on it;
* it is keyed on everything the derivation reads -- the accepted coordinate,
  the evaluation instant, the exact Claim set, and a fingerprint of the two
  stores a verdict consults BESIDES the accepted tree: the content-addressed
  body store, whose contents decide whether a capture can be replayed now, and
  the principal-authored attestation ledger;
* it is bounded, so a long-lived daemon reading many coordinates does not grow
  without limit.

It is deliberately NOT a projection table. Nothing here is accepted state,
nothing is served as a fact, and the compiler does not know it exists.
"""

from __future__ import annotations

import hashlib
import os
from collections import OrderedDict
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
    the derivation this memo exists to skip.
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
    # The CAS is sharded one level deep; the attestation ledger keeps its
    # partition files one level under its own directory.
    return "-".join(
        (
            _directory_fingerprint(cas, depth=1),
            _directory_fingerprint(exhaust / CLAIM_ATTESTATION_STORE_DIRECTORY, depth=1),
        )
    )


def memo_key(
    *,
    coordinate_digest: str,
    evaluation_time: str,
    claim_set_digest: str,
    input_fingerprint: str,
) -> tuple[str, str, str, str]:
    return (coordinate_digest, evaluation_time, claim_set_digest, input_fingerprint)


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
    "memo_key",
    "verdict_input_fingerprint",
]
