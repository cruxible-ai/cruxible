"""Keep next's expensive Claim rows current as their accepted inputs change.

This findings projection is disposable. Matching follows the accepted and door
heads plus CAS availability, and one bounded worker folds each new target
before moving the serving cursor. Time bounds come from the fold, never from
polling a clock: a deadline fire requests the next fold, and reads compute live
until that fold catches up. A failed target remains stalled until an input
changes or the operator rebuilds this disposable state, avoiding endless folds
of the same broken inputs.
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
)
from cruxible_core.consumers.state import DisposableState
from cruxible_core.server.config import get_disabled_consumers
from cruxible_core.service.claims.verdict_memo import interval_holds, verdict_input_fingerprint
from cruxible_core.triggers.journal import (
    cancel_deadline,
    latest_sequence,
    schedule_deadline,
    trigger_events,
)

if TYPE_CHECKING:
    from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
    from cruxible_core.service.discovery.next import _StoredClaimQueue

_SCHEMA = """
CREATE TABLE progress (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 target_coordinate TEXT NOT NULL, target_door TEXT NOT NULL, target_fingerprint TEXT,
 target_expire INTEGER NOT NULL DEFAULT 0, expire INTEGER NOT NULL DEFAULT 0,
 coordinate TEXT, door TEXT, generation INTEGER NOT NULL DEFAULT 0,
 checked_at TEXT, input_fingerprint TEXT, v1 TEXT, v2 TEXT,
 failed_coordinate TEXT, failed_door TEXT, failed_fingerprint TEXT, failed_expire INTEGER,
 last_error TEXT, last_error_at TEXT
) STRICT;
"""
_STATE = DisposableState("next/queue", _SCHEMA)


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

    if not _PART.active(instance) or coordinate != instance.accepted_coordinate():
        return None
    with _STATE.open(instance, create=False) as connection:
        if connection is None:
            return None
        row = connection.execute(
            "SELECT coordinate,door,input_fingerprint,expire,v1,v2 FROM progress"
        ).fetchone()
    if row is None or row[0] != AcceptedCoordinate.from_internal(coordinate).model_dump_json():
        return None
    if row[1] != door_head or row[2] is None or row[2] != verdict_input_fingerprint(instance):
        return None
    if row[3] != latest_sequence(instance, action="next.expire"):
        return None
    payload = row[4 if version == 1 else 5]
    if payload is None:
        return None
    stored = _StoredClaimQueue.model_validate_json(payload)
    return (
        stored
        if interval_holds((stored.valid_from, stored.valid_until), evaluation_time=evaluation_time)
        else None
    )


class ClaimQueuePart:
    name = "next"

    def active(self, instance: Any) -> bool:
        return self.name not in get_disabled_consumers()

    def match(self, instance: Any, *, now: datetime, daemon_id: str) -> None:
        coordinate = AcceptedCoordinate.from_internal(
            instance.accepted_coordinate()
        ).model_dump_json()
        door = instance.claim_attestation_evidence_store().head()
        fingerprint = verdict_input_fingerprint(instance)
        expiry = latest_sequence(instance, action="next.expire")
        with _STATE.open(instance) as connection:
            assert connection is not None
            targets = connection.execute(
                "SELECT target_coordinate,target_door,target_fingerprint,target_expire "
                "FROM progress"
            ).fetchone()
            if targets == (coordinate, door, fingerprint, expiry):
                return
            connection.execute(
                "INSERT INTO progress(singleton,target_coordinate,target_door,"
                "target_fingerprint,target_expire) "
                "VALUES (1,?,?,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET "
                "target_coordinate=excluded.target_coordinate,target_door=excluded.target_door,"
                "target_fingerprint=excluded.target_fingerprint,target_expire=excluded.target_expire",
                (coordinate, door, fingerprint, expiry),
            )

    def due(self, instance: Any, *, now: datetime) -> Iterable[ConsumerWork]:
        with _STATE.open(instance, create=False) as connection:
            if connection is None:
                return ()
            row = connection.execute(
                "SELECT target_coordinate,target_door,target_fingerprint,target_expire "
                "FROM progress "
                "WHERE (coordinate IS NOT target_coordinate OR door IS NOT target_door "
                "OR input_fingerprint IS NOT target_fingerprint OR expire IS NOT target_expire) "
                "AND (failed_coordinate IS NULL OR failed_coordinate IS NOT target_coordinate "
                "OR failed_door IS NOT target_door OR failed_fingerprint IS NOT target_fingerprint "
                "OR failed_expire IS NOT target_expire)"
            ).fetchone()
        return () if row is None else (ConsumerWork(key="queue", item=row),)

    def run(self, manager: Any, instance_id: str, work: ConsumerWork, *, now: datetime) -> None:
        from cruxible_core.service.discovery.next import build_stored_claim_queue

        instance = manager.get(instance_id)
        try:
            public = AcceptedCoordinate.model_validate_json(work.item[0])
            coordinate = instance.resolve_accepted_coordinate(
                git_oid=public.git_oid,
                semantic_root=public.semantic_root,
                generation_root=public.generation_root,
                compiler_digest=public.compiler_digest,
            )
            fingerprint = verdict_input_fingerprint(instance)
            if fingerprint != work.item[2]:
                # Matching will pick up the availability change on its next pass.
                return
            evaluated_at = now
            with _STATE.open(instance) as connection:
                assert connection is not None
                (completed_expiry,) = connection.execute("SELECT expire FROM progress").fetchone()
            if work.item[3] > completed_expiry:
                (event,) = trigger_events(
                    instance, after=work.item[3] - 1, action="next.expire", limit=1
                )
                evaluated_at = event.fired_at
            # Both wire versions are served. V1 ignores door observations, so
            # keeping both ready avoids read-triggered work or a full live fold.
            # Their resolution derivation shares the existing verdict memo.
            v1 = build_stored_claim_queue(
                instance, coordinate=coordinate, attestation_head=None, evaluation_time=evaluated_at
            )
            v2 = build_stored_claim_queue(
                instance,
                coordinate=coordinate,
                attestation_head=work.item[1],
                evaluation_time=evaluated_at,
            )
            if fingerprint != verdict_input_fingerprint(instance):
                # Inputs moved during the fold. Matching will record their new target.
                return
            with instance.accepted_history_reader(at=public) as history:
                generation = history.sequence
            boundaries = [bound for bound in (v1.valid_until, v2.valid_until) if bound is not None]
            # Arm before publishing: a crash cannot leave a served queue without
            # its refresh signal. A restarted fold may replace the same deadline.
            if boundaries:
                schedule_deadline(instance, "next.expire", min(boundaries))
            else:
                cancel_deadline(instance, "next.expire")
            with _STATE.open(instance) as connection:
                assert connection is not None
                connection.execute(
                    "UPDATE progress SET coordinate=?,door=?,generation=?,checked_at=?,"
                    "input_fingerprint=?,v1=?,v2=?,expire=?,failed_coordinate=NULL,failed_door=NULL,"
                    "failed_fingerprint=NULL,failed_expire=NULL,last_error=NULL,last_error_at=NULL",
                    (
                        work.item[0],
                        work.item[1],
                        generation,
                        format_datetime(now),
                        fingerprint,
                        v1.model_dump_json(),
                        v2.model_dump_json(),
                        work.item[3],
                    ),
                )
        except Exception as exc:
            with _STATE.open(instance) as connection:
                assert connection is not None
                connection.execute(
                    "UPDATE progress SET failed_coordinate=?,failed_door=?,"
                    "failed_fingerprint=?,failed_expire=?,"
                    "last_error=?,last_error_at=?",
                    (*work.item, f"{type(exc).__name__}: {exc}", format_datetime(now)),
                )
            raise

    def health(self, instance: Any, *, now: datetime) -> tuple[ConsumerHealth, ...]:
        with _STATE.open(instance, create=False) as connection:
            if connection is None:
                return ()
            row = connection.execute(
                "SELECT coordinate,door,generation,last_error,input_fingerprint,expire "
                "FROM progress"
            ).fetchone()
        if row is None:
            return ()
        coordinate, door, generation, error, fingerprint, expiry = row
        expiry_head = latest_sequence(instance, action="next.expire")
        head = AcceptedCoordinate.from_internal(instance.accepted_coordinate()).model_dump_json()
        door_head = instance.claim_attestation_evidence_store().head()
        inputs_changed = fingerprint != verdict_input_fingerprint(instance)
        with instance.accepted_history_reader() as history:
            behind = max(0, history.sequence - generation)
        return (
            ConsumerHealth(
                kind=self.name,
                consumer_id="consumer:next",
                state="stalled"
                if error is not None
                else (
                    "lagging"
                    if coordinate != head
                    or door != door_head
                    or inputs_changed
                    or expiry != expiry_head
                    else "running"
                ),
                detail={
                    "generation": generation,
                    "generations_behind": behind,
                    "attestation_head_digest": door,
                    "attestations_behind": door != door_head,
                    "inputs_changed": inputs_changed,
                    "expiry_position": expiry,
                    "pending_expiry": expiry != expiry_head,
                    "last_error": error,
                },
                repair=(
                    ConsumerRepair(
                        operation="hand_edit",
                        required_change="resolve_the_worker_error_then_rebuild_the_next_queue_state",
                        arguments={},
                    )
                    if error is not None
                    else None
                ),
            ),
        )


_PART = ClaimQueuePart()
