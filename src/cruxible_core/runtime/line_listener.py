"""Daemon lifecycle for matching only; execution always uses an authenticated call."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import structlog

from cruxible_core.exhaust.line_dispatch import dispatch_root
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.server.registry import GOVERNED_DAEMON_BACKEND, get_registry

_log = structlog.get_logger(__name__)


class LineListener:
    def __init__(self, manager: Any):
        self.manager = manager
        self.daemon_id = uuid4().hex
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread is None or not self.thread.is_alive():
            self.daemon_id = uuid4().hex
            self.stop_event.clear()
            self.thread = threading.Thread(
                target=self._run, name="cruxible-line-listener", daemon=True
            )
            self.thread.start()

    def close(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()

    def _run(self) -> None:
        from cruxible_core.service.procedures.line_dispatch import service_match_listening_lines

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
                        # Opening a managed instance does not enable any Line.
                        instance = self.manager.get(record.instance_id)
                        if not dispatch_root(instance).exists():
                            continue
                        now = datetime.now(UTC)
                        actor = GovernedActorContext(
                            actor_type="system",
                            actor_id="line-listener",
                            org_id=instance.descriptor.instance_id,
                            operation_id=self.daemon_id,
                            timestamp=now,
                        )
                        service_match_listening_lines(
                            instance, actor=actor, now=now, daemon_id=self.daemon_id
                        )
                    except Exception:
                        _log.exception("line_listener_incomplete", instance_id=record.instance_id)
            except Exception:
                _log.exception("line_listener_registry_unavailable")
            self.stop_event.wait(1)
