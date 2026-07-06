"""Abstract base classes for instance and store interfaces.

Enables future cloud backends (e.g. CloudInstance backed by R2/D1)
without coupling handlers to concrete SQLite implementations.
Concrete stores must inherit from these ABCs — Python enforces the
contract at class-definition time.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from cruxible_core.config.schema import CoreConfig
    from cruxible_core.config.source_pointer import ComposedConfigSource
    from cruxible_core.decision.types import DecisionEvent, DecisionRecord
    from cruxible_core.feedback.types import FeedbackRecord, OutcomeRecord
    from cruxible_core.governance.actors import GovernedActorContext
    from cruxible_core.graph.entity_graph import EntityGraph
    from cruxible_core.graph.types import EntityInstance, RelationshipInstance
    from cruxible_core.group.types import CandidateGroup, CandidateMember, GroupResolution
    from cruxible_core.provider.types import ExecutionTrace
    from cruxible_core.receipt.types import Receipt
    from cruxible_core.snapshot.types import StateSnapshot, UpstreamMetadata
    from cruxible_core.source_artifacts.store import SourceArtifactStoreProtocol
    from cruxible_core.storage.protocols import UnitOfWorkProtocol


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
    def count_traces(
        self,
        *,
        workflow_name: str | None = None,
        provider_name: str | None = None,
    ) -> int: ...
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
    def get_receipts_for_entity(self, entity_type: str, entity_id: str) -> list[str]: ...
    @abstractmethod
    def close(self) -> None: ...


class DecisionStoreProtocol(ABC):
    """Interface for decision record and event storage."""

    @abstractmethod
    def save_record(self, record: DecisionRecord) -> str: ...
    @abstractmethod
    def get_record(self, decision_record_id: str) -> DecisionRecord | None: ...
    @abstractmethod
    def list_records(
        self,
        *,
        status: str | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        decision_class: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DecisionRecord]: ...
    @abstractmethod
    def count_records(
        self,
        *,
        status: str | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        decision_class: str | None = None,
    ) -> int: ...
    @abstractmethod
    def update_record(self, record: DecisionRecord) -> None: ...
    @abstractmethod
    def append_event(self, event: DecisionEvent) -> str: ...
    @abstractmethod
    def list_events(
        self,
        decision_record_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DecisionEvent]: ...
    @abstractmethod
    def find_events(
        self,
        *,
        receipt_id: str | None = None,
        trace_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DecisionEvent]: ...
    @abstractmethod
    def count_events(
        self,
        *,
        decision_record_id: str | None = None,
        receipt_id: str | None = None,
        trace_id: str | None = None,
        status: str | None = None,
    ) -> int: ...
    @abstractmethod
    def finalize_record(
        self,
        decision_record_id: str,
        *,
        final_decision: str,
        decision_class: str,
        rationale: str = "",
    ) -> DecisionRecord: ...
    @abstractmethod
    def abandon_record(self, decision_record_id: str, *, reason: str = "") -> DecisionRecord: ...
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
        offset: int = 0,
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
    def get_outcome(self, outcome_id: str) -> OutcomeRecord | None: ...
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
        offset: int = 0,
    ) -> list[OutcomeRecord]: ...
    @abstractmethod
    def count_outcomes(self, *, receipt_id: str | None = None) -> int: ...
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
        offset: int = 0,
        order_by: Literal["created_at", "review_priority"] = "created_at",
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
    def update_group_analysis_state(
        self,
        group_id: str,
        analysis_state: dict[str, Any],
    ) -> bool: ...
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
        resolved_actor_context: Any | None = None,
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
        offset: int = 0,
    ) -> list[GroupResolution]: ...
    @abstractmethod
    def count_resolutions(
        self,
        *,
        relationship_type: str | None = None,
        signature: str | None = None,
        action: str | None = None,
        confirmed: bool | None = None,
    ) -> int: ...
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
        self,
        resolution_id: str,
        trust_status: str,
        trust_reason: str = "",
        trust_actor_context: Any | None = None,
    ) -> bool: ...
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
    def get_config_source_path(self) -> Path: ...
    @abstractmethod
    def has_config_source(self) -> bool: ...
    @abstractmethod
    def load_composed_config_source(self) -> ComposedConfigSource: ...
    @abstractmethod
    def set_serving_config_source(self, composed: ComposedConfigSource) -> None: ...
    @abstractmethod
    def get_receipted_config_digest(self) -> str | None: ...
    @abstractmethod
    def verify_serving_config_receipted(self) -> None: ...
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
    def get_head_snapshot_id(self) -> str | None: ...
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
    @abstractmethod
    def get_receipt_store(self) -> ReceiptStoreProtocol: ...
    @abstractmethod
    def get_decision_store(self) -> DecisionStoreProtocol: ...
    @abstractmethod
    def get_feedback_store(self) -> FeedbackStoreProtocol: ...
    @abstractmethod
    def get_group_store(self) -> GroupStoreProtocol: ...
    @abstractmethod
    def get_source_artifact_store(self) -> SourceArtifactStoreProtocol: ...
