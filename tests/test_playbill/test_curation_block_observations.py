"""Explicit, attributed G9 block-observation ingestion."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.declared_blocks import (
    ProjectionBlockStampV1,
    ProjectionClaimBackingV1,
    ProjectionMarkerSummaryV1,
)
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.curation_detectors import _block_churn
from cruxible_core.playbill.service.documents import service_propose_playbill_document
from cruxible_core.service.playbill_curation import (
    BlockObservationV1,
    PlaybillCurationListRequestV1,
    build_block_observation,
    service_list_playbill_curation,
)
from cruxible_core.service.playbill_next import (
    PlaybillNextSourceObservationV2,
    PlaybillNextSourceObservationV3,
    PlaybillNextWorkspaceObservationV1,
)
from tests.test_playbill._knowledge_loop_support import accept_proposal
from tests.test_playbill._support import initialize_local

NOW = datetime(2026, 8, 26, 15, 0, tzinfo=timezone.utc)


def _actor() -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="service_account",
        actor_id="curator",
        org_id="org-test",
        operation_id="op-curation-list",
        timestamp=NOW,
    )


def _instance_with_document(tmp_path: Path):  # type: ignore[no-untyped-def]
    instance, owner = initialize_local(tmp_path)
    body = instance.store_document_body(b"# Runbook\n")
    proposed = service_propose_playbill_document(
        instance,
        shell=DocumentShell(
            identity="document:runbook",
            document_kind="runbook",
            title="Runbook",
            media_type="text/markdown",
            body_digest=body.digest,
            authority=DocumentAuthority(
                required_tier="graph_write",
                approval_roles=("owner",),
            ),
            governance_scope=("project:test",),
            lifecycle=DocumentLifecycle(revision=1),
        ),
        actor_id="owner",
        proposal_name="runbook",
        timestamp="2026-08-26T15:00:00.000000Z",
    )
    accept_proposal(instance, owner, proposed, sequence=1)
    return instance


def _marker(coordinate: AcceptedCoordinate) -> ProjectionMarkerSummaryV1:
    return ProjectionMarkerSummaryV1(
        stamp=ProjectionBlockStampV1(
            source_id="docs.runbook",
            block_id="status",
            declared_generation=1,
            declared_coordinate=coordinate,
            backing=(
                ProjectionClaimBackingV1(
                    identity=ArtifactIdentity(kind="Claim", name="CLM-" + "a" * 32),
                    statement_digest="sha256:" + "b" * 64,
                ),
            ),
            body_digest="sha256:" + "c" * 64,
        ),
        observed_body_digest="sha256:" + "d" * 64,
        start_byte=0,
        end_byte=100,
    )


def _v3(
    coordinate: AcceptedCoordinate,
    *,
    complete: bool = True,
    document_id: str | None = "runbook",
) -> PlaybillNextSourceObservationV3:
    return PlaybillNextSourceObservationV3(
        tag="playbill-next-source-observation-v3",
        source_id="docs.runbook",
        document_id=document_id,
        observed_source_digest="sha256:" + "e" * 64,
        byte_length=200,
        marker_summaries=(_marker(coordinate),),
        occurrences=(),
        scanned_commitment_digests=(),
        scan_complete=complete,
        scan_notes=() if complete else ("scan_incomplete",),
        marker_notes=(),
    )


def test_valid_stamped_v3_observation_persists_once_and_remains_client_observed(
    tmp_path: Path,
) -> None:
    instance = _instance_with_document(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    request = PlaybillCurationListRequestV1(
        evaluation_time=NOW,
        workspace_observation=PlaybillNextWorkspaceObservationV1(
            source_observations=(_v3(coordinate),)
        ),
    )

    first = service_list_playbill_curation(instance, request=request, actor_context=_actor())
    retry = service_list_playbill_curation(instance, request=request, actor_context=_actor())
    events = instance.review_operational_store().events(family="block_observation")

    assert first.observation_coverage.observed_block_count == 1
    assert retry.operational_head_digest == first.operational_head_digest
    assert len(events) == 1
    block = BlockObservationV1.model_validate(events[0][1])
    assert block.observation_basis == "client_observed"
    assert block.actor_principal_id == "curator"
    assert block.document_identity.qualified == "document:runbook"


def test_incomplete_legacy_and_unresolved_document_sources_are_coverage_omissions(
    tmp_path: Path,
) -> None:
    instance = _instance_with_document(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    marker = _marker(coordinate)
    legacy = PlaybillNextSourceObservationV2(
        tag="playbill-next-source-observation-v2",
        source_id="docs.legacy",
        observed_source_digest="sha256:" + "f" * 64,
        byte_length=200,
        marker_summaries=(
            marker.model_copy(
                update={"stamp": marker.stamp.model_copy(update={"source_id": "docs.legacy"})}
            ),
        ),
        occurrences=(),
        scanned_commitment_digests=(),
        scan_complete=True,
        scan_notes=(),
        marker_notes=(),
    )
    request = PlaybillCurationListRequestV1(
        evaluation_time=NOW,
        workspace_observation=PlaybillNextWorkspaceObservationV1(
            source_observations=(
                legacy,
                _v3(coordinate, complete=False),
                _v3(coordinate, document_id="missing").model_copy(
                    update={"source_id": "docs.unresolved", "marker_summaries": ()}
                ),
            )
        ),
    )

    result = service_list_playbill_curation(instance, request=request, actor_context=_actor())

    assert result.observation_coverage.observed_block_count == 0
    assert {item.reason: item.count for item in result.observation_coverage.omissions} == {
        "block_subject_unresolved": 1,
        "source_observation_not_v3": 1,
        "source_scan_incomplete": 1,
    }
    assert instance.review_operational_store().events(family="block_observation") == ()


def test_bootstrap_and_malformed_markers_are_explicit_coverage_omissions(
    tmp_path: Path,
) -> None:
    instance = _instance_with_document(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    source = _v3(coordinate).model_copy(
        update={
            "marker_summaries": (),
            "marker_notes": (
                "projection_block_unstamped",
                "projection_marker_invalid",
            ),
        }
    )

    result = service_list_playbill_curation(
        instance,
        request=PlaybillCurationListRequestV1(
            evaluation_time=NOW,
            workspace_observation=PlaybillNextWorkspaceObservationV1(source_observations=(source,)),
        ),
        actor_context=_actor(),
    )

    assert {item.reason: item.count for item in result.observation_coverage.omissions} == {
        "projection_block_unstamped": 1,
        "projection_marker_invalid": 1,
    }
    assert instance.review_operational_store().events(family="block_observation") == ()


def test_block_churn_counts_body_transitions_across_the_bounded_generation_window(
    tmp_path: Path,
) -> None:
    instance = _instance_with_document(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    document_identity = ArtifactIdentity(kind="document", name="runbook")
    store = instance.review_operational_store()
    for index, scan_generation in enumerate((0, 1, 1)):
        marker = _marker(coordinate).model_copy(
            update={"observed_body_digest": "sha256:" + str(index + 1) * 64}
        )
        source = _v3(coordinate).model_copy(
            update={
                "observed_source_digest": "sha256:" + str(index + 4) * 64,
                "marker_summaries": (marker,),
            }
        )
        observation = build_block_observation(
            document_identity=document_identity,
            source=source,
            marker=marker,
            scan_coordinate=coordinate,
            scan_generation=scan_generation,
            actor_context=_actor(),
        )
        store.append(
            family="block_observation",
            partition_id="document:runbook/docs.runbook/status",
            event_id=observation.event_id,
            payload=observation,
            coordinate=coordinate,
            generation=scan_generation,
            actor_context=_actor(),
            recorded_at=NOW,
        )

    detected, coverage = _block_churn(instance=instance, generation=1)

    assert coverage.evaluated_fact_count == 3
    assert len(detected) == 1
    assert detected[0].subject == document_identity
    assert detected[0].detail == {"block_id": "status", "source_id": "docs.runbook"}
