"""One findings kind maintains next's Claim queue, evidence and prediction windows.

Parts retain independent cursors and bounded work keys under one disposable
state namespace. The daemon gives this kind one pool; distinct part keys allow
work to progress together, and served health combines their lag and failures.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from cruxible_core.consumers.next import evidence, predictions, queue
from cruxible_core.consumers.protocol import (
    ConsumerHealth,
    ConsumerRepair,
    ConsumerWork,
    CursorPolicy,
    EffectClass,
)
from cruxible_core.consumers.state import DisposableState
from cruxible_core.server.config import get_disabled_consumers

_PARTS: dict[str, queue.ClaimQueuePart | evidence.EvidencePart | predictions.PredictionPart] = {
    "queue": queue._PART,
    "evidence": evidence._PART,
    "prediction": predictions._PART,
}

_CONTROL = DisposableState(
    "next/control",
    "CREATE TABLE errors (part TEXT NOT NULL, operation TEXT NOT NULL, error TEXT NOT NULL, "
    "PRIMARY KEY(part,operation)) STRICT;",
)


def _errors(instance: Any) -> dict[tuple[str, str], str]:
    with _CONTROL.open(instance, create=False) as connection:
        if connection is None:
            return {}
        return {
            (part, operation): error
            for part, operation, error in connection.execute(
                "SELECT part,operation,error FROM errors"
            )
        }


def _call(
    instance: Any, part: str, operation: str, action: Callable[[], Any], *, propagate: bool = False
) -> Any:
    try:
        result = action()
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        if _errors(instance).get((part, operation)) != message:
            with _CONTROL.open(instance) as connection:
                assert connection is not None
                connection.execute(
                    "INSERT INTO errors VALUES (?,?,?) ON CONFLICT(part,operation) "
                    "DO UPDATE SET error=excluded.error",
                    (part, operation, message),
                )
        if propagate:
            raise
        return None
    if (part, operation) in _errors(instance):
        with _CONTROL.open(instance) as connection:
            assert connection is not None
            connection.execute("DELETE FROM errors WHERE part=? AND operation=?", (part, operation))
    return result


class NextConsumers:
    name = "next"
    cursor_policy: CursorPolicy = "resume"
    effect_class: EffectClass = "findings"
    workers = 3

    def active(self, instance: Any) -> bool:
        return self.name not in get_disabled_consumers()

    def match(self, instance: Any, *, now: datetime, daemon_id: str) -> None:
        for name, part in _PARTS.items():
            _call(
                instance, name, "match", lambda: part.match(instance, now=now, daemon_id=daemon_id)
            )

    def due(self, instance: Any, *, now: datetime) -> Iterable[ConsumerWork]:
        failed = _errors(instance)
        work: list[ConsumerWork] = []
        for name, part in _PARTS.items():
            if (name, "match") in failed:
                continue
            units = _call(instance, name, "due", lambda: tuple(part.due(instance, now=now)))
            work.extend(
                ConsumerWork(key=f"{name}:{unit.key}", item=(name, unit)) for unit in units or ()
            )
        return tuple(work)

    def run(self, manager: Any, instance_id: str, work: ConsumerWork, *, now: datetime) -> None:
        name, unit = work.item
        _call(
            manager.get(instance_id),
            name,
            f"run:{unit.key}",
            lambda: _PARTS[name].run(manager, instance_id, unit, now=now),
            propagate=True,
        )

    def health(self, instance: Any, *, now: datetime) -> tuple[ConsumerHealth, ...]:
        details: dict[str, dict[str, Any]] = {}
        for name, part in _PARTS.items():
            try:
                healths = part.health(instance, now=now)
            except Exception as exc:
                details[name] = {"state": "stalled", "last_error": f"{type(exc).__name__}: {exc}"}
                continue
            for health in healths:
                # Check times are operational bookkeeping, not finding identity.
                details[name] = {
                    "state": health.state,
                    **{
                        key: value
                        for key, value in health.detail.items()
                        if key not in {"last_error_at", "sweep_completed_at"}
                    },
                }
        for (name, operation), error in _errors(instance).items():
            details.setdefault(name, {}).update(state="stalled")
            details[name].setdefault("errors", {})[operation] = error
        if not details:
            return ()
        stalled = any(detail["state"] == "stalled" for detail in details.values())
        lagging = any(detail["state"] == "lagging" for detail in details.values())
        return (
            ConsumerHealth(
                kind=self.name,
                consumer_id="consumer:next",
                state="stalled" if stalled else "lagging" if lagging else "running",
                detail=details,
                repair=(
                    ConsumerRepair(
                        operation="hand_edit",
                        required_change="resolve_the_worker_error_then_rebuild_the_next_state",
                        arguments={},
                    )
                    if stalled
                    else None
                ),
            ),
        )


NEXT_QUEUE = NextConsumers()
