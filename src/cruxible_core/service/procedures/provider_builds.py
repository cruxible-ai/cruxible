"""Governed provider retirement and moves, sized by their changed reverse pins."""

from __future__ import annotations

from collections.abc import Mapping

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.canonical import canonical_digest
from cruxible_client.contracts.named_references import name_provider, name_signing_provider
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.provider_installation import (
    ProviderMoveRequest,
    ProviderMoveResult,
    ProviderRetireRequest,
    ProviderRetireResult,
)
from cruxible_client.contracts.providers import (
    ProviderAny,
    ProviderBuild,
    parse_provider,
    provider_digest,
    provider_path,
    render_provider,
)
from cruxible_core.compiler.compiler import LIGHT_ARTIFACTS_COMPILER
from cruxible_core.proposals.proposals import ProposalAdmissionRequest
from cruxible_core.providers.build_moves import Mover, build_move, settle_changes
from cruxible_core.providers.builds import (
    indexed_exact_provider,
    indexed_move_pinners,
    indexed_newest_provider,
    live_pinners,
    resolve_provider_build,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from cruxible_core.service.change_preview import ChangeMode, admit_change_set, change_scope


def _submit(
    instance: PlaybillInstance,
    mode: ChangeMode,
    writes: Mapping[str, bytes],
    *,
    actor_id: str,
    timestamp: str,
) -> tuple[str, str | None, bool, str | None]:
    assert mode.head is not None
    candidate = instance.immutable_tree_at(mode.head.git_oid).fork()
    for path, content in writes.items():
        candidate[path] = content
    suffix = canonical_digest(
        "cruxible-provider-build-change-v1",
        {"base": mode.head.git_oid, "writes": {p: b.hex() for p, b in sorted(writes.items())}},
    )
    admitted = admit_change_set(
        instance,
        mode,
        actor_id=actor_id,
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/{actor_id}/provider-build-{suffix[:32]}",
            proposed_base_oid=mode.head.git_oid,
        ),
        candidate_tree=candidate,
        timestamp=timestamp,
    )
    status: str = admitted.status
    if status == "proposed" and not admitted.approval_required:
        assert admitted.proposal_id is not None
        activation = service_activate_playbill_proposal(
            instance,
            proposal_id=admitted.proposal_id,
            activated_by=actor_id,
        )
        if activation.status == "accepted":
            status = "accepted"
    return (
        status,
        admitted.proposal_id,
        admitted.approval_required,
        None if admitted.admitted else admitted.refusal_detail(),
    )


def _retired_provider(provider: ProviderAny, *, by_name: bool) -> ProviderAny:
    retired = provider.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=provider_digest(provider).tagged
            )
        }
    )
    if by_name:
        return (
            name_provider(retired)
            if isinstance(retired, ProviderBuild)
            else name_signing_provider(retired)
        )
    return retired


def service_retire_provider(
    instance: PlaybillInstance,
    request: ProviderRetireRequest,
    *,
    actor_id: str,
    timestamp: str,
) -> ProviderRetireResult:
    with change_scope(
        instance,
        dry_run=request.dry_run,
        at=request.at,
        kind="derived",
        operation="cruxible.provider.retire",
        describe=f"retiring provider {request.build}",
    ) as mode:
        assert mode.head is not None
        tree = instance.immutable_tree_at(mode.head.git_oid)
        name = request.build.removeprefix("Provider:")
        path = provider_path(name)
        content = tree.get(path)
        if content is None:
            return ProviderRetireResult(
                build=name,
                status="would_block" if mode.previewing else "blocked",
                detail="no such exact provider build",
                coordinate=AcceptedCoordinate.from_internal(mode.head),
            )
        provider = parse_provider(content, path=path)
        if provider.identity.name == "cruxible-builtin":
            return ProviderRetireResult(
                build=name,
                status="would_block" if mode.previewing else "blocked",
                detail="cruxible-builtin is core-owned",
                coordinate=AcceptedCoordinate.from_internal(mode.head),
            )
        if provider.lifecycle.state == "retired":
            return ProviderRetireResult(
                build=name,
                status="unchanged",
                coordinate=AcceptedCoordinate.from_internal(mode.head),
            )
        with instance.bind_accepted_projection(mode.head) as projection:
            pinners = live_pinners(
                projection.typed.connection,
                provider.identity.qualified,
                provider_digest(provider).tagged,
            )
        if pinners:
            return ProviderRetireResult(
                build=name,
                status="would_block" if mode.previewing else "blocked",
                pinned_by=pinners,
                detail=(
                    "live definitions pin this build; move them with cruxible provider move first"
                ),
                coordinate=AcceptedCoordinate.from_internal(mode.head),
            )
        retired = _retired_provider(
            provider, by_name=mode.head.compiler == LIGHT_ARTIFACTS_COMPILER
        )
        status, proposal_id, approval, detail = _submit(
            instance, mode, {path: render_provider(retired)}, actor_id=actor_id, timestamp=timestamp
        )
        return ProviderRetireResult(
            build=name,
            status={"accepted": "retired", "would_propose": "would_retire"}.get(status, status),  # type: ignore[arg-type]
            proposal_id=proposal_id,
            approval_required=approval,
            detail=detail,
            coordinate=AcceptedCoordinate.from_internal(mode.head),
        )


def service_move_provider(
    instance: PlaybillInstance,
    request: ProviderMoveRequest,
    *,
    actor_id: str,
    timestamp: str,
) -> ProviderMoveResult:
    with change_scope(
        instance,
        dry_run=request.dry_run,
        at=request.at,
        kind="derived",
        operation="cruxible.provider.move",
        describe=f"moving procedures on {request.provider}",
    ) as mode:
        assert mode.head is not None
        tree = instance.immutable_tree_at(mode.head.git_oid)
        name = request.provider.removeprefix("Provider:")
        target = (
            name
            if request.to == "newest"
            else (request.to if "/" in request.to else f"{name}/{request.to}")
        )
        with instance.bind_accepted_projection(mode.head) as projection:
            connection = projection.typed.connection
            row = (
                indexed_newest_provider(connection, name)
                if request.to == "newest"
                else indexed_exact_provider(connection, "Provider:" + target)
            )
            resolution = resolve_provider_build(
                target,
                rows=() if row is None else (row,),
                sequences={} if row is None else {row.identity: 0},
                coordinate=AcceptedCoordinate.from_internal(mode.head),
            )
        if resolution.resolved.partition("/")[0] != name:
            raise ValueError("the target build must belong to the requested provider")
        movers = []
        moves = {}
        with instance.bind_accepted_projection(mode.head) as projection:
            connection = projection.typed.connection
            for identity, path, source in indexed_move_pinners(
                connection,
                name,
                "Provider:" + resolution.resolved,
                request.only,
            ):
                kind, _, item_name = identity.partition(":")
                movers.append(
                    Mover(
                        ArtifactIdentity(kind=kind, name=item_name),
                        path,
                        source.partition(":")[2],
                        resolution.resolved,
                    )
                )
                if source not in moves:
                    moves[source] = build_move(tree, source.partition(":")[2], resolution.resolved)
            settled = settle_changes(
                tree,
                writes={},
                connection=connection,
                movers=movers,
                moves=moves,
                by_name=mode.head.compiler == LIGHT_ARTIFACTS_COMPILER,
            )
        if settled.refused or any(item.action == "refused" for item in settled.repins):
            status, proposal_id, approval, detail = (
                "would_block" if mode.previewing else "blocked",
                None,
                False,
                "; ".join(settled.refused)
                or "one or more definitions cannot move to this contract",
            )
        elif not settled.writes:
            status, proposal_id, approval, detail = "unchanged", None, False, None
        else:
            status, proposal_id, approval, detail = _submit(
                instance, mode, settled.writes, actor_id=actor_id, timestamp=timestamp
            )
        return ProviderMoveResult(
            provider=name,
            to=resolution,
            moved=tuple(settled.repins),
            stops=tuple(settled.stops),
            automation_carries=tuple(settled.automation_carries),
            carried_dependents=settled.carried,
            status=status,  # type: ignore[arg-type]
            proposal_id=proposal_id,
            approval_required=approval,
            detail=detail,
            coordinate=AcceptedCoordinate.from_internal(mode.head),
        )
