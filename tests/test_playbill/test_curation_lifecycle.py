"""Curation identities, append-only lifecycle, suppression, and recurrence."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
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
from cruxible_core.playbill.curation_detectors import CurationDetectorResult
from cruxible_core.playbill.review_operational import ReviewOperationalConcurrentChangeError
from cruxible_core.service.playbill_curation import (
    PlaybillCurationListRequestV1,
    PlaybillCurationOverruleRequestV1,
    PlaybillCurationSuppressRequestV1,
    service_list_playbill_curation,
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


def _detection(subject: str = "project.work_item.status"):  # type: ignore[no-untyped-def]
    kind = "playbill.curation.recurring_conflict_per_type.v1"
    return build_curation_detection(
        pattern_kind=kind,
        subject=ArtifactIdentity(kind="ClaimType", name=subject),
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
    accepted_before = instance.accepted_coordinate()
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
    assert instance.accepted_coordinate() == accepted_before
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
    accepted_before = instance.accepted_coordinate()
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
    assert instance.accepted_coordinate() == accepted_before


def _serve_detection(
    instance,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
    *,
    generation: int,
    detections: tuple[object, ...],
):  # type: ignore[no-untyped-def]
    coverage = tuple(item.coverage for item in detections)
    monkeypatch.setattr(
        "cruxible_core.service.playbill_curation._generation",
        lambda _instance, _coordinate: generation,
    )
    monkeypatch.setattr(
        "cruxible_core.service.playbill_curation.run_curation_detectors",
        lambda *args, **kwargs: CurationDetectorResult(  # type: ignore[arg-type]
            detections=detections,
            coverage=coverage,
        ),
    )
    return service_list_playbill_curation(
        instance,
        request=PlaybillCurationListRequestV1(
            evaluation_time=NOW,
            access_profile=CoverageAccessProfileV1(profile_id="test-curation"),
        ),
        actor_context=_actor(),
    )


def test_redetection_advances_one_item_and_suppressed_queue_reopens_after_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, item = _seed_item(tmp_path)
    suppressed = service_suppress_playbill_curation(
        instance,
        request=PlaybillCurationSuppressRequestV1(
            item_id=item.item_id,
            expected_latest_event_digest=item.latest_event_digest,
            reason="hide through the next accepted generation",
            scope="item",
            until_generation=1,
        ),
        actor_context=_actor(),
    )

    hidden = _serve_detection(
        instance,
        monkeypatch,
        generation=1,
        detections=(_detection(),),
    )
    projected = replay_curation_items(instance.review_operational_store().events(family="curation"))
    refreshed = next(row for row in projected if row.item_id == item.item_id)
    assert hidden.items == ()
    assert refreshed.observation_count == 2
    assert refreshed.last_observed_generation == 1
    assert refreshed.latest_event_digest != suppressed.item.latest_event_digest

    visible = _serve_detection(
        instance,
        monkeypatch,
        generation=2,
        detections=(_detection(),),
    )
    assert [row.item_id for row in visible.items] == [item.item_id]
    assert visible.items[0].observation_count == 3
    assert visible.items[0].last_observed_generation == 2


def test_item_and_instance_suppression_scopes_apply_in_the_served_fold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, first = _seed_item(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    second_observation = build_pattern_observation(
        detection=_detection("project.work_item.priority"),
        predecessor_item_id=None,
        accepted_generation=0,
    )
    instance.review_operational_store().append(
        family="curation",
        partition_id=second_observation.item_id,
        event_id=second_observation.event_id,
        payload=second_observation,
        coordinate=coordinate,
        generation=0,
        actor_context=_actor(),
        recorded_at=NOW,
        expected_latest_event_digest=None,
    )
    service_suppress_playbill_curation(
        instance,
        request=PlaybillCurationSuppressRequestV1(
            item_id=first.item_id,
            expected_latest_event_digest=first.latest_event_digest,
            reason="hide only this item",
            scope="item",
        ),
        actor_context=_actor(),
    )
    item_hidden = _serve_detection(
        instance,
        monkeypatch,
        generation=0,
        detections=(_detection(), _detection("project.work_item.priority")),
    )
    assert [row.subject.name for row in item_hidden.items] == ["project.work_item.priority"]

    remaining = item_hidden.items[0]
    service_suppress_playbill_curation(
        instance,
        request=PlaybillCurationSuppressRequestV1(
            item_id=remaining.item_id,
            expected_latest_event_digest=remaining.latest_event_digest,
            reason="hide this queue instance",
            scope="instance",
        ),
        actor_context=_actor(),
    )
    all_hidden = _serve_detection(
        instance,
        monkeypatch,
        generation=0,
        detections=(_detection(), _detection("project.work_item.priority")),
    )
    assert all_hidden.items == ()


def test_overrule_stays_silent_on_redetection_and_detector_identity_is_versioned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, item = _seed_item(tmp_path)
    service_overrule_playbill_curation(
        instance,
        request=PlaybillCurationOverruleRequestV1(
            item_id=item.item_id,
            expected_latest_event_digest=item.latest_event_digest,
            reason="version one is inapplicable",
        ),
        actor_context=_actor(),
    )
    result = _serve_detection(
        instance,
        monkeypatch,
        generation=1,
        detections=(_detection(),),
    )
    projected = replay_curation_items(instance.review_operational_store().events(family="curation"))
    assert result.items == ()
    assert len(projected) == 1
    assert projected[0].observation_count == 1

    other_version_identity = build_curation_detection(
        pattern_kind="playbill.curation.dead_vocabulary.v1",
        subject=ArtifactIdentity(kind="ClaimType", name="project.work_item.status"),
        detail={"artifact_family": "ClaimType"},
        coverage=CurationDetectorCoverageV1(
            pattern_kind="playbill.curation.dead_vocabulary.v1",
            status="complete",
            evaluated_fact_count=1,
        ),
        evidence_refs=(
            CurationEvidenceRefV1(
                kind="accepted_artifact",
                identity="ClaimType:project.work_item.status",
                generation=1,
            ),
        ),
    )
    assert other_version_identity.pattern_id != item.pattern_id


def test_curation_list_retries_one_same_generation_operational_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _item = _seed_item(tmp_path)
    store = instance.review_operational_store()
    original = store.append
    calls = 0

    def racing_append(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        if kwargs.get("family") == "curation" and calls == 0:
            calls += 1
            raise ReviewOperationalConcurrentChangeError()
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "append", racing_append)
    monkeypatch.setattr(instance, "review_operational_store", lambda: store)
    result = _serve_detection(
        instance,
        monkeypatch,
        generation=1,
        detections=(_detection(),),
    )

    assert calls == 1
    assert len(result.items) == 1
    assert result.items[0].last_observed_generation == 1
