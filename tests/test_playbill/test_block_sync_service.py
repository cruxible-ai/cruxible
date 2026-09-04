"""Currency verdicts over a declared block's held backings; nothing converges.

`playbill block sync` used to resolve one Claim backing to the accepted body a
block had been published from and write that body back into the page. Nothing
renders a projection block any more -- it is prose an agent wrote, held to an
explicit list of accepted Claims and artifacts -- so the only question accepted
state can answer about one is whether every member of that list is still there,
still live, and still saying what it said. These tests hold the daemon read to
that verdict, the client to reporting it, and both to leaving every byte of the
page exactly where the author put it.

The lineage-unreadable row `next` used to raise out of this read has no test
here, because it has no producer: `_projection_items` no longer consults the
block-sync read at all, so no failure of that read can reach the repair queue.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client import contracts
from cruxible_client.authoring.blocks import sync_projection_blocks
from cruxible_client.authoring.workspace import (
    observe_playbill_next_workspace,
    observe_playbill_next_workspace_with_coverage,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import (
    AuthoringExistingClaimDispositionV1,
    PlaybillBlockSyncReadRequestV1,
    SelfSourceBodyV1,
)
from cruxible_client.contracts.claims import (
    LiteralClaimObject,
    claim_path,
    claim_statement_digest,
    parse_claim,
)
from cruxible_client.contracts.declared_blocks import (
    ProjectionBlockStampV1,
    ProjectionClaimBackingV1,
    frame_projection_block,
    parse_projection_blocks,
)
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.repairs import RepairOperationV1, served_repair_for_refusal
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.coverage.adapter import WorkingSourceObservationV1
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1, CoverageCardBudgetV1
from cruxible_core.playbill.coverage.indexes import CoverageScanBudgetV1
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.service.documents import (
    PlaybillAcceptedCoordinate as ServiceAcceptedCoordinate,
)
from cruxible_core.service import playbill_projection_sync
from cruxible_core.service.playbill_coverage import service_resolve_playbill_coverage
from cruxible_core.service.playbill_next import (
    PlaybillNextRequestV1,
    PlaybillNextWorkspaceObservationV1,
    service_playbill_next,
)
from cruxible_core.service.playbill_projection_sync import (
    _claim_nodes,
    _terminal_node,
    service_read_playbill_block_sync_backing,
)
from cruxible_core.service.playbill_publications import service_declare_playbill_block
from tests.test_playbill.test_authoring_insertions_v2 import (
    _activate,
    _registered_publication,
    _submitted_publication,
    _successor_payload,
)
from tests.test_playbill.test_authoring_preflight import _self_source_payload

ACCESS_PROFILE = CoverageAccessProfileV1(
    profile_id="block-sync-service-test",
    permitted_access_classes=("instance", "public"),
)
EVALUATION_TIME = datetime(2026, 8, 21, 13, tzinfo=UTC)


class _ServiceClient:
    def __init__(self, instance) -> None:  # type: ignore[no-untyped-def]
        self.instance = instance

    def read_playbill_block_sync_backing(self, instance_id, *, request):  # type: ignore[no-untyped-def]
        assert instance_id == self.instance.descriptor.instance_id
        return service_read_playbill_block_sync_backing(self.instance, request=request)

    def declare_playbill_block(self, instance_id, stamp):  # type: ignore[no-untyped-def]
        assert instance_id == self.instance.descriptor.instance_id
        return service_declare_playbill_block(
            self.instance,
            actor_id="owner",
            stamp=ProjectionBlockStampV1.model_validate(stamp),
            declared_at="2026-09-10T12:00:00.000000Z",
        )

    def resolve_playbill_coverage(  # type: ignore[no-untyped-def]
        self,
        instance_id,
        *,
        observations,
        at=None,
        budget=None,
        scan_budget=None,
    ):
        assert instance_id == self.instance.descriptor.instance_id
        result = service_resolve_playbill_coverage(
            self.instance,
            instance_id=instance_id,
            observations=tuple(
                WorkingSourceObservationV1.model_validate(item) for item in observations
            ),
            at=(
                None
                if at is None
                else ServiceAcceptedCoordinate.model_validate(
                    at.model_dump(mode="json") if hasattr(at, "model_dump") else at
                )
            ),
            budget=None if budget is None else CoverageCardBudgetV1.model_validate(budget),
            scan_budget=(
                None if scan_budget is None else CoverageScanBudgetV1.model_validate(scan_budget)
            ),
        )
        return contracts.PlaybillCoverageResult(
            coordinate=contracts.PlaybillAcceptedCoordinate.model_validate(
                result.at.model_dump(mode="json")
            ),
            result=result.model_dump(mode="json"),
        )


def _workspace(root: Path, *, instance_id: str, content: bytes) -> Path:
    playbill = root / ".playbill"
    playbill.mkdir()
    (playbill / "coverage.json").write_text(
        "{"
        '"tag":"playbill-coverage-workspace-config-v2",'
        '"server_url":"https://sync.example.test",'
        f'"instance_id":"{instance_id}"'
        "}",
        encoding="utf-8",
    )
    (playbill / "sources.yaml").write_text(
        """\
tag: playbill-source-catalog-v1
catalog_kind: portable
entries:
  - name: repo.work-items
    locator: work-items.md
    document_id: work-items
    document_kind: runbook
    title: Work items
    media_type: text/markdown
    compiler_profile: document-v1
    required_tier: governed_write
    governance_scope: [Document:work-items]
""",
        encoding="utf-8",
    )
    source = root / "work-items.md"
    source.write_bytes(content)
    return source


def _observe(instance: PlaybillInstance, workspace_root: Path):  # type: ignore[no-untyped-def]
    """The workspace observation `next` reads, resolved through real coverage."""

    observation = observe_playbill_next_workspace(workspace_root)
    observation, coordinate = observe_playbill_next_workspace_with_coverage(
        _ServiceClient(instance),  # type: ignore[arg-type]
        instance.descriptor.instance_id,
        workspace_root,
        observation=observation,
        access_profile=ACCESS_PROFILE.model_dump(mode="json"),
    )
    assert coordinate is not None
    return observation, coordinate


def _next(instance: PlaybillInstance, observation: object):  # type: ignore[no-untyped-def]
    return service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(
            at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
            evaluation_time=EVALUATION_TIME,
            access_profile=ACCESS_PROFILE,
            workspace_observation=PlaybillNextWorkspaceObservationV1.model_validate(observation),
        ),
    )


def _accept_successor(
    instance: PlaybillInstance,
    owner: object,
    actor: AuthenticatedActor,
    *,
    claim_id: str,
    value: str,
    body: bytes,
    timestamp: str,
) -> None:
    """Accept one successor to ``claim_id``, carrying ``value`` and ``body``."""

    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    successor = coordinator.create(
        actor=actor,
        payload=_successor_payload(claim_id, value=value).model_copy(
            update={
                "source": SelfSourceBodyV1(content_base64=base64.b64encode(body).decode("ascii"))
            }
        ),
        canonical_timestamp=timestamp,
    ).intent
    submitted = coordinator.submit(successor.intent_id, actor=actor)
    assert submitted.status.proposal_id is not None, submitted.model_dump_json()
    assert submitted.status.candidate_digest is not None
    _activate(
        instance,
        owner,
        proposal_id=submitted.status.proposal_id,
        candidate_digest=submitted.status.candidate_digest,
    )


def _accept_sibling_claim(
    instance: PlaybillInstance,
    owner: object,
    actor: AuthenticatedActor,
    *,
    value: str,
    dispositioned: tuple[str, ...],
    timestamp: str,
) -> str:
    """Accept one more live Claim beside the published one, and name it.

    A held list needs members. The ClaimType this fixture seeds is
    single-cardinality over one subject, so every sibling has to disposition the
    Claims already living in that (subject, predicate, qualifier) slot;
    `not_tested` is the honest disposition, because a sibling reading of the
    same work item neither confirms nor refutes the readings already there. The
    dispositions are sorted here rather than at the call site because the Claim
    ids the coordinator mints are random, so insertion order is not sorted order
    and the payload refuses an unsorted list.
    """

    payload = _self_source_payload()
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    intent = coordinator.create(
        actor=actor,
        payload=payload.model_copy(
            update={
                "statement": payload.statement.model_copy(
                    update={"object": LiteralClaimObject(value=value)}
                ),
                "rationale": f"The writer observed the work item {value}.",
                "source": SelfSourceBodyV1(
                    content_base64=base64.b64encode(f"status: {value}\n".encode()).decode("ascii")
                ),
                "existing_claim_dispositions": tuple(
                    AuthoringExistingClaimDispositionV1(claim_id=name, disposition="not_tested")
                    for name in sorted(set(dispositioned), key=lambda item: item.encode("ascii"))
                ),
            }
        ),
        canonical_timestamp=timestamp,
    ).intent
    submitted = coordinator.submit(intent.intent_id, actor=actor)
    assert submitted.status.proposal_id is not None, submitted.model_dump_json()
    assert submitted.status.candidate_digest is not None
    _activate(
        instance,
        owner,
        proposal_id=submitted.status.proposal_id,
        candidate_digest=submitted.status.candidate_digest,
    )
    return coordinator.store.get(intent.intent_id, actor_id=actor.actor_id).semantic_identity


def _claim_backing(instance: PlaybillInstance, name: str) -> ProjectionClaimBackingV1:
    """The backing entry a stamp would carry for the live Claim ``name``."""

    path = claim_path(name)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    claim = parse_claim(tree[path], path=path)
    return ProjectionClaimBackingV1(
        identity=ArtifactIdentity(kind="Claim", name=name),
        statement_digest=claim_statement_digest(claim.statement).tagged,
    )


def test_a_body_only_successor_is_reported_stale_and_no_byte_is_written(
    tmp_path: Path,
) -> None:
    """The currency check sees a re-authored Claim the statement check cannot.

    A body-only successor keeps the statement it inherited and replaces only the
    self-source body behind it. The stamp holds the Claim, not its prose, so the
    daemon read answers `successor` -- the artifact the block declared is no
    longer the artifact the lineage terminates in -- and `block sync` reports the
    block `stale`. It reports it and stops: there is no accepted body to write
    back, so the page comes out of the call byte for byte as it went in.

    `next` says nothing about the same block, and that is not a gap. `next`
    compares the STATEMENT each backing committed against the statement the live
    Claim makes, and this successor did not touch the statement. The two verbs
    answer two different questions, and this is the case that separates them.
    """

    daemon_root = tmp_path / "daemon"
    workspace_root = tmp_path / "writer"
    daemon_root.mkdir()
    workspace_root.mkdir()
    instance, owner, coordinator, actor, intent_id, preimage, _clock = _submitted_publication(
        daemon_root
    )
    bound, landed = _registered_publication(instance, coordinator, actor, intent_id, preimage)
    assert bound.preparation is not None
    stamp = bound.preparation.stamp
    source = _workspace(
        workspace_root,
        instance_id=instance.descriptor.instance_id,
        content=landed,
    )

    settled = service_read_playbill_block_sync_backing(
        instance,
        request=PlaybillBlockSyncReadRequestV1(stamp=stamp),
    )
    assert settled.status == "current"
    assert settled.moved_backings == ()
    # The single held backing is echoed back at its current spelling even when
    # nothing moved, because `block repin --backing DIGEST` asks this read to
    # name the one artifact it should re-stamp.
    assert settled.backing == stamp.backing[0]
    assert settled.body_content_base64 is None
    assert settled.body_digest is None

    original = coordinator.store.get(intent_id, actor_id=actor.actor_id)
    _accept_successor(
        instance,
        owner,
        actor,
        claim_id=original.semantic_identity,
        value="ready",
        body=b"status: body-only revision\n",
        timestamp="2026-08-21T12:00:02.000000Z",
    )

    moved = service_read_playbill_block_sync_backing(
        instance,
        request=PlaybillBlockSyncReadRequestV1(stamp=stamp),
    )
    assert moved.status == "successor"
    assert tuple(item.identity.qualified for item in moved.moved_backings) == (
        stamp.backing[0].identity.qualified,
    )
    assert moved.body_content_base64 is None
    assert moved.body_digest is None

    observation, _coordinate = _observe(instance, workspace_root)
    assert not [
        item
        for item in _next(instance, observation).items
        if item.reason == "projection_backing_stale"
    ]

    accepted_before = instance.accepted_coordinate()
    tree_before = instance.tree_at(accepted_before.git_oid)
    history_before = instance.accepted_history()
    page_before = source.read_bytes()

    result = sync_projection_blocks(
        _ServiceClient(instance),  # type: ignore[arg-type]
        instance.descriptor.instance_id,
        workspace=workspace_root,
        paths=(source,),
    )

    (item,) = result.items
    assert item.outcome == "stale"
    assert item.reason == "block_backing_changed"
    assert item.repair == RepairOperationV1(
        operation="playbill.block.repin",
        arguments={"source_id": "repo.work-items", "block_id": stamp.block_id},
    )
    assert item.detail["moved_backings"] == [stamp.backing[0].identity.qualified]
    assert item.detail["backing_count"] == 1
    # A finding this verb cannot repair still has to fail the exit code of the
    # sync an activation runs as its last step.
    assert result.has_refusals is True
    assert result.would_change is False
    assert result.changed_file_count == 0
    assert source.read_bytes() == page_before
    assert instance.accepted_coordinate() == accepted_before
    assert instance.tree_at(accepted_before.git_oid) == tree_before
    assert instance.accepted_history() == history_before


def test_a_moved_statement_reaches_next_and_sync_without_either_rewriting_the_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two accepted successors move the statement, and neither verb converges.

    The page here is what a writer's worktree actually looks like: a stamped
    block a publication left behind, prose either side of it, and an unstamped
    draft block below. Everything that block declares -- its held Claim, its
    unstamped neighbour, its lineage -- is checked without writing, and the
    accepted state the checks read is the same coordinate, tree and history
    afterwards as before.

    The lineage laws come last, because a branch and a cycle are the two shapes
    that make "which artifact does this backing mean now?" unanswerable, and
    both must refuse by name rather than pick.
    """

    daemon_root = tmp_path / "daemon"
    workspace_root = tmp_path / "writer-one"
    daemon_root.mkdir()
    workspace_root.mkdir()
    instance, owner, coordinator, actor, intent_id, preimage, _clock = _submitted_publication(
        daemon_root
    )
    bound, landed = _registered_publication(instance, coordinator, actor, intent_id, preimage)
    assert bound.preparation is not None
    original_stamp = bound.preparation.stamp
    source = _workspace(
        workspace_root,
        instance_id=instance.descriptor.instance_id,
        content=b"PREFIX\n" + landed + b"SUFFIX\n",
    )
    stamped_content = source.read_bytes()
    draft = b"<!-- playbill:block:draft-note -->\ndraft\n<!-- /playbill:block:draft-note -->\n"
    source.write_bytes(stamped_content.removesuffix(b"SUFFIX\n") + draft + b"SUFFIX\n")

    current = service_read_playbill_block_sync_backing(
        instance,
        request=PlaybillBlockSyncReadRequestV1(stamp=original_stamp),
    )
    assert current.status == "current"
    assert current.body_content_base64 is None and current.body_digest is None

    original = coordinator.store.get(intent_id, actor_id=actor.actor_id)
    _accept_successor(
        instance,
        owner,
        actor,
        claim_id=original.semantic_identity,
        value="done",
        body=b"done\n",
        timestamp="2026-08-21T12:00:02.000000Z",
    )
    _accept_successor(
        instance,
        owner,
        actor,
        claim_id=original.semantic_identity,
        value="done",
        body=b"done final\n",
        timestamp="2026-08-21T12:00:03.000000Z",
    )
    accepted_before = instance.accepted_coordinate()
    tree_before = instance.tree_at(accepted_before.git_oid)
    history_before = instance.accepted_history()

    workspace_observation, observed_coordinate = _observe(instance, workspace_root)
    assert observed_coordinate.git_oid == accepted_before.git_oid
    (source_observation,) = workspace_observation["source_observations"]  # type: ignore[index]
    # The observation reports what it saw about the page itself. That contract
    # belongs to the scan, not to the sync, and it survived the sync rewrite
    # untouched: a catalogued source whose bytes are not the coverage card's,
    # carrying a block nobody has stamped yet.
    assert source_observation["tag"] == "playbill-next-source-observation-v4"
    assert source_observation["scan_notes"] == ["coverage_source_mismatch"]
    assert source_observation["marker_notes"] == ["projection_block_unstamped"]

    next_result = _next(instance, workspace_observation)
    (stale_row,) = tuple(
        item for item in next_result.items if item.reason == "projection_backing_stale"
    )
    assert any(item.reason == "projection_marker_invalid" for item in next_result.items)
    assert stale_row.detail["stale_backings"] == [original_stamp.backing[0].identity.qualified]
    # There is no syncable spelling of this repair any more. Nothing renders the
    # prose, so the only answer is to read the block against the state that
    # moved under it and re-declare the list it still means.
    assert stale_row.repair.operation == "playbill.block.repin"
    assert stale_row.repair.command == (
        f"cruxible playbill block repin repo.work-items {original_stamp.block_id}"
    )

    page_before = source.read_bytes()
    result = sync_projection_blocks(
        _ServiceClient(instance),  # type: ignore[arg-type]
        instance.descriptor.instance_id,
        workspace=workspace_root,
        all_sources=True,
    )

    assert [item.outcome for item in result.items] == ["skipped", "stale"]
    assert result.items[0].reason == "block_unstamped"
    # The prose repair list this batch retired becomes the declared hand edit the
    # typed reason resolves to; a first stamp is authored, not run.
    assert result.items[0].repair == served_repair_for_refusal("block_unstamped")
    assert "explicit --claim or --query" in result.items[0].detail["message"]
    assert result.items[1].reason == "block_backing_changed"
    assert result.has_refusals is True
    assert source.read_bytes() == page_before

    content = source.read_bytes()
    assert content.startswith(b"PREFIX\n") and content.endswith(b"SUFFIX\n")
    blocks = parse_projection_blocks(
        content[len(b"PREFIX\n") : -len(b"SUFFIX\n")],
        source_id="repo.work-items",
        allow_bootstrap=True,
    )
    block = next(item for item in blocks if item.stamp is not None)
    assert block.stamp is not None
    # The stamp still names the statement it was published against. A report is
    # not a re-declaration: only `block repin` may move that digest.
    assert block.stamp.backing[0].statement_digest == original_stamp.backing[0].statement_digest
    assert instance.accepted_coordinate() == accepted_before
    assert instance.tree_at(accepted_before.git_oid) == tree_before
    assert instance.accepted_history() == history_before

    assert current.original_artifact_digest is not None
    nodes = _claim_nodes(instance, path=claim_path(original.semantic_identity))
    terminal_node = max(nodes.values(), key=lambda node: node.generation)
    branched_terminal = replace(
        terminal_node,
        claim=terminal_node.claim.model_copy(
            update={
                "lifecycle": terminal_node.claim.lifecycle.model_copy(
                    update={"predecessor_digest": current.original_artifact_digest}
                )
            }
        ),
    )
    branched_nodes = {**nodes, terminal_node.artifact_digest: branched_terminal}
    ambiguous = _terminal_node(
        nodes=branched_nodes,
        original_digest=current.original_artifact_digest,
        preferred_successor_digest=None,
    )
    assert isinstance(ambiguous, tuple)
    assert len(ambiguous) == 2
    assert all(candidate.identity == original_stamp.backing[0].identity for candidate in ambiguous)
    monkeypatch.setattr(
        playbill_projection_sync,
        "_claim_nodes",
        lambda _instance, *, path: branched_nodes,
    )
    ambiguous_read = service_read_playbill_block_sync_backing(
        instance,
        request=PlaybillBlockSyncReadRequestV1(stamp=original_stamp),
    )
    assert ambiguous_read.status == "refused"
    assert ambiguous_read.reason == "block_successor_ambiguous"
    assert len(ambiguous_read.successor_candidates) == 2
    assert [candidate.artifact_digest for candidate in ambiguous_read.successor_candidates] == (
        sorted(
            (candidate.artifact_digest for candidate in ambiguous_read.successor_candidates),
            key=lambda value: value.encode("ascii"),
        )
    )
    source.write_bytes(b"PREFIX\n" + landed + b"SUFFIX\n")
    ambiguous_sync = sync_projection_blocks(
        _ServiceClient(instance),  # type: ignore[arg-type]
        instance.descriptor.instance_id,
        workspace=workspace_root,
        paths=(source,),
    )
    assert ambiguous_sync.items[0].outcome == "refused"
    assert ambiguous_sync.items[0].reason == "block_successor_ambiguous"
    assert ambiguous_sync.items[0].repair == RepairOperationV1(
        operation="playbill.block.repin",
        arguments={
            "source_id": "repo.work-items",
            "block_id": original_stamp.block_id,
            "backing_candidates": [
                candidate.artifact_digest for candidate in ambiguous_read.successor_candidates
            ],
        },
    )

    original_node = nodes[current.original_artifact_digest]
    cyclic_original = replace(
        original_node,
        claim=original_node.claim.model_copy(
            update={
                "lifecycle": original_node.claim.lifecycle.model_copy(
                    update={"predecessor_digest": terminal_node.artifact_digest}
                )
            }
        ),
    )
    with pytest.raises(ProposalIntegrityError, match="lineage contains a cycle"):
        _terminal_node(
            nodes={**nodes, current.original_artifact_digest: cyclic_original},
            original_digest=current.original_artifact_digest,
            preferred_successor_digest=None,
        )


def test_a_block_holding_three_claims_reports_one_ordinary_outcome(tmp_path: Path) -> None:
    """The one-backing gate is gone: a held list of three is not unsyncable.

    While `block sync` converged a body, a block could only be synchronized if
    exactly one Claim backed it -- anything else had no single accepted body to
    resolve to, and the verb refused the block by name. Nothing is resolved any
    more, so nothing about a list of three is special: the read walks every
    member, and the block gets one row for the whole list, carrying the count of
    what it holds.
    """

    daemon_root = tmp_path / "daemon"
    workspace_root = tmp_path / "writer"
    daemon_root.mkdir()
    workspace_root.mkdir()
    instance, owner, coordinator, actor, intent_id, preimage, _clock = _submitted_publication(
        daemon_root
    )
    bound, _landed = _registered_publication(instance, coordinator, actor, intent_id, preimage)
    assert bound.preparation is not None
    published = bound.preparation.stamp.backing[0].identity.name
    held = [published]
    for index, value in enumerate(("blocked", "done")):
        held.append(
            _accept_sibling_claim(
                instance,
                owner,
                actor,
                value=value,
                dispositioned=tuple(held),
                timestamp=f"2026-08-21T12:0{index + 1}:00.000000Z",
            )
        )

    body = b"Three accepted readings of one work item, in one paragraph.\n"
    stamp = ProjectionBlockStampV1(
        source_id="repo.work-items",
        block_id="held-three",
        declared_generation=instance.accepted_history()[-1].sequence,
        declared_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        backing=tuple(
            _claim_backing(instance, name)
            for name in sorted(held, key=lambda value: f"Claim:{value}".encode("utf-8"))
        ),
        body_digest="sha256:" + hashlib.sha256(body).hexdigest(),
    )
    source = _workspace(
        workspace_root,
        instance_id=instance.descriptor.instance_id,
        content=frame_projection_block(stamp=stamp, body=body),
    )

    read = service_read_playbill_block_sync_backing(
        instance,
        request=PlaybillBlockSyncReadRequestV1(stamp=stamp),
    )
    assert read.status == "current"
    assert read.moved_backings == ()
    # A list has no single member to re-stamp, so the singular fields stay
    # empty rather than naming an arbitrary one of the three.
    assert read.backing is None
    assert read.original_artifact_digest is None
    assert read.artifact_digest is None

    page_before = source.read_bytes()
    result = sync_projection_blocks(
        _ServiceClient(instance),  # type: ignore[arg-type]
        instance.descriptor.instance_id,
        workspace=workspace_root,
        paths=(source,),
    )

    (item,) = result.items
    assert item.outcome == "unchanged"
    assert item.reason is None
    assert item.detail["backing_count"] == 3
    assert result.has_refusals is False
    assert source.read_bytes() == page_before


def test_a_hand_edited_body_is_dirty_until_accept_local_restamps_it(tmp_path: Path) -> None:
    """A body that moved away from its stamp is a finding, never an overwrite.

    `--discard-local` used to name the losing side of a convergence: the local
    prose was thrown away and the accepted body written over it. There is no
    accepted body to write, so the flag is renamed to what it now does --
    `--accept-local` says the local prose IS the block -- and it records that by
    re-stamping the block on it. Silencing the row without a re-stamp would
    claim an alignment nothing proved, and `next` would go on reporting the
    same page dirty. The prose itself is never touched.
    """

    daemon_root = tmp_path / "daemon"
    workspace_root = tmp_path / "writer"
    daemon_root.mkdir()
    workspace_root.mkdir()
    instance, _owner, coordinator, actor, intent_id, preimage, _clock = _submitted_publication(
        daemon_root
    )
    bound, landed = _registered_publication(instance, coordinator, actor, intent_id, preimage)
    assert bound.preparation is not None
    stamp = bound.preparation.stamp
    source = _workspace(
        workspace_root,
        instance_id=instance.descriptor.instance_id,
        content=landed,
    )
    edited = source.read_bytes().replace(
        b"status: ready\n",
        b"A maintainer rewrote this paragraph by hand.\n",
    )
    assert edited != source.read_bytes()
    source.write_bytes(edited)

    dirty = sync_projection_blocks(
        _ServiceClient(instance),  # type: ignore[arg-type]
        instance.descriptor.instance_id,
        workspace=workspace_root,
        paths=(source,),
    )

    (dirty_item,) = dirty.items
    assert dirty_item.outcome == "dirty"
    assert dirty_item.reason == "block_locally_modified"
    assert dirty_item.repair == RepairOperationV1(
        operation="playbill.block.repin",
        arguments={"source_id": "repo.work-items", "block_id": stamp.block_id},
    )
    assert dirty_item.detail["last_synced_body_digest"] == stamp.body_digest
    assert dirty.has_refusals is True
    assert source.read_bytes() == edited
    dirty_observation, _dirty_coordinate = _observe(instance, workspace_root)
    assert any(
        item.reason == "projection_dirty" for item in _next(instance, dirty_observation).items
    )

    accepted = sync_projection_blocks(
        _ServiceClient(instance),  # type: ignore[arg-type]
        instance.descriptor.instance_id,
        workspace=workspace_root,
        paths=(source,),
        accept_local_paths=(source,),
    )

    (accepted_item,) = accepted.items
    assert accepted_item.outcome == "synced"
    assert accepted_item.reason is None
    assert accepted_item.detail["local_body_accepted"] is True
    assert accepted_item.detail["stamped_body_digest"] == stamp.body_digest
    assert accepted.has_refusals is False

    # The prose is untouched; the stamp moved onto it, and the instance holds
    # the declaration that records the alignment.
    restamped = source.read_bytes()
    (block,) = parse_projection_blocks(restamped, source_id="repo.work-items", allow_bootstrap=True)
    assert block.stamp is not None
    assert block.stamp.body_digest == block.body_digest
    assert block.stamp.backing == stamp.backing
    assert (
        b"A maintainer rewrote this paragraph by hand."
        in (restamped[block.body_start : block.body_end])
    )

    # A second pass reports it clean, and nothing is left dirty.
    settled = sync_projection_blocks(
        _ServiceClient(instance),  # type: ignore[arg-type]
        instance.descriptor.instance_id,
        workspace=workspace_root,
        paths=(source,),
    )
    (settled_item,) = settled.items
    assert settled_item.outcome == "unchanged"
    assert settled.has_refusals is False

    # The two surfaces agree, which is the point: `next` stopped reporting the
    # page dirty because the alignment was recorded, not because a flag hid it.
    observation, _coordinate = _observe(instance, workspace_root)
    assert all(item.reason != "projection_dirty" for item in _next(instance, observation).items)
