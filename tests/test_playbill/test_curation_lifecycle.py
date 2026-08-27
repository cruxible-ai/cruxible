"""Curation identities, append-only lifecycle, suppression, and recurrence."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.curation import (
    CURATION_DETECTOR_LAW_DIGEST_DOMAIN,
    CURATION_PATTERN_ID_DOMAIN,
    CurationAffectedMemberV1,
    CurationDetectorCoverageV1,
    CurationEvidenceRefV1,
    build_curation_accepted_fixed,
    build_curation_detection,
    build_pattern_observation,
    curation_detection_evidence_digest,
    curation_item_id,
    curation_observation_id,
    detector_law_digest,
    replay_curation_items,
)
from cruxible_core.playbill.curation_detectors import CurationDetectorResult
from cruxible_core.playbill.review_operational import ReviewOperationalConcurrentChangeError
from cruxible_core.service.playbill_curation import (
    PlaybillCurationAcceptFixedRequestV1,
    PlaybillCurationItemAlreadyResolved,
    PlaybillCurationListRequestV1,
    PlaybillCurationOverruleRequestV1,
    PlaybillCurationSuppressRequestV1,
    service_accept_fixed_playbill_curation,
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


def _provenance_detection(
    subject: str = "project.work_item.owner",
):  # type: ignore[no-untyped-def]
    kind = "playbill.curation.provenance_concentration.v1"
    identity = ArtifactIdentity(kind="ClaimType", name=subject)
    return build_curation_detection(
        pattern_kind=kind,
        subject=identity,
        detail={"basis": "effective_supporting_control_components"},
        coverage=CurationDetectorCoverageV1(
            pattern_kind=kind,
            status="complete",
            evaluated_fact_count=2,
        ),
        evidence_refs=(
            CurationEvidenceRefV1(
                kind="accepted_artifact",
                identity=identity.qualified,
                generation=1,
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


def _legacy_observation(
    *,
    pattern_kind: str,
    subject: ArtifactIdentity,
    detail: dict[str, object],
    old_law: dict[str, object],
) -> dict[str, object]:
    coverage = CurationDetectorCoverageV1(
        pattern_kind=pattern_kind,  # type: ignore[arg-type]
        status="complete",
        evaluated_fact_count=2,
    )
    refs = (
        CurationEvidenceRefV1(
            kind="accepted_artifact",
            identity=subject.qualified,
            generation=0,
        ),
    )
    pattern_id = typed_digest(
        Sha256Value,
        CURATION_PATTERN_ID_DOMAIN,
        {
            "pattern_kind": pattern_kind,
            "subject": subject.model_dump(mode="json"),
            "detail": detail,
        },
    ).tagged
    law_digest = typed_digest(
        Sha256Value,
        CURATION_DETECTOR_LAW_DIGEST_DOMAIN,
        {"pattern_kind": pattern_kind, "law": old_law},
    ).tagged
    assert law_digest != detector_law_digest(pattern_kind)  # type: ignore[arg-type]
    evidence_digest = curation_detection_evidence_digest(
        pattern_id=pattern_id,
        detector_law_digest_value=law_digest,
        coverage=coverage,
        evidence_refs=refs,
    )
    item_id = curation_item_id(pattern_id=pattern_id, predecessor_item_id=None)
    observation_id = curation_observation_id(
        item_id=item_id,
        accepted_generation=0,
        detection_evidence_digest=evidence_digest,
    )
    return {
        "tag": "playbill-curation-pattern-observed-v1",
        "event_id": observation_id,
        "observation_id": observation_id,
        "item_id": item_id,
        "predecessor_item_id": None,
        "pattern_id": pattern_id,
        "pattern_kind": pattern_kind,
        "subject": subject.model_dump(mode="json"),
        "detail": detail,
        "detector_law_digest": law_digest,
        "detection_evidence_digest": evidence_digest,
        "evidence_refs": [item.model_dump(mode="json") for item in refs],
        "coverage": coverage.model_dump(mode="json"),
        "accepted_generation": 0,
    }


def test_changed_detector_laws_quarantine_old_items_without_bricking_curation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, healthy = _seed_item(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    legacy = (
        _legacy_observation(
            pattern_kind="playbill.curation.admission_failure_cluster.v1",
            subject=ArtifactIdentity(kind="ClaimType", name="project.work_item.priority"),
            detail={"diagnostic_code": "playbill.claim.literal_schema_invalid"},
            old_law={
                "minimum_distinct_durable_attempts": 2,
                "discriminator": "diagnostic_code",
            },
        ),
        _legacy_observation(
            pattern_kind="playbill.curation.provenance_concentration.v1",
            subject=ArtifactIdentity(kind="ClaimType", name="project.work_item.owner"),
            detail={"basis": "effective_supporting_control_components"},
            old_law={
                "minimum_live_supported_claims": 2,
                "effective_supporting_control_components": 1,
            },
        ),
        _legacy_observation(
            pattern_kind="playbill.curation.duplicate_statement_lineages.v1",
            subject=ArtifactIdentity(kind="ClaimType", name="project.work_item.assignee"),
            detail={"statement_digest": "sha256:" + "a" * 64},
            old_law={
                "minimum_distinct_claim_identities": 2,
                "comparison": "exact_claim_statement_digest",
            },
        ),
    )
    for raw in legacy:
        instance.review_operational_store().append(
            family="curation",
            partition_id=str(raw["item_id"]),
            event_id=str(raw["event_id"]),
            payload=raw,
            coordinate=coordinate,
            generation=0,
            actor_context=_actor(),
            recorded_at=NOW,
            expected_latest_event_digest=None,
        )

    result = _serve_detection(
        instance,
        monkeypatch,
        generation=1,
        detections=(_detection(),),
    )

    by_status = {item.status: [] for item in result.items}
    for item in result.items:
        by_status[item.status].append(item)
    assert [item.item_id for item in by_status["open"]] == [healthy.item_id]
    quarantined = tuple(by_status["quarantined"])
    assert len(quarantined) == 3
    assert {item.pattern_kind for item in quarantined} == {
        "playbill.curation.admission_failure_cluster.v1",
        "playbill.curation.provenance_concentration.v1",
        "playbill.curation.duplicate_statement_lineages.v1",
    }
    assert all(item.quarantine_reason == "detector_law_unreproducible" for item in quarantined)
    assert all(
        item.current_detector_law_digest == detector_law_digest(item.pattern_kind)
        and item.current_detector_law_digest != item.detector_law_digest
        for item in quarantined
    )

    suppressed = service_suppress_playbill_curation(
        instance,
        request=PlaybillCurationSuppressRequestV1(
            item_id=quarantined[0].item_id,
            expected_latest_event_digest=quarantined[0].latest_event_digest,
            reason="quarantine reviewed later",
            scope="item",
        ),
        actor_context=_actor(),
    )
    assert suppressed.item.status == "quarantined"
    overruled = service_overrule_playbill_curation(
        instance,
        request=PlaybillCurationOverruleRequestV1(
            item_id=quarantined[1].item_id,
            expected_latest_event_digest=quarantined[1].latest_event_digest,
            reason="old detector law is no longer applicable",
        ),
        actor_context=_actor(),
    )
    assert overruled.item.status == "overruled"
    with pytest.raises(PlaybillCurationItemAlreadyResolved, match="quarantined"):
        service_accept_fixed_playbill_curation(
            instance,
            request=PlaybillCurationAcceptFixedRequestV1(
                item_id=quarantined[2].item_id,
                expected_latest_event_digest=quarantined[2].latest_event_digest,
                reason="must not resolve an unreproducible detector law as fixed",
                accepted_proposal_id="sha256:" + "b" * 64,
                accepted_changeset_digest="sha256:" + "c" * 64,
            ),
            actor_context=_actor(),
        )


def test_historical_accept_fixed_on_newly_quarantined_law_replays_as_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    raw = _legacy_observation(
        pattern_kind="playbill.curation.provenance_concentration.v1",
        subject=ArtifactIdentity(kind="ClaimType", name="project.work_item.owner"),
        detail={"basis": "effective_supporting_control_components"},
        old_law={
            "minimum_live_supported_claims": 2,
            "effective_supporting_control_components": 1,
        },
    )
    observed_event = instance.review_operational_store().append(
        family="curation",
        partition_id=str(raw["item_id"]),
        event_id=str(raw["event_id"]),
        payload=raw,
        coordinate=coordinate,
        generation=0,
        actor_context=_actor(),
        recorded_at=NOW,
        expected_latest_event_digest=None,
    )
    accepted = build_curation_accepted_fixed(
        item_id=str(raw["item_id"]),
        expected_latest_event_digest=observed_event.event_digest,
        actor_principal_id="curator",
        reason="the historical detector condition was fixed",
        accepted_proposal_id="sha256:" + "2" * 64,
        accepted_changeset_digest="sha256:" + "3" * 64,
        resolved_generation=1,
        affected_members=(
            CurationAffectedMemberV1(
                path="claim-types/project.work_item.owner.yaml",
                disposition="replace",
                predecessor_artifact_digest="sha256:" + "4" * 64,
                candidate_artifact_digest="sha256:" + "5" * 64,
            ),
        ),
    )
    instance.review_operational_store().append(
        family="curation",
        partition_id=str(raw["item_id"]),
        event_id=accepted.event_id,
        payload=accepted,
        coordinate=coordinate,
        generation=0,
        actor_context=_actor(),
        recorded_at=NOW,
        expected_latest_event_digest=observed_event.event_digest,
    )

    listed = _serve_detection(instance, monkeypatch, generation=1, detections=())
    projected = replay_curation_items(instance.review_operational_store().events(family="curation"))

    assert listed.items == ()
    assert len(projected) == 1
    assert projected[0].status == "accepted_fixed"
    assert projected[0].quarantine_reason == "detector_law_unreproducible"
    assert projected[0].accepted_proposal_id == accepted.accepted_proposal_id


def test_current_law_detection_chains_past_quarantine_but_absence_mints_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_path = tmp_path / "live"
    live_path.mkdir()
    instance, _owner = initialize_local(live_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    raw = _legacy_observation(
        pattern_kind="playbill.curation.provenance_concentration.v1",
        subject=ArtifactIdentity(kind="ClaimType", name="project.work_item.owner"),
        detail={"basis": "effective_supporting_control_components"},
        old_law={
            "minimum_live_supported_claims": 2,
            "effective_supporting_control_components": 1,
        },
    )
    instance.review_operational_store().append(
        family="curation",
        partition_id=str(raw["item_id"]),
        event_id=str(raw["event_id"]),
        payload=raw,
        coordinate=coordinate,
        generation=0,
        actor_context=_actor(),
        recorded_at=NOW,
        expected_latest_event_digest=None,
    )

    listed = _serve_detection(
        instance,
        monkeypatch,
        generation=1,
        detections=(_provenance_detection(),),
    )
    projected = replay_curation_items(instance.review_operational_store().events(family="curation"))
    original = next(item for item in projected if item.item_id == raw["item_id"])
    successor = next(item for item in projected if item.item_id != raw["item_id"])
    assert original.status == "quarantined"
    assert successor.status == "open"
    assert successor.predecessor_item_id == original.item_id
    assert successor.detector_law_digest == detector_law_digest(successor.pattern_kind)
    assert {item.item_id for item in listed.items} == {original.item_id, successor.item_id}

    gone_path = tmp_path / "gone"
    gone_path.mkdir()
    gone, _owner = initialize_local(gone_path)
    gone_coordinate = AcceptedCoordinate.from_internal(gone.accepted_coordinate())
    gone_event = gone.review_operational_store().append(
        family="curation",
        partition_id=str(raw["item_id"]),
        event_id=str(raw["event_id"]),
        payload=raw,
        coordinate=gone_coordinate,
        generation=0,
        actor_context=_actor(),
        recorded_at=NOW,
        expected_latest_event_digest=None,
    )
    service_overrule_playbill_curation(
        gone,
        request=PlaybillCurationOverruleRequestV1(
            item_id=str(raw["item_id"]),
            expected_latest_event_digest=gone_event.event_digest,
            reason="the historical condition is gone",
        ),
        actor_context=_actor(),
    )
    before = gone.review_operational_store().events(family="curation")

    gone_result = _serve_detection(gone, monkeypatch, generation=1, detections=())

    after = gone.review_operational_store().events(family="curation")
    assert gone_result.items == ()
    assert len(after) == len(before)


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


def test_overrule_redetection_mints_linked_successor_and_detector_identity_is_versioned(
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
    assert len(projected) == 2
    overruled = next(row for row in projected if row.item_id == item.item_id)
    successor = next(row for row in projected if row.item_id != item.item_id)
    assert overruled.status == "overruled"
    assert successor.status == "open"
    assert successor.predecessor_item_id == overruled.item_id
    assert [row.item_id for row in result.items] == [successor.item_id]

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
    versioned_result = _serve_detection(
        instance,
        monkeypatch,
        generation=2,
        detections=(other_version_identity,),
    )
    assert {row.pattern_id for row in versioned_result.items} == {
        item.pattern_id,
        other_version_identity.pattern_id,
    }


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


def test_curation_list_reraises_second_operational_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _item = _seed_item(tmp_path)
    store = instance.review_operational_store()
    original = store.append

    def always_conflicts(*args, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("family") == "curation":
            raise ReviewOperationalConcurrentChangeError()
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "append", always_conflicts)
    monkeypatch.setattr(instance, "review_operational_store", lambda: store)
    with pytest.raises(ReviewOperationalConcurrentChangeError):
        _serve_detection(
            instance,
            monkeypatch,
            generation=1,
            detections=(_detection(),),
        )
