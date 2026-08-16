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
    from cruxible_core.group.types import CandidateGroup, CandidateMember, GroupResolution
    from cruxible_core.procedure.types import (
        LinkedOutcomeSummary,
        ProcedureBudgetSpent,
        ProcedureEvidenceArtifact,
        ProcedureReading,
        ProcedureRecord,
        ProcedureRefusalReason,
        ProcedureRun,
        ProcedureRunFiredNode,
        ProcedureRunVerdict,
        ProcedureStatus,
        ProcedureTrackRecord,
    )
    from cruxible_core.provider.types import ExecutionTrace
    from cruxible_core.receipt.types import Receipt
    from cruxible_core.resolution_contracts.types import (
        ContractActivation,
        ContractResolution,
        ResolutionContract,
        ResolutionDisposition,
    )
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
        before: tuple[str, str] | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, Any]]: ...
    @abstractmethod
    def count_receipts(
        self,
        *,
        query_name: str | None = None,
        operation_type: str | None = None,
        before: tuple[str, str] | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> int: ...
    @abstractmethod
    def get_receipts_for_entity(self, entity_type: str, entity_id: str) -> list[str]: ...
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
    ) -> dict[tuple[str, str, str, str, str], list[CandidateGroup]]: ...
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
        trust_status: str = "watch",
        trust_reason: str = "",
        trust_actor_context: GovernedActorContext | None = None,
        confirmed: bool = False,
        resolved_actor_context: GovernedActorContext | None = None,
        receipt_id: str | None = None,
        resolution_source: str = "review",
    ) -> str: ...
    @abstractmethod
    def confirm_resolution(self, resolution_id: str) -> None: ...
    @abstractmethod
    def stamp_resolution_receipt_id(self, resolution_id: str, receipt_id: str) -> None: ...
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


class ProcedureStoreProtocol(ABC):
    """Interface for immutable procedure definitions and run records."""

    @abstractmethod
    def save_procedure(self, procedure: ProcedureRecord) -> str: ...
    @abstractmethod
    def get_procedure(self, procedure_id: str) -> ProcedureRecord | None: ...
    @abstractmethod
    def list_procedures(
        self,
        *,
        name: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ProcedureRecord]: ...
    @abstractmethod
    def count_procedures(
        self,
        *,
        name: str | None = None,
        status: str | None = None,
    ) -> int: ...
    @abstractmethod
    def transition_procedure(
        self,
        procedure_id: str,
        *,
        from_status: ProcedureStatus,
        to_status: ProcedureStatus,
        expected_version: int,
        resolved_actor_context: Any | None = None,
        resolved_at: str | None = None,
        retired_actor_context: GovernedActorContext | None = None,
        retired_at: str | None = None,
        reason: str | None = None,
        acceptance_config_digest: str | None = None,
        acceptance_lock_digest: str | None = None,
    ) -> bool: ...
    @abstractmethod
    def save_node_digests(self, procedure_id: str, digests: Sequence[Any]) -> int: ...
    @abstractmethod
    def list_node_digests(self, procedure_id: str) -> list[Any]: ...
    @abstractmethod
    def save_acceptance_node_pins(self, pins: Sequence[Any]) -> int: ...
    @abstractmethod
    def list_acceptance_node_pins(self, procedure_id: str) -> list[Any]: ...
    @abstractmethod
    def save_run(self, run: ProcedureRun) -> str: ...
    @abstractmethod
    def finalize_run(
        self,
        run_id: str,
        *,
        verdict: ProcedureRunVerdict,
        budget_spent: ProcedureBudgetSpent,
        receipt_id: str,
        finalized_at: str,
        refusal_reason: ProcedureRefusalReason | None = None,
    ) -> bool: ...
    @abstractmethod
    def get_run(self, run_id: str) -> ProcedureRun | None: ...
    @abstractmethod
    def list_runs(
        self,
        *,
        procedure_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ProcedureRun]: ...
    @abstractmethod
    def count_runs(
        self,
        *,
        procedure_id: str | None = None,
        status: str | None = None,
    ) -> int: ...
    @abstractmethod
    def get_run_track_records(
        self,
        procedure_ids: Sequence[str],
    ) -> dict[str, ProcedureTrackRecord]: ...
    @abstractmethod
    def save_evidence_artifact(self, artifact: ProcedureEvidenceArtifact) -> str: ...
    @abstractmethod
    def link_run_evidence(
        self,
        *,
        run_id: str,
        output_alias: str,
        artifact_id: str,
        receipt_id: str,
    ) -> None: ...
    @abstractmethod
    def get_evidence_artifact(
        self,
        artifact_id: str,
    ) -> ProcedureEvidenceArtifact | None: ...
    @abstractmethod
    def list_run_evidence_refs(self, run_id: str) -> list[Any]: ...
    @abstractmethod
    def close(self) -> None: ...


class ProcedureReadingStoreProtocol(ABC):
    """Interface for immutable procedure outcome readings."""

    @abstractmethod
    def save_reading(self, reading: ProcedureReading) -> str: ...
    @abstractmethod
    def get_reading(self, reading_id: str) -> ProcedureReading | None: ...
    @abstractmethod
    def find_idempotent_reading(
        self,
        *,
        idempotency_key: str,
        procedure_id: str,
        actor_org_id: str,
        actor_id: str,
    ) -> ProcedureReading | None: ...
    @abstractmethod
    def list_readings(
        self,
        *,
        procedure_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ProcedureReading]: ...
    @abstractmethod
    def save_fired_nodes(self, fired_nodes: Sequence[ProcedureRunFiredNode]) -> None: ...
    @abstractmethod
    def list_run_fired_nodes(self, run_id: str) -> list[ProcedureRunFiredNode]: ...
    @abstractmethod
    def get_linked_outcome_summaries(
        self,
        procedure_ids: Sequence[str],
    ) -> dict[str, LinkedOutcomeSummary]: ...
    @abstractmethod
    def close(self) -> None: ...


class ResolutionContractStoreProtocol(ABC):
    """Interface for resolution contracts, activations, and their answers."""

    @abstractmethod
    def save_contract(self, contract: ResolutionContract) -> str: ...
    @abstractmethod
    def get_contract(self, contract_id: str) -> ResolutionContract | None: ...
    @abstractmethod
    def find_idempotent_contract(
        self,
        *,
        idempotency_key: str,
        entity_type: str,
        entity_id: str,
        actor_org_id: str,
        actor_id: str,
    ) -> ResolutionContract | None: ...
    @abstractmethod
    def list_contracts(
        self,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ResolutionContract]: ...
    @abstractmethod
    def count_contracts(
        self,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
    ) -> int: ...
    @abstractmethod
    def find_eligible_contracts(
        self,
        *,
        entity_type: str,
        entity_id: str,
        subject_content_digest: str,
        now: str,
    ) -> list[ResolutionContract]: ...
    @abstractmethod
    def save_activation(self, activation: ContractActivation) -> str: ...
    @abstractmethod
    def get_activations(
        self,
        contract_ids: Sequence[str],
    ) -> dict[str, ContractActivation]: ...
    @abstractmethod
    def save_resolution(self, resolution: ContractResolution) -> str: ...
    @abstractmethod
    def get_resolution(self, resolution_id: str) -> ContractResolution | None: ...
    @abstractmethod
    def list_resolutions(self, contract_id: str) -> list[ContractResolution]: ...
    @abstractmethod
    def get_latest_resolutions(
        self,
        contract_ids: Sequence[str],
    ) -> dict[str, ContractResolution]: ...
    @abstractmethod
    def save_disposition(self, disposition: ResolutionDisposition) -> str: ...
    @abstractmethod
    def get_dispositions(
        self,
        resolution_ids: Sequence[str],
    ) -> dict[str, ResolutionDisposition]: ...
    @abstractmethod
    def list_dispositions(self, resolution_id: str) -> list[ResolutionDisposition]: ...
    @abstractmethod
    def list_activated_unresolved(
        self,
        *,
        before: str,
        use_expiry: bool,
    ) -> list[ResolutionContract]: ...
    @abstractmethod
    def list_undisposed_contradictions(
        self,
    ) -> list[tuple[ResolutionContract, ContractResolution]]: ...
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
    @abstractmethod
    def get_receipt_store(self) -> ReceiptStoreProtocol: ...
    @abstractmethod
    def get_group_store(self) -> GroupStoreProtocol: ...
    @abstractmethod
    def get_procedure_store(self) -> ProcedureStoreProtocol: ...
    @abstractmethod
    def get_procedure_reading_store(self) -> ProcedureReadingStoreProtocol: ...
    @abstractmethod
    def get_resolution_contract_store(self) -> ResolutionContractStoreProtocol: ...
