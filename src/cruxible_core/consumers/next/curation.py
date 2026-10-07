"""Curation detection within the next consumer.

The ``curation.detect`` internal action runs every curation detector at the
accepted head when its Trigger fires (by default on every accepted
generation), recording what it found in the review-operational store. So the
curation list is a pure read: it shows what detection last recorded, and when.

Detection is evaluated at the fire's recorded instant, never the worker's
clock or a reader's, and it acts as the system, not as any principal. A fired
event is handled once; a burst of fires that arrive before the worker runs is
detected once, at the newest. The detector coverage of the last run is kept
here as a disposable projection: deleting it only shows detection as never
run until the next fire.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cruxible_client.contracts.temporal import format_datetime, parse_datetime
from cruxible_client.contracts.triggers import INTERNAL_ACTIONS
from cruxible_core.consumers.protocol import ConsumerHealth, ConsumerRepair, ConsumerWork
from cruxible_core.consumers.state import DisposableState
from cruxible_core.curation.curation import CurationDetectorCoverageV1
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.triggers.journal import trigger_events

#: The internal action this part performs; its registry entry names this part.
ACTION = INTERNAL_ACTIONS["curation.detect"]

#: Fires one pass reads to find the newest pending one.
FIRE_BATCH = 256

_SCHEMA = """
CREATE TABLE IF NOT EXISTS progress (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 sequence INTEGER NOT NULL DEFAULT 0,
 generation INTEGER, detected_at TEXT, coverage TEXT,
 last_error TEXT, last_error_at TEXT
) STRICT;
INSERT OR IGNORE INTO progress(singleton) VALUES (1);
"""
_STATE = DisposableState("next/curation", _SCHEMA)


@dataclass(frozen=True)
class CurationDetection:
    """What the last detection run covered."""

    generation: int
    detected_at: datetime
    coverage: tuple[CurationDetectorCoverageV1, ...]


def curation_detection(instance: Any) -> CurationDetection | None:
    """The last recorded detection run; None before detection has run here."""

    with _STATE.open(instance, create=False) as connection:
        if connection is None:
            return None
        row = connection.execute("SELECT generation,detected_at,coverage FROM progress").fetchone()
    if row is None or row[0] is None:
        return None
    detected_at = parse_datetime(row[1])
    assert detected_at is not None
    return CurationDetection(
        generation=row[0],
        detected_at=detected_at,
        coverage=tuple(
            CurationDetectorCoverageV1.model_validate(item) for item in json.loads(row[2])
        ),
    )


def record_detection(instance: Any, *, sequence: int, detection: CurationDetection) -> None:
    """Keep what one detection run covered, as handled through trigger fire ``sequence``."""

    with _STATE.open(instance) as connection:
        assert connection is not None
        connection.execute(
            "UPDATE progress SET sequence=?,generation=?,detected_at=?,coverage=?,"
            "last_error=NULL,last_error_at=NULL",
            (
                sequence,
                detection.generation,
                format_datetime(detection.detected_at),
                json.dumps([item.model_dump(mode="json") for item in detection.coverage]),
            ),
        )


class CurationPart:
    def match(self, instance: Any, *, now: datetime, daemon_id: str) -> None:
        # Fires are read straight from the trigger journal; nothing to record.
        return None

    def due(self, instance: Any, *, now: datetime) -> Iterable[ConsumerWork]:
        with _STATE.open(instance) as connection:
            assert connection is not None
            (sequence,) = connection.execute("SELECT sequence FROM progress").fetchone()
        if trigger_events(instance, after=sequence, action=ACTION.name, limit=1):
            return (ConsumerWork(key="detect", item="detect"),)
        return ()

    def run(self, manager: Any, instance_id: str, work: ConsumerWork, *, now: datetime) -> None:
        from cruxible_core.service.discovery.curation import run_playbill_curation_detection

        instance = manager.get(instance_id)
        with _STATE.open(instance) as connection:
            assert connection is not None
            (sequence,) = connection.execute("SELECT sequence FROM progress").fetchone()
        events = trigger_events(instance, after=sequence, action=ACTION.name, limit=FIRE_BATCH)
        if not events:
            return
        # Fires that piled up are detected once, at the newest one.
        event = events[-1]
        actor = GovernedActorContext(
            actor_type="system",
            actor_id="curation-detector",
            org_id=instance.descriptor.instance_id,
            operation_id=f"curation.detect:{event.sequence}",
            timestamp=event.fired_at,
        )
        try:
            generation, coverage = run_playbill_curation_detection(
                instance, evaluation_time=event.fired_at, actor_context=actor
            )
        except Exception as exc:
            with _STATE.open(instance) as connection:
                assert connection is not None
                connection.execute(
                    "UPDATE progress SET last_error=?,last_error_at=?",
                    (f"{type(exc).__name__}: {exc}", format_datetime(now)),
                )
            raise
        record_detection(
            instance,
            sequence=event.sequence,
            detection=CurationDetection(
                generation=generation, detected_at=event.fired_at, coverage=coverage
            ),
        )

    def health(self, instance: Any, *, now: datetime) -> tuple[ConsumerHealth, ...]:
        with _STATE.open(instance, create=False) as connection:
            if connection is None:
                return ()
            row = connection.execute(
                "SELECT sequence,generation,last_error,last_error_at FROM progress"
            ).fetchone()
        sequence, generation, error, error_at = row
        failing = error is not None
        pending = bool(trigger_events(instance, after=sequence, action=ACTION.name, limit=1))
        return (
            ConsumerHealth(
                kind="next",
                consumer_id="consumer:next",
                state="stalled" if failing else "lagging" if pending else "running",
                detail={
                    "detected_through_generation": generation,
                    "fire_position": sequence,
                    "detection_pending": pending,
                    "last_error": error,
                    "last_error_at": error_at,
                },
                repair=(
                    ConsumerRepair(
                        operation="hand_edit",
                        required_change="resolve_the_worker_error_then_restart_the_daemon",
                        arguments={},
                    )
                    if failing
                    else None
                ),
            ),
        )


_PART = CurationPart()

__all__ = [
    "ACTION",
    "CurationDetection",
    "CurationPart",
    "curation_detection",
    "record_detection",
]
