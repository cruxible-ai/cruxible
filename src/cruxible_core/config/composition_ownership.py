"""Derived ownership view for composed world-model configs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from cruxible_core.config.composer import (
    compose_config_sequence,
    compose_runtime_configs,
    resolve_config_layers,
)
from cruxible_core.config.loader import load_config
from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import ConfigError
from cruxible_core.instance_protocol import InstanceProtocol

OwnershipSource = Literal["upstream_metadata", "extends", "unavailable"]


@dataclass(frozen=True)
class CompositionOwnershipView:
    """Type/name ownership inferred from runtime composition metadata."""

    source: OwnershipSource
    upstream_entity_types: set[str] = field(default_factory=set)
    local_entity_types: set[str] = field(default_factory=set)
    upstream_relationship_types: set[str] = field(default_factory=set)
    local_relationship_types: set[str] = field(default_factory=set)
    upstream_named_queries: set[str] = field(default_factory=set)
    local_named_queries: set[str] = field(default_factory=set)
    upstream_workflows: set[str] = field(default_factory=set)
    local_workflows: set[str] = field(default_factory=set)
    upstream_providers: set[str] = field(default_factory=set)
    local_providers: set[str] = field(default_factory=set)
    surface_ownership_available: bool = False

    @property
    def ownership_available(self) -> bool:
        return self.source != "unavailable"

    def is_local_entity_type(self, entity_type: str) -> bool:
        return entity_type in self.local_entity_types

    def is_upstream_entity_type(self, entity_type: str) -> bool:
        return entity_type in self.upstream_entity_types

    def is_local_relationship_type(self, relationship_type: str) -> bool:
        return relationship_type in self.local_relationship_types

    def is_upstream_relationship_type(self, relationship_type: str) -> bool:
        return relationship_type in self.upstream_relationship_types


@dataclass(frozen=True)
class CompositionResolution:
    """Composed config plus ownership metadata for read/render surfaces."""

    config: CoreConfig
    ownership: CompositionOwnershipView


def resolve_composition_for_instance(instance: InstanceProtocol) -> CompositionResolution:
    """Resolve the config used for rendering plus derived ownership metadata."""
    active_config = instance.load_config()
    active_path = instance.get_config_path()

    upstream = instance.get_upstream_metadata()
    if upstream is not None:
        composed_config = _compose_from_upstream_metadata(instance, fallback_config=active_config)
        ownership = _ownership_from_upstream_metadata(
            instance,
            config=composed_config,
        )
        return CompositionResolution(config=composed_config, ownership=ownership)

    composed_config = _compose_if_needed(active_config, active_path)
    if active_config.extends is not None:
        ownership = _ownership_from_extends(active_config, active_path, composed_config)
        return CompositionResolution(config=composed_config, ownership=ownership)

    return CompositionResolution(
        config=composed_config,
        ownership=CompositionOwnershipView(source="unavailable"),
    )


def _compose_if_needed(config: CoreConfig, config_path: Path) -> CoreConfig:
    if config.extends is None:
        return config
    return compose_config_sequence(resolve_config_layers(config, config_path=config_path.resolve()))


def _compose_from_upstream_metadata(
    instance: InstanceProtocol,
    *,
    fallback_config: CoreConfig,
) -> CoreConfig:
    upstream = instance.get_upstream_metadata()
    assert upstream is not None
    root = instance.get_root_path()

    active_path = root / upstream.active_config_path
    active_config = _try_load_config(active_path)
    if active_config is not None:
        return active_config

    upstream_config_path = root / upstream.config_path
    overlay_config_path = root / upstream.overlay_config_path
    upstream_config = _try_load_config(upstream_config_path)
    overlay_config = _try_load_config(overlay_config_path)
    if upstream_config is None or overlay_config is None:
        return fallback_config

    return compose_runtime_configs(
        upstream_config,
        overlay_config,
        base_config_path=upstream_config_path,
        overlay_config_path=overlay_config_path,
    )


def _ownership_from_upstream_metadata(
    instance: InstanceProtocol,
    *,
    config: CoreConfig,
) -> CompositionOwnershipView:
    upstream = instance.get_upstream_metadata()
    assert upstream is not None

    upstream_entity_types = set(upstream.owned_entity_types)
    upstream_relationship_types = set(upstream.owned_relationship_types)
    local_entity_types = set(config.entity_types) - upstream_entity_types
    relationship_names = {relationship.name for relationship in config.relationships}
    local_relationship_types = relationship_names - upstream_relationship_types

    surface = _surface_ownership_from_upstream_paths(instance)
    return CompositionOwnershipView(
        source="upstream_metadata",
        upstream_entity_types=upstream_entity_types,
        local_entity_types=local_entity_types,
        upstream_relationship_types=upstream_relationship_types,
        local_relationship_types=local_relationship_types,
        upstream_named_queries=surface.upstream_named_queries,
        local_named_queries=surface.local_named_queries,
        upstream_workflows=surface.upstream_workflows,
        local_workflows=surface.local_workflows,
        upstream_providers=surface.upstream_providers,
        local_providers=surface.local_providers,
        surface_ownership_available=surface.surface_ownership_available,
    )


def _ownership_from_extends(
    config: CoreConfig,
    config_path: Path,
    composed_config: CoreConfig,
) -> CompositionOwnershipView:
    layers = resolve_config_layers(config, config_path=config_path.resolve())
    upstream_layer = layers[0].config
    local_layers = [layer.config for layer in layers[1:]]

    upstream_relationship_types = {
        relationship.name for relationship in upstream_layer.relationships
    }
    local_relationship_types = {
        relationship.name for layer in local_layers for relationship in layer.relationships
    }
    upstream_entity_types = set(upstream_layer.entity_types)
    local_entity_types = set(composed_config.entity_types) - upstream_entity_types

    return CompositionOwnershipView(
        source="extends",
        upstream_entity_types=upstream_entity_types,
        local_entity_types=local_entity_types,
        upstream_relationship_types=upstream_relationship_types,
        local_relationship_types=local_relationship_types,
        upstream_named_queries=set(upstream_layer.named_queries),
        local_named_queries={
            name for layer in local_layers for name in layer.named_queries
        },
        upstream_workflows=set(upstream_layer.workflows),
        local_workflows={name for layer in local_layers for name in layer.workflows},
        upstream_providers=set(upstream_layer.providers),
        local_providers={name for layer in local_layers for name in layer.providers},
        surface_ownership_available=True,
    )


def _surface_ownership_from_upstream_paths(
    instance: InstanceProtocol,
) -> CompositionOwnershipView:
    upstream = instance.get_upstream_metadata()
    assert upstream is not None
    root = instance.get_root_path()
    upstream_config = _try_load_config(root / upstream.config_path)
    overlay_config = _try_load_config(root / upstream.overlay_config_path)
    if upstream_config is None or overlay_config is None:
        return CompositionOwnershipView(source="upstream_metadata")

    return CompositionOwnershipView(
        source="upstream_metadata",
        upstream_named_queries=set(upstream_config.named_queries),
        local_named_queries=set(overlay_config.named_queries),
        upstream_workflows=set(upstream_config.workflows),
        local_workflows=set(overlay_config.workflows),
        upstream_providers=set(upstream_config.providers),
        local_providers=set(overlay_config.providers),
        surface_ownership_available=True,
    )


def _try_load_config(path: Path) -> CoreConfig | None:
    try:
        if not path.exists():
            return None
        return load_config(path)
    except (ConfigError, OSError):
        return None
