"""Accepted Claim lineage resolution for zero-authority block synchronization."""

from __future__ import annotations

import base64
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.authoring.blocks import sync_projection_blocks
from cruxible_client.contracts.authoring.models import (
    PlaybillBlockSyncReadRequestV1,
    SelfSourceBodyV1,
)
from cruxible_client.contracts.claims import claim_path
from cruxible_client.contracts.declared_blocks import (
    ProjectionMarkerSummaryV1,
    parse_projection_blocks,
)
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.service.playbill_next import (
    PlaybillNextRequestV1,
    PlaybillNextSourceObservationV3,
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


def test_two_writer_successor_sync_converges_without_mutating_accepted_state(
    tmp_path: Path,
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

    next_result = service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(
            at=AcceptedCoordinate.from_internal(accepted_before),
            evaluation_time=datetime(2026, 8, 21, 13, tzinfo=UTC),
            access_profile=CoverageAccessProfileV1(
                profile_id="block-sync-service-test",
                permitted_access_classes=("instance",),
            ),
            workspace_observation=PlaybillNextWorkspaceObservationV1(
                source_observations=(
                    PlaybillNextSourceObservationV3(
                        tag="playbill-next-source-observation-v3",
                        source_id=original_stamp.source_id,
                        observed_source_digest="sha256:" + "1" * 64,
                        byte_length=len(landed.content),
                        marker_summaries=(
                            ProjectionMarkerSummaryV1(
                                stamp=original_stamp,
                                observed_body_digest=original_stamp.body_digest,
                                start_byte=0,
                                end_byte=len(landed.content),
                            ),
                        ),
                        occurrences=(),
                        scanned_commitment_digests=(),
                        scan_complete=True,
                        scan_notes=(),
                        marker_notes=(),
                    ),
                )
            ),
        ),
    )
    (stale_row,) = tuple(
        item for item in next_result.items if item.reason == "projection_backing_stale"
    )
    assert stale_row.repair.operation == "playbill.block.sync"
    assert stale_row.repair.command == "cruxible playbill block sync --all"

    result = sync_projection_blocks(
        _ServiceClient(instance),  # type: ignore[arg-type]
        instance.descriptor.instance_id,
        workspace=workspace_root,
        paths=(source,),
    )

    assert [item.outcome for item in result.items] == ["synced"]
    content = source.read_bytes()
    assert content.startswith(b"PREFIX\n") and content.endswith(b"SUFFIX\n")
    (block,) = parse_projection_blocks(
        content[len(b"PREFIX\n") : -len(b"SUFFIX\n")],
        source_id="repo.work-items",
    )
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
    ambiguous = _terminal_node(
        nodes={**nodes, terminal_node.artifact_digest: branched_terminal},
        original_digest=current.original_artifact_digest,
        preferred_successor_digest=None,
    )
    assert isinstance(ambiguous, tuple)
    assert len(ambiguous) == 2
    assert all(candidate.identity == original_stamp.backing[0].identity for candidate in ambiguous)

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
