"""Snapshot and release metadata types for immutable state."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.temporal import utc_now

_RELEASE_ID_PATTERN = re.compile(r"[a-zA-Z0-9._-]+")

INSTANCE_BACKUP_FORMAT_VERSION = 2
"""Current instance-backup format.

Bumped to 2 by edge identity (storage migration 0004): the backed-up state
database's ``graph_relationships`` table is keyed by ``claim_id`` and no longer
carries the derived ``relationship_id`` column.
"""

StateCompatibility = Literal["data_only", "additive_schema", "breaking"]
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


class StateSnapshot(BaseModel):
    """Immutable local snapshot of graph state and build lineage."""

    snapshot_id: str
    created_at: datetime = Field(default_factory=utc_now)
    label: str | None = None
    config_digest: str
    lock_digest: str | None = None
    graph_digest: str
    parent_snapshot_id: str | None = None
    origin_snapshot_id: str | None = None
    actor_context: GovernedActorContext | None = None


class InstanceBackupManifest(BaseModel):
    """Portable same-identity instance backup metadata.

    ``format_version`` is the version of THIS manifest shape.
    ``min_reader_format_version`` is the harder promise: the lowest format a
    reader must understand to install the payload safely. They came apart at
    edge identity, because a post-0004 ``state.db`` cannot be read by a build
    that still expects ``graph_relationships.relationship_id`` -- that column is
    gone. Declaring the minimum lets ``restore`` refuse BEFORE installation,
    with a version message, instead of installing the database and failing later
    on a missing column with an opaque SQL error.
    """

    format_version: int = INSTANCE_BACKUP_FORMAT_VERSION
    min_reader_format_version: int = INSTANCE_BACKUP_FORMAT_VERSION
    instance_id: str
    created_at: datetime = Field(default_factory=utc_now)
    cruxible_version: str
    label: str | None = None
    original_config_path: str
    restored_config_path: str = "config.yaml"
    instance_mode: str
    artifacts: dict[str, str] = Field(default_factory=dict)


class PublishedStateManifest(BaseModel):
    """Distribution metadata for a published state release bundle.

    ``bundle_format_version`` and ``members_digest`` are the bundle's
    non-downgradable member contract. A publisher that emits the per-member
    digest sidecar records both here, in the one member every consumer reads
    first, so *removing* the sidecar is detectable: a manifest declaring the
    format while the sidecar is absent or contradicts ``members_digest`` is
    refused. Both are ``None`` on bundles published before the sidecar existed,
    which keeps those on the older partial-verification story rather than
    failing them closed.

    They establish integrity relative to the manifest, not authenticity: an
    attacker who can rewrite the whole bundle in transit can rewrite the
    manifest too. Authenticity needs signing, which is future work; the
    transport and the first pull are the trust boundary today.
    """

    format_version: int = 1
    state_id: str
    release_id: str
    snapshot_id: str
    compatibility: StateCompatibility
    owned_entity_types: list[str] = Field(default_factory=list)
    owned_relationship_types: list[str] = Field(default_factory=list)
    parent_release_id: str | None = None
    bundle_format_version: int | None = None
    members_digest: str | None = None

    @field_validator("state_id")
    @classmethod
    def validate_state_id(cls, value: str) -> str:
        return _validate_path_safe_id(value, "state_id")

    @field_validator("release_id")
    @classmethod
    def validate_release_id(cls, value: str) -> str:
        return _validate_path_safe_id(value, "release_id")


class UpstreamMetadata(PublishedStateManifest):
    """Per-instance upstream release tracking metadata for pullable overlays.

    Extends ``PublishedStateManifest`` with transport and local-path
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
    upstream_config_digest: str | None = None
    upstream_lock_digest: str | None = None
    identity_map_digest: str | None = None
    """Digest of the normalized LEGACY claim-identity reconcile map.

    Only pre-identity upstream releases need entries in that map (post-upgrade
    releases carry their own ids), so this stays the empty-map digest for a
    modern upstream. Its job is to make id churn VISIBLE: two pulls of the same
    release whose upstream identities moved differ here even though every
    content digest is byte-identical.
    """
