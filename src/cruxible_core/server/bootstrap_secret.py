"""The runtime bootstrap secret: kept in an owner-only file, never printed.

With auth on, the daemon's bootstrap secret is its unscoped operator credential
(host create, status, restart, stop). It is never written to stdout, stderr or
the request log. The daemon writes it owner-only (0600) to
``<state-root>/daemon/bootstrap-secret`` once it holds the state root, and
``server status`` / ``restart`` / ``stop`` use it by default when they talk to
the daemon that owns that state root, so a local restart needs no manually
supplied credential. They never send it: each request carries a MAC keyed by
the secret (``cruxible_client.contracts.operator_mac``) that the daemon
verifies once.
"""

from __future__ import annotations

import hmac
import os
import re
import secrets
import stat
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from cruxible_client.contracts.operator_mac import (
    OPERATOR_MAC_MAX_SKEW_SECONDS,
    operator_request_mac,
)
from cruxible_core.server.config import (
    get_runtime_bootstrap_secret,
    is_server_auth_enabled,
)
from cruxible_core.server.state_lock import read_state_lock, state_lock_holder_is_alive

BOOTSTRAP_SECRET_FILE = "bootstrap-secret"
_BOOTSTRAP_SECRET_ENV = "CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET"


def bootstrap_secret_path(state_root: Path) -> Path:
    """Where the daemon keeps its bootstrap secret for local operator commands."""

    return state_root / "daemon" / BOOTSTRAP_SECRET_FILE


def write_owner_only_secret(path: Path, secret: str) -> Path:
    """Write ``secret`` to ``path`` with mode 0600, never following a symlink."""

    resolved = path.expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(resolved, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(f"{secret}\n")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return resolved


def prepare_bootstrap_secret(state_root: Path, *, extra_file: Path | None = None) -> Path | None:
    """Hold the operator secret for this daemon run; return where it was written.

    Runs under the state-root lock, so a second daemon refused by the lock can
    never overwrite the running daemon's file. With auth on, the secret comes
    from ``CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET`` or is generated into it (an
    in-place restart keeps the environment, so keeps the secret), then written
    owner-only to the state root and to ``extra_file`` when one was asked for.
    Only the paths are printed. With auth off a stale file is removed, since no
    secret is in force.
    """

    path = bootstrap_secret_path(state_root)
    if not is_server_auth_enabled():
        path.unlink(missing_ok=True)
        return None
    secret = get_runtime_bootstrap_secret()
    if secret is None:
        secret = secrets.token_urlsafe(32)
        os.environ[_BOOTSTRAP_SECRET_ENV] = secret
    written = write_owner_only_secret(path, secret)
    if extra_file is not None:
        write_owner_only_secret(extra_file, secret)
    print(
        f"Auth on: bootstrap secret written to {written} (0600, never printed); "
        "`cruxible server status|restart|stop` read it from there. "
        f"Claim an admin token: cruxible credential claim-bootstrap --secret-file {written}",
        file=sys.stderr,
    )
    return written


def _same_transport(recorded: str, *, server_url: str | None, server_socket: str | None) -> bool:
    if server_socket:
        prefix = "unix socket "
        if not recorded.startswith(prefix):
            return False
        recorded_path = Path(recorded.removeprefix(prefix)).expanduser().resolve()
        return recorded_path == Path(server_socket).expanduser().resolve()
    if not server_url or recorded.startswith("unix socket "):
        return False
    host, _, port = recorded.rpartition(":")
    split = urlsplit(server_url)
    if split.hostname is None or split.port is None or str(split.port) != port:
        return False
    # Exactly the endpoint the daemon bound, never an alias: IPv4 and IPv6
    # loopback can host different listeners on one port, and a name such as
    # localhost may resolve to either of them.
    return split.hostname.lower() == host.strip("[]").lower()


def read_local_bootstrap_secret(
    state_root: Path,
    *,
    server_url: str | None,
    server_socket: str | None,
) -> str | None:
    """The bootstrap secret of the live local daemon this client targets, else None.

    Read only to key request MACs: it never goes on the wire. As defense in
    depth it is read only when a live daemon holds the state-root lock right
    now, the lock records exactly the transport the client is about to use, and
    the file is a regular file this user owns with mode 0600. What grants
    authority is the daemon verifying each request's MAC under its own secret.
    """

    if not state_lock_holder_is_alive(state_root):
        return None
    lock = read_state_lock(state_root)
    if lock is None or not _same_transport(
        lock.transport, server_url=server_url, server_socket=server_socket
    ):
        return None
    path = bootstrap_secret_path(state_root)
    try:
        status = os.lstat(path)
    except OSError:
        return None
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.getuid()
        or stat.S_IMODE(status.st_mode) & 0o077
    ):
        return None
    secret = path.read_text(encoding="utf-8").strip()
    return secret or None


class OperatorRequestRefused(Exception):
    """Why a MAC-signed operator request was refused: one code per cause."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


_NONCE_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_seen_nonces: dict[str, float] = {}
_seen_nonces_lock = threading.Lock()


def reset_operator_nonces() -> None:
    """Forget every seen nonce. Used by tests."""

    with _seen_nonces_lock:
        _seen_nonces.clear()


def verify_operator_request(
    secret: str,
    *,
    method: str,
    path: str,
    query: str,
    nonce: str | None,
    timestamp: str | None,
    mac: str | None,
    now: float | None = None,
) -> None:
    """Accept one MAC-signed, bodyless operator request exactly once, or refuse.

    The MAC must be ``operator_request_mac`` under this daemon's own secret over
    the request as received, the timestamp within the skew window of this
    daemon's clock, and the nonce unseen within that window.
    """

    current = time.time() if now is None else now
    if nonce is None or timestamp is None or mac is None or not _NONCE_PATTERN.fullmatch(nonce):
        raise OperatorRequestRefused(
            "runtime_bootstrap.operator_mac_invalid", "the operator request signature is malformed"
        )
    try:
        signed_at = int(timestamp)
    except ValueError as exc:
        raise OperatorRequestRefused(
            "runtime_bootstrap.operator_mac_invalid", "the operator request timestamp is malformed"
        ) from exc
    expected = operator_request_mac(
        secret,
        method=method,
        path=path,
        query=query,
        body=b"",
        nonce=nonce,
        timestamp=timestamp,
    )
    if not hmac.compare_digest(mac, expected):
        raise OperatorRequestRefused(
            "runtime_bootstrap.operator_mac_invalid",
            "the operator request is not signed with this daemon's bootstrap secret",
        )
    if abs(current - signed_at) > OPERATOR_MAC_MAX_SKEW_SECONDS:
        raise OperatorRequestRefused(
            "runtime_bootstrap.operator_mac_stale",
            "the operator request was not signed just now",
        )
    with _seen_nonces_lock:
        for seen, expires in list(_seen_nonces.items()):
            if expires < current:
                del _seen_nonces[seen]
        if nonce in _seen_nonces:
            raise OperatorRequestRefused(
                "runtime_bootstrap.operator_mac_replayed",
                "this signed operator request was already accepted",
            )
        # Kept until the timestamp can no longer pass the skew check.
        _seen_nonces[nonce] = signed_at + OPERATOR_MAC_MAX_SKEW_SECONDS + 1


__all__ = [
    "BOOTSTRAP_SECRET_FILE",
    "OperatorRequestRefused",
    "bootstrap_secret_path",
    "prepare_bootstrap_secret",
    "read_local_bootstrap_secret",
    "reset_operator_nonces",
    "verify_operator_request",
    "write_owner_only_secret",
]
