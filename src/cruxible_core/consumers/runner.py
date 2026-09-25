"""The daemon loop that runs every consumer kind over every managed instance.

Each tick matches every active kind on every governed instance, then hands the
due work to that kind's bounded pool. Matching runs on the loop thread and never
acts. A work key is in flight at most once at a time, so one slow unit never
delays matching or another key's work, and each kind has its own pool, so one
kind's backlog never starves another's.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import structlog

from cruxible_core.consumers.protocol import ConsumerHealth, ConsumerKind, ConsumerWork
from cruxible_core.server.registry import GOVERNED_DAEMON_BACKEND, get_registry

_log = structlog.get_logger(__name__)


def consumer_kinds() -> tuple[ConsumerKind, ...]:
    from cruxible_core.consumers.evidence import EVIDENCE_AVAILABILITY
    from cruxible_core.consumers.lines import LINE_ARMS

    return (LINE_ARMS, EVIDENCE_AVAILABILITY)


def consumer_health(instance: Any, *, now: datetime) -> tuple[ConsumerHealth, ...]:
    """Every consumer of every kind on the instance, as it stands at `now`."""

    return tuple(
        health
        for kind in consumer_kinds()
        if kind.active(instance)
        for health in kind.health(instance, now=now)
    )


def consumer_statuses(manager: Any) -> tuple[Any, ...]:
    """Every consumer on every instance the daemon already has open, for server status.

    Only open instances are read, so status never opens one; the runner holds
    every governed instance open, so on a running daemon that is all of them.
    """

    from cruxible_client import contracts

    now = datetime.now(UTC)
    statuses: list[contracts.ConsumerStatusV1] = []
    for instance_id, instance in manager.open_instances():
        for kind in consumer_kinds():
            if not kind.active(instance):
                if kind.effect_class == "findings":
                    statuses.append(
                        contracts.ConsumerStatusV1(
                            instance_id=instance_id,
                            kind=kind.name,
                            consumer_id=f"consumer:{kind.name}",
                            state="disabled",
                        )
                    )
                continue
            try:
                healths = kind.health(instance, now=now)
            except Exception:
                _log.exception("consumer_health_unavailable", instance_id=instance_id)
                continue
            statuses.extend(
                contracts.ConsumerStatusV1(
                    instance_id=instance_id,
                    kind=health.kind,
                    consumer_id=health.consumer_id,
                    state=health.state,
                    detail=health.detail,
                )
                for health in healths
            )
    return tuple(statuses)


class ConsumerRunner:
    def __init__(self, manager: Any, *, kinds: Sequence[ConsumerKind] | None = None):
        self.manager = manager
        self.kinds = tuple(consumer_kinds() if kinds is None else kinds)
        self.daemon_id = uuid4().hex
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._executors: dict[str, ThreadPoolExecutor] = {}
        self._in_flight: set[tuple[str, str, str]] = set()
        self._in_flight_lock = threading.Lock()

    def start(self) -> None:
        if self.thread is None or not self.thread.is_alive():
            self.daemon_id = uuid4().hex
            self.stop_event.clear()
            self._executors = {
                kind.name: ThreadPoolExecutor(
                    max_workers=kind.workers, thread_name_prefix=f"cruxible-consumer-{kind.name}"
                )
                for kind in self.kinds
            }
            self.thread = threading.Thread(
                target=self._run, name="cruxible-consumer-runner", daemon=True
            )
            self.thread.start()

    def close(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()
        for executor in self._executors.values():
            # Work already started finishes; nothing new starts once the runner stops.
            executor.shutdown(wait=True, cancel_futures=True)
        self._executors = {}

    @property
    def running(self) -> bool:
        """Whether this daemon's consumer loop is running now."""

        return self.thread is not None and self.thread.is_alive()

    def match_once(self, instance_id: str, instance: Any, *, now: datetime) -> None:
        """Match every active kind on one instance and schedule its due work."""

        for kind in self.kinds:
            if not kind.active(instance):
                continue
            try:
                kind.match(instance, now=now, daemon_id=self.daemon_id)
                for work in kind.due(instance, now=now):
                    self._schedule(instance_id, kind, work)
            except Exception:
                _log.exception("consumer_match_incomplete", instance_id=instance_id, kind=kind.name)

    def _schedule(self, instance_id: str, kind: ConsumerKind, work: ConsumerWork) -> None:
        key = (instance_id, kind.name, work.key)
        with self._in_flight_lock:
            executor = self._executors.get(kind.name)
            if key in self._in_flight or executor is None:
                return
            self._in_flight.add(key)
        try:
            executor.submit(self._run_work, key, instance_id, kind, work)
        except RuntimeError:
            # The pool is shutting down; the work stays due for the next start.
            with self._in_flight_lock:
                self._in_flight.discard(key)

    def _run_work(
        self, key: tuple[str, str, str], instance_id: str, kind: ConsumerKind, work: ConsumerWork
    ) -> None:
        try:
            kind.run(self.manager, instance_id, work, now=datetime.now(UTC))
        except Exception:
            # The work stays due; the next tick schedules it again.
            _log.exception("consumer_work_incomplete", instance_id=instance_id, kind=kind.name)
        finally:
            with self._in_flight_lock:
                self._in_flight.discard(key)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                records = get_registry().list_instances()
                for record in records:
                    if (
                        record.backend != GOVERNED_DAEMON_BACKEND
                        or not (Path(record.location) / "instance.json").is_file()
                    ):
                        continue
                    try:
                        instance = self.manager.get(record.instance_id)
                        self.match_once(record.instance_id, instance, now=datetime.now(UTC))
                    except Exception:
                        _log.exception("consumer_runner_incomplete", instance_id=record.instance_id)
            except Exception:
                _log.exception("consumer_runner_registry_unavailable")
            self.stop_event.wait(1)
