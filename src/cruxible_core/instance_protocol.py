"""Abstract base classes for instance and store interfaces.

Enables future cloud backends (e.g. CloudInstance backed by R2/D1)
without coupling handlers to concrete SQLite implementations.
Concrete stores must inherit from these ABCs — Python enforces the
contract at class-definition time.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from cruxible_core.config.schema import CoreConfig
from cruxible_core.feedback.types import FeedbackRecord, OutcomeRecord
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.group.types import CandidateGroup, CandidateMember, GroupResolution
from cruxible_core.provider.types import ExecutionTrace
from cruxible_core.receipt.types import Receipt
from cruxible_core.snapshot.types import UpstreamMetadata, WorldSnapshot


class ReceiptStoreProtocol(ABC):
    """Interface for receipt and execution-trace storage."""

    @abstractmethod
    def save_receipt(self, receipt: Receipt) -> str: ...
    @abstractmethod
    def get_receipt(self, receipt_id: str) -> Receipt | None: ...
    @abstractmethod
    def save_trace(self, trace: ExecutionTrace) -> str: ...
    @abstractmethod
    def get_trace(self, trace_id: str) -> ExecutionTrace | None: ...
    @abstractmethod
    def list_traces(
        self,
        *,
        workflow_name: str | None = None,
        provider_name: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]: ...
    @abstractmethod
    def list_receipts(
        self,
        *,
        query_name: str | None = None,
        operation_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]: ...
    @abstractmethod
    def count_receipts(
        self, *, query_name: str | None = None, operation_type: str | None = None
    ) -> int: ...
    @abstractmethod
    def get_receipts_for_entity(
        self, entity_type: str, entity_id: str
    ) -> list[str]: ...
    @abstractmethod
    def close(self) -> None: ...


class FeedbackStoreProtocol(ABC):
    """Interface for feedback and outcome storage."""

    @abstractmethod
    def save_feedback(self, record: FeedbackRecord) -> str: ...
    @abstractmethod
    def save_feedback_batch(self, records: list[FeedbackRecord]) -> list[str]: ...
    @abstractmethod
    def get_feedback(self, feedback_id: str) -> FeedbackRecord | None: ...
    @abstractmethod
    def list_feedback(
        self,
        *,
        receipt_id: str | None = None,
        relationship_type: str | None = None,
        action: str | None = None,
        decision_surface_type: str | None = None,
        decision_surface_name: str | None = None,
        limit: int = 100,
    ) -> list[FeedbackRecord]: ...
    @abstractmethod
    def list_feedback_by_entity_ids(
        self,
        entity_ids: list[str],
        limit: int = 100,
    ) -> list[FeedbackRecord]: ...
    @abstractmethod
    def count_feedback(self, *, receipt_id: str | None = None) -> int: ...
    @abstractmethod
    def save_outcome(self, record: OutcomeRecord) -> str: ...
    @abstractmethod
    def list_outcomes(
        self,
        *,
        receipt_id: str | None = None,
        anchor_type: str | None = None,
        anchor_id: str | None = None,
        relationship_type: str | None = None,
        decision_surface_type: str | None = None,
        decision_surface_name: str | None = None,
        limit: int = 100,
    ) -> list[OutcomeRecord]: ...
    @abstractmethod
    def count_outcomes(self, *, receipt_id: str | None = None) -> int: ...
    @contextmanager
    def transaction(self) -> Iterator[None]:
        raise NotImplementedError
        yield  # pragma: no cover
    @abstractmethod
    def close(self) -> None: ...


class GroupStoreProtocol(ABC):
    """Interface for candidate group, member, and resolution storage."""

    @abstractmethod
    def get_group(self, group_id: str) -> CandidateGroup | None: ...
    @abstractmethod
    def get_group_by_resolution(self, resolution_id: str) -> CandidateGroup | None: ...
    @abstractmethod
    def list_groups(
        self,
        *,
        relationship_type: str | None = None,
        signature: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[CandidateGroup]: ...
    @abstractmethod
    def count_groups(
        self,
        *,
        relationship_type: str | None = None,
        signature: str | None = None,
        status: str | None = None,
    ) -> int: ...
    @abstractmethod
    def save_group(self, group: CandidateGroup) -> str: ...
    @abstractmethod
    def save_members(self, group_id: str, members: list[CandidateMember]) -> None: ...
    @abstractmethod
    def get_members(self, group_id: str) -> list[CandidateMember]: ...
    @abstractmethod
    def replace_members(self, group_id: str, members: list[CandidateMember]) -> None: ...
    @abstractmethod
    def delete_group(self, group_id: str) -> bool: ...
    @abstractmethod
    def find_pending_group(
        self,
        relationship_type: str,
        signature: str,
        *,
        group_kind: str = "propose",
    ) -> CandidateGroup | None: ...
    @abstractmethod
    def find_pending_groups_for_tuples(
        self,
        relationship_type: str,
        tuples: list[tuple[str, str, str, str, str]],
        *,
        exclude_group_id: str | None = None,
        statuses: tuple[str, ...] = ("pending_review", "applying"),
    ) -> dict[tuple[str, str, str, str, str], CandidateGroup]: ...
    @abstractmethod
    def save_resolution(
        self,
        relationship_type: str,
        signature: str,
        action: str,
        rationale: str,
        thesis_text: str,
        thesis_facts: dict[str, Any],
        analysis_state: dict[str, Any],
        resolved_by: str,
        trust_status: str = "watch",
        confirmed: bool = False,
    ) -> str: ...
    @abstractmethod
    def confirm_resolution(self, resolution_id: str, trust_status: str | None = None) -> None: ...
    @abstractmethod
    def get_resolution(self, resolution_id: str) -> GroupResolution | None: ...
    @abstractmethod
    def find_resolution(
        self,
        relationship_type: str,
        signature: str,
        action: str | None = None,
        confirmed: bool | None = None,
    ) -> GroupResolution | None: ...
    @abstractmethod
    def list_resolutions(
        self,
        *,
        relationship_type: str | None = None,
        signature: str | None = None,
        action: str | None = None,
        confirmed: bool | None = None,
        limit: int = 50,
    ) -> list[GroupResolution]: ...
    @abstractmethod
    def list_approved_relationship_tuples(
        self,
        relationship_type: str,
        signature: str,
        *,
        group_kind: str = "propose",
    ) -> set[tuple[str, str, str, str, str]]: ...
    @abstractmethod
    def update_group_status(
        self, group_id: str, status: str, resolution_id: str | None = None
    ) -> bool: ...
    @abstractmethod
    def update_group(
        self,
        group_id: str,
        *,
        status: str | None = None,
        pending_version: int | None = None,
        member_count: int | None = None,
        resolution_id: str | None = None,
        review_priority: str | None = None,
    ) -> bool: ...
    @abstractmethod
    def update_resolution_trust_status(
        self, resolution_id: str, trust_status: str, trust_reason: str = ""
    ) -> bool: ...
    @contextmanager
    def transaction(self) -> Iterator[None]:
        raise NotImplementedError
        yield  # pragma: no cover
    @abstractmethod
    def close(self) -> None: ...


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
    @abstractmethod
    def load_graph(self) -> EntityGraph: ...
    @abstractmethod
    def save_graph(self, graph: EntityGraph) -> None: ...
    @abstractmethod
    def invalidate_graph_cache(self) -> None: ...
    @abstractmethod
    def get_head_snapshot_id(self) -> str | None: ...
    @abstractmethod
    def get_upstream_metadata(self) -> UpstreamMetadata | None: ...
    @abstractmethod
    def set_upstream_metadata(self, metadata: UpstreamMetadata | None) -> None: ...
    @abstractmethod
    def create_snapshot(self, label: str | None = None) -> WorldSnapshot: ...
    @abstractmethod
    def commit_graph_snapshot(
        self, graph: EntityGraph, label: str | None = None
    ) -> WorldSnapshot: ...
    @abstractmethod
    def get_snapshot(self, snapshot_id: str) -> WorldSnapshot | None: ...
    @abstractmethod
    def list_snapshots(self) -> list[WorldSnapshot]: ...
    @abstractmethod
    def get_receipt_store(self) -> ReceiptStoreProtocol: ...
    @abstractmethod
    def get_feedback_store(self) -> FeedbackStoreProtocol: ...
    @abstractmethod
    def get_group_store(self) -> GroupStoreProtocol: ...
