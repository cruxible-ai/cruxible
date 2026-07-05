"""Load and parse YAML config files into CoreConfig models."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from cruxible_core import __version__
from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import ConfigError


@dataclass
class _ParsedConfig:
    data: dict[str, Any]
    all_adjacent_queries: dict[str, dict[str, Any]] = field(default_factory=dict)


def load_config(source: str | Path) -> CoreConfig:
    """Load a CoreConfig from a YAML file path or raw YAML string.

    An overlay kit entry config (a file next to a ``cruxible-kit.yaml`` with
    ``role: overlay``) is validated as a partial layer: cross-layer reference
    checks are deferred to post-compose validation, since the base layer is
    declared by the manifest's ``target_state`` rather than an ``extends`` line.

    Args:
        source: Path to a YAML file, or a raw YAML string.

    Returns:
        Validated CoreConfig instance.

    Raises:
        ConfigError: If the file can't be read or YAML is invalid.
    """
    source_path = _resolve_source_path(source)
    raw_yaml = _read_source(source)
    return _validate_config(
        _parse_config_yaml(raw_yaml),
        partial_layer=_is_overlay_kit_entry_config(source_path),
    )


def load_config_from_string(yaml_str: str, *, partial_layer: bool = False) -> CoreConfig:
    """Load a CoreConfig from a raw YAML string.

    Unlike :func:`load_config`, this bypasses the ``_read_source()``
    heuristic and treats *yaml_str* as literal YAML content — never
    as a file path.

    Args:
        yaml_str: Raw YAML string.
        partial_layer: Validate as one layer of a composition (an overlay kit
            entry config uploaded from a kit workspace), deferring cross-layer
            reference checks to post-compose validation.

    Returns:
        Validated CoreConfig instance.

    Raises:
        ConfigError: If the YAML is invalid or fails validation.
    """
    return _validate_config(_parse_config_yaml(yaml_str), partial_layer=partial_layer)


def _parse_config_yaml(raw_yaml: str) -> _ParsedConfig:
    """Parse config YAML, expanding the compact authoring grammar if present.

    The compact form (kits authored as ``config.yaml``) expands deterministically to
    the explicit ``CoreConfig`` shape. Expansion runs from the raw TEXT (relationship
    descriptions are carried as trailing comments), so there is no separate committed
    expanded artifact -- the compact source is the single source of truth and the
    explicit form exists only transiently in memory. Explicit configs are untouched.
    """
    data = _parse_yaml(raw_yaml)
    # Imported lazily: compact.py imports the schema, and this keeps loader import-light.
    from cruxible_core.config.compact import (
        expand_compact_full,
        looks_compact,
        materialize_all_adjacent_queries,
    )

    if looks_compact(data):
        expanded = expand_compact_full(raw_yaml)
        materialized = materialize_all_adjacent_queries(
            expanded.config,
            expanded.all_adjacent_queries,
        )
        return _ParsedConfig(
            data=materialized,
            all_adjacent_queries=expanded.all_adjacent_queries,
        )
    return _ParsedConfig(data=data)


def _resolve_source_path(source: str | Path) -> Path | None:
    """Return the file path a config source refers to, or None for raw YAML."""
    if isinstance(source, Path) or (
        isinstance(source, str)
        and not source.strip().startswith(("{", "version", "#"))
        and "\n" not in source
        and Path(source).suffix in (".yaml", ".yml")
    ):
        return Path(source)
    return None


def _read_source(source: str | Path) -> str:
    """Read YAML content from a file path or return raw string."""
    path = _resolve_source_path(source)
    if path is not None:
        if not path.exists():
            raise ConfigError(f"Config file not found: {path}")
        try:
            return path.read_text()
        except OSError as e:
            raise ConfigError(f"Failed to read config file: {e}") from e

    assert isinstance(source, str)
    return source


def _is_overlay_kit_entry_config(path: Path | None) -> bool:
    """Return whether a config path is the entry config of an overlay kit directory."""
    if path is None:
        return False
    # Imported lazily to keep the loader import-light.
    from cruxible_core.kits import KIT_MANIFEST_FILE, load_kit_manifest

    resolved = path.resolve()
    kit_dir = resolved.parent
    if not (kit_dir / KIT_MANIFEST_FILE).exists():
        return False
    try:
        manifest = load_kit_manifest(kit_dir)
    except ConfigError:
        return False
    return manifest.role == "overlay" and (kit_dir / manifest.entry_config).resolve() == resolved


def _parse_yaml(raw: str) -> dict[str, Any]:
    """Parse YAML string into a dict."""
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML: {e}") from e

    if not isinstance(data, dict):
        raise ConfigError("Config YAML must be a mapping at the top level")

    return data


def save_config(config: CoreConfig, path: str | Path) -> None:
    """Serialize a CoreConfig to YAML and write to disk atomically."""
    stamped = config.model_copy(update={"cruxible_version": __version__})
    path = Path(path)
    data = stamped.model_dump(mode="python", by_alias=True, exclude_none=True)
    yaml_str = yaml.safe_dump(data, default_flow_style=False, sort_keys=False)
    try:
        fd = tempfile.NamedTemporaryFile(
            mode="w",
            dir=path.parent,
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        )
        tmp_path = Path(fd.name)
        try:
            with fd:
                fd.write(yaml_str)
                fd.flush()
            tmp_path.replace(path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
    except OSError as e:
        raise ConfigError(f"Failed to write config file: {e}") from e


def _validate_config(parsed: _ParsedConfig, *, partial_layer: bool = False) -> CoreConfig:
    """Validate parsed YAML data against CoreConfig schema."""
    try:
        config = CoreConfig.model_validate(
            parsed.data,
            context={"partial_layer": True} if partial_layer else None,
        )
        config._compact_all_adjacent_queries = parsed.all_adjacent_queries
        return config
    except ValidationError as e:
        errors = [
            f"{' → '.join(str(p) for p in err['loc'])}: {err['msg']}"
            if err.get("loc")
            else err["msg"]
            for err in e.errors()
        ]
        raise ConfigError(
            f"Config validation failed with {len(errors)} error(s)",
            errors=errors,
        ) from e
