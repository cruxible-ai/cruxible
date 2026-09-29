"""The runtime bootstrap secret: kept in an owner-only file, never printed.

With auth on, the daemon's bootstrap secret is its unscoped operator credential
(host create, status, restart, stop). It is never written to stdout, stderr or
the request log. The daemon writes it owner-only (0600) to
``<state-root>/daemon/bootstrap-secret`` once it holds the state root, and
``server status`` / ``restart`` / ``stop`` read it from there by default when
they talk to the daemon that owns that state root, so a local restart needs no
manually supplied credential.
"""

from __future__ import annotations

import os
import secrets
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit

from cruxible_core.server.config import (
    get_runtime_bootstrap_secret,
    is_server_auth_enabled,
)
from cruxible_core.server.state_lock import read_state_lock

BOOTSTRAP_SECRET_FILE = "bootstrap-secret"
_BOOTSTRAP_SECRET_ENV = "CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


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
    client_host = split.hostname.lower()
    recorded_host = host.strip("[]").lower()
    return client_host == recorded_host or (
        client_host in _LOOPBACK_HOSTS and recorded_host in _LOOPBACK_HOSTS
    )


def read_local_bootstrap_secret(
    state_root: Path, *, server_url: str | None, server_socket: str | None
) -> str | None:
    """The bootstrap secret of the local daemon this client targets, if it is that one.

    Returned only when the state root's lock records exactly the transport the
    client is about to use, so the secret is never sent to some other daemon,
    and only when the file is a regular file this user owns with mode 0600.
    """

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


__all__ = [
    "BOOTSTRAP_SECRET_FILE",
    "bootstrap_secret_path",
    "prepare_bootstrap_secret",
    "read_local_bootstrap_secret",
    "write_owner_only_secret",
]
