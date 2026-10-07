"""Persisted client-side CLI context for governed server usage."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Mapping

from cruxible_core.errors import ConfigError


@dataclass(frozen=True)
class RememberedPrincipals:
    """The principal settings files the CLI knows for one instance, and which it acts as.

    ``settings`` maps a principal ID to its ``cruxible.env`` (written by `cruxible
    init` or `cruxible principal add`); ``active`` is the one the CLI loads.
    """

    active: str | None = None
    settings: Mapping[str, str] = field(default_factory=dict)

    def as_json(self) -> dict[str, object]:
        payload: dict[str, object] = {"settings": dict(sorted(self.settings.items()))}
        if self.active is not None:
            payload["active"] = self.active
        return payload


@dataclass(frozen=True)
class CliContextState:
    """Remembered server transport, active governed instance and principal settings."""

    server_url: str | None = None
    server_socket: str | None = None
    instance_id: str | None = None
    instance_transport: str | None = None
    #: Per instance ID: the principal settings files the CLI knows and loads.
    principals: Mapping[str, RememberedPrincipals] = field(default_factory=dict)

    def bound_instance_transport(self) -> str | None:
        if self.instance_transport:
            return self.instance_transport
        if self.server_url:
            return self.server_url.rstrip("/")
        if self.server_socket:
            return f"unix://{Path(self.server_socket).expanduser().resolve()}"
        return None

    def as_json(self) -> dict[str, object]:
        payload: dict[str, object] = {}
        if self.server_url:
            payload["server_url"] = self.server_url
        if self.server_socket:
            payload["server_socket"] = self.server_socket
        if self.instance_id:
            payload["instance_id"] = self.instance_id
            bound_transport = self.bound_instance_transport()
            if bound_transport:
                payload["instance_transport"] = bound_transport
        if self.principals:
            payload["principals"] = {
                instance: entry.as_json() for instance, entry in sorted(self.principals.items())
            }
        return payload

    def remember_principal(
        self, instance_id: str, principal_id: str, settings_path: Path, *, activate: bool
    ) -> CliContextState:
        """This state with one principal's settings file known (and optionally active)."""

        entry = self.principals.get(instance_id, RememberedPrincipals())
        updated = RememberedPrincipals(
            active=principal_id if activate else entry.active,
            settings={**entry.settings, principal_id: str(settings_path)},
        )
        return replace(self, principals={**self.principals, instance_id: updated})

    def select_principal(self, instance_id: str, principal_id: str) -> CliContextState:
        """This state acting as one already-known principal of ``instance_id``."""

        entry = self.principals[instance_id]
        return replace(
            self,
            principals={
                **self.principals,
                instance_id: RememberedPrincipals(active=principal_id, settings=entry.settings),
            },
        )


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
    instance_transport = payload.get("instance_transport")
    for key, value in (
        ("server_url", server_url),
        ("server_socket", server_socket),
        ("instance_id", instance_id),
        ("instance_transport", instance_transport),
    ):
        if value is not None and not isinstance(value, str):
            raise ConfigError(f"CLI context field '{key}' must be a string when set")
    return CliContextState(
        server_url=server_url,
        server_socket=server_socket,
        instance_id=instance_id,
        instance_transport=instance_transport,
        principals=_load_principals(payload.get("principals"), path),
    )


def _load_principals(raw: object, path: Path) -> dict[str, RememberedPrincipals]:
    if raw is None:
        return {}
    malformed = ConfigError(
        f"CLI context field 'principals' at {path} must map instance IDs to "
        "{active, settings} objects"
    )
    if not isinstance(raw, dict):
        raise malformed
    principals: dict[str, RememberedPrincipals] = {}
    for instance, entry in raw.items():
        if not isinstance(entry, dict):
            raise malformed
        active = entry.get("active")
        settings = entry.get("settings", {})
        if (active is not None and not isinstance(active, str)) or not isinstance(settings, dict):
            raise malformed
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in settings.items()):
            raise malformed
        principals[str(instance)] = RememberedPrincipals(active=active, settings=dict(settings))
    return principals


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
