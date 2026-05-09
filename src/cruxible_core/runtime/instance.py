""".cruxible/ directory management and graph serialization.

Manages the local instance directory structure:
    .cruxible/
        instance.json   - metadata (config path, data dir, version)
        graph.json      - networkx node_link_data JSON
        receipts.db     - SQLite for receipts
        feedback.db     - SQLite for feedback, outcomes, and groups
        decisions.db    - SQLite for decision records and events
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cruxible_core import __version__
from cruxible_core.config.loader import load_config, save_config
from cruxible_core.config.schema import CoreConfig
from cruxible_core.decision.store import DecisionStore
from cruxible_core.errors import ConfigError, InstanceNotFoundError
from cruxible_core.feedback.store import FeedbackStore
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.group.store import GroupStore
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.receipt.store import SQLiteReceiptStore
from cruxible_core.snapshot.types import UpstreamMetadata, WorldSnapshot
from cruxible_core.workflow.compiler import (
    LOCK_FILE_NAME,
    compute_lock_config_digest,
    resolve_lock_path,
)

logger = logging.getLogger(__name__)


class CruxibleInstance(InstanceProtocol):
    """Manages a .cruxible/ project instance."""

    INSTANCE_DIR = ".cruxible"
    DEV_MODE = "dev"
    GOVERNED_MODE = "governed"

    def __init__(self, root: Path, metadata: dict[str, Any]) -> None:
        self.root = root
        self.instance_dir = root / self.INSTANCE_DIR
        self.metadata = metadata
        self._graph_cache: EntityGraph | None = None

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
        cls._enforce_agent_mode(root)
        resolved_config = Path(config_path)
        if not resolved_config.is_absolute():
            resolved_config = root / resolved_config

        load_config(resolved_config)

        instance_dir = root / cls.INSTANCE_DIR
        instance_dir.mkdir(parents=True, exist_ok=True)

        metadata: dict[str, Any] = {
            "config_path": str(config_path),
            "data_dir": data_dir or ".",
            "instance_mode": instance_mode,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "version": __version__,
        }
        (instance_dir / "instance.json").write_text(json.dumps(metadata, indent=2))

        graph_data = EntityGraph().to_dict()
        (instance_dir / "graph.json").write_text(json.dumps(graph_data, indent=2))

        return cls(root, metadata)

    @classmethod
    def load(cls, root: Path | None = None) -> CruxibleInstance:
        """Load an existing instance, walking up from root (or cwd) to find .cruxible/."""
        if root is None:
            root = Path.cwd()

        search = root
        while True:
            candidate = search / cls.INSTANCE_DIR / "instance.json"
            if candidate.exists():
                cls._enforce_agent_mode(search)
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
        """Return the persisted instance mode, defaulting legacy instances to dev."""
        mode = self.metadata.get("instance_mode")
        if not isinstance(mode, str):
            return self.DEV_MODE
        return mode

    def is_dev_mode(self) -> bool:
        """Return whether the instance is a workspace-rooted dev instance."""
        return self.get_instance_mode() == self.DEV_MODE

    def is_governed_mode(self) -> bool:
        """Return whether the instance is a daemon-owned governed instance."""
        return self.get_instance_mode() == self.GOVERNED_MODE

    def set_config_path(self, config_path: str) -> None:
        """Update the config path recorded in instance metadata."""
        self.metadata["config_path"] = config_path
        self._write_metadata()

    def get_config_path(self) -> Path:
        """Return the resolved config path for the instance."""
        config_path = Path(self.metadata["config_path"])
        if not config_path.is_absolute():
            config_path = self.root / config_path
        return config_path

    def load_graph(self) -> EntityGraph:
        """Load the entity graph from graph.json. Returns cached graph if available."""
        if self._graph_cache is not None:
            return self._graph_cache

        graph_path = self.instance_dir / "graph.json"
        if not graph_path.exists():
            logger.warning(
                "graph.json not found in %s — returning empty graph",
                self.instance_dir,
            )
            graph = EntityGraph()
        else:
            data = json.loads(graph_path.read_text())
            graph = EntityGraph.from_dict(data)

        self._graph_cache = graph
        return graph

    def save_graph(self, graph: EntityGraph) -> None:
        """Save the entity graph to graph.json atomically.

        Uses temp-file + os.replace() (atomic on POSIX). Invalidates
        _graph_cache on exception so failed writes never leave phantom
        edges visible to subsequent load_graph() calls.
        """
        data = graph.to_dict()
        graph_path = self.instance_dir / "graph.json"
        try:
            fd, tmp_path = tempfile.mkstemp(dir=self.instance_dir, suffix=".tmp", prefix="graph_")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp_path, graph_path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception:
            self._graph_cache = None
            raise
        self._graph_cache = graph

    def invalidate_graph_cache(self) -> None:
        """Clear the in-memory graph cache, forcing next load_graph to read from disk."""
        self._graph_cache = None

    def get_head_snapshot_id(self) -> str | None:
        """Return the current head snapshot identifier, if any."""
        value = self.metadata.get("head_snapshot_id")
        return str(value) if value is not None else None

    def get_upstream_metadata(self) -> UpstreamMetadata | None:
        """Return typed upstream metadata for release-backed overlay instances."""
        raw = self.metadata.get("upstream")
        if raw is None:
            return None
        return UpstreamMetadata.model_validate(raw)

    def set_upstream_metadata(self, metadata: UpstreamMetadata | None) -> None:
        """Persist upstream metadata for release-backed overlay instances."""
        if metadata is None:
            self.metadata.pop("upstream", None)
        else:
            self.metadata["upstream"] = metadata.model_dump(mode="json")
        self._write_metadata()

    def _metadata_path(self) -> Path:
        return self.instance_dir / "instance.json"

    def _write_metadata(self) -> None:
        self._metadata_path().write_text(json.dumps(self.metadata, indent=2, sort_keys=True))

    def _snapshots_dir(self) -> Path:
        path = self.instance_dir / "snapshots"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _snapshot_dir(self, snapshot_id: str) -> Path:
        return self._snapshots_dir() / snapshot_id

    def create_snapshot(self, label: str | None = None) -> WorldSnapshot:
        """Persist an immutable full snapshot of the current graph + config state."""
        return self._write_snapshot(self.load_graph(), label=label, persist_live_graph=False)

    def commit_graph_snapshot(self, graph: EntityGraph, label: str | None = None) -> WorldSnapshot:
        """Persist a snapshot for a provided graph, then atomically advance live state."""
        return self._write_snapshot(graph, label=label, persist_live_graph=True)

    def _write_snapshot(
        self,
        graph: EntityGraph,
        *,
        label: str | None = None,
        persist_live_graph: bool,
    ) -> WorldSnapshot:
        """Write snapshot artifacts and optionally advance the live graph/head."""
        snapshot_id = f"snap_{uuid.uuid4().hex[:16]}"
        snapshot_dir = self._snapshot_dir(snapshot_id)
        snapshot_dir.mkdir(parents=True, exist_ok=False)

        config = self.load_config()
        config_path = self.get_config_path()
        graph_json = json.dumps(graph.to_dict(), indent=2, sort_keys=True)
        graph_digest = f"sha256:{hashlib.sha256(graph_json.encode()).hexdigest()}"

        (snapshot_dir / "graph.json").write_text(graph_json)
        (snapshot_dir / "config.yaml").write_text(config_path.read_text())

        lock_path = resolve_lock_path(self)
        lock_digest: str | None = None
        if lock_path.exists():
            lock_bytes = lock_path.read_bytes()
            lock_digest = f"sha256:{hashlib.sha256(lock_bytes).hexdigest()}"
            shutil.copy2(lock_path, snapshot_dir / LOCK_FILE_NAME)

        snapshot = WorldSnapshot(
            snapshot_id=snapshot_id,
            created_at=datetime.now(timezone.utc),
            label=label,
            config_digest=compute_lock_config_digest(config),
            lock_digest=lock_digest,
            graph_digest=graph_digest,
            parent_snapshot_id=self.metadata.get("head_snapshot_id"),
            origin_snapshot_id=self.metadata.get("origin_snapshot_id"),
        )
        (snapshot_dir / "snapshot.json").write_text(
            json.dumps(snapshot.model_dump(mode="json"), indent=2, sort_keys=True)
        )

        if persist_live_graph:
            self.save_graph(graph)
        self.metadata["head_snapshot_id"] = snapshot_id
        if snapshot.origin_snapshot_id is not None:
            self.metadata["origin_snapshot_id"] = snapshot.origin_snapshot_id
        self._write_metadata()
        return snapshot

    def get_snapshot(self, snapshot_id: str) -> WorldSnapshot | None:
        """Load snapshot metadata by ID."""
        snapshot_path = self._snapshot_dir(snapshot_id) / "snapshot.json"
        if not snapshot_path.exists():
            return None
        raw = json.loads(snapshot_path.read_text())
        return WorldSnapshot.model_validate(raw)

    def list_snapshots(self) -> list[WorldSnapshot]:
        """List local snapshots in reverse chronological order."""
        snapshots: list[WorldSnapshot] = []
        for path in self._snapshots_dir().glob("*/snapshot.json"):
            raw = json.loads(path.read_text())
            snapshots.append(WorldSnapshot.model_validate(raw))
        return sorted(
            snapshots,
            key=lambda item: (item.created_at, item.snapshot_id),
            reverse=True,
        )

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

        snapshot_dir = source_instance._snapshot_dir(snapshot_id)
        shutil.copy2(snapshot_dir / "config.yaml", config_target)
        instance = cls.init(root, "config.yaml", instance_mode=instance_mode)

        graph_data = json.loads((snapshot_dir / "graph.json").read_text())
        instance.save_graph(EntityGraph.from_dict(graph_data))

        snapshot_lock = snapshot_dir / LOCK_FILE_NAME
        if snapshot_lock.exists():
            shutil.copy2(snapshot_lock, instance.get_instance_dir() / LOCK_FILE_NAME)

        instance.metadata["head_snapshot_id"] = snapshot.snapshot_id
        instance.metadata["origin_snapshot_id"] = (
            snapshot.origin_snapshot_id or snapshot.snapshot_id
        )
        instance._write_metadata()
        return instance, snapshot

    # NOTE: graph.json, receipts.db, and feedback.db are independent stores with
    # no cross-store transactions. A crash between sequential writes (e.g. graph
    # saved but receipt not recorded) can leave stores inconsistent. Acceptable
    # for a single-user local tool; revisit if the runtime moves server-side.

    def get_receipt_store(self) -> SQLiteReceiptStore:
        """Get or create the receipt SQLite store."""
        return SQLiteReceiptStore(self.instance_dir / "receipts.db")

    def get_decision_store(self) -> DecisionStore:
        """Get or create the decision record SQLite store."""
        return DecisionStore(self.instance_dir / "decisions.db")

    def get_feedback_store(self) -> FeedbackStore:
        """Get or create the feedback SQLite store."""
        return FeedbackStore(self.instance_dir / "feedback.db")

    def get_group_store(self) -> GroupStore:
        """Get or create the group SQLite store (shares feedback.db)."""
        return GroupStore(self.instance_dir / "feedback.db")

    @classmethod
    def _validate_instance_mode(cls, instance_mode: str) -> None:
        if instance_mode not in {cls.DEV_MODE, cls.GOVERNED_MODE}:
            raise ConfigError(
                f"Unsupported instance_mode '{instance_mode}'. "
                f"Expected one of: {cls.DEV_MODE}, {cls.GOVERNED_MODE}"
            )

    @classmethod
    def _enforce_agent_mode(cls, root: Path) -> None:
        """Block init/load outside the daemon directory when CRUXIBLE_AGENT_MODE is set."""
        from cruxible_core.server.config import get_server_state_dir, is_agent_mode

        if not is_agent_mode():
            return
        allowed_dir = get_server_state_dir() / "instances"
        try:
            root.resolve().relative_to(allowed_dir.resolve())
        except ValueError:
            raise ConfigError(
                "CRUXIBLE_AGENT_MODE is set — instances can only be created or loaded "
                "under the daemon directory. Use server mode."
            ) from None
