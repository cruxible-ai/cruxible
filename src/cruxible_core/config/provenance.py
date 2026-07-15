"""Config source provenance and materialized-integrity helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from cruxible_core.config.composer import (
    ResolvedConfigLayer,
    compose_config_sequence,
    resolve_config_layers,
)
from cruxible_core.config.loader import load_config
from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import ConfigError
from cruxible_core.temporal import format_datetime, utc_now


class ConfigSourceDigest(BaseModel):
    """Exact digest of one authored file in a composed config chain."""

    model_config = ConfigDict(extra="forbid")

    path: str
    digest: str


class ConfigSourceManifest(BaseModel):
    """Caller-visible source inputs that produced a composed config."""

    model_config = ConfigDict(extra="forbid")

    root_path: str | None = None
    layers: list[ConfigSourceDigest] = Field(default_factory=list)
    composed_digest: str


class ConfigProvenanceMetadata(ConfigSourceManifest):
    """Source manifest plus the exact materialized file recorded by an instance."""

    active_config_digest: str
    materialized_digest: str
    recorded_at: str


def compute_bytes_digest(content: bytes) -> str:
    """Return the canonical SHA-256 label for exact bytes."""
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def compute_file_digest(path: Path) -> str:
    """Hash one file exactly, surfacing filesystem failures as config errors."""
    try:
        return compute_bytes_digest(path.read_bytes())
    except OSError as exc:
        raise ConfigError(f"Failed to read config source {path}: {exc}") from exc


def compute_composed_config_digest(config: CoreConfig) -> str:
    """Hash effective config semantics while ignoring the runtime version stamp."""
    unversioned = config.model_copy(update={"cruxible_version": None})
    dumped = json.dumps(
        unversioned.model_dump(mode="python", by_alias=True, exclude_none=True),
        sort_keys=True,
        default=str,
    )
    return compute_bytes_digest(dumped.encode())


def source_manifest_for_layers(
    layers: list[ResolvedConfigLayer],
    composed: CoreConfig,
    *,
    root_path: Path | None,
) -> ConfigSourceManifest:
    """Build provenance for all file-backed layers in one composition."""
    source_layers = [
        ConfigSourceDigest(
            path=str(layer.config_path), digest=compute_file_digest(layer.config_path)
        )
        for layer in layers
        if layer.config_path is not None
    ]
    return ConfigSourceManifest(
        root_path=str(root_path.resolve()) if root_path is not None else None,
        layers=source_layers,
        composed_digest=compute_composed_config_digest(composed),
    )


def compose_file_with_source_manifest(
    config_path: str | Path,
) -> tuple[CoreConfig, ConfigSourceManifest]:
    """Compose one authored root and describe every resolved source file."""
    root_path = Path(config_path).expanduser().resolve()
    config = load_config(root_path)
    layers = resolve_config_layers(config, config_path=root_path)
    composed = compose_config_sequence(layers)
    return composed, source_manifest_for_layers(layers, composed, root_path=root_path)


def record_materialized_provenance(
    source: ConfigSourceManifest,
    materialized_path: Path,
) -> ConfigProvenanceMetadata:
    """Bind source provenance to the exact bytes currently active."""
    recorded_at = format_datetime(utc_now())
    assert recorded_at is not None
    return ConfigProvenanceMetadata(
        **source.model_dump(mode="python"),
        active_config_digest=compute_composed_config_digest(load_config(materialized_path)),
        materialized_digest=compute_file_digest(materialized_path),
        recorded_at=recorded_at,
    )


def materialized_header(source_label: str | None) -> str:
    """Return the warning header stamped on generated active config files."""
    label = (source_label or "inline configuration").replace("\n", " ").replace("\r", " ")
    return f"MATERIALIZED - DO NOT EDIT\nSource: {label}"
