"""Result envelopes owned by the stdio MCP adapter rather than the daemon."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from cruxible_client import contracts


class McpWhoAmIResult(BaseModel):
    """The instance this adapter acts on and the caller's identity there."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["cruxible-mcp-whoami-v1"] = "cruxible-mcp-whoami-v1"
    instance_id: str
    adapter_version: str
    daemon_version: str
    identity: contracts.WhoAmI
    #: Where this server's principal came from: its environment, the folder's
    #: record (`.cruxible/identity.json`, written by `cruxible init`), or none.
    identity_source: Literal["environment", "folder", "none"]


class McpServerInfoResult(BaseModel):
    """Daemon metadata for a daemon-scope caller, one instance's for a scoped one."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["cruxible-mcp-server-info-v1"] = "cruxible-mcp-server-info-v1"
    scope: Literal["daemon", "instance"]
    instance_id: str | None
    adapter_version: str
    daemon_version: str
    daemon: contracts.ServerInfoResult | None = None
    host: contracts.HostInspection | None = None
    identity: contracts.WhoAmI | None = None

    @model_validator(mode="after")
    def _scope_fields(self) -> McpServerInfoResult:
        if self.scope == "daemon" and self.daemon is None:
            raise ValueError("a daemon-scope answer carries the daemon metadata")
        if self.scope == "instance" and (
            self.daemon is not None or self.instance_id is None or self.identity is None
        ):
            raise ValueError("an instance-scope answer carries its instance and identity only")
        return self


class McpInitResult(BaseModel):
    """What `cruxible_init` made (or found) for the workspace folder; never a key."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["cruxible-mcp-init-v1"] = "cruxible-mcp-init-v1"
    status: Literal["initialized", "already_initialized"]
    instance_id: str
    #: The owner this folder's MCP server and CLI act as (its identity record);
    #: None for a folder initialized before records existed.
    principal_id: str | None
    #: The owner's key directory, outside the project; the key never leaves it.
    key_dir: str | None
    approval_policy: Literal["self_approval_allowed", "independent_approval_required"] | None
    workspace_root: str
    folder_identity_path: str | None


__all__ = ["McpInitResult", "McpServerInfoResult", "McpWhoAmIResult"]
