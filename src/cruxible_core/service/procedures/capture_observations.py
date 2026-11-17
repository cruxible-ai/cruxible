"""An acquiring call's Capture reuse and content observation, bound to one instance.

The executor builds each fetched Capture's envelope and asks here whether an
identical re-fetch reproduced a held Capture (ruling 1c): the same contract,
source, request, selector, exact bytes, producer binding and derivation
inputs, looked up by the envelope's stable fields. A held Capture is reused
only while its envelope and its body are present at the landing log's head
and no erasure names its body: an erased or damaged Capture is never reused.
Each call's fetch is then recorded as one content observation (ruling 1a).
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from datetime import datetime
from typing import Any

from cruxible_client.contracts.captures import CaptureEnvelope, parse_capture_envelope
from cruxible_client.contracts.errors import CruxibleError
from cruxible_core.evidence.capture_erasures import CaptureErasureStore, erasure_journal_present
from cruxible_core.evidence.claim_reobservations import (
    ClaimReobservationStore,
    ObservedContent,
    ReobservationReader,
    RowMap,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.storage.cas import BodyAccessContext
from cruxible_core.storage.read_positions import body_present

_ACCESS = BodyAccessContext(principal_id="capture-reuse", can_read_body=True)
#: Parsed held envelopes, by digest: an immutable artifact parsed once per process.
_ENVELOPES: OrderedDict[str, CaptureEnvelope] = OrderedDict()
_ENVELOPES_CAPACITY = 4096
_LOCK = threading.Lock()


class InstanceCaptureObservations:
    """``CaptureObservationsProtocol`` over one instance's stores."""

    def __init__(self, instance: PlaybillInstance) -> None:
        self.instance = instance

    def held_capture(
        self, content_key: str, *, commitment_digest: str
    ) -> tuple[str, CaptureEnvelope] | None:
        """The held Capture of these contents, or None when none may be reused.

        Presence is the landing log's answer at its head, never a stat or a
        hash of the body: an envelope or body recorded missing or corrupt is
        not reused, and neither is one an erasure names.
        """

        store = self.instance.body_store()
        found = store.capture_envelopes.capture_for_content_key(content_key)
        if found is None:
            return None
        digest, proof = found
        # Named only once its run journaled producing it (F-008): reusable only
        # while that production record's body is not recorded missing, corrupt
        # or erased (a journal payload lands with its record, so only damage
        # is logged for it): one indexed answer, never a read of the producing
        # run's records.
        if _proof_lost(store, proof) or not _proof_shown(store, proof):
            return None
        if body_present(store, digest) is False or body_present(store, commitment_digest) is False:
            return None
        if erasure_journal_present(self.instance) and CaptureErasureStore(self.instance).for_bodies(
            (commitment_digest, digest)
        ):
            return None
        envelope = _held_envelope(store, digest)
        if envelope is None or envelope.commitment.digest != commitment_digest:
            return None
        return digest, envelope

    def remember(self, content_key: str, capture_digest: str, *, proof: str) -> None:
        """Name the new Capture these contents' next identical re-fetch reuses."""

        self.instance.body_store().capture_envelopes.remember_content_key(
            content_key, capture_digest, proof=proof
        )

    def observe_rows(
        self,
        maps: tuple[RowMap, ...],
        *,
        earliest: datetime,
        latest: datetime,
        run_id: str,
        node_id: str,
        actor_id: str,
        lineage: dict[str, str] | None = None,
    ) -> None:
        ClaimReobservationStore(self.instance).observe_rows(
            maps,
            earliest=earliest,
            latest=latest,
            run_id=run_id,
            node_id=node_id,
            actor_id=actor_id,
            lineage=lineage,
        )

    def extraction_map(self, extraction: str) -> str | None:
        return ReobservationReader.of(self.instance).extraction_map(extraction)

    def map_rows(self, map_digest: str) -> dict[str, str]:
        return ReobservationReader.of(self.instance).map_rows(map_digest)

    def rechecked(self, claim_id: str, capture_digest: str) -> bool:
        return ReobservationReader.of(self.instance).recorded(claim_id, capture_digest)

    def row_of(self, capture_digest: str, spec_digest: str, locator: str) -> str | None:
        found = ReobservationReader.of(self.instance).row_of(capture_digest, spec_digest, locator)
        return None if found is None else found[1]

    def observe(
        self,
        contents: tuple[tuple[str, str, str], ...],
        *,
        earliest: datetime,
        latest: datetime,
        run_id: str,
        node_id: str,
        actor_id: str,
        lineage: dict[str, str] | None = None,
    ) -> None:
        ClaimReobservationStore(self.instance).observe(
            [ObservedContent(*item) for item in contents],
            earliest=earliest,
            latest=latest,
            run_id=run_id,
            node_id=node_id,
            actor_id=actor_id,
            lineage=lineage,
        )


#: Production proof bodies read whole once in this process (immutable by
#: digest: a later loss is a recorded damage or erasure, ``_proof_lost``).
_SHOWN: set[tuple[str, str]] = set()


def _proof_shown(store: Any, digest: str) -> bool:
    """Whether the production record's body can be read (review 2d #3).

    A journal payload's arrival is not in the landing log, so its loss can go
    unrecorded: the candidate's proof is read once per process, and a Capture
    whose proof cannot be shown is never reused; the run fetches anew and
    records producing it, which replaces the reuse key.
    """

    key = (str(getattr(store, "root", "")), digest)
    if key in _SHOWN:
        return True
    try:
        store.read(digest, access=_ACCESS)
    except (CruxibleError, OSError, ValueError):
        return False
    with _LOCK:
        _SHOWN.add(key)
    return True


def _proof_lost(store: Any, digest: str) -> bool:
    """Whether the landing log records a production proof body damaged or erased."""

    landings = getattr(store, "landings", None)
    if landings is None:
        return False
    head = landings.head()
    kind = landings.last_completion(digest, identity=head.identity, sequence=head.sequence)
    return kind not in (None, "stored")


def _held_envelope(store: Any, digest: str) -> CaptureEnvelope | None:
    with _LOCK:
        held = _ENVELOPES.get(digest)
        if held is not None:
            _ENVELOPES.move_to_end(digest)
            return held
    try:
        parsed = parse_capture_envelope(store.read(digest, access=_ACCESS))
    except (CruxibleError, ValueError):
        return None
    if not isinstance(parsed, CaptureEnvelope):
        return None
    with _LOCK:
        _ENVELOPES[digest] = parsed
        while len(_ENVELOPES) > _ENVELOPES_CAPACITY:
            _ENVELOPES.popitem(last=False)
    return parsed


__all__ = ["InstanceCaptureObservations"]
