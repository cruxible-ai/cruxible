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


__all__ = ["McpServerInfoResult", "McpWhoAmIResult"]
