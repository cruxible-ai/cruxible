"""The instance a stdio MCP adapter acts on when a tool call names none."""

from __future__ import annotations

from collections.abc import Mapping

from cruxible_client.authoring.context import (
    PlaybillContextResolutionError,
    resolve_playbill_context,
)
from cruxible_core.errors import ConfigError

MCP_INSTANCE_ENV = "CRUXIBLE_INSTANCE_ID"

NO_INSTANCE_MESSAGE = (
    "No Playbill instance selected: pass instance_id, or launch the MCP server with "
    f"{MCP_INSTANCE_ENV}=<instance id> in its environment (the `env` block of the MCP "
    "client config)."
)


def configured_instance_id(
    instance_id: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve explicit > environment through the shared CLI/SDK context layers.

    The adapter reads only its own environment, so remembered CLI context and
    workspace discovery never retarget it.
    """

    try:
        resolved = resolve_playbill_context(
            instance_id=instance_id,
            environ=environ,
            remembered={},
            no_workspace=True,
        )
    except PlaybillContextResolutionError as exc:
        raise ConfigError(f"{exc}. {NO_INSTANCE_MESSAGE}") from exc
    return resolved.instance_id


def require_instance_id(
    instance_id: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return the instance a tool acts on, or refuse with the repair."""

    resolved = configured_instance_id(instance_id, environ=environ)
    if resolved is None:
        raise ConfigError(NO_INSTANCE_MESSAGE)
    return resolved


__all__ = [
    "MCP_INSTANCE_ENV",
    "NO_INSTANCE_MESSAGE",
    "configured_instance_id",
    "require_instance_id",
]
