"""The one file a principal's process loads to act: connection, identity, key, token.

`cruxible init` writes it for the owner and `cruxible principal add` for each
agent, beside the private key in the principal's key directory. It is a shell
file of `export` lines (`set -a; . DIR/cruxible.env; set +a`), readable by the
CLI, the SDK and the MCP server alike because each reads the same variables.
The CLI also remembers each file it wrote in its context and loads the active
principal's file itself (`cruxible context use --principal ID` switches); a
process that sets these variables itself keeps them.
The bearer token appears only when the daemon runs with auth; it is written
owner-only (0600) and never printed.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from cruxible_client.authoring.principal_settings import (
    PrincipalSettings,
    default_key_dir,
    read_principal_settings,
)
from cruxible_client.contracts.principals import PRINCIPAL_KEY_ENV

PRINCIPAL_SETTINGS_FILE = "cruxible.env"
_TOKEN_ENV = "CRUXIBLE_SERVER_BEARER_TOKEN"


PRINCIPAL_ID_ENV = "CRUXIBLE_PRINCIPAL_ID"
PRINCIPAL_TOKEN_ENV = _TOKEN_ENV


def principal_settings_path(key_dir: Path) -> Path:
    return key_dir.expanduser().resolve() / PRINCIPAL_SETTINGS_FILE


def _transport_lines(obj: Mapping[str, object]) -> list[str]:
    socket = obj.get("server_socket")
    if socket:
        resolved = Path(str(socket)).expanduser().resolve()
        return [f"export CRUXIBLE_SERVER_SOCKET={shlex.quote(str(resolved))}"]
    return [f"export CRUXIBLE_SERVER_URL={shlex.quote(str(obj['server_url']))}"]


def write_principal_settings(
    key_dir: Path,
    *,
    ctx_obj: Mapping[str, object],
    instance_id: str,
    principal_id: str,
    private_key_path: Path,
    token: str | None,
    written_by: str,
) -> Path:
    """Write (or replace) the principal's settings file owner-only; return its path."""

    path = principal_settings_path(key_dir)
    lines = [
        f"# Cruxible settings for principal {principal_id}, written by `{written_by}`.",
        f"# Load with: set -a; . {shlex.quote(str(path))}; set +a",
        *_transport_lines(ctx_obj),
        f"export CRUXIBLE_INSTANCE_ID={shlex.quote(instance_id)}",
        f"export CRUXIBLE_PRINCIPAL_ID={shlex.quote(principal_id)}",
        f"export {PRINCIPAL_KEY_ENV}={shlex.quote(str(private_key_path.resolve()))}",
    ]
    if token is not None:
        lines.append(f"export {_TOKEN_ENV}={shlex.quote(token)}")
    _write_owner_only(path, "\n".join(lines) + "\n")
    return path


class SettingsTokenMismatch(ValueError):
    """The settings file holds a token other than the one being replaced."""

    def __init__(self, path: Path) -> None:
        super().__init__(str(path))
        self.path = path


def set_principal_settings_token(
    key_dir: Path, token: str, *, replaces: str | None = None
) -> Path | None:
    """Replace the bearer token in an existing settings file; None when there is none.

    ``replaces`` (S5): compare-and-write. The file is rewritten only when the
    token it holds is exactly ``replaces`` (the token this rotation replaces,
    read before the request); otherwise ``SettingsTokenMismatch`` names the
    file and nothing is written. The compare and the write hold an exclusive
    lock beside the file, so a concurrent update is never lost.
    """

    path = principal_settings_path(key_dir)
    if not path.is_file() or path.is_symlink():
        return None
    with _settings_lock(path):
        lines = path.read_text(encoding="utf-8").splitlines()
        if replaces is not None and read_principal_settings(path).token != replaces:
            raise SettingsTokenMismatch(path)
        kept = [line for line in lines if not line.startswith(f"export {_TOKEN_ENV}=")]
        kept.append(f"export {_TOKEN_ENV}={shlex.quote(token)}")
        _write_owner_only(path, "\n".join(kept) + "\n")
    return path


@contextmanager
def _settings_lock(path: Path) -> Iterator[None]:
    """An exclusive advisory lock on ``.<name>.lock`` beside a settings file."""

    import fcntl

    lock = path.with_name(f".{path.name}.lock")
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _write_owner_only(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    path.chmod(0o600)


__all__ = [
    "SettingsTokenMismatch",
    "PRINCIPAL_ID_ENV",
    "PRINCIPAL_KEY_ENV",
    "PRINCIPAL_SETTINGS_FILE",
    "PRINCIPAL_TOKEN_ENV",
    "PrincipalSettings",
    "default_key_dir",
    "principal_settings_path",
    "read_principal_settings",
    "set_principal_settings_token",
    "write_principal_settings",
]
