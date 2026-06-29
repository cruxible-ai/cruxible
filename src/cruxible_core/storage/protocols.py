"""Backend-neutral storage contracts for durable instance state."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from typing import Any, Protocol

from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.instance_protocol import (
    DecisionStoreProtocol,
    FeedbackStoreProtocol,
    GroupStoreProtocol,
    ReceiptStoreProtocol,
)
from cruxible_core.snapshot.types import StateSnapshot
from cruxible_core.source_artifacts.store import SourceArtifactStoreProtocol


class GraphRepositoryProtocol(Protocol):
    """Repository contract for live graph state."""

    def load_graph(self) -> EntityGraph: ...
    def save_graph(self, graph: EntityGraph) -> None: ...
    def upsert_entities(self, entities: Iterable[EntityInstance]) -> None: ...
    def upsert_relationships(self, relationships: Iterable[RelationshipInstance]) -> None: ...
    def is_empty(self) -> bool: ...


class SnapshotRepositoryProtocol(Protocol):
    """Repository contract for DB-authoritative snapshot state."""

    def save_snapshot(
        self,
        snapshot: StateSnapshot,
        artifacts: Mapping[str, bytes | str],
    ) -> None: ...
    def get_snapshot(self, snapshot_id: str) -> StateSnapshot | None: ...
    def list_snapshots(self, limit: int | None = None) -> list[StateSnapshot]: ...
    def get_snapshot_artifact(self, snapshot_id: str, artifact_name: str) -> bytes | None: ...
    def list_snapshot_artifacts(self, snapshot_id: str) -> dict[str, bytes]: ...
    def set_instance_state(self, key: str, value: Any) -> None: ...
    def get_instance_state(self, key: str) -> Any | None: ...


class UnitOfWorkProtocol(Protocol):
    """Transaction boundary spanning graph and audit repositories."""

    graph: GraphRepositoryProtocol
    snapshots: SnapshotRepositoryProtocol
    receipts: ReceiptStoreProtocol
    feedback: FeedbackStoreProtocol
    groups: GroupStoreProtocol
    decisions: DecisionStoreProtocol
    source_artifacts: SourceArtifactStoreProtocol

    def register_after_commit(self, callback: Any) -> None: ...
    def register_after_rollback(self, callback: Any) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


class StorageBackendProtocol(Protocol):
    """Factory contract for storage backends."""

    def initialize(self) -> None: ...
    def unit_of_work(self) -> AbstractContextManager[UnitOfWorkProtocol]: ...
    def graph_repository(self) -> AbstractContextManager[GraphRepositoryProtocol]: ...
    def snapshot_repository(self) -> AbstractContextManager[SnapshotRepositoryProtocol]: ...
