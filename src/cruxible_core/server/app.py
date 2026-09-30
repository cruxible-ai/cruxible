"""FastAPI application and entry point for the Cruxible server."""

from __future__ import annotations

import faulthandler
import os
import signal
import socket
import sqlite3
import stat
import sys
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import IO, Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from cruxible_client.contracts.authoring.models import (
    AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST,
)
from cruxible_client.contracts.errors import (
    ClaimAttestationRequestInvalid,
    PlaybillSinceRequestInvalid,
)
from cruxible_client.contracts.temporal import ISO_8601_FORMAT_HINT
from cruxible_core import __version__
from cruxible_core.errors import ConfigError, CoreError
from cruxible_core.ledger.checkpoints import QUIET_CHECKPOINT_SECONDS
from cruxible_core.runtime.execution_policy import discover_isolated_executors
from cruxible_core.runtime.permissions import init_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.auth import token_auth_middleware
from cruxible_core.server.bootstrap_secret import prepare_bootstrap_secret
from cruxible_core.server.config import (
    auth_off_startup_notice,
    get_server_fatal_log_path,
    get_server_state_root,
    is_server_auth_enabled,
    validate_server_startup_settings,
    volatile_state_path_warnings,
)
from cruxible_core.server.credentials import get_runtime_credential_store
from cruxible_core.server.errors import (
    STANDARD_ERROR_RESPONSES,
    ErrorResponse,
    error_to_response,
)
from cruxible_core.server.registry import get_registry
from cruxible_core.server.request_logging import configure_request_logging
from cruxible_core.server.restart import PROCESS_BOOT_ID
from cruxible_core.server.routes.hosted_instances import router as hosted_instances_router
from cruxible_core.server.routes.instances import router as instances_router
from cruxible_core.server.routes.playbill import router as playbill_router
from cruxible_core.server.routes.runtime_credentials import (
    router as runtime_credentials_router,
)
from cruxible_core.server.state_lock import StateRootLock

_log = structlog.get_logger("cruxible.server.app")

# Generic, schema-free client message for any database error. The real sqlite
# detail (e.g. "UNIQUE constraint failed: internal_table.internal_column") names live
# tables and columns and must never reach the client; it is logged server-side
# instead. See wi-daemon-network-security-hardening (#5).
_DB_CONSTRAINT_MESSAGE = "database constraint violation"
_DB_ERROR_MESSAGE = "database error"

# Pydantic tags every datetime/date failure with a type starting "datetime" or
# "date" (datetime_parsing, datetime_type, date_from_datetime_parsing, ...).
# Its own message names WHAT went wrong ("invalid character in year") but never
# what a good value looks like, so callers resubmit the same malformed shape.
_TEMPORAL_ERROR_TYPE_PREFIXES = ("datetime", "date")


def _format_request_validation_error(error: Mapping[str, Any]) -> str:
    """Render one pydantic request-validation error, self-correcting when temporal."""
    location = ".".join(str(part) for part in (error.get("loc") or ()))
    message = str(error.get("msg", "invalid"))
    error_type = str(error.get("type", ""))
    if error_type.startswith(_TEMPORAL_ERROR_TYPE_PREFIXES):
        message = f"{message} ({ISO_8601_FORMAT_HINT})"
    return f"{location}: {message}"


def create_app() -> FastAPI:
    """Create and configure the Cruxible server app."""
    get_registry()
    # Fails CLOSED and BEFORE any route exists: a distribution that advertises
    # an isolated executor this build cannot load leaves the shared hosted
    # profile refusing every Provider run for a reason no caller could see, so
    # the daemon refuses to start instead and names the entry point.
    registrations = discover_isolated_executors()
    if registrations:
        _log.info(
            "isolated_executors_registered",
            backend_ids=[item.backend_id for item in registrations],
        )
    manager = get_playbill_manager()
    try:
        manager.recover_provider_runtime()
    except Exception as exc:
        # Last-resort isolation: Provider fence recovery must never prevent the
        # non-Provider daemon surfaces from starting.
        manager.cached_provider_runtime_operator().mark_unavailable(
            "provider_runtime_recovery_failed",
            f"Provider runtime startup recovery failed: {exc}",
            retryable=True,
        )
    # Proposal terminals prepared before a crash are resolved after the Provider
    # fences, so a recovered run reads as one complete attempt: the receipt of
    # the proposal it produced, then its finalization. Recovery logs and
    # continues per instance; it never keeps the daemon from starting.
    manager.recover_proposal_egress()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        manager.consumer_runner.start()
        try:
            yield
        finally:
            manager.consumer_runner.close()
            manager.flush_replay_checkpoints()

    app = FastAPI(title="cruxible", responses=STANDARD_ERROR_RESPONSES, lifespan=lifespan)
    app.middleware("http")(token_auth_middleware)

    @app.exception_handler(CoreError)
    async def core_error_handler(request: Request, exc: CoreError) -> JSONResponse:
        request.state.error_type = exc.__class__.__name__
        status_code, body = error_to_response(exc)
        return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        if request.url.path.endswith("/playbill/since"):
            typed = PlaybillSinceRequestInvalid.from_validation_errors(exc.errors())
            request.state.error_type = typed.__class__.__name__
            status_code, body = error_to_response(typed)
            content = body.model_dump(mode="json")
            content["errors"] = [_format_request_validation_error(err) for err in exc.errors()]
            return JSONResponse(status_code=status_code, content=content)
        if request.url.path.endswith("/playbill/claim-attestations"):
            attestation_error = ClaimAttestationRequestInvalid.from_validation_errors(exc.errors())
            request.state.error_type = attestation_error.__class__.__name__
            status_code, body = error_to_response(attestation_error)
            content = body.model_dump(mode="json")
            content["errors"] = [_format_request_validation_error(err) for err in exc.errors()]
            return JSONResponse(status_code=status_code, content=content)
        errors = [_format_request_validation_error(err) for err in exc.errors()]
        body = ErrorResponse(
            error_type="RequestValidationError",
            message="Request validation failed",
            errors=errors,
        )
        return JSONResponse(status_code=422, content=body.model_dump(mode="json"))

    # Deliberately no blanket pydantic ValidationError handler: it cannot tell a
    # caller's malformed request from a frozen model failing deep inside a
    # service, so it would turn internal invariant breaches into 400s and put
    # internal model names on the wire. Request-shaped refusals are raised as
    # typed CoreErrors where the request is understood; anything else is a real
    # server fault and takes the generic 500 below.
    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity_error_handler(
        request: Request, exc: sqlite3.IntegrityError
    ) -> JSONResponse:
        # An unhandled sqlite IntegrityError (UNIQUE/FOREIGN KEY/CHECK/NOT NULL)
        # otherwise surfaces through the catch-all handler below, echoing the raw
        # message (e.g. "UNIQUE constraint failed: <table.col>") and leaking the
        # internal schema. Return a generic 409 and log the real detail only on
        # the server. See wi-daemon-network-security-hardening (#5).
        request.state.error_type = exc.__class__.__name__
        _log.warning(
            "database_integrity_error",
            route=request.url.path,
            method=request.method,
            detail=str(exc),
        )
        body = ErrorResponse(
            error_type="DatabaseIntegrityError",
            message=_DB_CONSTRAINT_MESSAGE,
        )
        return JSONResponse(status_code=409, content=body.model_dump(mode="json"))

    @app.exception_handler(sqlite3.DatabaseError)
    async def database_error_handler(request: Request, exc: sqlite3.DatabaseError) -> JSONResponse:
        # Any other low-level sqlite error (OperationalError, etc.) may also carry
        # SQL fragments / schema names. Keep the client message generic and log
        # the detail server-side.
        request.state.error_type = exc.__class__.__name__
        _log.error(
            "database_error",
            route=request.url.path,
            method=request.method,
            detail=str(exc),
        )
        body = ErrorResponse(
            error_type="DatabaseError",
            message=_DB_ERROR_MESSAGE,
        )
        return JSONResponse(status_code=500, content=body.model_dump(mode="json"))

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        # Genuine fallthrough only: every Cruxible domain error subclasses
        # CoreError (dedicated handler above, preserves its intended message),
        # RequestValidationError and the sqlite3 error families also have their
        # own handlers. So `exc` here is an UNEXPECTED exception whose raw
        # str(exc) may embed sqlite SQL, file paths, or other internal detail
        # (e.g. a RuntimeError/ValueError wrapping sqlite text that escaped the
        # sqlite3.* handlers). Returning a generic body keeps that detail off the
        # wire; the real exception is logged server-side for diagnosis. See
        # wi-daemon-network-security-hardening (#5).
        request.state.error_type = exc.__class__.__name__
        _log.error(
            "unhandled_server_error",
            route=request.url.path,
            method=request.method,
            error_type=exc.__class__.__name__,
            detail=str(exc),
            exc_info=exc,
        )
        body = ErrorResponse(
            error_type="InternalServerError",
            message="internal server error",
        )
        return JSONResponse(status_code=500, content=body.model_dump(mode="json"))

    @app.get("/health")
    async def health() -> dict[str, str]:
        # Liveness only. /health is reachable without a credential, so it must
        # not disclose the daemon's capability ceiling: that tells an
        # unauthenticated prober exactly how much authority this daemon can
        # ever grant. Authorized callers read the tier from the denial context
        # of a refused operation instead.
        return {"status": "ok"}

    @app.get("/version")
    async def version() -> dict[str, str]:
        return {
            "version": __version__,
            "sdk_contract_snapshot_digest": AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST,
            "boot_id": PROCESS_BOOT_ID,
        }

    app.include_router(instances_router)
    app.include_router(hosted_instances_router)
    app.include_router(runtime_credentials_router)
    app.include_router(playbill_router)
    return app


def run_server(
    *,
    host: str | None = None,
    port: int | None = None,
    state_root: str | None = None,
    socket_path: str | None = None,
    capability_ceiling: str | None = None,
    auth: bool = False,
    bootstrap_secret_file: str | None = None,
) -> None:
    """Launch the Cruxible daemon over UDS or host/port transport.

    This is the single daemon-launch path, invoked by ``cruxible server start``.
    Explicit arguments override the corresponding environment variables
    (``CRUXIBLE_HOST`` / ``CRUXIBLE_PORT`` / ``CRUXIBLE_STATE_ROOT`` /
    ``CRUXIBLE_SERVER_SOCKET`` / ``CRUXIBLE_MODE``); when an argument is ``None``
    the env default is used. Overrides are applied to ``os.environ`` before any
    config is resolved so the registry, credential store, permission ceiling,
    and startup validation all observe the same effective settings, and so an
    in-place re-exec (``cruxible server restart``) reproduces them via
    ``sys.argv``.

    ``auth=True`` is the explicit local opt-in (``server start --auth``); it sets
    ``CRUXIBLE_SERVER_AUTH=true``, which stays the env form of the same switch.
    A Unix-socket daemon defaults to auth off; a TCP daemon refuses without it.
    """
    if auth:
        os.environ["CRUXIBLE_SERVER_AUTH"] = "true"
    if host is not None:
        os.environ["CRUXIBLE_HOST"] = host
    if port is not None:
        os.environ["CRUXIBLE_PORT"] = str(port)
    if state_root is not None:
        os.environ["CRUXIBLE_STATE_ROOT"] = state_root
    if socket_path is not None:
        os.environ["CRUXIBLE_SERVER_SOCKET"] = socket_path
    if capability_ceiling is not None:
        os.environ["CRUXIBLE_MODE"] = capability_ceiling

    # Take exclusive ownership of the state root BEFORE any store is opened, so
    # a second daemon over the same root refuses without touching its SQLite
    # files or its ledger. Two daemons over one root can both answer a probe and
    # both write the accepted tree.
    resolved_socket = os.environ.get("CRUXIBLE_SERVER_SOCKET")
    transport = (
        f"unix socket {resolved_socket}"
        if resolved_socket
        else f"{os.environ.get('CRUXIBLE_HOST', '127.0.0.1')}:"
        f"{os.environ.get('CRUXIBLE_PORT', '8100')}"
    )
    with StateRootLock(get_server_state_root(), transport=transport, boot_id=PROCESS_BOOT_ID):
        _serve(
            resolved_socket,
            bootstrap_secret_file=(
                None if bootstrap_secret_file is None else Path(bootstrap_secret_file)
            ),
        )


#: The fatal-fault log handle, held for the life of the process. faulthandler
#: writes through the file DESCRIPTOR it was handed; a handle that went out of
#: scope would be closed and the fault trace would land nowhere.
_fatal_fault_log: IO[bytes] | None = None


def enable_fatal_fault_handler(path: Path | None = None) -> Path | None:
    """Point the fatal-fault handler at the daemon's own log directory.

    A daemon death is never silent. A fatal fault -- a segfault, an abort, a bus
    error, a stack overflow -- otherwise takes the process out between two
    access-log lines, leaving the last request logged without a response and
    nothing at all about the exit; that is exactly how a large compile looked
    when it killed a daemon hosting two instances.

    faulthandler's default target is stderr, and stderr is NOT where this
    daemon's log is: `configure_request_logging` binds structlog to a rotating
    file under `<state-root>/daemon/logs/`, so under a terminal multiplexer a
    fault trace written to stderr lands in a scrollback nobody keeps and the
    death still reads as silent in the log an operator actually follows. The
    trace goes to `fatal.log` beside the request log instead.

    Returns the path the handler writes to, or None when no file could be
    opened -- an unwritable log directory, or a build where enabling the
    handler is refused. Losing the trace is not worth refusing to serve, so
    both degrade to a debug line. A SIGKILL from the kernel's OOM killer still
    cannot be observed from inside the process; the MemoryError path in
    authoring preflight is what covers the allocation failure this build sees.
    """

    global _fatal_fault_log
    resolved = get_server_fatal_log_path() if path is None else path
    handle: IO[bytes] | None = None
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        # Append, unbuffered: the writer is a signal handler that may be the
        # last thing this process does, so nothing may be left in a buffer.
        handle = resolved.open("ab", buffering=0)
        faulthandler.enable(file=handle)
    except (ValueError, OSError):
        if handle is not None:
            handle.close()
        _log.debug("faulthandler_unavailable", path=str(resolved))
        return None
    _fatal_fault_log = handle
    return resolved


_SOCKET_LOCATION_REPAIR = (
    "pass a --socket path inside a directory only you own and can access (mode 0700), "
    "under ancestors other users cannot write"
)


def _refuse_socket_location(detail: str) -> ConfigError:
    return ConfigError(
        f"Unsafe daemon socket location: {detail}. Repair: {_SOCKET_LOCATION_REPAIR}."
    )


def _check_socket_ancestor(path: Path, status: os.stat_result) -> None:
    """An ancestor no other user can use to replace the directory below it.

    Only root or this user may own it, and group/other write is allowed only on
    a sticky root-owned directory such as ``/tmp``, where nobody but the entry's
    owner (or root) may rename or remove an entry.
    """
    uid = os.getuid()
    if status.st_uid not in (0, uid):
        raise _refuse_socket_location(
            f"{path} is owned by uid {status.st_uid}, who could replace the directory below it"
        )
    writable = status.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    sticky_root = bool(status.st_mode & stat.S_ISVTX) and status.st_uid == 0
    if writable and not sticky_root:
        raise _refuse_socket_location(
            f"{path} is writable by group or others (mode {stat.S_IMODE(status.st_mode):04o}), "
            "so the directory below it could be replaced"
        )


def _check_socket_parent(path: Path, status: os.stat_result) -> None:
    """The socket's own directory: a real directory this user owns, owner-only."""
    if not stat.S_ISDIR(status.st_mode):
        raise _refuse_socket_location(f"{path} is not a directory (a symlink is refused)")
    if status.st_uid != os.getuid():
        raise _refuse_socket_location(f"{path} is owned by uid {status.st_uid}, not by you")
    if stat.S_IMODE(status.st_mode) & 0o077:
        raise _refuse_socket_location(
            f"{path} is not owner-only (mode {stat.S_IMODE(status.st_mode):04o}); "
            f"run `chmod 700 {path}`"
        )


_MAX_SOCKET_PATH_SYMLINKS = 40


def _inspect_socket_path_entry(path: Path) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError as exc:
        raise _refuse_socket_location(f"could not inspect {path}: {exc}") from exc


def _check_socket_ancestors(directory: Path) -> None:
    """Apply the ancestor rule to every directory the path actually traverses.

    The path is walked one component at a time and each symlink is resolved
    here, so every directory a lookup passes through -- including the ones a
    symlink target leads through on the way to the next symlink -- is checked.
    A directory another user could write would let them replace whatever the
    path resolves through it. The socket's own directory is checked separately.
    """
    current = Path(os.sep)
    _check_socket_ancestor(current, _inspect_socket_path_entry(current))
    remaining = list(directory.parts[1:])
    followed = 0
    while remaining:
        part = remaining.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent
            continue
        entry = current / part
        status = _inspect_socket_path_entry(entry)
        if stat.S_ISLNK(status.st_mode):
            # A link's owner can repoint it at any time, whatever its directory
            # allows (a sticky /tmp keeps others from removing the link, not its
            # owner from replacing it).
            if status.st_uid not in (0, os.getuid()):
                raise _refuse_socket_location(
                    f"{entry} is a symlink owned by uid {status.st_uid}, who could repoint it"
                )
            followed += 1
            if followed > _MAX_SOCKET_PATH_SYMLINKS:
                raise _refuse_socket_location(f"{directory} passes through too many symlinks")
            target = Path(os.readlink(entry))
            if target.is_absolute():
                current = Path(os.sep)
                remaining = list(target.parts[1:]) + remaining
            else:
                remaining = list(target.parts) + remaining
            continue
        if not stat.S_ISDIR(status.st_mode):
            raise _refuse_socket_location(f"{entry} is not a directory")
        if remaining:
            _check_socket_ancestor(entry, status)
        current = entry


def _socket_location(path: str | os.PathLike[str]) -> Path:
    """The path exactly as a client will look it up, never lexically normalized.

    A relative path is joined to the working directory without normalizing it.
    ``..`` is refused outright: collapsing it lexically, before the symlinks
    ahead of it are resolved, names a different directory than the one a
    lookup actually walks.
    """
    raw = os.fspath(path)
    located = Path(raw if os.path.isabs(raw) else os.path.join(os.getcwd(), raw))
    if ".." in located.parts:
        raise _refuse_socket_location(
            f"{raw} contains a `..` component; name the socket path without `..`"
        )
    return located


def prepare_socket_directory(directory: Path) -> None:
    """Make sure no other user can create or replace the daemon socket.

    A missing directory is created 0700. The socket's own directory must be a
    real directory this user owns with no group or other access, and no
    ancestor may let another user replace it: anyone who can swap the socket
    receives every bearer token clients send.
    """
    directory = _socket_location(directory)
    if not os.path.lexists(directory):
        directory.mkdir(mode=0o700, parents=True)
    try:
        status = os.lstat(directory)
    except OSError as exc:
        raise _refuse_socket_location(f"could not inspect {directory}: {exc}") from exc
    _check_socket_parent(directory, status)
    _check_socket_ancestors(directory)


def _bind_socket_path(sock: socket.socket, path: str) -> None:
    sock.bind(path)


def bind_private_unix_socket(socket_file: Path) -> socket.socket:
    """Bind the daemon socket owner-only (0600) inside the directory it validated.

    uvicorn's own bind makes the socket 0666, so the daemon binds it and hands
    uvicorn the descriptor. The validated directory is held open; after the
    bind, the path must still name that same directory and the bound entry must
    be the one inside it, or the socket is unlinked and startup is refused.
    """
    socket_file = _socket_location(socket_file)
    directory, name = socket_file.parent, socket_file.name
    prepare_socket_directory(directory)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        dir_fd = os.open(directory, flags)
    except OSError as exc:
        raise _refuse_socket_location(f"could not open {directory}: {exc}") from exc
    try:
        pinned = os.fstat(dir_fd)
        _check_socket_parent(directory, pinned)
        try:
            os.unlink(name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous_umask = os.umask(0o177)
        try:
            _bind_socket_path(sock, str(socket_file))
        except OSError:
            sock.close()
            raise
        finally:
            os.umask(previous_umask)
        try:
            _verify_bound_socket(socket_file, name=name, dir_fd=dir_fd, pinned=pinned)
        except ConfigError:
            sock.close()
            raise
        # The entry was just verified to be our socket, not a link.
        os.chmod(name, 0o600, dir_fd=dir_fd)
        sock.set_inheritable(True)
        return sock
    finally:
        os.close(dir_fd)


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _verify_bound_socket(
    socket_file: Path, *, name: str, dir_fd: int, pinned: os.stat_result
) -> None:
    """Refuse, and remove what was bound, unless the bind landed in the pinned directory."""
    try:
        current = os.stat(socket_file.parent)
        inside = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        at_path = os.lstat(socket_file)
        intact = (
            _same_file(current, pinned)
            and _same_file(inside, at_path)
            and stat.S_ISSOCK(at_path.st_mode)
            and at_path.st_uid == os.getuid()
        )
    except OSError:
        intact = False
    if intact:
        return
    try:
        stray = os.lstat(socket_file)
        if stat.S_ISSOCK(stray.st_mode) and stray.st_uid == os.getuid():
            os.unlink(socket_file)
    except OSError:
        pass
    raise _refuse_socket_location(
        f"the socket directory {socket_file.parent} changed while the socket was being bound"
    )


@contextmanager
def _sigterm_unwinds() -> Iterator[None]:
    """Let a SIGTERM stop unwind this process instead of killing it outright.

    uvicorn shuts down gracefully on SIGTERM and then re-raises the signal under
    the handler it found. Under the default handler that re-raise ends the
    process on the spot, so no ``finally`` below ever runs and the socket file
    `server stop` promised to release is left behind. A handler that raises
    SystemExit(0) turns the re-raise into an ordinary unwind.
    """

    def _exit(_signum: int, _frame: object) -> None:
        raise SystemExit(0)

    try:
        previous = signal.signal(signal.SIGTERM, _exit)
    except ValueError:  # not the main thread: uvicorn installs no handlers either
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _serve(resolved_socket: str | None, *, bootstrap_secret_file: Path | None = None) -> None:
    """Start uvicorn under an already-held state-root lock."""
    enable_fatal_fault_handler()
    if resolved_socket:
        # Refuse an unsafe socket directory before any store is opened.
        prepare_socket_directory(Path(resolved_socket).parent)
    # Resolve and freeze the process ceiling before registry/config access or
    # uvicorn startup. Unknown names and attempts to reinitialize this process
    # at a different tier therefore fail closed before the daemon serves.
    init_permissions()

    # Under the lock, so a daemon refused by it never replaces the running
    # daemon's secret file. Only the path is printed, never the secret.
    prepare_bootstrap_secret(get_server_state_root(), extra_file=bootstrap_secret_file)
    credential_store = get_runtime_credential_store()
    registry = get_registry()
    runtime_credentials_available = credential_store.has_active_credentials()
    auth_required = credential_store.is_auth_required()
    validate_server_startup_settings(
        runtime_credentials_available=runtime_credentials_available,
        auth_required=auth_required,
    )
    if is_server_auth_enabled():
        credential_store.mark_auth_required("server_startup_auth_enabled")
    else:
        # Only a Unix-socket daemon gets here without auth (validation above
        # refuses TCP), so this one line is the whole local trust model.
        print(auth_off_startup_notice(), file=sys.stderr)
    for warning in volatile_state_path_warnings(
        instance_locations=[
            (record.instance_id, record.location) for record in registry.list_instances()
        ],
    ):
        print(f"Warning: {warning}", file=sys.stderr)

    import uvicorn

    configure_request_logging()
    get_playbill_manager().quiet_checkpoint_seconds = QUIET_CHECKPOINT_SECONDS
    app = create_app()

    if resolved_socket:
        socket_file = Path(resolved_socket)
        sock = bind_private_unix_socket(socket_file)
        try:
            with _sigterm_unwinds():
                uvicorn.run(app, fd=sock.fileno())
        finally:
            sock.close()
            socket_file.unlink(missing_ok=True)
        return

    resolved_host = os.environ.get("CRUXIBLE_HOST", "127.0.0.1")
    resolved_port = int(os.environ.get("CRUXIBLE_PORT", "8100"))
    uvicorn.run(app, host=resolved_host, port=resolved_port)
