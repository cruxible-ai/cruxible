"""Snapshot and release metadata types for immutable world-model state."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

_RELEASE_ID_PATTERN = re.compile(r"[a-zA-Z0-9._-]+")

WorldCompatibility = Literal["data_only", "additive_schema", "breaking"]
"""Compatibility class between a published release and its predecessors.

- ``data_only``: graph data changes only; no schema changes.
- ``additive_schema``: schema additions that are backward-compatible.
- ``breaking``: schema changes that require overlay action.
"""


def _validate_path_safe_id(value: str, field_name: str) -> str:
    if (
        not _RELEASE_ID_PATTERN.fullmatch(value)
        or value in {"", ".", ".."}
        or value.startswith(".")
    ):
        raise ValueError(f"{field_name} must match [a-zA-Z0-9._-]+ and cannot be dot-relative")
    return value


class WorldSnapshot(BaseModel):
    """Immutable local snapshot of graph state and build lineage."""

    snapshot_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    label: str | None = None
    config_digest: str
    lock_digest: str | None = None
    graph_digest: str
    parent_snapshot_id: str | None = None
    origin_snapshot_id: str | None = None


class PublishedWorldManifest(BaseModel):
    """Distribution metadata for a published world release bundle."""

    format_version: int = 1
    world_id: str
    release_id: str
    snapshot_id: str
    compatibility: WorldCompatibility
    owned_entity_types: list[str] = Field(default_factory=list)
    owned_relationship_types: list[str] = Field(default_factory=list)
    parent_release_id: str | None = None

    @field_validator("world_id")
    @classmethod
    def validate_world_id(cls, value: str) -> str:
        return _validate_path_safe_id(value, "world_id")

    @field_validator("release_id")
    @classmethod
    def validate_release_id(cls, value: str) -> str:
        return _validate_path_safe_id(value, "release_id")


class UpstreamMetadata(PublishedWorldManifest):
    """Per-instance upstream release tracking metadata for pullable overlays.

    Extends ``PublishedWorldManifest`` with transport and local-path
    bookkeeping. The manifest fields record what was pulled; the rest
    tracks how it was fetched and where it lives on disk.
    """

    transport_ref: str
    requested_source_ref: str | None = None
    requested_transport_ref: str | None = None
    overlay_config_path: str = "config.yaml"
    manifest_path: str = ".cruxible/upstream/current/manifest.json"
    graph_path: str = ".cruxible/upstream/current/graph.json"
    upstream_config_path: str = ".cruxible/upstream/current/config.yaml"
    lock_path: str = ".cruxible/upstream/current/cruxible.lock.yaml"
    manifest_digest: str | None = None
    graph_digest: str | None = None
