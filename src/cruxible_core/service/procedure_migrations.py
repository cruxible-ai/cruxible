"""Read and orchestration services for procedure-format convergence.

The orchestration surface deliberately exposes only the ordinary procedure
verbs.  Implementations may be embedded service calls or authenticated client
calls, but neither gets a migration-specific persistence operation.
"""

from __future__ import annotations

import heapq
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from cruxible_core.errors import ConfigError, CoreError
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.procedure.digest import compute_node_digests
from cruxible_core.procedure.migration import (
    PendingLiftDisposition,
    PendingLiftScan,
    lift_v1_procedure_definition,
    scan_pending_procedure_lift,
)
from cruxible_core.procedure.types import (
    ProcedureDefinition,
    ProcedureRecord,
    ProcedureStatus,
    compute_procedure_definition_digest,
)
from cruxible_core.service.procedures import (
    service_accept_procedure,
    service_list_procedures,
    service_propose_procedure,
)

_PENDING_SCAN_PAGE_SIZE = 100
_MIGRATION_LIST_PAGE_SIZE = 100
READING_CONTINUITY_REPORT = (
    "node/arm readings retained on the retired predecessor and digest-matchable; "
    "unit readings retained there only; not aggregated into the successor's `linked_outcomes`"
)

ProcedureMigrationOutcome = Literal[
    "planned",
    "already_pending",
    "proposed",
    "accepted",
    "refused",
]


class ProcedureMigrationActorIdentity(BaseModel):
    """Credential-derived identity fields relevant to reviewer independence."""

    org_id: str
    actor_id: str

    model_config = ConfigDict(extra="forbid", frozen=True)


class ProcedureMigrationItem(BaseModel):
    """One live v1 procedure's governed-lift disposition."""

    name: str
    predecessor_procedure_id: str
    successor_procedure_id: str | None = None
    outcome: ProcedureMigrationOutcome
    dedupe_disposition: PendingLiftDisposition
    definition_digest_before: str
    definition_digest_after: str | None = None
    graph_format_before: int = 1
    graph_format_after: int = 2
    changed_fields: list[str] = Field(default_factory=lambda: ["graph_format"])
    steps_changed: bool = False
    node_local_digests_unchanged: bool = True
    refusal: str | None = None

    model_config = ConfigDict(extra="forbid")


class ProcedureMigrationResult(BaseModel):
    """Deterministic result of one dry-run or supervised apply sweep."""

    mode: Literal["dry_run", "apply"]
    propose_only: bool
    items: list[ProcedureMigrationItem]
    reading_continuity: str = READING_CONTINUITY_REPORT

    model_config = ConfigDict(extra="forbid")


class ProcedureMigrationSurface(Protocol):
    """The ordinary lifecycle verbs used by migration orchestration."""

    def list_procedures(
        self,
        *,
        status: str,
        limit: int,
        offset: int,
    ) -> list[ProcedureRecord]: ...

    def propose_procedure(
        self,
        definition: ProcedureDefinition,
        *,
        supersedes_procedure_id: str,
    ) -> ProcedureRecord: ...

    def accept_procedure(self, procedure: ProcedureRecord) -> ProcedureRecord: ...


class _LocalProcedureMigrationSurface:
    def __init__(
        self,
        instance: InstanceProtocol,
        *,
        proposer_actor: GovernedActorContext | None,
        reviewer_actor: GovernedActorContext | None,
    ) -> None:
        self._instance = instance
        self._proposer_actor = proposer_actor
        self._reviewer_actor = reviewer_actor

    def list_procedures(
        self,
        *,
        status: str,
        limit: int,
        offset: int,
    ) -> list[ProcedureRecord]:
        result = service_list_procedures(
            self._instance,
            status=cast(ProcedureStatus, status),
            limit=limit,
            offset=offset,
        )
        return list(result.items)

    def propose_procedure(
        self,
        definition: ProcedureDefinition,
        *,
        supersedes_procedure_id: str,
    ) -> ProcedureRecord:
        return service_propose_procedure(
            self._instance,
            definition,
            actor_context=self._proposer_actor,
            supersedes_procedure_id=supersedes_procedure_id,
        ).procedure

    def accept_procedure(self, procedure: ProcedureRecord) -> ProcedureRecord:
        return service_accept_procedure(
            self._instance,
            procedure.procedure_id,
            expected_version=procedure.version,
            actor_context=self._reviewer_actor,
        ).procedure


def _actor_identity(actor: GovernedActorContext | None) -> ProcedureMigrationActorIdentity | None:
    if actor is None:
        return None
    return ProcedureMigrationActorIdentity(org_id=actor.org_id, actor_id=actor.actor_id)


def _refuse_same_migration_actor(
    proposer: ProcedureMigrationActorIdentity | None,
    reviewer: ProcedureMigrationActorIdentity | None,
) -> None:
    if proposer is None or reviewer is None:
        return
    if (proposer.org_id, proposer.actor_id) != (reviewer.org_id, reviewer.actor_id):
        return
    raise ConfigError(
        "Procedure migration refused before any write: proposer and reviewer "
        f"both identify actor '{reviewer.actor_id}' in org '{reviewer.org_id}'. "
        "Use distinct proposer and reviewer credentials, or omit the reviewer "
        "credential to run propose-only."
    )


def _list_all(
    surface: ProcedureMigrationSurface,
    *,
    status: str,
) -> list[ProcedureRecord]:
    rows: list[ProcedureRecord] = []
    offset = 0
    while True:
        page = surface.list_procedures(
            status=status,
            limit=_MIGRATION_LIST_PAGE_SIZE,
            offset=offset,
        )
        rows.extend(page)
        if len(page) < _MIGRATION_LIST_PAGE_SIZE:
            return rows
        offset += len(page)


def _dependency_ordered(procedures: list[ProcedureRecord]) -> list[ProcedureRecord]:
    """Topologically order in-corpus supersession edges with a stable fallback."""
    by_id = {row.procedure_id: row for row in procedures}
    successors: dict[str, list[str]] = {procedure_id: [] for procedure_id in by_id}
    indegree = {procedure_id: 0 for procedure_id in by_id}
    for row in procedures:
        predecessor_id = row.supersedes_procedure_id
        if predecessor_id in by_id:
            successors[predecessor_id].append(row.procedure_id)
            indegree[row.procedure_id] += 1

    ready = [
        (row.definition.name, row.procedure_id)
        for row in procedures
        if indegree[row.procedure_id] == 0
    ]
    heapq.heapify(ready)
    ordered: list[ProcedureRecord] = []
    while ready:
        _name, procedure_id = heapq.heappop(ready)
        ordered.append(by_id[procedure_id])
        for successor_id in sorted(successors[procedure_id]):
            indegree[successor_id] -= 1
            if indegree[successor_id] == 0:
                successor = by_id[successor_id]
                heapq.heappush(ready, (successor.definition.name, successor_id))
    if len(ordered) != len(procedures):
        cyclic = sorted(procedure_id for procedure_id, degree in indegree.items() if degree)
        raise ConfigError(
            "Procedure migration refused before any write: supersession dependency cycle "
            f"among live v1 procedures: {', '.join(cyclic)}"
        )
    return ordered


def _node_local_digests(definition: ProcedureDefinition) -> dict[str, str]:
    return {
        node_id: digest.local_digest for node_id, digest in compute_node_digests(definition).items()
    }


def _refused_item(
    predecessor: ProcedureRecord,
    *,
    dedupe_disposition: PendingLiftDisposition,
    reason: str,
    definition_digest_after: str | None = None,
    successor_procedure_id: str | None = None,
) -> ProcedureMigrationItem:
    return ProcedureMigrationItem(
        name=predecessor.definition.name,
        predecessor_procedure_id=predecessor.procedure_id,
        successor_procedure_id=successor_procedure_id,
        outcome="refused",
        dedupe_disposition=dedupe_disposition,
        definition_digest_before=predecessor.definition_digest,
        definition_digest_after=definition_digest_after,
        refusal=reason,
    )


def run_procedure_migration(
    surface: ProcedureMigrationSurface,
    *,
    apply: bool,
    proposer_identity: ProcedureMigrationActorIdentity | None = None,
    reviewer_identity: ProcedureMigrationActorIdentity | None = None,
) -> ProcedureMigrationResult:
    """Run the convergence sweep exclusively through ordinary lifecycle verbs."""
    if apply:
        if reviewer_identity is not None and proposer_identity is None:
            raise ConfigError(
                "Procedure migration refused before any write: reviewer identity was supplied "
                "without an authoritative proposer identity"
            )
        _refuse_same_migration_actor(proposer_identity, reviewer_identity)

    live_v1 = [
        row for row in _list_all(surface, status="live") if row.definition_format_version == 1
    ]
    ordered = _dependency_ordered(live_v1)
    pending_rows = _list_all(surface, status="pending")
    items: list[ProcedureMigrationItem] = []

    for predecessor in ordered:
        name = predecessor.definition.name
        try:
            lifted = lift_v1_procedure_definition(predecessor.definition)
            definition_digest_after = compute_procedure_definition_digest(lifted)
            if _node_local_digests(predecessor.definition) != _node_local_digests(lifted):
                raise ConfigError(
                    f"Procedure migration refused for '{name}': node local digests changed "
                    "across the lift"
                )
        except CoreError as exc:
            items.append(
                _refused_item(
                    predecessor,
                    dedupe_disposition="none",
                    reason=str(exc),
                )
            )
            continue

        scan = scan_pending_procedure_lift(predecessor, lifted, pending_rows)
        if scan.disposition == "refused":
            assert scan.reason is not None
            items.append(
                _refused_item(
                    predecessor,
                    dedupe_disposition="refused",
                    reason=scan.reason,
                    definition_digest_after=definition_digest_after,
                )
            )
            continue

        pending = scan.pending
        if not apply:
            items.append(
                ProcedureMigrationItem(
                    name=name,
                    predecessor_procedure_id=predecessor.procedure_id,
                    successor_procedure_id=(None if pending is None else pending.procedure_id),
                    outcome="planned" if pending is None else "already_pending",
                    dedupe_disposition=scan.disposition,
                    definition_digest_before=predecessor.definition_digest,
                    definition_digest_after=definition_digest_after,
                )
            )
            continue

        if pending is None:
            try:
                pending = surface.propose_procedure(
                    lifted,
                    supersedes_procedure_id=predecessor.procedure_id,
                )
            except CoreError as exc:
                items.append(
                    _refused_item(
                        predecessor,
                        dedupe_disposition="none",
                        reason=f"Procedure migration refused for '{name}': {exc}",
                        definition_digest_after=definition_digest_after,
                    )
                )
                continue
            pending_rows.append(pending)

        if reviewer_identity is None:
            items.append(
                ProcedureMigrationItem(
                    name=name,
                    predecessor_procedure_id=predecessor.procedure_id,
                    successor_procedure_id=pending.procedure_id,
                    outcome=("proposed" if scan.disposition == "none" else "already_pending"),
                    dedupe_disposition=scan.disposition,
                    definition_digest_before=predecessor.definition_digest,
                    definition_digest_after=definition_digest_after,
                )
            )
            continue

        try:
            accepted = surface.accept_procedure(pending)
        except CoreError as exc:
            items.append(
                _refused_item(
                    predecessor,
                    dedupe_disposition=scan.disposition,
                    reason=f"Procedure migration refused for '{name}': {exc}",
                    definition_digest_after=definition_digest_after,
                    successor_procedure_id=pending.procedure_id,
                )
            )
            continue
        items.append(
            ProcedureMigrationItem(
                name=name,
                predecessor_procedure_id=predecessor.procedure_id,
                successor_procedure_id=accepted.procedure_id,
                outcome="accepted",
                dedupe_disposition=scan.disposition,
                definition_digest_before=predecessor.definition_digest,
                definition_digest_after=definition_digest_after,
            )
        )

    return ProcedureMigrationResult(
        mode="apply" if apply else "dry_run",
        propose_only=apply and reviewer_identity is None,
        items=items,
    )


def service_migrate_procedures(
    instance: InstanceProtocol,
    *,
    apply: bool,
    proposer_actor: GovernedActorContext | None = None,
    reviewer_actor: GovernedActorContext | None = None,
) -> ProcedureMigrationResult:
    """Embedded service entry point using the ordinary propose/accept services."""
    if apply and proposer_actor is None:
        raise ConfigError(
            "Procedure migration apply requires a proposer credential; no writes were attempted"
        )
    surface = _LocalProcedureMigrationSurface(
        instance,
        proposer_actor=proposer_actor,
        reviewer_actor=reviewer_actor,
    )
    return run_procedure_migration(
        surface,
        apply=apply,
        proposer_identity=_actor_identity(proposer_actor),
        reviewer_identity=_actor_identity(reviewer_actor),
    )


def service_scan_pending_procedure_lift(
    instance: InstanceProtocol,
    predecessor: ProcedureRecord,
    lifted: ProcedureDefinition,
) -> PendingLiftScan:
    """Exhaustively scan pending rows for one procedure name without writing."""
    store = instance.get_procedure_store()
    try:
        total = store.count_procedures(name=predecessor.definition.name, status="pending")
        pending: list[ProcedureRecord] = []
        for offset in range(0, total, _PENDING_SCAN_PAGE_SIZE):
            pending.extend(
                store.list_procedures(
                    name=predecessor.definition.name,
                    status="pending",
                    limit=_PENDING_SCAN_PAGE_SIZE,
                    offset=offset,
                )
            )
    finally:
        store.close()
    return scan_pending_procedure_lift(predecessor, lifted, pending)


__all__ = [
    "ProcedureMigrationActorIdentity",
    "ProcedureMigrationItem",
    "ProcedureMigrationResult",
    "ProcedureMigrationSurface",
    "READING_CONTINUITY_REPORT",
    "run_procedure_migration",
    "service_migrate_procedures",
    "service_scan_pending_procedure_lift",
]
