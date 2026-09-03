"""Accepted Claim lineage resolution for zero-authority block synchronization."""

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
from cruxible_client.contracts.authoring.models import (
    PlaybillBlockSyncReadRequestV1,
    SelfSourceBodyV1,
)
from cruxible_client.contracts.claims import claim_path
from cruxible_client.contracts.declared_blocks import (
    parse_projection_blocks,
)
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.repairs import RepairOperationV1, served_repair_for_refusal
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.coverage.adapter import WorkingSourceObservationV1
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1, CoverageCardBudgetV1
from cruxible_core.playbill.coverage.indexes import CoverageScanBudgetV1
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
from tests.test_playbill.test_authoring_insertions_v2 import (
    _activate,
    _observation,
    _submitted_publication,
    _successor_payload,
)


class _ServiceClient:
    def __init__(self, instance) -> None:  # type: ignore[no-untyped-def]
        self.instance = instance

    def read_playbill_block_sync_backing(self, instance_id, *, request):  # type: ignore[no-untyped-def]
        assert instance_id == self.instance.descriptor.instance_id
        return service_read_playbill_block_sync_backing(self.instance, request=request)

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


def test_body_only_amend_emits_sync_repair_and_converges_green(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon_root = tmp_path / "daemon"
    workspace_root = tmp_path / "writer"
    daemon_root.mkdir()
    workspace_root.mkdir()
    instance, owner, coordinator, actor, intent_id, preimage, _clock = _submitted_publication(
        daemon_root
    )
    prepared = coordinator.prepare_publication(
        intent_id,
        actor=actor,
        observation=_observation(preimage),
    )
    assert prepared.preparation is not None
    from cruxible_client.authoring.insertions import apply_playbill_publication
    from cruxible_core.playbill.authoring.insertions import publication_confirmation_from_source

    landed = apply_playbill_publication(
        preimage,
        intent_id=intent_id,
        expectation=prepared.expectation.model_dump(mode="json"),
        retained_body=b"status: ready\n",
    )
    confirmation = publication_confirmation_from_source(
        intent_id=intent_id,
        expectation=prepared.expectation,
        observation=_observation(landed.content),
    )
    assert confirmation is not None
    assert (
        coordinator.confirm_insertion(
            intent_id,
            actor=actor,
            observation=confirmation,
        ).outcome
        == "bound"
    )
    source = _workspace(
        workspace_root,
        instance_id=instance.descriptor.instance_id,
        content=landed.content,
    )

    original = coordinator.store.get(intent_id, actor_id=actor.actor_id)
    revised_body = b"status: body-only revision\n"
    body_only_payload = _successor_payload(
        original.semantic_identity,
        value="ready",
    ).model_copy(
        update={
            "source": SelfSourceBodyV1(
                content_base64=base64.b64encode(revised_body).decode("ascii")
            )
        }
    )
    successor = (
        AuthoringIntentCoordinator.for_instance(instance)
        .create(
            actor=actor,
            payload=body_only_payload,
            canonical_timestamp="2026-08-21T12:00:02.000000Z",
        )
        .intent
    )
    submitted = AuthoringIntentCoordinator.for_instance(instance).submit(
        successor.intent_id,
        actor=actor,
    )
    assert submitted.status.proposal_id is not None
    assert submitted.status.candidate_digest is not None
    _activate(
        instance,
        owner,
        proposal_id=submitted.status.proposal_id,
        candidate_digest=submitted.status.candidate_digest,
    )

    access_profile = CoverageAccessProfileV1(
        profile_id="body-only-block-sync-test",
        permitted_access_classes=("instance", "public"),
    )

    def observed_next():  # type: ignore[no-untyped-def]
        observation = observe_playbill_next_workspace(workspace_root)
        observation, coordinate = observe_playbill_next_workspace_with_coverage(
            _ServiceClient(instance),  # type: ignore[arg-type]
            instance.descriptor.instance_id,
            workspace_root,
            observation=observation,
            access_profile=access_profile.model_dump(mode="json"),
        )
        assert coordinate is not None
        return service_playbill_next(
            instance,
            request=PlaybillNextRequestV1(
                at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
                evaluation_time=datetime(2026, 8, 21, 13, tzinfo=UTC),
                access_profile=access_profile,
                workspace_observation=PlaybillNextWorkspaceObservationV1.model_validate(
                    observation
                ),
            ),
        )

    stale = next(
        item for item in observed_next().items if item.reason == "projection_backing_stale"
    )
    assert stale.repair.operation == "playbill.block.sync"
    assert stale.detail["stamped_body_digest"] == prepared.preparation.body_digest
    assert (
        stale.detail["terminal_body_digest"] == "sha256:" + hashlib.sha256(revised_body).hexdigest()
    )

    def corrupt_lineage(*_args: object, **_kwargs: object) -> None:
        raise ProposalIntegrityError("accepted Claim block-sync lineage contains a cycle")

    with monkeypatch.context() as scoped:
        scoped.setattr(
            "cruxible_core.service.playbill_next.service_read_playbill_block_sync_backing",
            corrupt_lineage,
        )
        degraded = observed_next()
    (lineage_row,) = tuple(
        item
        for item in degraded.items
        if item.detail.get("error_code") == "playbill.projection.backing_lineage_unreadable"
    )
    claim_id = prepared.preparation.stamp.backing[0].identity.name
    assert lineage_row.reason == "projection_marker_invalid"
    assert lineage_row.repair.command == (
        "cruxible playbill block repin repo.work-items "
        f"{prepared.preparation.stamp.block_id} --claim {claim_id}"
    )

    synced = sync_projection_blocks(
        _ServiceClient(instance),  # type: ignore[arg-type]
        instance.descriptor.instance_id,
        workspace=workspace_root,
        paths=(source,),
    )
    assert [item.outcome for item in synced.items] == ["synced"]
    assert revised_body in source.read_bytes()
    assert not [item for item in observed_next().items if item.reason == "projection_backing_stale"]


def test_two_writer_successor_sync_converges_without_mutating_accepted_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon_root = tmp_path / "daemon"
    workspace_root = tmp_path / "writer-one"
    daemon_root.mkdir()
    workspace_root.mkdir()
    (
        instance,
        owner,
        coordinator,
        actor,
        intent_id,
        _preimage,
        _clock,
    ) = _submitted_publication(daemon_root)
    prepared = coordinator.prepare_publication(
        intent_id,
        actor=actor,
        observation=_observation(_preimage),
    )
    assert prepared.preparation is not None
    from cruxible_client.authoring.insertions import apply_playbill_publication
    from cruxible_core.playbill.authoring.insertions import publication_confirmation_from_source

    landed = apply_playbill_publication(
        _preimage,
        intent_id=intent_id,
        expectation=prepared.expectation.model_dump(mode="json"),
        retained_body=b"status: ready\n",
    )
    confirmation = publication_confirmation_from_source(
        intent_id=intent_id,
        expectation=prepared.expectation,
        observation=_observation(landed.content),
    )
    assert confirmation is not None
    assert (
        coordinator.confirm_insertion(
            intent_id,
            actor=actor,
            observation=confirmation,
        ).outcome
        == "bound"
    )
    source = _workspace(
        workspace_root,
        instance_id=instance.descriptor.instance_id,
        content=b"PREFIX\n" + landed.content + b"SUFFIX\n",
    )
    stamped_content = source.read_bytes()
    draft = b"<!-- playbill:block:draft-note -->\ndraft\n<!-- /playbill:block:draft-note -->\n"
    source.write_bytes(stamped_content.removesuffix(b"SUFFIX\n") + draft + b"SUFFIX\n")
    original_stamp = prepared.preparation.stamp
    current = service_read_playbill_block_sync_backing(
        instance,
        request=PlaybillBlockSyncReadRequestV1(stamp=original_stamp),
    )
    assert current.status == "current"
    assert current.body == b"status: ready\n"

    original = coordinator.store.get(intent_id, actor_id=actor.actor_id)
    other = AuthoringIntentCoordinator.for_instance(instance)
    successor_payload = _successor_payload(original.semantic_identity, value="done").model_copy(
        update={
            "source": SelfSourceBodyV1(content_base64=base64.b64encode(b"done\n").decode("ascii"))
        }
    )
    successor = other.create(
        actor=AuthenticatedActor(actor_id=actor.actor_id),
        payload=successor_payload,
        canonical_timestamp="2026-08-21T12:00:02.000000Z",
    ).intent
    submitted = other.submit(successor.intent_id, actor=actor)
    assert submitted.status.proposal_id is not None, submitted.model_dump_json()
    assert submitted.status.candidate_digest is not None
    _activate(
        instance,
        owner,
        proposal_id=submitted.status.proposal_id,
        candidate_digest=submitted.status.candidate_digest,
    )
    terminal_payload = _successor_payload(original.semantic_identity, value="done").model_copy(
        update={
            "source": SelfSourceBodyV1(
                content_base64=base64.b64encode(b"done final\n").decode("ascii")
            )
        }
    )
    terminal = other.create(
        actor=actor,
        payload=terminal_payload,
        canonical_timestamp="2026-08-21T12:00:03.000000Z",
    ).intent
    terminal_submitted = other.submit(terminal.intent_id, actor=actor)
    assert terminal_submitted.status.proposal_id is not None, terminal_submitted.model_dump_json()
    assert terminal_submitted.status.candidate_digest is not None
    _activate(
        instance,
        owner,
        proposal_id=terminal_submitted.status.proposal_id,
        candidate_digest=terminal_submitted.status.candidate_digest,
    )
    accepted_before = instance.accepted_coordinate()
    tree_before = instance.tree_at(accepted_before.git_oid)
    history_before = instance.accepted_history()

    access_profile = CoverageAccessProfileV1(
        profile_id="block-sync-service-test",
        permitted_access_classes=("instance", "public"),
    )
    workspace_observation = observe_playbill_next_workspace(workspace_root)
    workspace_observation, observed_coordinate = observe_playbill_next_workspace_with_coverage(
        _ServiceClient(instance),  # type: ignore[arg-type]
        instance.descriptor.instance_id,
        workspace_root,
        observation=workspace_observation,
        access_profile=access_profile.model_dump(mode="json"),
    )
    assert observed_coordinate is not None
    assert observed_coordinate.git_oid == accepted_before.git_oid
    (source_observation,) = workspace_observation["source_observations"]  # type: ignore[index]
    assert source_observation["tag"] == "playbill-next-source-observation-v4"
    assert source_observation["scan_notes"] == ["coverage_source_mismatch"]
    assert source_observation["marker_notes"] == ["projection_block_unstamped"]

    next_result = service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(
            at=AcceptedCoordinate.from_internal(accepted_before),
            evaluation_time=datetime(2026, 8, 21, 13, tzinfo=UTC),
            access_profile=access_profile,
            workspace_observation=PlaybillNextWorkspaceObservationV1.model_validate(
                workspace_observation
            ),
        ),
    )
    (stale_row,) = tuple(
        item for item in next_result.items if item.reason == "projection_backing_stale"
    )
    assert any(item.reason == "projection_marker_invalid" for item in next_result.items)
    assert stale_row.repair.operation == "playbill.block.sync"
    assert stale_row.repair.command == "cruxible playbill block sync --all"

    result = sync_projection_blocks(
        _ServiceClient(instance),  # type: ignore[arg-type]
        instance.descriptor.instance_id,
        workspace=workspace_root,
        all_sources=True,
    )

    assert [item.outcome for item in result.items] == ["skipped", "synced"]
    assert result.items[0].reason == "block_unstamped"
    # The prose repair list this batch retired becomes the declared hand edit the
    # typed reason resolves to; a first stamp is authored, not run.
    assert result.items[0].repair == served_repair_for_refusal("block_unstamped")
    assert "explicit --claim or --query" in result.items[0].detail["message"]
    assert result.has_refusals is False
    content = source.read_bytes()
    assert content.startswith(b"PREFIX\n") and content.endswith(b"SUFFIX\n")
    blocks = parse_projection_blocks(
        content[len(b"PREFIX\n") : -len(b"SUFFIX\n")],
        source_id="repo.work-items",
        allow_bootstrap=True,
    )
    block = next(item for item in blocks if item.stamp is not None)
    assert block.stamp is not None
    assert block.stamp.backing[0].statement_digest != original_stamp.backing[0].statement_digest
    assert content[len(b"PREFIX\n") + block.body_start : len(b"PREFIX\n") + block.body_end] == (
        b"done final\n"
    )
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
    source.write_bytes(b"PREFIX\n" + landed.content + b"SUFFIX\n")
    ambiguous_sync = sync_projection_blocks(
        _ServiceClient(instance),  # type: ignore[arg-type]
        instance.descriptor.instance_id,
        workspace=workspace_root,
        paths=(source,),
    )
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
