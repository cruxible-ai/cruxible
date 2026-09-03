"""Pure service-unit rendering and narrow user-service installation."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from cruxible_core.errors import ConfigError
from cruxible_core.server.credentials import RuntimeCredentialStore

SERVICE_LABEL = "ai.cruxible.daemon"
SERVICE_CONFIG_NAME = "service-install-v1.json"


class ServiceInstallConfigV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["cruxible-service-install-v1"] = "cruxible-service-install-v1"
    platform: Literal["darwin", "linux"]
    executable: str
    state_root: str
    socket_path: str | None = None
    host: str | None = None
    port: int | None = None
    capability_ceiling: Literal["read_only", "governed_write", "graph_write", "admin"]
    auth_enabled: bool

    @model_validator(mode="after")
    def _transport_shape(self) -> ServiceInstallConfigV1:
        socket_selected = self.socket_path is not None
        tcp_complete = self.host is not None and self.port is not None
        if socket_selected == tcp_complete:
            raise ValueError("service settings must select exactly one complete transport")
        if self.port is not None and not 1 <= self.port <= 65535:
            raise ValueError("service TCP port must be between 1 and 65535")
        for field_name in ("executable", "state_root", "socket_path", "host"):
            value = getattr(self, field_name)
            if value is None:
                continue
            if value.startswith("="):
                raise ValueError(f"service {field_name} must not start with '='")
            if any(ord(character) < 32 or ord(character) == 127 for character in value):
                raise ValueError(f"service {field_name} must not contain control characters")
        return self


def resolved_cruxible_executable() -> Path:
    found = shutil.which("cruxible")
    if found is None:
        raise ConfigError("cruxible executable is not on PATH; activate the installation first")
    executable = Path(found).resolve(strict=True)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ConfigError(f"recorded cruxible executable is not executable: {executable}")
    return executable


def service_destination(platform: str, *, home: Path | None = None) -> Path:
    root = (home or Path.home()).expanduser().resolve()
    if platform == "darwin":
        return root / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
    if platform == "linux":
        return root / ".config" / "systemd" / "user" / "cruxible.service"
    raise ConfigError(f"server install-service is unsupported on platform {platform!r}")


def _start_arguments(config: ServiceInstallConfigV1) -> list[str]:
    values = [
        config.executable,
        "server",
        "start",
        "--state-root",
        config.state_root,
        "--capability-ceiling",
        config.capability_ceiling,
    ]
    if config.socket_path is not None:
        values.extend(("--socket", config.socket_path))
    else:
        values.extend(("--host", config.host or "127.0.0.1", "--port", str(config.port or 8100)))
    return values


def render_launchd_service(config: ServiceInstallConfigV1) -> bytes:
    payload: dict[str, object] = {
        "Label": SERVICE_LABEL,
        "ProgramArguments": _start_arguments(config),
        "RunAtLoad": False,
        "KeepAlive": False,
        "EnvironmentVariables": {
            "CRUXIBLE_SERVER_AUTH": "true" if config.auth_enabled else "false"
        },
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def render_systemd_service(config: ServiceInstallConfigV1) -> bytes:
    command = " ".join(_systemd_quote(value) for value in _start_arguments(config))
    auth = "true" if config.auth_enabled else "false"
    return (
        "[Unit]\n"
        "Description=Cruxible governed-state daemon\n\n"
        "[Service]\n"
        f"ExecStart={command}\n"
        f"Environment=CRUXIBLE_SERVER_AUTH={auth}\n"
        "Restart=on-failure\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    ).encode("utf-8")


def _systemd_quote(value: str) -> str:
    """Quote one already-validated argv value for systemd's non-shell parser."""

    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def render_service(config: ServiceInstallConfigV1) -> bytes:
    if config.platform == "darwin":
        return render_launchd_service(config)
    return render_systemd_service(config)


def durable_credentials_available(state_root: Path) -> bool:
    database = state_root / "daemon" / "runtime_credentials.db"
    if not database.is_file():
        return False
    try:
        return RuntimeCredentialStore(database, initialize=False).has_active_credentials()
    except sqlite3.Error:
        return False


def service_auth_required(state_root: Path) -> bool:
    """Read the durable auth latch without creating daemon state."""

    database = state_root / "daemon" / "runtime_credentials.db"
    if not database.is_file():
        return False
    try:
        return RuntimeCredentialStore(database, initialize=False).is_auth_required()
    except sqlite3.Error:
        return False


def resolve_service_auth_posture(state_root: Path, requested: bool | None) -> bool:
    """Mirror the state root's live auth latch and refuse explicit disagreement."""

    required = service_auth_required(state_root)
    if requested is None or requested == required:
        return required
    if requested:
        repair = (
            "run `cruxible server start --bootstrap-secret-file PATH`, claim the bootstrap "
            "credential, then rerun install-service with --auth"
        )
    else:
        repair = "rerun install-service with --auth"
    raise ConfigError(
        "service_install.auth_posture_mismatch: explicit auth setting disagrees with the "
        f"state root's durable auth latch (required={required}); repair: {repair}"
    )


def build_service_config(**values: object) -> ServiceInstallConfigV1:
    """Construct the settings record, refusing hostile values with a typed error.

    ``ServiceInstallConfigV1``'s validators reject control characters, leading
    ``=``, and out-of-range ports so nothing hostile is ever rendered into a
    service unit. Raised bare, that refusal is a ``pydantic.ValidationError``,
    which is not a ``CoreError``: the CLI's ``handle_errors`` re-raises it and
    the operator sees an empty stdout and stderr. Converting it here keeps the
    security refusal on the same typed channel as every other refusal on this
    surface.
    """

    try:
        return ServiceInstallConfigV1(**values)  # type: ignore[arg-type]
    except ValidationError as exc:
        reasons = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'settings'}: {error['msg']}"
            for error in exc.errors()
        )
        raise ConfigError(
            f"service_install.settings_invalid: {reasons}; repair: rerun install-service with "
            "server settings that carry no control characters, no leading '=', and a port "
            "between 1 and 65535"
        ) from exc


def service_config_path(state_root: Path) -> Path:
    return state_root / "daemon" / SERVICE_CONFIG_NAME


def load_service_config(state_root: Path) -> ServiceInstallConfigV1:
    """Load and revalidate the authoritative operational service settings."""

    record = service_config_path(state_root)
    try:
        config = ServiceInstallConfigV1.model_validate_json(record.read_bytes())
    except (OSError, ValidationError) as exc:
        raise ConfigError(f"recorded service settings cannot be read: {record}: {exc}") from exc
    if Path(config.state_root).resolve(strict=False) != state_root.resolve(strict=False):
        raise ConfigError("recorded service state root differs from its operational location")
    executable = Path(config.executable)
    try:
        resolved = executable.resolve(strict=True)
    except OSError as exc:
        raise ConfigError(f"recorded cruxible executable no longer exists: {executable}") from exc
    if resolved != executable or not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ConfigError(f"recorded cruxible executable is no longer executable: {executable}")
    return config


def _atomic_write(path: Path, content: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            "wb", prefix=f".{path.name}.", dir=path.parent, delete=False
        ) as out:
            temporary = Path(out.name)
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        temporary = None
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def install_service(config: ServiceInstallConfigV1, *, replace: bool = False) -> Path:
    destination = service_destination(config.platform)
    if (destination.exists() or destination.is_symlink()) and not replace:
        raise ConfigError(
            f"service unit already exists at {destination}; rerun with --replace to replace it"
        )
    _atomic_write(destination, render_service(config), mode=stat.S_IRUSR | stat.S_IWUSR)
    record = service_config_path(Path(config.state_root))
    _atomic_write(
        record,
        json.dumps(config.model_dump(mode="json"), indent=2, sort_keys=True).encode("utf-8")
        + b"\n",
        mode=stat.S_IRUSR | stat.S_IWUSR,
    )
    if config.platform == "darwin":
        subprocess.run(["launchctl", "load", "-w", str(destination)], check=True)
    else:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", destination.name], check=True)
    return destination


def current_service_platform() -> Literal["darwin", "linux"]:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    raise ConfigError(f"server install-service is unsupported on platform {sys.platform!r}")


__all__ = [
    "SERVICE_CONFIG_NAME",
    "SERVICE_LABEL",
    "ServiceInstallConfigV1",
    "current_service_platform",
    "durable_credentials_available",
    "install_service",
    "load_service_config",
    "render_launchd_service",
    "render_service",
    "render_systemd_service",
    "resolve_service_auth_posture",
    "resolved_cruxible_executable",
    "service_destination",
    "service_config_path",
    "service_auth_required",
]
