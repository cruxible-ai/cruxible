"""Kit-derived facts injected into MCP tool descriptions.

An agent meeting a Cruxible instance for the first time cannot invent the
config's vocabulary: which named queries exist, which providers a procedure may
compose, which contracts a payload must satisfy. Today those facts arrive only
by prompt enumeration outside the protocol, or by spending calls on
``cruxible_list_queries`` / ``cruxible_schema`` before the first real one. The
tool surface already reaches the agent; this module makes it carry the answers.

Two hard boundaries:

- **Descriptions only.** Tool SCHEMAS are contract surface and stay byte-stable
  whatever kit is loaded; only the prose an agent reads is parameterized.
- **Never at listing cost.** Resolution is local (a config file, or the local
  instance registry) and never contacts the daemon: ``tools/list`` must answer
  on a host with no reachable daemon, and a description is not worth breaking
  that. Every failure degrades to the static description rather than raising.
- **Only the kit actually served.** The local-instance fallback is confined to
  local mode. A remote-transport process describes nothing unless it is told
  explicitly what to describe, because the local registry on its host says
  nothing about the daemon it talks to.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from cruxible_core.config.schema import ContractSchema, CoreConfig
from cruxible_core.server.config import ServerSettings

KIT_SURFACE_CONFIG_ENV = "CRUXIBLE_MCP_KIT_CONFIG"
"""Explicit config path to describe, for topologies with no local instance."""

MAX_NAMES_IN_DESCRIPTION = 40
"""Cap on names listed for one fact, matching the error-message convention."""

MAX_CONTRACTS_WITH_FIELDS = 12
"""Contracts that get an inline field summary before the list degrades to names."""

MAX_FIELDS_PER_CONTRACT = 4
"""Fields summarized inline for one contract."""


@dataclass(frozen=True)
class KitSurface:
    """The loaded config's authoring vocabulary, as tool-description prose."""

    config_name: str
    named_queries: tuple[str, ...]
    providers: tuple[str, ...]
    contracts: tuple[tuple[str, tuple[str, ...], int], ...]

    @property
    def is_empty(self) -> bool:
        return not (self.named_queries or self.providers or self.contracts)


def summarize_kit_surface(config: CoreConfig) -> KitSurface:
    """Extract the description-worthy vocabulary from a loaded config."""
    return KitSurface(
        config_name=config.name,
        named_queries=tuple(sorted(config.named_queries)),
        providers=tuple(sorted(config.providers)),
        contracts=tuple(
            (name, _contract_field_preview(contract), len(contract.fields))
            for name, contract in sorted(config.contracts.items())
        ),
    )


def resolve_kit_surface(
    *,
    settings: ServerSettings,
    environ: Mapping[str, str] | None = None,
) -> KitSurface | None:
    """Resolve the loaded kit's surface from local state, or None.

    Explicit configuration wins in every mode, so a remote-daemon topology can
    still describe the kit it serves.

    Without it, the fallback is only sound in LOCAL mode, and *settings* is
    required rather than re-derived from the environment so a caller cannot
    resolve one transport and describe another. This process's local instance
    registry describes instances this process would serve itself; when
    ``settings.enabled`` the served state lives on another host, and any local
    record here belongs to an unrelated instance that happens to share the
    machine. Advertising its named queries and contracts would be worse than
    silence: the agent would be handed a vocabulary the daemon does not have.
    Remote mode therefore degrades to the static descriptions.

    Even in local mode only a SOLE instance is described: with more than one
    there is no "the" kit, and guessing would put another instance's vocabulary
    in front of the agent.
    """
    env = os.environ if environ is None else environ
    configured = (env.get(KIT_SURFACE_CONFIG_ENV) or "").strip()
    if configured:
        return _surface_from_config_path(Path(configured).expanduser())
    if settings.enabled:
        return None
    return _surface_from_sole_local_instance()


def describe_named_queries(surface: KitSurface) -> str | None:
    """Render the config's named queries, or None when it declares none."""
    if not surface.named_queries:
        return None
    return f"Named queries in '{surface.config_name}': {_join(surface.named_queries)}."


def describe_providers(surface: KitSurface) -> str | None:
    """Render the config's registered providers, or None when it declares none."""
    if not surface.providers:
        return None
    return f"Registered providers in '{surface.config_name}': {_join(surface.providers)}."


def describe_contracts(surface: KitSurface) -> str | None:
    """Render the config's contracts, with field previews while they stay cheap."""
    if not surface.contracts:
        return None
    entries = [
        _contract_entry(name, fields, total, index)
        for index, (name, fields, total) in enumerate(surface.contracts)
    ]
    return f"Contracts in '{surface.config_name}': {_join(entries)}."


def _contract_entry(name: str, fields: tuple[str, ...], total: int, index: int) -> str:
    if index >= MAX_CONTRACTS_WITH_FIELDS or not fields:
        return name
    shown = ", ".join(fields)
    remainder = total - len(fields)
    suffix = f", +{remainder} more" if remainder > 0 else ""
    return f"{name}({shown}{suffix})"


def _contract_field_preview(contract: ContractSchema) -> tuple[str, ...]:
    names = sorted(contract.fields)
    return tuple(names[:MAX_FIELDS_PER_CONTRACT])


def _join(names: tuple[str, ...] | list[str]) -> str:
    """Join names, truncating past the cap with a total so nothing reads complete."""
    items = list(names)
    if len(items) > MAX_NAMES_IN_DESCRIPTION:
        shown = ", ".join(items[:MAX_NAMES_IN_DESCRIPTION])
        return f"{shown}, ... ({len(items)} total; first {MAX_NAMES_IN_DESCRIPTION} shown)"
    return ", ".join(items)


def _surface_from_config_path(path: Path) -> KitSurface | None:
    try:
        from cruxible_core.config.loader import load_config

        return summarize_kit_surface(load_config(path))
    except Exception:
        # A description is never worth failing server creation over: an
        # unreadable or invalid config leaves the static text in place.
        return None


def _surface_from_sole_local_instance() -> KitSurface | None:
    try:
        from cruxible_core.runtime.instance_manager import get_manager
        from cruxible_core.server.registry import get_registry

        records = get_registry().list_instances()
        if len(records) != 1:
            return None
        instance = get_manager().get(records[0].instance_id)
        return summarize_kit_surface(instance.load_config())
    except Exception:
        return None


__all__ = [
    "KIT_SURFACE_CONFIG_ENV",
    "MAX_CONTRACTS_WITH_FIELDS",
    "MAX_FIELDS_PER_CONTRACT",
    "MAX_NAMES_IN_DESCRIPTION",
    "KitSurface",
    "describe_contracts",
    "describe_named_queries",
    "describe_providers",
    "resolve_kit_surface",
    "summarize_kit_surface",
]
