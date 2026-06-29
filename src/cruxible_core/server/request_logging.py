"""Structured runtime request logging for the HTTP server."""

from __future__ import annotations

import io
import sys
import threading
from pathlib import Path
from typing import Any, TextIO, cast

import structlog
from fastapi import Request

from cruxible_core.server.config import get_server_log_path

_log = structlog.get_logger("cruxible.server.requests")
_DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
_DEFAULT_LOG_BACKUP_COUNT = 5
_request_log_failure_warned = False


class _RotatingFileLogSink(io.TextIOBase):
    """Append-only text sink with small built-in log rotation."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int = _DEFAULT_LOG_MAX_BYTES,
        backup_count: int = _DEFAULT_LOG_BACKUP_COUNT,
    ) -> None:
        super().__init__()
        self._path = Path(path)
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._lock = threading.Lock()
        self._file: io.TextIOWrapper | None = None

    @property
    def path(self) -> Path:
        return self._path

    def writable(self) -> bool:
        return True

    def write(self, message: str) -> int:
        if self.closed:
            raise ValueError("I/O operation on closed request log sink")
        with self._lock:
            try:
                self._rotate_before_write(message)
                return self._open().write(message)
            except Exception:
                self._close_file()
                raise

    def flush(self) -> None:
        if self.closed:
            return
        with self._lock:
            try:
                if self._file is not None:
                    self._file.flush()
            except Exception:
                self._close_file()
                raise

    def close(self) -> None:
        with self._lock:
            self._close_file()
        super().close()

    def _open(self) -> io.TextIOWrapper:
        if self._file is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self._path.open("a", encoding="utf-8", buffering=1)
        return self._file

    def _close_file(self) -> None:
        if self._file is None:
            return
        try:
            self._file.close()
        finally:
            self._file = None

    def _rotate_before_write(self, message: str) -> None:
        if self._max_bytes <= 0:
            return
        current_size = self._current_size()
        incoming_size = len(message.encode("utf-8"))
        if current_size > 0 and current_size + incoming_size > self._max_bytes:
            self._rotate()

    def _current_size(self) -> int:
        if self._file is not None:
            self._file.flush()
        try:
            return self._path.stat().st_size
        except FileNotFoundError:
            return 0

    def _rotate(self) -> None:
        self._close_file()
        if not self._path.exists():
            return
        if self._backup_count <= 0:
            self._path.unlink()
            return
        oldest = self._backup_path(self._backup_count)
        oldest.unlink(missing_ok=True)
        for index in range(self._backup_count - 1, 0, -1):
            source = self._backup_path(index)
            if source.exists():
                source.replace(self._backup_path(index + 1))
        self._path.replace(self._backup_path(1))

    def _backup_path(self, index: int) -> Path:
        return self._path.with_name(f"{self._path.name}.{index}")


def configure_request_logging(
    log_path: str | Path | None = None,
    *,
    max_bytes: int = _DEFAULT_LOG_MAX_BYTES,
    backup_count: int = _DEFAULT_LOG_BACKUP_COUNT,
) -> Path:
    """Configure production server request logs as durable JSON lines."""
    resolved_log_path = (
        Path(log_path).expanduser().resolve() if log_path is not None else get_server_log_path()
    )
    sink = _RotatingFileLogSink(
        resolved_log_path,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=cast(TextIO, sink)),
        # Not cached: a cache-bound logger survives later structlog.configure
        # calls, which permanently detaches request logs from any
        # reconfiguration (observed as test-order-dependent log capture).
        cache_logger_on_first_use=False,
    )
    return resolved_log_path


def log_runtime_request(
    request: Request,
    *,
    status: int,
    auth_context: Any,
    operation_id: str | None = None,
    error_type: str | None = None,
) -> None:
    """Emit one safe structured log event for a runtime HTTP request."""
    fields: dict[str, Any] = {
        "method": request.method,
        "route": _request_route(request),
        "status": status,
        "principal_id": _context_field(auth_context, "principal_id", "anonymous"),
        "principal_label": _context_field(auth_context, "principal_label", "anonymous"),
        "credential_type": _context_field(auth_context, "credential_type", "anonymous"),
        "role": _context_field(auth_context, "role", None),
        "instance_scope": _context_field(auth_context, "instance_scope", None),
        "instance_id": _request_instance_id(request),
    }
    resolved_operation_id = operation_id or getattr(request.state, "operation_id", None)
    if resolved_operation_id is not None:
        fields["operation_id"] = str(resolved_operation_id)
    if error_type is not None:
        fields["error_type"] = error_type
    _emit("runtime_request", fields)


def _emit(event: str, fields: dict[str, Any]) -> None:
    """Write one log event, swallowing any sink failure.

    Request logging must never take down request handling. Catch broadly so a
    dead log sink degrades to a dropped log line rather than a failed request.
    """
    try:
        _log.info(event, **fields)
    except Exception as exc:
        _warn_request_log_failure_once(exc)


def _warn_request_log_failure_once(exc: Exception) -> None:
    """Emit one best-effort warning when request logs can no longer be written."""
    global _request_log_failure_warned
    if _request_log_failure_warned:
        return
    _request_log_failure_warned = True
    try:
        print(
            "Warning: Cruxible request log sink failed; runtime request logs "
            f"may be dropped ({exc.__class__.__name__}: {exc})",
            file=sys.stderr,
        )
    except Exception:
        pass


def _context_field(auth_context: Any, field: str, default: str | None) -> str | None:
    if auth_context is None:
        return default
    value = getattr(auth_context, field, default)
    if value is None:
        return None
    return str(value)


def _request_route(request: Request) -> str:
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    if isinstance(route_path, str) and route_path:
        return route_path
    return request.url.path


def _request_instance_id(request: Request) -> str | None:
    value = request.path_params.get("instance_id")
    if value is None:
        return None
    return str(value)
