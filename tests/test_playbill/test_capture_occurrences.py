from __future__ import annotations

from cruxible_core.playbill.artifacts import ArtifactIdentity
from cruxible_core.playbill.capture_journal import CaptureLandingEventV1
from cruxible_core.playbill.captures import CaptureRunCoordinateV1
from cruxible_core.playbill.occurrences import capture_landing_occurrence, occurrence_digest
from tests.test_playbill._pc_c_support import NOW, digest


def _event() -> CaptureLandingEventV1:
    return CaptureLandingEventV1(
        instance_id="inst-occurrence",
        partition_id="11" * 32,
        sequence=4,
        event_id="22" * 32,
        idempotency_key="33" * 32,
        capture_digest=digest("capture", "one"),
        capture_contract_digest=digest("contract", "one"),
        run_coordinate=CaptureRunCoordinateV1(
            run_kind="watcher",
            run_id="watcher-1",
            bound_generation=digest("generation", "one"),
            executable_identity=ArtifactIdentity(kind="Watcher", name="orders"),
            executable_digest=digest("watcher", "one"),
        ),
        run_receipt_digest=digest("receipt", "one"),
        producer_binding_digest=digest("binding", "one"),
        previous_event_digest="44" * 32,
        landed_at=NOW,
    )


def test_retry_keeps_occurrence_and_trigger_epoch_changes_it() -> None:
    event = _event()
    first = capture_landing_occurrence(line_id="order-release", occurrence_epoch=2, anchor=event)
    retry = capture_landing_occurrence(line_id="order-release", occurrence_epoch=2, anchor=event)
    successor = capture_landing_occurrence(
        line_id="order-release",
        occurrence_epoch=3,
        anchor=event,
    )
    assert occurrence_digest(first) == occurrence_digest(retry)
    assert occurrence_digest(first) != occurrence_digest(successor)
