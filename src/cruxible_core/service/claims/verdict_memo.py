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
  accepted coordinate, the exact Claim set, and the head of the body store's
  landing log, which moves when an evidence body arrives or any body leaves
  (deciding whether a capture can be replayed now) but not for journal-record
  payloads. Verification records damage and repair in that same log; reads
  never re-check body files to validate a remembered answer.
  A positioned read keys on its recorded landing and re-observation positions
  instead of the live heads, so it answers only for the snapshot it read.
  Accepted attestations are bound by the coordinate; pending door attestations
  are not consumed by this derivation;
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
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

MEMO_CAPACITY = 4


def verdict_input_fingerprint(instance: object) -> str | None:
    """Fingerprint every input a Claim verdict reads besides the accepted tree.

    The body store logs every evidence body that lands and every erase, after
    the object as well as before (``ContentAddressedBodyStore.landings``): the
    log's identity and newest sequence are one indexed read, and they do not
    move for journal-record payloads (run records, Line dispatch coverage,
    settlements), which are reachable only through the record that names them,
    appended after the body lands. This is a writer-owned availability signal,
    not a per-read file integrity check. Verification records missing/corrupt
    bodies and repairs as sequenced completions in the same log, so later
    reads invalidate from those records. An unreadable log disables reuse
    entirely: two failed observations must never form a reusable key.

    Inside a positioned read (``storage.read_positions.reading_at``) body
    presence and re-observations answer as of the read's recorded landing and
    journal positions, so those positions name the derivation instead: a fold at
    an older position never hits, nor stores under, a later read's entry.
    """

    from cruxible_client.contracts.errors import CasError

    root = getattr(instance, "root", None)
    descriptor = getattr(instance, "descriptor", None)
    if not isinstance(root, Path) or descriptor is None:
        return None
    storage = getattr(descriptor, "storage", None)
    body_store = getattr(instance, "body_store", None)
    if storage is None or body_store is None:
        return None
    from cruxible_core.storage.read_positions import current_positions

    positions = current_positions()
    if positions is not None:
        # A positioned read answers as of the positions it recorded, not where the
        # logs stand now: its derivation is named by those positions, so it neither
        # serves nor stores an answer for any other. The door is not an input here.
        return (
            f"positioned|landings:{positions.landing_log}:{positions.landing}"
            f"|reobservation:{positions.reobs}"
        )
    try:
        # Through the store's held directory: never a pathname an ancestor swap redirects.
        head = body_store().landings.head()
    except (CasError, OSError):
        return None
    token = f"{head.identity}:{head.sequence}"
    # A recorded re-observation moves a cited content's freshness without an
    # evidence body landing: the re-observation journal's own log is an input
    # too, read from the authority so an interrupted append is never missed.
    from cruxible_core.evidence.claim_reobservations import reobservation_journal_signature

    try:
        journal = reobservation_journal_signature(
            root,
            exhaust=str(getattr(storage, "exhaust", "exhaust")),
            instance_id=str(descriptor.instance_id),
        )
    except (OSError, ValueError):
        return None
    if journal is None:
        return "landings:" + token
    return "landings:" + token + "|reobservation:" + journal


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


__all__ = [
    "MEMO_CAPACITY",
    "claim_set_digest",
    "interval_holds",
    "invariance_interval",
    "memo_key",
    "verdict_input_fingerprint",
]
