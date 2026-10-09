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
  accepted coordinate, the exact Claim set, and the body store's evidence
  generation, which moves when an evidence body arrives or any body leaves
  (deciding whether a capture can be replayed now) but not for journal-record
  payloads. The generation sees bodies arrive and leave, not a body rewritten
  in place, so each entry also keeps the file identity of every body-store
  object its verdicts read and is served only while all of them hold (one stat
  each);
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

    The body store replaces its evidence-generation token after every evidence
    body lands and every erase (``ContentAddressedBodyStore.store``): one small
    read, and it does not move for journal-record payloads (run records, Line
    dispatch coverage, settlements), which are reachable only through the
    record that names them, appended after the body lands. This is a
    writer-owned availability signal, not a proof against in-place tampering
    with body bytes; the body fingerprints kept with each remembered derivation
    cover that, and they record a consulted body's absence too. An unreadable
    token disables reuse entirely: two failed observations must never form a
    reusable key.
    """

    from cruxible_core.storage.cas import ContentAddressedBodyStore

    root = getattr(instance, "root", None)
    descriptor = getattr(instance, "descriptor", None)
    if not isinstance(root, Path) or descriptor is None:
        return None
    storage = getattr(descriptor, "storage", None)
    if storage is None:
        return None
    marker = (
        root
        / getattr(storage, "cas", "cas")
        / "sha256"
        / ContentAddressedBodyStore.EVIDENCE_GENERATION
    )
    try:
        token = marker.read_text("ascii")
    except FileNotFoundError:
        token = ""
    except (OSError, ValueError):
        return None
    # A recorded re-observation moves a cited content's freshness without an
    # evidence body landing: its journal's generation marker is an input too.
    from cruxible_core.evidence.claim_reobservations import (
        REOBSERVATION_DIRECTORY,
        REOBSERVATION_GENERATION,
    )

    reobserved = (
        root / getattr(storage, "exhaust", "exhaust") / REOBSERVATION_DIRECTORY
    ) / REOBSERVATION_GENERATION
    try:
        sequence = reobserved.read_text("ascii")
    except FileNotFoundError:
        return "evidence-generation:" + token
    except (OSError, ValueError):
        return None
    return "evidence-generation:" + token + "|reobservation:" + sequence


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
