"""A principal's ``cruxible.env`` as the CLI, the SDK and the MCP server read it.

`cruxible init` and `cruxible principal add` write the file beside the
principal's private key and remember it in the CLI context, per instance on one
daemon endpoint, with the active principal named. `Cruxible.connect()` acts as
that remembered principal when the process names none of its own, as the CLI
does.
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

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


def remembered_context(
    environ: Mapping[str, str] | None = None, *, path: Path | None = None
) -> dict[str, object]:
    """The CLI context file (``path``, else ``CRUXIBLE_CLI_CONTEXT_PATH``, else the
    per-user one), or nothing.

    An unreadable or malformed file reads as nothing remembered: a key lookup
    then goes on to its next source and names the repair if none has the key.
    """

    env = os.environ if environ is None else environ
    if path is None:
        raw = env.get("CRUXIBLE_CLI_CONTEXT_PATH")
        path = Path(raw).expanduser() if raw else Path.home() / ".cruxible" / "client-context.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def remembered_settings_of(
    remembered: Mapping[str, object],
    principal_id: str | None,
    *,
    instance_id: str,
    server_url: str | None,
    server_socket: str | None,
) -> PrincipalSettings | None:
    """The settings file the CLI context remembers for one principal of this instance.

    ``principal_id`` None names the active principal. None when nothing is
    remembered for it on this endpoint, or when the file is unreadable or names
    another principal, instance or endpoint: a credential is never offered to a
    daemon it was not written for, and never as another principal's.
    """

    transport = _normalized_transport(server_url, server_socket)
    principals = remembered.get("principals")
    if transport is None or not isinstance(principals, Mapping):
        return None
    entry = principals.get(f"{instance_id}@{transport}")
    if not isinstance(entry, Mapping):
        return None
    wanted = entry.get("active") if principal_id is None else principal_id
    settings = entry.get("settings")
    if not isinstance(wanted, str) or not isinstance(settings, Mapping):
        return None
    raw_path = settings.get(wanted)
    if not isinstance(raw_path, str):
        return None
    try:
        loaded = read_principal_settings(Path(raw_path))
    except (OSError, ValueError):
        return None
    if (
        loaded.instance_id != instance_id
        or loaded.principal_id != wanted
        or loaded.transport != transport
    ):
        return None
    return loaded


def remembered_principal_settings(
    remembered: Mapping[str, object],
    *,
    instance_id: str,
    server_url: str | None,
    server_socket: str | None,
) -> PrincipalSettings | None:
    """The active principal the CLI context remembers for this instance on this endpoint."""

    return remembered_settings_of(
        remembered,
        None,
        instance_id=instance_id,
        server_url=server_url,
        server_socket=server_socket,
    )


def default_key_dir(
    instance_id: str, principal_id: str, environ: Mapping[str, str] | None = None
) -> Path:
    """Where `cruxible init` keeps a principal's key when no --key-dir is given.

    A per-user config path (``$XDG_CONFIG_HOME/cruxible``, else
    ``~/.config/cruxible``), one directory per instance and principal: outside
    every workspace and outside the daemon state root, so custody never lands
    beside the bytes it signs for.
    """

    env = os.environ if environ is None else environ
    raw = env.get("XDG_CONFIG_HOME")
    base = Path(raw).expanduser() if raw else Path.home() / ".config"
    return (base / "cruxible" / "keys" / instance_id / principal_id).resolve()


PrincipalKeySource = Literal[
    "option", "environment", "remembered_settings", "folder", "default_key_dir"
]


@dataclass(frozen=True)
class PrincipalKey:
    """The private key that signs as one principal, and where the lookup found it."""

    principal_id: str
    private_key_path: Path
    source: PrincipalKeySource
    #: The remembered ``cruxible.env`` naming the key, when that is the source.
    settings_path: Path | None = None
    #: The bearer token of that same settings file, from the one read that
    #: verified its principal, instance and endpoint; never read again.
    token: str | None = field(default=None, repr=False)


def principal_key(
    principal_id: str,
    *,
    instance_id: str,
    server_url: str | None,
    server_socket: str | None,
    explicit: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    remembered: Mapping[str, object] | None = None,
    folder: Path | None = None,
) -> PrincipalKey | None:
    """Which key signs as ``principal_id``: the one lookup of the CLI, the SDK and MCP.

    First that applies:

    1. ``explicit``: the caller's own argument (``--key``);
    2. ``CRUXIBLE_PRINCIPAL_KEY``, unless ``CRUXIBLE_PRINCIPAL_ID`` names another
       principal: a sourced ``cruxible.env`` sets both, and its key is that
       principal's only;
    3. the settings file the CLI context remembers for this principal on this
       instance and endpoint (`cruxible init`, `cruxible principal add`);
    4. the bound ``folder``'s identity record, when it names this principal;
    5. the default key directory `cruxible init` uses for this principal.

    None when no source has it (``principal_key_missing`` words the repair).
    It is never another principal's key. Nothing is read from the key: the
    signer checks custody and the public half when it opens it.
    """

    env = os.environ if environ is None else environ
    if explicit:
        return PrincipalKey(principal_id, Path(explicit).expanduser(), "option")
    raw = (env.get(PRINCIPAL_KEY_ENV) or "").strip()
    named = (env.get(PRINCIPAL_ID_ENV) or "").strip()
    if raw and named in ("", principal_id):
        return PrincipalKey(principal_id, Path(raw).expanduser(), "environment")
    settings = remembered_settings_of(
        remembered_context(env) if remembered is None else remembered,
        principal_id,
        instance_id=instance_id,
        server_url=server_url,
        server_socket=server_socket,
    )
    if settings is not None:
        return PrincipalKey(
            principal_id,
            Path(settings.private_key_path),
            "remembered_settings",
            settings_path=settings.path,
            token=settings.token,
        )
    if folder is not None and server_url is None:
        # Lazy: the folder record's module imports the signer.
        from cruxible_client.authoring.folder_identity import (
            FolderIdentityRefused,
            read_bound_folder_identity,
        )

        try:
            record = read_bound_folder_identity(folder, instance_id=instance_id)
        except FolderIdentityRefused:
            record = None
        if record is not None and record.principal_id == principal_id:
            return PrincipalKey(principal_id, record.private_key_path, "folder")
    default = default_key_dir(instance_id, principal_id, env) / f"{principal_id}.ed25519"
    if default.is_file():
        return PrincipalKey(principal_id, default, "default_key_dir")
    return None


PrincipalKeyClient = Literal["cli", "sdk", "mcp"]


def principal_key_missing(principal_id: str, *, client: PrincipalKeyClient) -> str:
    """No key signs as ``principal_id`` here: the repairs that work on ``client``."""

    lead = f"no signing key for principal {principal_id} here"
    source = "`set -a; . DIR/cruxible.env; set +a` (the settings file beside its key)"
    if client == "cli":
        return (
            f"{lead}; repair: `cruxible context use --principal {principal_id}` where its "
            f"settings are remembered, or load them with {source}, or pass --key FILE; "
            "`cruxible principal add ID --key-dir DIR` makes a new principal and its key"
        )
    if client == "sdk":
        return (
            f"{lead}; repair: pass cx.signer({principal_id!r}, key=FILE), or start this "
            f"process with its settings loaded: {source}"
        )
    return (
        f"{lead}; repair: start this MCP server with {principal_id}'s settings in its "
        f"environment ({PRINCIPAL_ID_ENV} and {PRINCIPAL_KEY_ENV}, as its cruxible.env sets "
        "them, in the env block of the MCP client config), or set CRUXIBLE_MCP_KEY_DIR to "
        f"the directory holding {principal_id}.ed25519. A remote signer uses "
        "cruxible_proposal_approve_prepare and cruxible_proposal_approve_submit (profile full)"
    )


__all__ = [
    "PRINCIPAL_TOKEN_ENV",
    "PrincipalKey",
    "PrincipalKeyClient",
    "PrincipalKeySource",
    "PrincipalSettings",
    "default_key_dir",
    "principal_key",
    "principal_key_missing",
    "read_principal_settings",
    "remembered_context",
    "remembered_principal_settings",
    "remembered_settings_of",
]
