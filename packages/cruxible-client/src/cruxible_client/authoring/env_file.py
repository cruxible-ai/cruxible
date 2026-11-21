"""Load Cruxible client settings from an env file (``CRUXIBLE_ENV_FILE`` / ``--env-file``).

`cruxible init` and `cruxible principal add` write a principal's connection,
identity, signing key path and (when daemon auth is on) bearer token to
``DIR/cruxible.env``. Passing that path via ``--env-file`` or
``CRUXIBLE_ENV_FILE`` lets the CLI, MCP server and Python SDK load the file
directly without shell-sourcing or copying bearer tokens into MCP client JSON.

Security invariants:
- Only allowlisted ``CRUXIBLE_*`` keys are accepted; process-control variables
  such as ``PATH``, ``PYTHONPATH`` or ``LD_PRELOAD`` are refused.
- The file is opened once with ``O_NOFOLLOW | O_NONBLOCK`` and validated via
  ``fstat`` on the held descriptor (no symlink, regular file, <= 64 KiB).
- If the file contains ``CRUXIBLE_SERVER_BEARER_TOKEN``, its mode must be
  owner-only (``mode & 0o077 == 0``), refusing with a ``chmod 600`` repair.
- Existing non-empty variables in the caller's environment (and explicit flags)
  always take precedence.
- When the caller explicitly names a transport or a different principal, the
  file's transport or credential keys (key and bearer token) are never mixed in.
- Error messages never echo token values or raw assignment payloads.
"""

from __future__ import annotations

import errno
import os
import re
import shlex
import stat
from collections.abc import Callable, Mapping, MutableMapping
from pathlib import Path

from cruxible_client.contracts.principals import PRINCIPAL_ID_ENV, PRINCIPAL_KEY_ENV

ENV_FILE_ENV = "CRUXIBLE_ENV_FILE"
ENV_FILE_ERROR_CODE = "cruxible.config.env_file_invalid"
ENV_FILE_MAX_BYTES = 64 * 1024

_TOKEN_ENV = "CRUXIBLE_SERVER_BEARER_TOKEN"
_URL_ENV = "CRUXIBLE_SERVER_URL"
_SOCKET_ENV = "CRUXIBLE_SERVER_SOCKET"
_TRANSPORT_KEYS = frozenset({_URL_ENV, _SOCKET_ENV})
_CREDENTIAL_KEYS = frozenset({PRINCIPAL_KEY_ENV, _TOKEN_ENV})
_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)

#: The only variables an env file may supply.
ENV_FILE_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        _URL_ENV,
        _SOCKET_ENV,
        "CRUXIBLE_REQUIRE_SERVER",
        "CRUXIBLE_STATE_ROOT",
        "CRUXIBLE_INSTANCE_ID",
        "CRUXIBLE_WORKSPACE",
        PRINCIPAL_ID_ENV,
        PRINCIPAL_KEY_ENV,
        _TOKEN_ENV,
        "CRUXIBLE_MODE",
        "CRUXIBLE_MCP_PROFILE",
        "CRUXIBLE_MCP_TOOLS",
        "CRUXIBLE_MCP_KEY_DIR",
        "CRUXIBLE_MCP_WORKSPACE_ROOT",
    }
)


class EnvFileError(ValueError):
    """An env file could not be read, failed custody checks, or contained invalid settings."""

    code = ENV_FILE_ERROR_CODE


def resolve_env_file_path(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """Resolve an explicit env-file path or ``CRUXIBLE_ENV_FILE`` from ``environ``.

    An explicit empty string refuses; an empty ``CRUXIBLE_ENV_FILE`` in the
    environment is treated as unset.
    """

    if path is not None:
        raw = str(path).strip()
        if not raw:
            raise EnvFileError(f"{ENV_FILE_ERROR_CODE}: env file path must not be empty")
        return Path(raw).expanduser()
    env = os.environ if environ is None else environ
    raw = (env.get(ENV_FILE_ENV) or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def _read_env_file_bytes(path: Path) -> tuple[bytes, os.stat_result]:
    """Open ``path`` once without following symlinks and return its bytes and stat."""

    try:
        fd = os.open(path, _READ_FLAGS)
    except FileNotFoundError as exc:
        raise EnvFileError(f"{ENV_FILE_ERROR_CODE}: env file {path} does not exist") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise EnvFileError(
                f"{ENV_FILE_ERROR_CODE}: env file {path} must not be a symbolic link"
            ) from exc
        raise EnvFileError(
            f"{ENV_FILE_ERROR_CODE}: env file {path} cannot be opened: {exc.strerror or exc}"
        ) from exc

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise EnvFileError(f"{ENV_FILE_ERROR_CODE}: env file {path} is not a regular file")
        if st.st_size > ENV_FILE_MAX_BYTES:
            raise EnvFileError(
                f"{ENV_FILE_ERROR_CODE}: env file {path} exceeds the "
                f"{ENV_FILE_MAX_BYTES}-byte limit"
            )
        chunks: list[bytes] = []
        remaining = ENV_FILE_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(16384, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > ENV_FILE_MAX_BYTES:
            raise EnvFileError(
                f"{ENV_FILE_ERROR_CODE}: env file {path} exceeds the "
                f"{ENV_FILE_MAX_BYTES}-byte limit"
            )
        return data, st
    except OSError as exc:
        raise EnvFileError(
            f"{ENV_FILE_ERROR_CODE}: env file {path} cannot be read: {exc.strerror or exc}"
        ) from exc
    finally:
        os.close(fd)


def read_env_file(path: str | Path) -> dict[str, str]:
    """Parse and validate a Cruxible env file (such as ``DIR/cruxible.env``).

    Accepts blank lines, ``#`` comment lines, ``export KEY=VALUE`` and
    ``KEY=VALUE`` lines. Values are shell-unquoted with ``shlex`` (supporting
    single quotes, double quotes, unquoted tokens and trailing ``#`` comments).
    """

    target = Path(path).expanduser()
    raw_bytes, st = _read_env_file_bytes(target)
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EnvFileError(f"{ENV_FILE_ERROR_CODE}: env file {target} is not valid UTF-8") from exc

    values: dict[str, str] = {}
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assignment = stripped
        if assignment.startswith("export ") or assignment.startswith("export\t"):
            assignment = assignment[6:].lstrip()
        name, sep, raw_value = assignment.partition("=")
        name = name.strip()
        if not sep or not _KEY_PATTERN.match(name):
            raise EnvFileError(
                f"{ENV_FILE_ERROR_CODE}: {target}:{lineno}: expected KEY=VALUE or export KEY=VALUE"
            )
        if name not in ENV_FILE_ALLOWED_KEYS:
            raise EnvFileError(
                f"{ENV_FILE_ERROR_CODE}: {target}:{lineno}: key {name!r} is not allowed "
                "in a Cruxible env file"
            )
        try:
            words = shlex.split(raw_value, comments=True)
        except ValueError as exc:
            raise EnvFileError(
                f"{ENV_FILE_ERROR_CODE}: {target}:{lineno}: invalid quoting for {name}"
            ) from exc
        if len(words) != 1 or not words[0].strip():
            raise EnvFileError(
                f"{ENV_FILE_ERROR_CODE}: {target}:{lineno}: {name} must have one non-empty value"
            )
        values[name] = words[0]

    if not values:
        raise EnvFileError(f"{ENV_FILE_ERROR_CODE}: env file {target} sets no Cruxible variables")

    if _URL_ENV in values and _SOCKET_ENV in values:
        raise EnvFileError(
            f"{ENV_FILE_ERROR_CODE}: env file {target} cannot set both {_URL_ENV} and {_SOCKET_ENV}"
        )

    if _TOKEN_ENV in values:
        mode = stat.S_IMODE(st.st_mode)
        if mode & 0o077 != 0:
            raise EnvFileError(
                f"{ENV_FILE_ERROR_CODE}: env file {target} holds {_TOKEN_ENV} with mode "
                f"{mode:04o} (readable by group or others); repair: chmod 600 "
                f"{shlex.quote(str(target))}"
            )

    return values


def merge_env_file(
    path: str | Path,
    *,
    base_environ: Mapping[str, str] | None = None,
    explicit_transport: bool = False,
    explicit_principal: str | None = None,
) -> dict[str, str]:
    """Return the subset of env-file variables that should be layered onto ``base_environ``.

    Precedence and isolation rules:
    - Any key already non-empty in ``base_environ`` is preserved (not returned).
    - If ``explicit_transport`` is True or either ``CRUXIBLE_SERVER_URL`` or
      ``CRUXIBLE_SERVER_SOCKET`` is already non-empty in ``base_environ``, neither
      transport key is taken from the file.
    - If the caller explicitly names a principal (``explicit_principal`` or
      ``CRUXIBLE_PRINCIPAL_ID`` in ``base_environ``) that differs from the file's
      ``CRUXIBLE_PRINCIPAL_ID``, neither ``CRUXIBLE_PRINCIPAL_KEY`` nor
      ``CRUXIBLE_SERVER_BEARER_TOKEN`` is taken from the file.
    """

    base = os.environ if base_environ is None else base_environ
    parsed = read_env_file(path)

    has_transport = explicit_transport or any(
        (base.get(key) or "").strip() for key in _TRANSPORT_KEYS
    )
    explicit_principal_clean = (explicit_principal or "").strip()
    caller_principal = explicit_principal_clean or (base.get(PRINCIPAL_ID_ENV) or "").strip()
    file_principal = (parsed.get(PRINCIPAL_ID_ENV) or "").strip()
    skip_credentials = bool(
        caller_principal and file_principal and caller_principal != file_principal
    )

    additions: dict[str, str] = {}
    for key, value in parsed.items():
        if (base.get(key) or "").strip():
            continue
        if has_transport and key in _TRANSPORT_KEYS:
            continue
        if skip_credentials and (key in _CREDENTIAL_KEYS or key == PRINCIPAL_ID_ENV):
            continue
        additions[key] = value
    return additions


def apply_env_file(
    path: str | Path | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
    explicit_transport: bool = False,
    explicit_principal: str | None = None,
) -> tuple[Path | None, Callable[[], None]]:
    """Load an env file into ``environ`` (default ``os.environ``) and return ``(path, restore)``.

    Calling ``restore()`` reverts every variable touched by this call back to its
    prior state (missing or previous value). When no env file is named (neither
    ``path`` nor ``CRUXIBLE_ENV_FILE``), returns ``(None, noop)``.
    """

    target_env: MutableMapping[str, str] = os.environ if environ is None else environ
    resolved = resolve_env_file_path(path, environ=target_env)
    if resolved is None:
        return None, lambda: None

    additions = merge_env_file(
        resolved,
        base_environ=target_env,
        explicit_transport=explicit_transport,
        explicit_principal=explicit_principal,
    )
    previous = {key: target_env.get(key) for key in additions}
    target_env.update(additions)

    def restore() -> None:
        for key, value in previous.items():
            if value is None:
                target_env.pop(key, None)
            else:
                target_env[key] = value

    return resolved.resolve(), restore


__all__ = [
    "ENV_FILE_ALLOWED_KEYS",
    "ENV_FILE_ENV",
    "ENV_FILE_ERROR_CODE",
    "ENV_FILE_MAX_BYTES",
    "EnvFileError",
    "apply_env_file",
    "merge_env_file",
    "read_env_file",
    "resolve_env_file_path",
]
