"""Curation identities, append-only lifecycle, suppression, and recurrence."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.curation import (
    CurationAffectedMemberV1,
    CurationDetectorCoverageV1,
    CurationEvidenceRefV1,
    build_curation_accepted_fixed,
    build_curation_detection,
    build_pattern_observation,
    curation_item_id,
    replay_curation_items,
)
from cruxible_core.playbill.review_operational import ReviewOperationalConcurrentChangeError
from cruxible_core.service.playbill_curation import (
    PlaybillCurationOverruleRequestV1,
    PlaybillCurationSuppressRequestV1,
    service_overrule_playbill_curation,
    service_suppress_playbill_curation,
)
from tests.test_playbill._support import initialize_local

NOW = datetime(2026, 8, 26, 16, 0, tzinfo=UTC)


def _actor() -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id="curator",
        org_id="org-test",
        operation_id="op-curation",
        timestamp=NOW,
    )


def _detection():  # type: ignore[no-untyped-def]
    kind = "playbill.curation.recurring_conflict_per_type.v1"
    return build_curation_detection(
        pattern_kind=kind,
        subject=ArtifactIdentity(kind="ClaimType", name="project.work_item.status"),
        detail={
            "cardinality": "one",
            "slot_partition": "subject+predicate+qualifier",
        },
        coverage=CurationDetectorCoverageV1(
            pattern_kind=kind,
            status="complete",
            evaluated_fact_count=2,
        ),
        evidence_refs=(
            CurationEvidenceRefV1(
                kind="slot",
                identity="sha256:" + "1" * 64,
                facts={"claim_count": 2, "contender_count": 2},
            ),
        ),
    )


def _seed_item(tmp_path: Path):  # type: ignore[no-untyped-def]
    instance, _owner = initialize_local(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    observation = build_pattern_observation(
        detection=_detection(), predecessor_item_id=None, accepted_generation=0
    )
    instance.review_operational_store().append(
        family="curation",
        partition_id=observation.item_id,
        event_id=observation.event_id,
        payload=observation,
        coordinate=coordinate,
        generation=0,
        actor_context=_actor(),
        recorded_at=NOW,
        expected_latest_event_digest=None,
    )
    item = replay_curation_items(instance.review_operational_store().events(family="curation"))[0]
    return instance, item


def test_redetection_reuses_item_and_accept_fixed_recurrence_mints_linked_successor(
    tmp_path: Path,
) -> None:
    instance, item = _seed_item(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    accepted = build_curation_accepted_fixed(
        item_id=item.item_id,
        expected_latest_event_digest=item.latest_event_digest,
        actor_principal_id="curator",
        reason="linked repair was accepted",
        accepted_proposal_id="sha256:" + "2" * 64,
        accepted_changeset_digest="sha256:" + "3" * 64,
        resolved_generation=1,
        affected_members=(
            CurationAffectedMemberV1(
                path="claim-types/project.work_item.status.yaml",
                disposition="replace",
                predecessor_artifact_digest="sha256:" + "4" * 64,
                candidate_artifact_digest="sha256:" + "5" * 64,
            ),
        ),
    )
    accepted_event = instance.review_operational_store().append(
        family="curation",
        partition_id=item.item_id,
        event_id=accepted.event_id,
        payload=accepted,
        coordinate=coordinate,
        generation=0,
        actor_context=_actor(),
        recorded_at=NOW,
        expected_latest_event_digest=item.latest_event_digest,
    )
    successor = build_pattern_observation(
        detection=_detection(),
        predecessor_item_id=item.item_id,
        accepted_generation=2,
    )
    assert successor.item_id == curation_item_id(
        pattern_id=item.pattern_id, predecessor_item_id=item.item_id
    )
    assert successor.item_id != item.item_id
    instance.review_operational_store().append(
        family="curation",
        partition_id=successor.item_id,
        event_id=successor.event_id,
        payload=successor,
        coordinate=coordinate,
        generation=0,
        actor_context=_actor(),
        recorded_at=NOW,
        expected_latest_event_digest=None,
    )
    projected = replay_curation_items(instance.review_operational_store().events(family="curation"))
    by_id = {row.item_id: row for row in projected}
    assert (by_id[item.item_id].status, by_id[item.item_id].predecessor_item_id) == (
        "accepted_fixed",
        None,
    )
    assert (by_id[successor.item_id].status, by_id[successor.item_id].predecessor_item_id) == (
        "open",
        item.item_id,
    )
    assert accepted_event.event_digest == by_id[item.item_id].latest_event_digest


def test_suppression_is_non_resolving_and_compare_and_append_is_mandatory(
    tmp_path: Path,
) -> None:
    instance, item = _seed_item(tmp_path)
    result = service_suppress_playbill_curation(
        instance,
        request=PlaybillCurationSuppressRequestV1(
            item_id=item.item_id,
            expected_latest_event_digest=item.latest_event_digest,
            reason="operator is handling this pattern elsewhere",
            scope="pattern",
            until_generation=10,
        ),
        actor_context=_actor(),
    )
    assert result.item.status == "open"
    assert result.item.suppressed_at(0, all_items=(result.item,))
    with pytest.raises(ReviewOperationalConcurrentChangeError):
        service_overrule_playbill_curation(
            instance,
            request=PlaybillCurationOverruleRequestV1(
                item_id=item.item_id,
                expected_latest_event_digest=item.latest_event_digest,
                reason="stale concurrent lifecycle write",
            ),
            actor_context=_actor(),
        )


def test_overrule_is_terminal_for_the_detector_version(tmp_path: Path) -> None:
    instance, item = _seed_item(tmp_path)
    result = service_overrule_playbill_curation(
        instance,
        request=PlaybillCurationOverruleRequestV1(
            item_id=item.item_id,
            expected_latest_event_digest=item.latest_event_digest,
            reason="the mechanical pattern is inapplicable",
        ),
        actor_context=_actor(),
    )
    assert result.item.status == "overruled"
    assert result.item.resolved_at_generation == 0
