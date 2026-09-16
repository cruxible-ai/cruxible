"""One currency evaluator for workspace checks and the next repair queue."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Literal

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import (
    PlaybillBlockSyncReadRequestV1,
    PlaybillBlockSyncReadResultV1,
    PlaybillBlockSyncSuccessorCandidateV1,
    PlaybillProjectionCheckRequestV1,
    PlaybillProjectionCheckResultV1,
    ProjectionDependencyIssueV1,
)
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_types import (
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
)
from cruxible_client.contracts.claim_verdicts import ClaimVerdictResultAny
from cruxible_client.contracts.claims import (
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
    parse_claim,
)
from cruxible_client.contracts.declared_blocks import (
    ProjectionArtifactBackingV1,
    ProjectionBackingV1,
    ProjectionBlockStamp,
    ProjectionClaimBackingV1,
    ProjectionQueryBackingV1,
    projection_query_semantic_result_digest,
)
from cruxible_client.contracts.errors import PlaybillError, ProposalIntegrityError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.query.definitions import QueryEvaluationPolicyV1
from cruxible_client.contracts.subjects import parse_subject, subject_digest, subject_path
from cruxible_client.contracts.temporal import ensure_utc
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.query.backends import claim_row_visibility
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.query import _AcceptedQueryFactsRead, evaluate_accepted_query
from cruxible_core.service.discovery.query_definitions import (
    _resolve_coordinate,
    accepted_query_definition,
)
from cruxible_core.service.discovery.search import claim_resolution_statuses
from cruxible_core.service.floor.projection_lineage import (
    ClaimLineageNode as _ClaimNode,
)
from cruxible_core.service.floor.projection_lineage import (
    read_claim_lineages,
)

_LINEAGES: ContextVar[
    tuple[PlaybillInstance, dict[str, dict[str, _ClaimNode] | PlaybillError]] | None
] = ContextVar("block_sync_lineages", default=None)


def _refusal(
    *,
    status: str,
    reason: str,
    detail: str,
    original_artifact_digest: str | None = None,
    candidates: tuple[PlaybillBlockSyncSuccessorCandidateV1, ...] = (),
) -> PlaybillBlockSyncReadResultV1:
    return PlaybillBlockSyncReadResultV1.model_validate(
        {
            "status": status,
            "reason": reason,
            "detail": detail,
            "original_artifact_digest": original_artifact_digest,
            "successor_candidates": [item.model_dump(mode="json") for item in candidates],
        }
    )


def _claim_nodes(instance: PlaybillInstance, *, path: str) -> dict[str, _ClaimNode]:
    prepared = _LINEAGES.get()
    if prepared is not None and prepared[0] is instance and path in prepared[1]:
        cached_nodes = prepared[1][path]
        if isinstance(cached_nodes, PlaybillError):
            raise cached_nodes
        return cached_nodes
    nodes = read_claim_lineages(
        instance, paths=(path,), at=AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    )[path]
    if isinstance(nodes, PlaybillError):
        raise nodes
    return nodes


def _candidate(node: _ClaimNode) -> PlaybillBlockSyncSuccessorCandidateV1:
    return PlaybillBlockSyncSuccessorCandidateV1(
        identity=node.claim.identity,
        artifact_digest=node.artifact_digest,
        coordinate=node.coordinate,
        generation=node.generation,
    )


def _terminal_node(
    *,
    nodes: dict[str, _ClaimNode],
    original_digest: str,
    preferred_successor_digest: str | None,
) -> _ClaimNode | tuple[PlaybillBlockSyncSuccessorCandidateV1, ...]:
    successors: dict[str, dict[str, _ClaimNode]] = {}
    for node in nodes.values():
        predecessor = node.claim.lifecycle.predecessor_digest
        if predecessor is not None:
            successors.setdefault(predecessor, {})[node.artifact_digest] = node
    states: dict[str, int] = {}
    terminals: dict[str, _ClaimNode] = {}

    def visit(digest: str) -> None:
        state = states.get(digest, 0)
        if state == 1:
            raise ProposalIntegrityError("accepted Claim block-sync lineage contains a cycle")
        if state == 2:
            return
        states[digest] = 1
        children = successors.get(digest, {})
        if not children:
            terminals[digest] = nodes[digest]
        else:
            for child_digest in sorted(children, key=lambda item: item.encode("ascii")):
                visit(child_digest)
        states[digest] = 2

    visit(original_digest)
    live = tuple(
        terminals[digest]
        for digest in sorted(terminals, key=lambda item: item.encode("ascii"))
        if terminals[digest].claim.lifecycle.state == "live"
    )
    if preferred_successor_digest is not None:
        selected = tuple(
            node for node in live if node.artifact_digest == preferred_successor_digest
        )
        if len(selected) == 1:
            return selected[0]
    if len(live) > 1:
        return tuple(_candidate(item) for item in live)
    if live:
        return live[0]
    retired = tuple(terminals.values())
    if not retired:
        raise ProposalIntegrityError("accepted Claim block-sync lineage has no terminal")
    return max(retired, key=lambda item: (item.generation, item.artifact_digest))


def _artifact_path(identity: ArtifactIdentity) -> str:
    if identity.kind == "ClaimType":
        return claim_type_path(identity.name)
    subject_kind, separator, subject_id = identity.name.partition("/")
    if identity.kind != "Subject" or not separator:
        raise ValueError("unsupported projection artifact backing identity")
    return subject_path(subject_kind, subject_id)


def _artifact_digest(*, identity: ArtifactIdentity, path: str, raw: bytes) -> str:
    if identity.kind == "ClaimType":
        claim_type = parse_claim_type(raw, path=path)
        if claim_type.identity != identity:
            raise ValueError("ClaimType backing identity does not reproduce")
        return claim_type_digest(claim_type).tagged
    subject = parse_subject(raw, path=path)
    if subject.identity != identity:
        raise ValueError("Subject backing identity does not reproduce")
    return subject_digest(subject).tagged


def _artifact_backing_state(
    instance: PlaybillInstance,
    *,
    stamp_coordinate: AcceptedCoordinate,
    current: AcceptedCoordinate,
    backing: ProjectionArtifactBackingV1,
) -> ProjectionArtifactBackingV1 | PlaybillBlockSyncReadResultV1 | None:
    """The artifact backing's current spelling when it moved, ``None`` when it did not."""

    path = _artifact_path(backing.identity)
    original_raw = instance.blob_at(stamp_coordinate.git_oid, path)
    if original_raw is None:
        return _refusal(
            status="refused",
            reason="block_backing_missing",
            detail="the declared artifact backing is absent at the marker coordinate",
        )
    try:
        original_digest = _artifact_digest(identity=backing.identity, path=path, raw=original_raw)
    except (PlaybillError, ValueError):
        return _refusal(
            status="refused",
            reason="block_backing_changed",
            detail="the artifact backing does not reproduce at its declared coordinate",
        )
    if original_digest != backing.artifact_digest:
        return _refusal(
            status="refused",
            reason="block_backing_changed",
            detail="the artifact backing digest does not reproduce at its declared coordinate",
            original_artifact_digest=original_digest,
        )
    current_raw = instance.blob_at(current.git_oid, path)
    if current_raw is None:
        return _refusal(
            status="unsyncable",
            reason="block_backing_missing",
            detail="the governed artifact backing is absent at the current coordinate",
            original_artifact_digest=original_digest,
        )
    try:
        current_digest = _artifact_digest(identity=backing.identity, path=path, raw=current_raw)
    except (PlaybillError, ValueError):
        return _refusal(
            status="unsyncable",
            reason="block_backing_changed",
            detail="the governed artifact backing does not reproduce at the current coordinate",
            original_artifact_digest=original_digest,
        )
    if current_digest == original_digest:
        return None
    return ProjectionArtifactBackingV1(
        identity=backing.identity,
        artifact_digest=current_digest,
    )


def _claim_backing_state(
    instance: PlaybillInstance,
    *,
    stamp_coordinate: AcceptedCoordinate,
    backing: ProjectionClaimBackingV1,
    preferred_successor_digest: str | None,
) -> tuple[ProjectionClaimBackingV1 | None, _ClaimNode, str] | PlaybillBlockSyncReadResultV1:
    """The Claim backing's terminal spelling, or the typed refusal its lineage earns."""

    path = claim_path(backing.identity.name)
    raw = instance.blob_at(stamp_coordinate.git_oid, path)
    if raw is None:
        return _refusal(
            status="refused",
            reason="block_backing_missing",
            detail="the declared Claim backing is absent at the marker coordinate",
        )
    original = parse_claim(raw, path=path)
    if (
        original.identity != backing.identity
        or claim_statement_digest(original.statement).tagged != backing.statement_digest
    ):
        return _refusal(
            status="refused",
            reason="block_backing_changed",
            detail="the marker backing does not reproduce at its declared coordinate",
        )
    original_digest = claim_artifact_digest(original).tagged
    nodes = _claim_nodes(instance, path=path)
    if original_digest not in nodes:
        raise ProposalIntegrityError("accepted Claim block-sync origin disappeared from history")
    terminal = _terminal_node(
        nodes=nodes,
        original_digest=original_digest,
        preferred_successor_digest=preferred_successor_digest,
    )
    if isinstance(terminal, tuple):
        return _refusal(
            status="refused",
            reason="block_successor_ambiguous",
            detail="the accepted Claim lineage has multiple live successor candidates",
            original_artifact_digest=original_digest,
            candidates=terminal,
        )
    if terminal.claim.lifecycle.state != "live":
        return _refusal(
            status="refused",
            reason="block_backing_retired",
            detail="the accepted Claim lineage terminates in retirement",
            original_artifact_digest=original_digest,
        )
    if claim_statement_digest(terminal.claim.statement).tagged == backing.statement_digest:
        return None, terminal, original_digest
    return (
        ProjectionClaimBackingV1(
            identity=ArtifactIdentity(kind="Claim", name=terminal.claim.identity.name),
            statement_digest=claim_statement_digest(terminal.claim.statement).tagged,
        ),
        terminal,
        original_digest,
    )


class ProjectionCheckContext:
    """Request-scoped reads at one revision and instant, shared across every block."""

    def __init__(
        self,
        instance: PlaybillInstance,
        *,
        coordinate: AcceptedProjectionCoordinate,
        evaluation_time: datetime,
        stamps: Sequence[ProjectionBlockStamp],
        facts_reader: _AcceptedQueryFactsRead | None = None,
        verdicts_by_identity: MutableMapping[str, ClaimVerdictResultAny] | None = None,
        resolution_statuses: Mapping[str, str] | None = None,
    ) -> None:
        self.instance = instance
        self.coordinate = coordinate
        self.accepted = AcceptedCoordinate.from_internal(coordinate)
        self.evaluation_time = ensure_utc(evaluation_time)
        with instance.accepted_history_reader(at=self.accepted) as reader:
            location = reader.generation_for_oid(coordinate.git_oid)
            if location is None:
                raise ProposalIntegrityError("selected projection coordinate is not accepted")
            self.generation = location.sequence
        self.facts = facts_reader or _AcceptedQueryFactsRead(instance, coordinate=coordinate)
        self.verdicts = verdicts_by_identity
        self.queries: dict[bytes, ProjectionQueryBackingV1 | PlaybillError | ValueError] = {}
        self.statuses: Mapping[str, str] | None = resolution_statuses
        self.visible_claim_ids: frozenset[str] | None = None
        self.claim_ids = {
            b.identity.qualified
            for stamp in stamps
            for b in stamp.backing
            if isinstance(b, ProjectionClaimBackingV1)
        }
        paths = tuple(
            sorted(
                {
                    claim_path(b.identity.name)
                    for stamp in stamps
                    for b in stamp.backing
                    if isinstance(b, ProjectionClaimBackingV1)
                }
            )
        )
        self.lineages = (
            read_claim_lineages(instance, paths=paths, at=self.accepted, defer_errors=True)
            if paths
            else {}
        )

    def _claim_status(self, identity: ArtifactIdentity) -> str:
        facts = self.facts.build()
        if self.statuses is None:
            self.statuses = claim_resolution_statuses(
                self.instance,
                claims=tuple(
                    row.accepted.claim
                    for row in facts.claims
                    if row.accepted.claim.identity.qualified in self.claim_ids
                ),
                at=self.accepted,
                evaluation_time=self.evaluation_time,
                verdicts_by_identity=self.verdicts,
            )
        if self.visible_claim_ids is None:
            subjects = {s.path: s for s in facts.subjects}
            providers = {p.identity.qualified: p for p in facts.providers}
            policy = QueryEvaluationPolicyV1(
                visible_verdicts=("contradicted", "stale", "supported", "uncovered", "unresolved"),
                visible_currency=("current", "not_applicable", "stale"),
                conflict_behavior="surface_conflicts",
            )
            self.visible_claim_ids = frozenset(
                row.accepted.claim.identity.qualified
                for row in facts.claims
                if row.accepted.claim.identity.qualified in self.claim_ids
                and claim_row_visibility(
                    row,
                    subject=subjects.get(row.subject_path),
                    providers=providers,
                    policy=policy,
                    evaluation_time=self.evaluation_time,
                )
                is not None
            )
        if identity.qualified not in self.visible_claim_ids:
            raise ValueError("Claim backing is not visible under the evaluation policy")
        assert self.statuses is not None
        return self.statuses[identity.name]

    def _query(self, backing: ProjectionQueryBackingV1) -> ProjectionQueryBackingV1:
        key = canonical_bytes(
            [
                backing.identity.qualified,
                [x.model_dump(mode="json") for x in backing.resolved_parameter_bindings],
            ]
        )
        if key not in self.queries:
            try:
                definition = accepted_query_definition(
                    self.instance, name=backing.identity.name, coordinate=self.coordinate
                )
                result = evaluate_accepted_query(
                    self.instance,
                    definition,
                    facts=self.facts.build,
                    coordinate=self.coordinate,
                    evaluation_time=self.evaluation_time,
                    parameters={x.name: x.value for x in backing.resolved_parameter_bindings},
                )
                if result.verdict != "completed" or result.truncation.clipped_budgets:
                    raise ValueError("query evaluation was refused or truncated")
                self.queries[key] = backing.model_copy(
                    update={
                        "definition_digest": definition.artifact_digest,
                        "semantic_result_digest": projection_query_semantic_result_digest(result),
                        "declared_evaluation_time": self.evaluation_time,
                    }
                )
            except (PlaybillError, ValueError) as exc:
                self.queries[key] = exc
        value = self.queries[key]
        if isinstance(value, (PlaybillError, ValueError)):
            raise value
        return value

    def read(self, request: PlaybillBlockSyncReadRequestV1) -> PlaybillBlockSyncReadResultV1:
        token = _LINEAGES.set((self.instance, self.lineages))
        try:
            return self._read(request)
        finally:
            _LINEAGES.reset(token)

    def _read(self, request: PlaybillBlockSyncReadRequestV1) -> PlaybillBlockSyncReadResultV1:
        stamp = request.stamp
        try:
            declared = AcceptedCoordinate.from_internal(
                self.instance.coordinate_for_oid(stamp.declared_coordinate.git_oid)
            )
        except PlaybillError:
            declared = None
        if declared != stamp.declared_coordinate:
            return _refusal(
                status="refused",
                reason="block_workspace_instance_mismatch",
                detail="the marker coordinate is not accepted by the attached instance",
            )
        issues: list[ProjectionDependencyIssueV1] = []
        moved: list[ProjectionBackingV1] = []
        current: list[ProjectionBackingV1] = []
        candidates: tuple[PlaybillBlockSyncSuccessorCandidateV1, ...] = ()
        original_digest = None
        current_digest = None
        for backing in stamp.backing:
            try:
                failure = None
                if isinstance(backing, ProjectionQueryBackingV1):
                    # Verify the definition binding at the origin without replaying an
                    # old observation against today's external availability.
                    origin = accepted_query_definition(
                        self.instance,
                        name=backing.identity.name,
                        coordinate=_resolve_coordinate(self.instance, declared),
                    )
                    if origin.artifact_digest != backing.definition_digest:
                        issues.append(
                            ProjectionDependencyIssueV1(
                                identity=backing.identity,
                                status="invalid",
                                reason="block_backing_changed",
                                detail="query definition does not reproduce at its origin",
                            )
                        )
                        continue
                    updated = self._query(backing)
                    current.append(updated)
                    if (updated.definition_digest, updated.semantic_result_digest) != (
                        backing.definition_digest,
                        backing.semantic_result_digest,
                    ):
                        moved.append(updated)
                elif isinstance(backing, ProjectionArtifactBackingV1):
                    state = _artifact_backing_state(
                        self.instance,
                        stamp_coordinate=declared,
                        current=self.accepted,
                        backing=backing,
                    )
                    if isinstance(state, PlaybillBlockSyncReadResultV1):
                        failure = state
                    else:
                        current.append(backing if state is None else state)
                        original_digest = backing.artifact_digest
                        current_digest = (
                            backing.artifact_digest if state is None else state.artifact_digest
                        )
                        if state is not None:
                            moved.append(state)
                else:
                    state_claim = _claim_backing_state(
                        self.instance,
                        stamp_coordinate=declared,
                        backing=backing,
                        preferred_successor_digest=request.preferred_successor_digest,
                    )
                    if isinstance(state_claim, PlaybillBlockSyncReadResultV1):
                        failure = state_claim
                    else:
                        updated_claim, terminal, original_digest = state_claim
                        # A lineage terminal may have disappeared at the selected revision.
                        if (
                            self.instance.blob_at(
                                self.accepted.git_oid, claim_path(backing.identity.name)
                            )
                            is None
                        ):
                            failure = _refusal(
                                status="unsyncable",
                                reason="block_backing_missing",
                                detail="Claim is absent at the selected coordinate",
                            )
                        else:
                            if self._claim_status(terminal.claim.identity) == "overturned":
                                issues.append(
                                    ProjectionDependencyIssueV1(
                                        identity=backing.identity,
                                        status="stale",
                                        reason="block_backing_changed",
                                        detail="Claim has been overturned",
                                    )
                                )
                            current.append(backing if updated_claim is None else updated_claim)
                            current_digest = terminal.artifact_digest
                            if updated_claim is not None:
                                moved.append(updated_claim)
                if failure is not None:
                    assert failure.reason is not None
                    kind: Literal["stale", "unchecked", "invalid"] = (
                        "unchecked"
                        if failure.reason == "block_successor_ambiguous"
                        else "stale"
                        if failure.reason == "block_backing_retired"
                        or failure.status == "unsyncable"
                        else "invalid"
                    )
                    issues.append(
                        ProjectionDependencyIssueV1(
                            identity=backing.identity,
                            status=kind,
                            reason=failure.reason,
                            detail=failure.detail or failure.reason,
                        )
                    )
                    candidates = failure.successor_candidates or candidates
                    original_digest = failure.original_artifact_digest or original_digest
            except (PlaybillError, ValueError) as exc:
                issues.append(
                    ProjectionDependencyIssueV1(
                        identity=backing.identity,
                        status="invalid"
                        if isinstance(exc, ProposalIntegrityError)
                        else "unchecked",
                        reason="block_query_unchecked"
                        if isinstance(backing, ProjectionQueryBackingV1)
                        else "block_backing_missing",
                        detail=str(exc),
                    )
                )
        # Do not discard successful sibling checks when one dependency fails.
        if issues:
            issue = min(issues, key=lambda i: {"invalid": 0, "unchecked": 1, "stale": 2}[i.status])
            return PlaybillBlockSyncReadResultV1(
                status="refused"
                if issue.status == "invalid"
                else "unchecked"
                if issue.status == "unchecked"
                else "unsyncable",
                reason=issue.reason,
                detail=issue.detail,
                issues=tuple(issues),
                moved_backings=tuple(moved),
                current_backings=tuple(current),
                original_artifact_digest=original_digest,
                successor_candidates=candidates
                if issue.reason == "block_successor_ambiguous"
                else (),
            )
        return PlaybillBlockSyncReadResultV1(
            status="successor" if moved else "current",
            coordinate=self.accepted,
            generation=self.generation,
            backing=current[0] if len(current) == 1 else None,
            artifact_digest=current_digest if len(current) == 1 else None,
            original_artifact_digest=original_digest if len(current) == 1 else None,
            current_backings=tuple(current),
            moved_backings=tuple(moved),
        )


def service_read_playbill_block_sync_backing(
    instance: PlaybillInstance, *, request: PlaybillBlockSyncReadRequestV1
) -> PlaybillBlockSyncReadResultV1:
    context = ProjectionCheckContext(
        instance,
        coordinate=_resolve_coordinate(instance, request.at),
        evaluation_time=request.evaluation_time or datetime.now(UTC),
        stamps=(request.stamp,),
    )
    return context.read(request)


def service_check_projection_blocks(
    instance: PlaybillInstance, *, request: PlaybillProjectionCheckRequestV1
) -> PlaybillProjectionCheckResultV1:
    instant = request.evaluation_time or datetime.now(UTC)
    context = ProjectionCheckContext(
        instance,
        coordinate=_resolve_coordinate(instance, request.at),
        evaluation_time=instant,
        stamps=request.stamps,
    )
    return PlaybillProjectionCheckResultV1(
        coordinate=context.accepted,
        evaluation_time=instant,
        results=tuple(
            context.read(PlaybillBlockSyncReadRequestV1(stamp=s)) for s in request.stamps
        ),
    )


__all__ = [
    "ProjectionCheckContext",
    "service_read_playbill_block_sync_backing",
    "service_check_projection_blocks",
]
