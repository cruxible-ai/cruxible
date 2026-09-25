"""Daemon lifecycle: match armed Lines, then hand their due work to bounded workers.

Matching runs on the listener thread and never executes a Procedure. Each
armed Line's due work is drained by at most one worker at a time, on a small
pool, so one slow Procedure never delays matching or another Line's runs.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import structlog

from cruxible_core.exhaust.line_dispatch import dispatch_root
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.server.registry import GOVERNED_DAEMON_BACKEND, get_registry

_log = structlog.get_logger(__name__)

#: Concurrent automatic drains across all armed Lines.
ARMED_DISPATCH_WORKERS = 2


class LineListener:
    def __init__(self, manager: Any, *, workers: int = ARMED_DISPATCH_WORKERS):
        self.manager = manager
        self.daemon_id = uuid4().hex
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._workers = workers
        self._executor: ThreadPoolExecutor | None = None
        self._in_flight: set[tuple[str, str]] = set()
        self._in_flight_lock = threading.Lock()

    def start(self) -> None:
        if self.thread is None or not self.thread.is_alive():
            self.daemon_id = uuid4().hex
            self.stop_event.clear()
            self._executor = ThreadPoolExecutor(
                max_workers=self._workers, thread_name_prefix="cruxible-line-arm"
            )
            self.thread = threading.Thread(
                target=self._run, name="cruxible-line-listener", daemon=True
            )
            self.thread.start()

    def close(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()
        if self._executor is not None:
            # Admitted runs finish; no new drain starts once the listener stops.
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._executor = None

    def match_once(self, instance_id: str, instance: Any, *, now: datetime) -> None:
        """Match one instance's armed Lines and schedule their due work."""

        from cruxible_core.service.procedures.line_dispatch import (
            armed_work,
            service_match_listening_lines,
        )

        actor = GovernedActorContext(
            actor_type="system",
            actor_id="line-listener",
            org_id=instance.descriptor.instance_id,
            operation_id=self.daemon_id,
            timestamp=now,
        )
        service_match_listening_lines(instance, actor=actor, now=now, daemon_id=self.daemon_id)
        for arm in armed_work(instance, now=now):
            self._schedule(instance_id, arm)

    def _schedule(self, instance_id: str, arm: dict[str, Any]) -> None:
        key = (instance_id, arm["line_id"])
        with self._in_flight_lock:
            if key in self._in_flight or self._executor is None:
                return
            self._in_flight.add(key)
        try:
            self._executor.submit(self._drain, key, instance_id, arm)
        except RuntimeError:
            # The pool is shutting down; the work stays pending for the next start.
            with self._in_flight_lock:
                self._in_flight.discard(key)

    def _drain(self, key: tuple[str, str], instance_id: str, arm: dict[str, Any]) -> None:
        from cruxible_core.runtime.line_arms import dispatch_armed_line

        try:
            dispatch_armed_line(self.manager, instance_id, arm)
        except Exception:
            # The occurrence stays pending; the next tick schedules it again.
            _log.exception("line_arm_dispatch_incomplete", instance_id=instance_id)
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
                        # Opening a managed instance does not arm any Line.
                        instance = self.manager.get(record.instance_id)
                        if not dispatch_root(instance).exists():
                            continue
                        self.match_once(record.instance_id, instance, now=datetime.now(UTC))
                    except Exception:
                        _log.exception("line_listener_incomplete", instance_id=record.instance_id)
            except Exception:
                _log.exception("line_listener_registry_unavailable")
            self.stop_event.wait(1)
