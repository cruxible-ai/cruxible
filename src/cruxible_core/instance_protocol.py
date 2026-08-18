"""Abstract base classes for instance and store interfaces.

Enables future cloud backends (e.g. CloudInstance backed by R2/D1)
without coupling handlers to concrete SQLite implementations.
Concrete stores must inherit from these ABCs — Python enforces the
contract at class-definition time.
"""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, field_validator

from cruxible_core.errors import ConfigError
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.temporal import utc_now

if TYPE_CHECKING:
    from cruxible_core.config.provenance import ConfigProvenanceMetadata
    from cruxible_core.config.schema import CoreConfig
    from cruxible_core.graph.entity_graph import EntityGraph
    from cruxible_core.graph.types import EntityInstance, RelationshipInstance
    from cruxible_core.storage.protocols import UnitOfWorkProtocol


_RELEASE_ID_PATTERN = re.compile(r"[a-zA-Z0-9._-]+")
UpstreamMember = Literal["manifest.json", "graph.json", "config.yaml", "cruxible.lock.yaml"]
ALL_UPSTREAM_MEMBERS: tuple[UpstreamMember, ...] = (
    "manifest.json",
    "graph.json",
    "config.yaml",
    "cruxible.lock.yaml",
)
_UPSTREAM_MEMBER_FIELDS: dict[UpstreamMember, tuple[str, str]] = {
    "manifest.json": ("manifest_path", "manifest_digest"),
    "graph.json": ("graph_path", "graph_digest"),
    "config.yaml": ("upstream_config_path", "upstream_config_digest"),
    "cruxible.lock.yaml": ("lock_path", "upstream_lock_digest"),
}


def _validate_path_safe_id(value: str, field_name: str) -> str:
    if (
        not _RELEASE_ID_PATTERN.fullmatch(value)
        or value in {"", ".", ".."}
        or value.startswith(".")
    ):
        raise ValueError(f"{field_name} must match [a-zA-Z0-9._-]+ and cannot be dot-relative")
    return value


class StateSnapshot(BaseModel):
    """Temporary immutable-state model retained by the donor parity harness."""

    snapshot_id: str
    created_at: datetime = Field(default_factory=utc_now)
    label: str | None = None
    config_digest: str
    lock_digest: str | None = None
    graph_digest: str
    parent_snapshot_id: str | None = None
    origin_snapshot_id: str | None = None
    actor_context: GovernedActorContext | None = None


class UpstreamMetadata(BaseModel):
    """Temporary overlay metadata retained by legacy donor tests."""

    format_version: int = 1
    state_id: str
    release_id: str
    snapshot_id: str
    compatibility: Literal["data_only", "additive_schema", "breaking"]
    owned_entity_types: list[str] = Field(default_factory=list)
    owned_relationship_types: list[str] = Field(default_factory=list)
    parent_release_id: str | None = None
    bundle_format_version: int | None = None
    members_digest: str | None = None
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

    @field_validator("state_id")
    @classmethod
    def validate_state_id(cls, value: str) -> str:
        return _validate_path_safe_id(value, "state_id")

    @field_validator("release_id")
    @classmethod
    def validate_release_id(cls, value: str) -> str:
        return _validate_path_safe_id(value, "release_id")


def sha256_file(path: Path) -> str | None:
    """Return the donor file's sha256 commitment, or ``None`` when absent."""

    if not path.exists():
        return None
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def verify_tracked_upstream(
    root: Path,
    upstream: UpstreamMetadata,
    *,
    members: tuple[UpstreamMember, ...] = ALL_UPSTREAM_MEMBERS,
) -> None:
    """Preserve exact-byte verification while overlay donors remain."""

    for member in members:
        path_field, digest_field = _UPSTREAM_MEMBER_FIELDS[member]
        expected = getattr(upstream, digest_field)
        if expected is None:
            continue
        relative_path = getattr(upstream, path_field)
        path = root / relative_path
        if not path.exists():
            raise ConfigError(
                f"Tracked upstream release {upstream.state_id}:{upstream.release_id} is "
                f"missing its materialized '{member}' at {relative_path}, which upstream "
                f"tracking pins at {expected}. Re-pull the release in REPAIR mode "
                "(`cruxible state pull-preview --repair` then "
                "`cruxible state pull-apply --repair --apply-digest ...`) or re-create the "
                "overlay from the published release; nothing may be read from a missing "
                "upstream. Repair preserves claim ids -- a plain re-pull of the release "
                "already tracked is refused as a no-op."
            )
        actual = sha256_file(path)
        if actual != expected:
            raise ConfigError(
                f"Tracked upstream release {upstream.state_id}:{upstream.release_id} no "
                f"longer matches its recorded '{member}' digest: expected {expected}, "
                f"found {actual} at {relative_path}. The materialized upstream was edited "
                "locally, and pulled state must stay byte-identical to what was published. "
                "Restore the file from the published release -- re-pull it in REPAIR mode "
                "(`cruxible state pull-preview --repair` then "
                "`cruxible state pull-apply --repair --apply-digest ...`) or re-create the "
                "overlay -- then retry. Repair preserves claim ids; a plain re-pull of the "
                "release already tracked is refused as a no-op."
            )


class InstanceProtocol(ABC):
    """Interface for a cruxible instance."""

    @abstractmethod
    def get_root_path(self) -> Path: ...
    @abstractmethod
    def get_instance_dir(self) -> Path: ...
    @abstractmethod
    def get_config_path(self) -> Path: ...
    @abstractmethod
    def set_config_path(self, config_path: str) -> None: ...
    @abstractmethod
    def load_config(self) -> CoreConfig: ...
    @abstractmethod
    def save_config(self, config: CoreConfig) -> None: ...
    def get_config_provenance(self) -> ConfigProvenanceMetadata | None:
        """Return config provenance when the instance implementation supports it."""
        return None

    def set_config_provenance(self, provenance: ConfigProvenanceMetadata | None) -> None:
        """Persist config provenance when supported by the instance implementation."""
        raise NotImplementedError

    def verify_config_integrity(self) -> None:
        """Verify materialized config integrity when supported."""
        return None

    @abstractmethod
    def load_graph(self) -> EntityGraph: ...
    @abstractmethod
    def save_graph(self, graph: EntityGraph) -> None: ...
    @abstractmethod
    def save_graph_delta(
        self,
        graph: EntityGraph,
        *,
        entities: Sequence[EntityInstance] = (),
        relationships: Sequence[RelationshipInstance] = (),
    ) -> None: ...
    @abstractmethod
    def invalidate_graph_cache(self) -> None: ...
    @abstractmethod
    def write_transaction(self) -> AbstractContextManager[UnitOfWorkProtocol]: ...
    @abstractmethod
    def active_unit_of_work(self) -> UnitOfWorkProtocol | None:
        """Return the currently open write boundary, or None outside one.

        A caller that must write ATOMICALLY with the surrounding write — the
        resolution-contract activation is the first such case — needs to know
        whether it is inside someone else's transaction rather than able to
        open (and independently commit) its own.
        """
        ...

    @abstractmethod
    def get_head_snapshot_id(self) -> str | None: ...
    @abstractmethod
    def get_read_revision(self) -> int: ...

    def get_instance_state(self, key: str) -> Any | None:
        """Read a raw ``instance_state`` value, or None when unsupported.

        DELIBERATELY NOT ABSTRACT. Adding an abstract method to a published
        protocol breaks every embedded implementor at import time, and this one
        arrived for a single internal reader -- the legacy claim-identity
        reconcile map on the pull path -- which already treats an absent map as
        an empty one. A default of ``None`` therefore degrades exactly the way
        that caller is written to expect (a pre-identity upstream's ids get
        re-minted, as they would have been anyway on an instance that never
        stored a map) rather than turning a missing optional accessor into an
        import-time failure for code that never asked for this feature.

        An implementor that DOES persist instance state should override it:
        without the override the reconcile map can be written and never read
        back, and the churn it exists to bound returns.
        """
        return None

    def get_origin_snapshot_id(self) -> str | None:
        """Return the clone-lineage origin snapshot id, or None.

        Origin is CLONE provenance, not "where I started": the only writer of a
        non-None value is ``clone_from_snapshot``, so on an init-created
        instance it is None forever. Promoted to the protocol because ``origin``
        survives as a named ``state diff`` coordinate even though it is not the
        bare default.

        DELIBERATELY NOT ABSTRACT, for the reason spelled out on
        ``get_instance_state``: adding an abstract method to a published
        protocol breaks every embedded implementor at import time. The default
        degrades to "this instance has no clone origin", which is exactly what
        the ``origin`` coordinate's refusal already says.
        """
        return None

    def get_snapshot_artifact(self, snapshot_id: str, artifact_name: str) -> bytes | None:
        """Return one stored snapshot artifact's exact bytes, or None.

        Generic on purpose, not graph-only: ``state diff`` reads ``graph.json``,
        ``procedures.json``, and ``upstream.json`` through this one accessor.
        It bridges the storage-repository-level ``get_snapshot_artifact`` up to
        the instance protocol. Non-abstract for the ``get_instance_state``
        reason; the default makes every snapshot coordinate refuse with the
        named missing-member message rather than failing at import.
        """
        return None

    @abstractmethod
    def get_upstream_metadata(self) -> UpstreamMetadata | None: ...
    @abstractmethod
    def set_upstream_metadata(self, metadata: UpstreamMetadata | None) -> None: ...
    @abstractmethod
    def create_snapshot(
        self,
        label: str | None = None,
        *,
        actor_context: GovernedActorContext | None = None,
    ) -> StateSnapshot: ...
    @abstractmethod
    def commit_graph_snapshot(
        self,
        graph: EntityGraph,
        label: str | None = None,
        *,
        entities: Sequence[EntityInstance] | None = None,
        relationships: Sequence[RelationshipInstance] | None = None,
        actor_context: GovernedActorContext | None = None,
    ) -> StateSnapshot: ...
    @abstractmethod
    def get_snapshot(self, snapshot_id: str) -> StateSnapshot | None: ...
    @abstractmethod
    def list_snapshots(self) -> list[StateSnapshot]: ...
