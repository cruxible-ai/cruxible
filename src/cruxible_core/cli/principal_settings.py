"""The one file a principal's process loads to act: connection, identity, key, token.

`playbill init` writes it for the owner and `playbill principal add` for each
agent, beside the private key in the principal's key directory. It is a shell
file of `export` lines (`set -a; . DIR/cruxible.env; set +a`), readable by the
CLI, the SDK and the MCP server alike because each reads the same variables.
The bearer token appears only when the daemon runs with auth; it is written
owner-only (0600) and never printed.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from pathlib import Path

PRINCIPAL_SETTINGS_FILE = "cruxible.env"
PRINCIPAL_KEY_ENV = "CRUXIBLE_PRINCIPAL_KEY"
_TOKEN_ENV = "CRUXIBLE_SERVER_BEARER_TOKEN"


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


def set_principal_settings_token(key_dir: Path, token: str) -> Path | None:
    """Replace the bearer token in an existing settings file; None when there is none."""

    path = principal_settings_path(key_dir)
    if not path.is_file() or path.is_symlink():
        return None
    kept = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith(f"export {_TOKEN_ENV}=")
    ]
    kept.append(f"export {_TOKEN_ENV}={shlex.quote(token)}")
    _write_owner_only(path, "\n".join(kept) + "\n")
    return path


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
    "PRINCIPAL_KEY_ENV",
    "PRINCIPAL_SETTINGS_FILE",
    "principal_settings_path",
    "set_principal_settings_token",
    "write_principal_settings",
]
