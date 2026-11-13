"""A principal's ``cruxible.env`` as the CLI, the SDK and the MCP server read it.

`cruxible init` and `cruxible principal add` write the file beside the
principal's private key and remember it in the CLI context, per instance on one
daemon endpoint, with the active principal named. `Cruxible.connect()` acts as
that remembered principal when the process names none of its own, as the CLI
does.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from cruxible_client.authoring.context import _normalized_transport
from cruxible_client.contracts.principals import PRINCIPAL_ID_ENV, PRINCIPAL_KEY_ENV

PRINCIPAL_TOKEN_ENV = "CRUXIBLE_SERVER_BEARER_TOKEN"


@dataclass(frozen=True)
class PrincipalSettings:
    """What a principal's ``cruxible.env`` names: who acts, on what, with which key."""

    path: Path
    #: The daemon endpoint the file was written for (``unix://<realpath>`` or a URL).
    transport: str | None
    instance_id: str
    principal_id: str
    private_key_path: str
    token: str | None

    def environment(self) -> dict[str, str]:
        """The variables a process that sourced this file would have."""

        values = {
            PRINCIPAL_ID_ENV: self.principal_id,
            PRINCIPAL_KEY_ENV: self.private_key_path,
        }
        if self.token is not None:
            values[PRINCIPAL_TOKEN_ENV] = self.token
        return values


def read_principal_settings(path: Path) -> PrincipalSettings:
    """Parse the ``export`` lines `cruxible init` wrote; refuse anything else."""

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("export "):
            continue
        name, _, raw = line.removeprefix("export ").partition("=")
        words = shlex.split(raw)
        if len(words) != 1:
            raise ValueError(f"principal settings line for {name} is not one quoted value")
        values[name] = words[0]
    missing = [
        name
        for name in ("CRUXIBLE_INSTANCE_ID", PRINCIPAL_ID_ENV, PRINCIPAL_KEY_ENV)
        if not values.get(name)
    ]
    if missing:
        raise ValueError(f"principal settings {path} name no {', '.join(missing)}")
    return PrincipalSettings(
        path=path,
        transport=_normalized_transport(
            values.get("CRUXIBLE_SERVER_URL"), values.get("CRUXIBLE_SERVER_SOCKET")
        ),
        instance_id=values["CRUXIBLE_INSTANCE_ID"],
        principal_id=values[PRINCIPAL_ID_ENV],
        private_key_path=values[PRINCIPAL_KEY_ENV],
        token=values.get(PRINCIPAL_TOKEN_ENV),
    )


def remembered_principal_settings(
    remembered: Mapping[str, object],
    *,
    instance_id: str,
    server_url: str | None,
    server_socket: str | None,
) -> PrincipalSettings | None:
    """The active principal the CLI context remembers for this instance on this endpoint.

    None when nothing is remembered, or when the file is unreadable or names
    another principal, instance or endpoint: a credential is never offered to a
    daemon it was not written for.
    """

    transport = _normalized_transport(server_url, server_socket)
    principals = remembered.get("principals")
    if transport is None or not isinstance(principals, Mapping):
        return None
    entry = principals.get(f"{instance_id}@{transport}")
    if not isinstance(entry, Mapping):
        return None
    active = entry.get("active")
    settings = entry.get("settings")
    if not isinstance(active, str) or not isinstance(settings, Mapping):
        return None
    raw_path = settings.get(active)
    if not isinstance(raw_path, str):
        return None
    try:
        loaded = read_principal_settings(Path(raw_path))
    except (OSError, ValueError):
        return None
    if (
        loaded.instance_id != instance_id
        or loaded.principal_id != active
        or loaded.transport != transport
    ):
        return None
    return loaded


__all__ = [
    "PRINCIPAL_TOKEN_ENV",
    "PrincipalSettings",
    "read_principal_settings",
    "remembered_principal_settings",
]
