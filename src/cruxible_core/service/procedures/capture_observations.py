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
        digest = store.capture_envelopes.capture_for_content_key(content_key)
        if digest is None:
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
        if not self._produced(digest, envelope):
            return None
        return digest, envelope

    def _produced(self, digest: str, envelope: CaptureEnvelope) -> bool:
        """Whether the held Capture's producing run recorded producing it, proof available (F-008).

        Read through the request's verification context: one read of that
        run's records, shared with every later proof check of the request.
        A Capture whose production is unproven is never reused: the new one
        is built and recorded instead.
        """

        context = self.instance.capture_verification_context()
        run_id = envelope.run_coordinate.run_id
        try:
            produced = any(
                record.capture_digest == digest
                and record.capture_contract_digest == envelope.capture_contract_digest
                and record.receipt_digest in (None, envelope.producer_receipt_digest)
                for record in context.produced_capture(digest, run_id=run_id)
            )
            return produced and (
                context.producer_receipt(envelope.producer_receipt_digest, run_id=run_id)
                is not None
            )
        except (CruxibleError, ValueError, OSError):
            return False

    def remember(self, content_key: str, capture_digest: str) -> None:
        """Name the new Capture these contents' next identical re-fetch reuses."""

        self.instance.body_store().capture_envelopes.remember_content_key(
            content_key, capture_digest
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

    def observe_enumeration(
        self,
        *,
        contract_digest: str,
        enumeration: str,
        capture_digest: str,
        earliest: datetime,
        latest: datetime,
        run_id: str,
        node_id: str,
        actor_id: str,
        lineage: dict[str, str] | None = None,
    ) -> None:
        ClaimReobservationStore(self.instance).observe_enumeration(
            contract_digest=contract_digest,
            enumeration=enumeration,
            capture_digest=capture_digest,
            earliest=earliest,
            latest=latest,
            run_id=run_id,
            node_id=node_id,
            actor_id=actor_id,
            lineage=lineage,
        )

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
