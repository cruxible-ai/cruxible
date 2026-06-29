"""Explicit base+overlay config composition for release-backed local instances."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cruxible_core.config.loader import load_config, save_config
from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import ConfigError

_SAFE_LIST_KEYS = {"constraints", "quality_checks", "tests"}
_KEYED_MAP_KEYS = {
    "entity_types",
    "named_queries",
    "contracts",
    "artifacts",
    "providers",
    "workflows",
}


def compose_configs(base: CoreConfig, overlay: CoreConfig) -> CoreConfig:
    """Compose a base config and overlay using strict append-only semantics."""
    base_data = base.model_dump(mode="python", by_alias=True, exclude_none=True)
    overlay_data = overlay.model_dump(mode="python", by_alias=True, exclude_none=True)
    composed = _compose_mapping(base_data, overlay_data)
    composed.pop("extends", None)
    return CoreConfig.model_validate(composed)


def write_composed_config(
    *,
    base_path: Path,
    overlay_path: Path,
    output_path: Path,
) -> CoreConfig:
    """Compose base+overlay configs and materialize the merged output on disk."""
    base = load_config(base_path)
    overlay = load_config(overlay_path)
    composed = compose_configs(base, overlay)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    save_config(composed, output_path)
    return composed


def compose_config_files(
    *,
    base_path: Path,
    overlay_path: Path,
) -> CoreConfig:
    """Compose two config files without writing the merged result to disk."""
    base = load_config(base_path)
    overlay = load_config(overlay_path)
    return compose_configs(base, overlay)


def _compose_mapping(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, overlay_value in overlay.items():
        if key == "extends":
            result[key] = overlay_value
            continue
        if key in {"name", "description"}:
            result[key] = overlay_value
            continue
        if key in _SAFE_LIST_KEYS:
            result[key] = list(base.get(key, [])) + list(overlay_value)
            continue
        if key == "relationships":
            result[key] = _compose_relationships(base.get(key, []), overlay_value)
            continue
        if key in _KEYED_MAP_KEYS:
            base_map = dict(base.get(key, {}))
            for child_key, child_value in overlay_value.items():
                if child_key in base_map:
                    raise ConfigError(
                        f"Overlay cannot redefine upstream '{key}' entry '{child_key}'"
                    )
                base_map[child_key] = child_value
            result[key] = base_map
            continue
        if key in result and result[key] != overlay_value:
            raise ConfigError(f"Overlay cannot override upstream field '{key}'")
        result[key] = overlay_value
    return result


def _compose_relationships(
    base_rels: list[dict[str, Any]],
    overlay_rels: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    names = {rel["name"] for rel in base_rels}
    merged = list(base_rels)
    for rel in overlay_rels:
        if rel["name"] in names:
            raise ConfigError(f"Overlay cannot redefine upstream relationship '{rel['name']}'")
        merged.append(rel)
    return merged
