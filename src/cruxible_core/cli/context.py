"""Persisted client-side CLI context for governed server usage."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Mapping

from cruxible_core.errors import ConfigError


@dataclass(frozen=True)
class CliContextState:
    """Remembered server transport and active governed instance."""

    server_url: str | None = None
    server_socket: str | None = None
    instance_id: str | None = None

    def as_json(self) -> dict[str, str]:
        payload: dict[str, str] = {}
        if self.server_url:
            payload["server_url"] = self.server_url
        if self.server_socket:
            payload["server_socket"] = self.server_socket
        if self.instance_id:
            payload["instance_id"] = self.instance_id
        return payload


def get_cli_context_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return the user-scoped CLI context path."""
    env = environ or os.environ
    raw = env.get("CRUXIBLE_CLI_CONTEXT_PATH")
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".cruxible" / "client-context.json").resolve()


def load_cli_context(environ: Mapping[str, str] | None = None) -> CliContextState:
    """Load remembered CLI context if present."""
    path = get_cli_context_path(environ)
    if not path.exists():
        return CliContextState()
    try:
        payload = json.loads(path.read_text())
    except OSError as exc:
        raise ConfigError(f"Failed to read CLI context at {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"CLI context at {path} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ConfigError(f"CLI context at {path} must contain a JSON object")

    server_url = payload.get("server_url")
    server_socket = payload.get("server_socket")
    instance_id = payload.get("instance_id")
    for key, value in (
        ("server_url", server_url),
        ("server_socket", server_socket),
        ("instance_id", instance_id),
    ):
        if value is not None and not isinstance(value, str):
            raise ConfigError(f"CLI context field '{key}' must be a string when set")
    return CliContextState(
        server_url=server_url,
        server_socket=server_socket,
        instance_id=instance_id,
    )


def save_cli_context(
    state: CliContextState,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Persist remembered CLI context atomically."""
    path = get_cli_context_path(environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
        prefix=f".{path.name}.tmp.",
    ) as handle:
        json.dump(state.as_json(), handle, indent=2, sort_keys=True)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(path)
    return path


def clear_cli_context(*, environ: Mapping[str, str] | None = None) -> Path:
    """Clear remembered CLI context."""
    path = get_cli_context_path(environ)
    if path.exists():
        path.unlink()
    return path
