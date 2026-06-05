""".cruxible/ directory management and graph storage.

Manages the local instance directory structure:
    .cruxible/
    instance.json   - metadata (config path, data dir, version)
    state.db        - SQLite live graph, audit/governance, snapshots, and head state
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from cruxible_core import __version__
from cruxible_core.config.loader import load_config, save_config
from cruxible_core.config.schema import CoreConfig
from cruxible_core.decision.store import DecisionStore
from cruxible_core.errors import ConfigError, InstanceNotFoundError
from cruxible_core.feedback.store import FeedbackStore
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.group.store import GroupStore
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.primitives import new_id
from cruxible_core.receipt.store import SQLiteReceiptStore
from cruxible_core.snapshot.types import UpstreamMetadata, WorldSnapshot
from cruxible_core.storage.sqlite import (
    SQLiteSourceArtifactStore,
    SQLiteStorageBackend,
    SQLiteUnitOfWork,
)
from cruxible_core.temporal import format_datetime, utc_now
from cruxible_core.workflow.compiler import (
    LOCK_FILE_NAME,
    compute_lock_config_digest,
    resolve_lock_path,
)

if TYPE_CHECKING:
    from cruxible_core.storage.protocols import UnitOfWorkProtocol

logger = logging.getLogger(__name__)

InstanceMode = Literal["dev", "governed"]
_HEAD_SNAPSHOT_STATE_KEY = "head_snapshot_id"
_ORIGIN_SNAPSHOT_STATE_KEY = "origin_snapshot_id"


class InstanceMetadata(BaseModel):
    """Typed contents of ``.cruxible/instance.json``."""

    model_config = ConfigDict(extra="allow", validate_assignment=True)

    config_path: str
    data_dir: str = "."
    instance_mode: InstanceMode = "dev"
    created_at: str | None = None
    version: str | None = None
    head_snapshot_id: str | None = None
    origin_snapshot_id: str | None = None
    upstream: UpstreamMetadata | None = None


class CruxibleInstance(InstanceProtocol):
    """Manages a .cruxible/ project instance."""

    INSTANCE_DIR = ".cruxible"
    DEV_MODE = "dev"
    GOVERNED_MODE = "governed"

    def __init__(self, root: Path, metadata: InstanceMetadata | dict[str, Any]) -> None:
        self.root = root
        self.instance_dir = root / self.INSTANCE_DIR
        self.metadata = self._parse_metadata(metadata)
        self._graph_cache: EntityGraph | None = None
        self._active_uow: SQLiteUnitOfWork | None = None

    @classmethod
    def init(
        cls,
        root: Path,
        config_path: str,
        data_dir: str | None = None,
        *,
        instance_mode: str = DEV_MODE,
    ) -> CruxibleInstance:
        """Initialize a new .cruxible/ instance directory.

        Validates the config file exists and is loadable before creating
        the instance directory.
        """
        cls._validate_instance_mode(instance_mode)
        resolved_config = Path(config_path)
        if not resolved_config.is_absolute():
            resolved_config = root / resolved_config

        load_config(resolved_config)

        instance_dir = root / cls.INSTANCE_DIR
        instance_dir.mkdir(parents=True, exist_ok=True)

        metadata = InstanceMetadata(
            config_path=str(config_path),
            data_dir=data_dir or ".",
            instance_mode=cast(InstanceMode, instance_mode),
            created_at=format_datetime(utc_now()),
            version=__version__,
        )
        instance = cls(root, metadata)
        instance._write_metadata()

        instance.save_graph(EntityGraph())

        return instance

    @classmethod
    def load(cls, root: Path | None = None) -> CruxibleInstance:
        """Load an existing instance, walking up from root (or cwd) to find .cruxible/."""
        if root is None:
            root = Path.cwd()

        search = root
        while True:
            candidate = search / cls.INSTANCE_DIR / "instance.json"
            if candidate.exists():
                metadata = json.loads(candidate.read_text())
                return cls(search, metadata)
            parent = search.parent
            if parent == search:
                break
            search = parent

        raise InstanceNotFoundError(f"No .cruxible/ directory found at or above {root}")

    def load_config(self) -> CoreConfig:
        """Load the CoreConfig from the stored config path."""
        return load_config(self.get_config_path())

    def get_root_path(self) -> Path:
        """Return the instance root directory."""
        return self.root

    def get_instance_dir(self) -> Path:
        """Return the .cruxible directory for the instance."""
        return self.instance_dir

    def save_config(self, config: CoreConfig) -> None:
        """Save the CoreConfig back to the YAML file on disk."""
        save_config(config, self.get_config_path())

    def get_instance_mode(self) -> str:
        """Return the persisted instance mode for this workspace."""
        return self.metadata.instance_mode

    def is_dev_mode(self) -> bool:
        """Return whether the instance is a workspace-rooted dev instance."""
        return self.get_instance_mode() == self.DEV_MODE

    def is_governed_mode(self) -> bool:
        """Return whether the instance is a daemon-owned governed instance."""
        return self.get_instance_mode() == self.GOVERNED_MODE

    def set_config_path(self, config_path: str) -> None:
        """Update the config path recorded in instance metadata."""
        self.metadata.config_path = config_path
        self._write_metadata()

    def get_config_path(self) -> Path:
        """Return the resolved config path for the instance."""
        config_path = Path(self.metadata.config_path)
        if not config_path.is_absolute():
            config_path = self.root / config_path
        return config_path

    def load_graph(self) -> EntityGraph:
        """Load the entity graph from SQLite. Returns cached graph if available."""
        if self._graph_cache is not None:
            return self._graph_cache

        if self._active_uow is not None:
            graph = self._active_uow.graph.load_graph()
        else:
            self._ensure_state_initialized()
            with self._storage_backend().graph_repository() as repo:
                graph = repo.load_graph()

        self._graph_cache = graph
        return graph

    def save_graph(self, graph: EntityGraph) -> None:
        """Replace the live SQL graph rows with a full graph image."""
        try:
            if self._active_uow is not None:
                self._active_uow.graph.save_graph(graph)
            else:
                with self.write_transaction() as uow:
                    uow.graph.save_graph(graph)
        except Exception:
            self._graph_cache = None
            raise
        self._graph_cache = graph

    def save_graph_delta(
        self,
        graph: EntityGraph,
        *,
        entities: Sequence[EntityInstance] = (),
        relationships: Sequence[RelationshipInstance] = (),
    ) -> None:
        """Persist touched live graph rows without replacing the whole graph."""
        try:
            if self._active_uow is not None:
                self._active_uow.graph.upsert_entities(entities)
                self._active_uow.graph.upsert_relationships(relationships)
            else:
                with self.write_transaction() as uow:
                    uow.graph.upsert_entities(entities)
                    uow.graph.upsert_relationships(relationships)
        except Exception:
            self._graph_cache = None
            raise
        self._graph_cache = graph

    def invalidate_graph_cache(self) -> None:
        """Clear the in-memory graph cache, forcing next load_graph to read from disk."""
        self._graph_cache = None

    def get_head_snapshot_id(self) -> str | None:
        """Return the current head snapshot identifier, if any."""
        value = self._get_snapshot_state(_HEAD_SNAPSHOT_STATE_KEY)
        return value if isinstance(value, str) else None

    def get_upstream_metadata(self) -> UpstreamMetadata | None:
        """Return typed upstream metadata for release-backed overlay instances."""
        return self.metadata.upstream

    def set_upstream_metadata(self, metadata: UpstreamMetadata | None) -> None:
        """Persist upstream metadata for release-backed overlay instances."""
        self.metadata.upstream = metadata
        self._write_metadata()

    def _metadata_path(self) -> Path:
        return self.instance_dir / "instance.json"

    def _write_metadata(self) -> None:
        self._metadata_path().write_text(
            json.dumps(
                self.metadata.model_dump(mode="json", exclude_none=True),
                indent=2,
                sort_keys=True,
            )
        )

    def _state_db_path(self) -> Path:
        return self.instance_dir / "state.db"

    def _storage_backend(self) -> SQLiteStorageBackend:
        return SQLiteStorageBackend(self._state_db_path())

    def _ensure_state_initialized(self) -> None:
        self._storage_backend().initialize()

    @contextmanager
    def write_transaction(self) -> Iterator[UnitOfWorkProtocol]:
        """Open the authoritative instance-owned write boundary."""
        if self._active_uow is not None:
            yield self._active_uow
            return

        self._ensure_state_initialized()
        backend = self._storage_backend()
        try:
            with backend.unit_of_work() as uow:
                self._active_uow = uow
                try:
                    yield uow
                finally:
                    self._active_uow = None
        except Exception:
            self._graph_cache = None
            raise

    def _snapshots_dir(self) -> Path:
        path = self.instance_dir / "snapshots"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _snapshot_dir(self, snapshot_id: str) -> Path:
        return self._snapshots_dir() / snapshot_id

    def _get_snapshot_state(self, key: str) -> Any | None:
        if self._active_uow is not None:
            return self._active_uow.snapshots.get_instance_state(key)
        self._ensure_state_initialized()
        with self._storage_backend().snapshot_repository() as snapshots:
            return snapshots.get_instance_state(key)

    def _get_origin_snapshot_id(self) -> str | None:
        value = self._get_snapshot_state(_ORIGIN_SNAPSHOT_STATE_KEY)
        return value if isinstance(value, str) else None

    def _mirror_snapshot_state_to_metadata(
        self,
        *,
        head_snapshot_id: str,
        origin_snapshot_id: str | None,
    ) -> None:
        self.metadata.head_snapshot_id = head_snapshot_id
        self.metadata.origin_snapshot_id = origin_snapshot_id
        self._write_metadata()

    def _after_snapshot_commit(
        self,
        *,
        snapshot_id: str,
        origin_snapshot_id: str | None,
    ) -> None:
        try:
            self._mirror_snapshot_state_to_metadata(
                head_snapshot_id=snapshot_id,
                origin_snapshot_id=origin_snapshot_id,
            )
        except Exception:
            logger.warning(
                "Could not mirror DB snapshot head to instance.json",
                exc_info=True,
            )
        try:
            self._export_snapshot_artifacts(snapshot_id)
        except Exception:
            logger.warning(
                "Could not export DB-backed snapshot artifacts",
                exc_info=True,
            )

    def _read_snapshot_artifacts(self, snapshot_id: str) -> dict[str, bytes]:
        if self._active_uow is not None:
            return self._active_uow.snapshots.list_snapshot_artifacts(snapshot_id)
        self._ensure_state_initialized()
        with self._storage_backend().snapshot_repository() as snapshots:
            return snapshots.list_snapshot_artifacts(snapshot_id)

    def _export_snapshot_artifacts(self, snapshot_id: str) -> Path:
        artifacts = self._read_snapshot_artifacts(snapshot_id)
        if not artifacts:
            raise ConfigError(f"Snapshot '{snapshot_id}' has no DB-backed artifacts")
        snapshot_dir = self._snapshot_dir(snapshot_id)
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        for artifact_name, content in artifacts.items():
            (snapshot_dir / artifact_name).write_bytes(content)
        return snapshot_dir

    def create_snapshot(self, label: str | None = None) -> WorldSnapshot:
        """Persist an immutable full snapshot of the current graph + config state."""
        return self._write_snapshot(self.load_graph(), label=label, persist_live_graph=False)

    def commit_graph_snapshot(
        self,
        graph: EntityGraph,
        label: str | None = None,
        *,
        entities: Sequence[EntityInstance] | None = None,
        relationships: Sequence[RelationshipInstance] | None = None,
    ) -> WorldSnapshot:
        """Persist a snapshot for a provided graph, then atomically advance live state."""
        return self._write_snapshot(
            graph,
            label=label,
            persist_live_graph=True,
            entities=entities,
            relationships=relationships,
        )

    def _write_snapshot(
        self,
        graph: EntityGraph,
        *,
        label: str | None = None,
        persist_live_graph: bool,
        entities: Sequence[EntityInstance] | None = None,
        relationships: Sequence[RelationshipInstance] | None = None,
    ) -> WorldSnapshot:
        """Persist DB-authoritative snapshot state and export portable artifacts."""
        snapshot_id = new_id("snap", length=16, separator="_")
        config = self.load_config()
        config_path = self.get_config_path()
        graph_json = json.dumps(graph.to_dict(), indent=2, sort_keys=True).encode("utf-8")
        graph_digest = f"sha256:{hashlib.sha256(graph_json).hexdigest()}"
        artifacts: dict[str, bytes] = {
            "graph.json": graph_json,
            "config.yaml": config_path.read_bytes(),
        }

        lock_path = resolve_lock_path(self)
        lock_digest: str | None = None
        if lock_path.exists():
            lock_bytes = lock_path.read_bytes()
            lock_digest = f"sha256:{hashlib.sha256(lock_bytes).hexdigest()}"
            artifacts[LOCK_FILE_NAME] = lock_bytes

        snapshot = WorldSnapshot(
            snapshot_id=snapshot_id,
            created_at=utc_now(),
            label=label,
            config_digest=compute_lock_config_digest(config),
            lock_digest=lock_digest,
            graph_digest=graph_digest,
            parent_snapshot_id=self.get_head_snapshot_id(),
            origin_snapshot_id=self._get_origin_snapshot_id(),
        )
        artifacts["snapshot.json"] = json.dumps(
            snapshot.model_dump(mode="json"),
            indent=2,
            sort_keys=True,
        ).encode("utf-8")

        def persist_snapshot(uow: UnitOfWorkProtocol) -> None:
            if persist_live_graph and (entities is not None or relationships is not None):
                uow.graph.upsert_entities(entities or ())
                uow.graph.upsert_relationships(relationships or ())
            elif persist_live_graph:
                uow.graph.save_graph(graph)
            uow.snapshots.save_snapshot(snapshot, artifacts)
            uow.snapshots.set_instance_state(_HEAD_SNAPSHOT_STATE_KEY, snapshot_id)
            uow.snapshots.set_instance_state(
                _ORIGIN_SNAPSHOT_STATE_KEY,
                snapshot.origin_snapshot_id,
            )
            uow.register_after_commit(
                lambda: self._after_snapshot_commit(
                    snapshot_id=snapshot_id,
                    origin_snapshot_id=snapshot.origin_snapshot_id,
                )
            )

        if self._active_uow is not None:
            persist_snapshot(self._active_uow)
        else:
            with self.write_transaction() as uow:
                persist_snapshot(uow)

        if persist_live_graph:
            self._graph_cache = graph
        return snapshot

    def get_snapshot(self, snapshot_id: str) -> WorldSnapshot | None:
        """Load DB-authoritative snapshot metadata by ID."""
        if self._active_uow is not None:
            return self._active_uow.snapshots.get_snapshot(snapshot_id)
        self._ensure_state_initialized()
        with self._storage_backend().snapshot_repository() as snapshots:
            return snapshots.get_snapshot(snapshot_id)

    def list_snapshots(self) -> list[WorldSnapshot]:
        """List DB-authoritative snapshots in reverse chronological order."""
        if self._active_uow is not None:
            return self._active_uow.snapshots.list_snapshots()
        self._ensure_state_initialized()
        with self._storage_backend().snapshot_repository() as snapshots:
            return snapshots.list_snapshots()

    @classmethod
    def clone_from_snapshot(
        cls,
        source_instance: CruxibleInstance,
        snapshot_id: str,
        root_dir: str | Path,
        *,
        instance_mode: str = DEV_MODE,
    ) -> tuple[CruxibleInstance, WorldSnapshot]:
        """Create a new local instance rooted at a chosen snapshot."""
        cls._validate_instance_mode(instance_mode)
        snapshot = source_instance.get_snapshot(snapshot_id)
        if snapshot is None:
            raise ConfigError(f"Snapshot '{snapshot_id}' not found")

        root = Path(root_dir)
        instance_json = root / cls.INSTANCE_DIR / "instance.json"
        if instance_json.exists():
            raise ConfigError(f"Instance already exists at {root}")

        root.mkdir(parents=True, exist_ok=True)
        config_target = root / "config.yaml"
        if config_target.exists():
            raise ConfigError(f"config.yaml already exists at {root}")

        artifacts = source_instance._read_snapshot_artifacts(snapshot_id)
        config_bytes = artifacts.get("config.yaml")
        graph_bytes = artifacts.get("graph.json")
        if config_bytes is None or graph_bytes is None:
            raise ConfigError(f"Snapshot '{snapshot_id}' is missing required DB artifacts")

        config_target.write_bytes(config_bytes)
        instance = cls.init(root, "config.yaml", instance_mode=instance_mode)

        graph_data = json.loads(graph_bytes.decode("utf-8"))
        graph = EntityGraph.from_dict(graph_data)

        lock_bytes = artifacts.get(LOCK_FILE_NAME)
        if lock_bytes is not None:
            (instance.get_instance_dir() / LOCK_FILE_NAME).write_bytes(lock_bytes)

        origin_snapshot_id = snapshot.origin_snapshot_id or snapshot.snapshot_id
        with instance.write_transaction() as uow:
            uow.graph.save_graph(graph)
            uow.snapshots.save_snapshot(snapshot, artifacts)
            uow.snapshots.set_instance_state(_HEAD_SNAPSHOT_STATE_KEY, snapshot.snapshot_id)
            uow.snapshots.set_instance_state(_ORIGIN_SNAPSHOT_STATE_KEY, origin_snapshot_id)
            uow.register_after_commit(
                lambda: instance._after_snapshot_commit(
                    snapshot_id=snapshot.snapshot_id,
                    origin_snapshot_id=origin_snapshot_id,
                )
            )
        instance._graph_cache = graph
        return instance, snapshot

    def get_receipt_store(self) -> SQLiteReceiptStore:
        """Get or create the receipt SQLite store."""
        if self._active_uow is not None:
            return self._active_uow.receipts
        self._ensure_state_initialized()
        return SQLiteReceiptStore(self._state_db_path())

    def get_decision_store(self) -> DecisionStore:
        """Get or create the decision record SQLite store."""
        if self._active_uow is not None:
            return self._active_uow.decisions
        self._ensure_state_initialized()
        return DecisionStore(self._state_db_path())

    def get_feedback_store(self) -> FeedbackStore:
        """Get or create the feedback SQLite store."""
        if self._active_uow is not None:
            return self._active_uow.feedback
        self._ensure_state_initialized()
        return FeedbackStore(self._state_db_path())

    def get_group_store(self) -> GroupStore:
        """Get or create the group SQLite store."""
        if self._active_uow is not None:
            return self._active_uow.groups
        self._ensure_state_initialized()
        return GroupStore(self._state_db_path())

    def get_source_artifact_store(self) -> SQLiteSourceArtifactStore:
        """Get or create the source artifact SQLite store."""
        if self._active_uow is not None:
            return self._active_uow.source_artifacts
        self._ensure_state_initialized()
        return SQLiteSourceArtifactStore(self._state_db_path())

    @classmethod
    def _validate_instance_mode(cls, instance_mode: str) -> None:
        if instance_mode not in {cls.DEV_MODE, cls.GOVERNED_MODE}:
            raise ConfigError(
                f"Unsupported instance_mode '{instance_mode}'. "
                f"Expected one of: {cls.DEV_MODE}, {cls.GOVERNED_MODE}"
            )

    @staticmethod
    def _parse_metadata(metadata: InstanceMetadata | dict[str, Any]) -> InstanceMetadata:
        if isinstance(metadata, InstanceMetadata):
            return metadata
        try:
            return InstanceMetadata.model_validate(metadata)
        except ValidationError as exc:
            errors = [
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            ]
            raise ConfigError("Invalid instance metadata", errors=errors) from exc
