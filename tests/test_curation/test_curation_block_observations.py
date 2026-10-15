"""Explicit, attributed G9 block-observation ingestion."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import get_args

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.declared_blocks import (
    DeclaredBlockPageObservation,
    ProjectionBlockStampV1,
    ProjectionClaimBacking,
    ProjectionMarkerSummary,
)
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.coverage.contracts import CoverageAccessProfile
from cruxible_core.curation.curation_detectors import _block_churn
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.service.authoring.documents import service_propose_playbill_document
from cruxible_core.service.discovery.curation import (
    BlockObservationV1,
    PlaybillCurationListRequestV1,
    PlaybillCurationObservationOmissionReason,
    build_block_observation,
)
from tests.core_support._knowledge_loop_support import accept_proposal
from tests.core_support._support import initialize_local
from tests.support.curation import detect_and_list

NOW = datetime(2026, 8, 26, 15, 0, tzinfo=timezone.utc)
ACCESS = CoverageAccessProfile(profile_id="test-curation")


def test_observation_omission_reasons_exhaust_the_closed_wire_vocabulary() -> None:
    assert set(get_args(PlaybillCurationObservationOmissionReason)) == {
        "block_subject_unresolved",
        "marker_coordinate_unaccepted",
        "projection_block_unstamped",
        "projection_marker_invalid",
    }


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
            ),
            governance_scope=("project:test",),
            lifecycle=DocumentLifecycle(revision=1),
        ),
        actor_id="owner",
        proposal_name="runbook",
        timestamp="2026-08-26T15:00:00.000000Z",
    )
    accept_proposal(instance, owner, proposed)
    return instance


def _marker(coordinate: AcceptedCoordinate) -> ProjectionMarkerSummary:
    return ProjectionMarkerSummary(
        stamp=ProjectionBlockStampV1(
            source_id="docs.runbook",
            block_id="status",
            declared_generation=1,
            declared_coordinate=coordinate,
            backing=(
                ProjectionClaimBacking(
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


def _page(
    coordinate: AcceptedCoordinate,
    *,
    document_id: str | None = "runbook",
) -> DeclaredBlockPageObservation:
    return DeclaredBlockPageObservation(
        source_id="docs.runbook",
        document_id=document_id,
        observed_source_digest="sha256:" + "e" * 64,
        byte_length=200,
        marker_summaries=(_marker(coordinate),),
    )


def test_a_stamped_page_observation_persists_once_and_remains_client_observed(
    tmp_path: Path,
) -> None:
    instance = _instance_with_document(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    request = PlaybillCurationListRequestV1(access_profile=ACCESS)
    observation = (_page(coordinate),)

    first = detect_and_list(
        instance,
        request=request,
        evaluation_time=NOW,
        actor_context=_actor(),
        pages=observation,
    )
    retry = detect_and_list(
        instance,
        request=request,
        evaluation_time=NOW,
        actor_context=_actor(),
        pages=observation,
    )
    events = instance.review_operational_store().events(family="block_observation")

    assert first.observation_coverage.observed_block_count == 1
    assert retry.operational_head_digest == first.operational_head_digest
    assert len(events) == 1
    block = BlockObservationV1.model_validate(events[0][1])
    assert block.observation_basis == "client_observed"
    assert block.actor_principal_id == "curator"
    assert block.document_identity.qualified == "document:runbook"


def test_unresolved_document_sources_are_coverage_omissions(
    tmp_path: Path,
) -> None:
    instance = _instance_with_document(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    request = PlaybillCurationListRequestV1(access_profile=ACCESS)
    observation = (
        _page(coordinate, document_id=None),
        _page(coordinate, document_id="missing").model_copy(
            update={"source_id": "docs.unresolved", "marker_summaries": ()}
        ),
    )

    result = detect_and_list(
        instance,
        request=request,
        evaluation_time=NOW,
        actor_context=_actor(),
        pages=observation,
    )

    assert result.observation_coverage.observed_block_count == 0
    assert {item.reason: item.count for item in result.observation_coverage.omissions} == {
        "block_subject_unresolved": 2,
    }
    assert instance.review_operational_store().events(family="block_observation") == ()
    block_coverage = next(
        item
        for item in result.detector_coverage
        if item.pattern_kind == "playbill.curation.block_churn.v1"
    )
    assert [item.model_dump(mode="json") for item in block_coverage.omissions] == [
        {"reason": "block_document_association_unavailable", "count": 2}
    ]


def test_bootstrap_and_malformed_markers_are_explicit_coverage_omissions(
    tmp_path: Path,
) -> None:
    instance = _instance_with_document(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    source = _page(coordinate).model_copy(
        update={
            "marker_summaries": (),
            "marker_notes": (
                "projection_block_unstamped",
                "projection_marker_invalid",
            ),
        }
    )

    result = detect_and_list(
        instance,
        request=PlaybillCurationListRequestV1(access_profile=ACCESS),
        evaluation_time=NOW,
        actor_context=_actor(),
        pages=(source,),
    )

    assert {item.reason: item.count for item in result.observation_coverage.omissions} == {
        "projection_block_unstamped": 1,
        "projection_marker_invalid": 1,
    }
    assert instance.review_operational_store().events(family="block_observation") == ()


def test_unaccepted_marker_coordinate_is_an_explicit_coverage_omission(
    tmp_path: Path,
) -> None:
    instance = _instance_with_document(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    unaccepted = coordinate.model_copy(update={"git_oid": "f" * 64})

    result = detect_and_list(
        instance,
        request=PlaybillCurationListRequestV1(access_profile=ACCESS),
        evaluation_time=NOW,
        actor_context=_actor(),
        pages=(_page(unaccepted),),
    )

    assert result.observation_coverage.observed_block_count == 0
    assert [item.model_dump(mode="json") for item in result.observation_coverage.omissions] == [
        {"reason": "marker_coordinate_unaccepted", "count": 1}
    ]
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
        source = _page(coordinate).model_copy(
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


def test_block_churn_never_parses_or_evaluates_observations_outside_its_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Block sync/repin record on every run; this detector's own work must not grow with them.

    A counter on the payload parse proves old observations, well outside the
    fixed-size generation window, are skipped before ever being parsed,
    grouped, or resolved against accepted state -- not merely excluded from
    the final detections after that work ran.
    """

    instance = _instance_with_document(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    document_identity = ArtifactIdentity(kind="document", name="runbook")
    store = instance.review_operational_store()

    old_count = 30
    for index in range(old_count):
        marker = _marker(coordinate).model_copy(
            update={"observed_body_digest": "sha256:" + format(index, "064x")}
        )
        source = _page(coordinate).model_copy(
            update={
                "observed_source_digest": "sha256:" + format(index + 1000, "064x"),
                "marker_summaries": (marker,),
            }
        )
        observation = build_block_observation(
            document_identity=document_identity,
            source=source,
            marker=marker,
            scan_coordinate=coordinate,
            scan_generation=0,
            actor_context=_actor(),
        )
        store.append(
            family="block_observation",
            partition_id="document:runbook/docs.runbook/status",
            event_id=observation.event_id,
            payload=observation,
            coordinate=coordinate,
            generation=0,
            actor_context=_actor(),
            recorded_at=NOW,
        )
    for offset, scan_generation in enumerate((11, 12, 12)):
        marker = _marker(coordinate).model_copy(
            update={"observed_body_digest": "sha256:" + format(old_count + offset, "064x")}
        )
        source = _page(coordinate).model_copy(
            update={
                "observed_source_digest": "sha256:" + format(old_count + offset + 1000, "064x"),
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

    parsed: list[object] = []
    original_validate = BlockObservationV1.model_validate.__func__  # type: ignore[attr-defined]

    def _counting_validate(cls: type[BlockObservationV1], payload: object) -> BlockObservationV1:
        parsed.append(payload)
        return original_validate(cls, payload)

    monkeypatch.setattr(BlockObservationV1, "model_validate", classmethod(_counting_validate))

    # BLOCK_CHURN_ACCEPTED_GENERATION_WINDOW is 10, so at generation=20 the
    # window is [11, 20]: it holds the 3 freshly-appended events and excludes
    # every one of the 30 old (generation 0) events, however many accumulate.
    detected, coverage = _block_churn(instance=instance, generation=20)

    assert len(parsed) == 3
    assert coverage.evaluated_fact_count == 3
    assert len(detected) == 1
    assert detected[0].subject == document_identity
