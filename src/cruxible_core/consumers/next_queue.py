"""Keep the expensive Claim portion of next current by following two log heads.

This findings projection is disposable. Matching records a new accepted/door
pair, and one bounded worker folds it before moving the serving cursor. Time
bounds come from the fold, never from polling a clock: crossing a bound makes
reads compute live until another log change causes a recomputation.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import format_datetime
from cruxible_core.consumers.protocol import (
    ConsumerHealth,
    ConsumerRepair,
    ConsumerWork,
    CursorPolicy,
    EffectClass,
)
from cruxible_core.consumers.state import DisposableState
from cruxible_core.server.config import get_disabled_consumers
from cruxible_core.service.claims.verdict_memo import interval_holds, verdict_input_fingerprint

if TYPE_CHECKING:
    from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
    from cruxible_core.service.discovery.next import _StoredClaimQueue

_SCHEMA = """
CREATE TABLE progress (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 target_coordinate TEXT NOT NULL, target_door TEXT NOT NULL,
 coordinate TEXT, door TEXT, generation INTEGER NOT NULL DEFAULT 0,
 checked_at TEXT, input_fingerprint TEXT, v1 TEXT, v2 TEXT,
 last_error TEXT, last_error_at TEXT
) STRICT;
"""
_STATE = DisposableState("next-queue", _SCHEMA)


def stored_claim_queue(
    instance: Any,
    *,
    coordinate: AcceptedProjectionCoordinate,
    door_head: str,
    evaluation_time: datetime,
    version: int,
) -> _StoredClaimQueue | None:
    """Read only a matching current-head projection; a miss does no background work."""

    from cruxible_core.service.discovery.next import _StoredClaimQueue

    if not NEXT_QUEUE.active(instance) or coordinate != instance.accepted_coordinate():
        return None
    with _STATE.open(instance, create=False) as connection:
        if connection is None:
            return None
        row = connection.execute(
            "SELECT coordinate,door,input_fingerprint,v1,v2 FROM progress"
        ).fetchone()
    if row is None or row[0] != AcceptedCoordinate.from_internal(coordinate).model_dump_json():
        return None
    if row[1] != door_head or row[2] is None or row[2] != verdict_input_fingerprint(instance):
        return None
    payload = row[3 if version == 1 else 4]
    if payload is None:
        return None
    stored = _StoredClaimQueue.model_validate_json(payload)
    return (
        stored
        if interval_holds((stored.valid_from, stored.valid_until), evaluation_time=evaluation_time)
        else None
    )


class NextQueueConsumers:
    name = "next"
    cursor_policy: CursorPolicy = "resume"
    effect_class: EffectClass = "findings"
    workers = 1

    def active(self, instance: Any) -> bool:
        return self.name not in get_disabled_consumers()

    def match(self, instance: Any, *, now: datetime, daemon_id: str) -> None:
        coordinate = AcceptedCoordinate.from_internal(
            instance.accepted_coordinate()
        ).model_dump_json()
        door = instance.claim_attestation_evidence_store().head()
        with _STATE.open(instance) as connection:
            assert connection is not None
            connection.execute(
                "INSERT INTO progress(singleton,target_coordinate,target_door) VALUES (1,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET "
                "target_coordinate=excluded.target_coordinate,target_door=excluded.target_door",
                (coordinate, door),
            )

    def due(self, instance: Any, *, now: datetime) -> Iterable[ConsumerWork]:
        with _STATE.open(instance, create=False) as connection:
            if connection is None:
                return ()
            row = connection.execute(
                "SELECT target_coordinate,target_door FROM progress "
                "WHERE coordinate IS NULL OR coordinate!=target_coordinate OR door!=target_door"
            ).fetchone()
        return () if row is None else (ConsumerWork(key="queue", item=row),)

    def run(self, manager: Any, instance_id: str, work: ConsumerWork, *, now: datetime) -> None:
        from cruxible_core.service.discovery.next import build_stored_claim_queue

        instance = manager.get(instance_id)
        public = AcceptedCoordinate.model_validate_json(work.item[0])
        coordinate = instance.resolve_accepted_coordinate(
            git_oid=public.git_oid,
            semantic_root=public.semantic_root,
            generation_root=public.generation_root,
            compiler_digest=public.compiler_digest,
        )
        try:
            fingerprint = verdict_input_fingerprint(instance)
            v1 = build_stored_claim_queue(
                instance, coordinate=coordinate, attestation_head=None, evaluation_time=now
            )
            v2 = build_stored_claim_queue(
                instance, coordinate=coordinate, attestation_head=work.item[1], evaluation_time=now
            )
            if fingerprint != verdict_input_fingerprint(instance):
                # Inputs moved during the fold. Leave this pair due to be rebuilt.
                return
            with instance.accepted_history_reader(at=public) as history:
                generation = history.sequence
            with _STATE.open(instance) as connection:
                assert connection is not None
                connection.execute(
                    "UPDATE progress SET coordinate=?,door=?,generation=?,checked_at=?,"
                    "input_fingerprint=?,v1=?,v2=?,last_error=NULL,last_error_at=NULL",
                    (
                        *work.item,
                        generation,
                        format_datetime(now),
                        fingerprint,
                        v1.model_dump_json(),
                        v2.model_dump_json(),
                    ),
                )
        except Exception as exc:
            with _STATE.open(instance) as connection:
                assert connection is not None
                connection.execute(
                    "UPDATE progress SET last_error=?,last_error_at=?",
                    (f"{type(exc).__name__}: {exc}", format_datetime(now)),
                )
            raise

    def health(self, instance: Any, *, now: datetime) -> tuple[ConsumerHealth, ...]:
        with _STATE.open(instance, create=False) as connection:
            if connection is None:
                return ()
            row = connection.execute(
                "SELECT coordinate,door,generation,checked_at,last_error,last_error_at "
                "FROM progress"
            ).fetchone()
        if row is None:
            return ()
        coordinate, door, generation, checked_at, error, error_at = row
        head = AcceptedCoordinate.from_internal(instance.accepted_coordinate()).model_dump_json()
        door_head = instance.claim_attestation_evidence_store().head()
        with instance.accepted_history_reader() as history:
            behind = max(0, history.sequence - generation)
        return (
            ConsumerHealth(
                kind=self.name,
                consumer_id="consumer:next",
                state="stalled"
                if error is not None
                else ("lagging" if coordinate != head or door != door_head else "running"),
                detail={
                    "generation": generation,
                    "generations_behind": behind,
                    "attestation_head_digest": door,
                    "attestations_behind": door != door_head,
                    "checked_at": checked_at,
                    "last_error": error,
                    "last_error_at": error_at,
                },
                repair=(
                    ConsumerRepair(
                        operation="hand_edit",
                        required_change="resolve_the_worker_error_then_restart_the_daemon",
                        arguments={},
                    )
                    if error is not None
                    else None
                ),
            ),
        )


NEXT_QUEUE = NextQueueConsumers()
