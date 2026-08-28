"""Accepted-fixed lifecycle links only exact related accepted ChangeSets."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    document_digest,
    document_path,
    parse_document,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.subjects import subject_digest, subject_path
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.curation import (
    CurationAcceptedFixedV1,
    CurationDetectorCoverageV1,
    CurationEvidenceRefV1,
    build_curation_detection,
    build_pattern_observation,
    replay_curation_items,
)
from cruxible_core.playbill.service.documents import service_propose_playbill_document
from cruxible_core.playbill.service.subjects import service_propose_playbill_subject
from cruxible_core.service.playbill_curation import (
    PlaybillCurationAcceptFixedRequestV1,
    PlaybillCurationListRequestV1,
    PlaybillCurationResolvingChangeUnrelated,
    _accepted_retirements_for_items,
    service_accept_fixed_playbill_curation,
    service_list_playbill_curation,
)
from tests.test_playbill._knowledge_loop_support import accept_proposal, subject_shell
from tests.test_playbill._support import initialize_local

NOW = datetime(2026, 8, 26, 18, tzinfo=UTC)


def _actor() -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id="curator",
        org_id="org-test",
        operation_id="op-accept-fixed",
        timestamp=NOW,
    )


def _document_shell(body_digest: str) -> DocumentShell:
    return DocumentShell(
        identity="document:runbook",
        document_kind="runbook",
        title="Runbook",
        media_type="text/markdown",
        body_digest=body_digest,
        authority=DocumentAuthority(
            required_tier="graph_write",
        ),
        governance_scope=("project:test",),
        lifecycle=DocumentLifecycle(revision=1),
    )


def _append_document_item(instance) -> object:  # type: ignore[no-untyped-def]
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    path = document_path("runbook")
    shell = parse_document(instance.tree_at(coordinate.git_oid)[path], path=path)
    kind = "playbill.curation.block_churn.v1"
    detection = build_curation_detection(
        pattern_kind=kind,
        subject=ArtifactIdentity(kind="document", name="runbook"),
        detail={"block_id": "status", "source_id": "docs.runbook"},
        coverage=CurationDetectorCoverageV1(
            pattern_kind=kind,
            status="complete",
            evaluated_fact_count=3,
        ),
        evidence_refs=(
            CurationEvidenceRefV1(
                kind="accepted_artifact",
                identity="document:runbook",
                path=path,
                generation=1,
                artifact_digest=document_digest(shell).tagged,
            ),
        ),
    )
    observation = build_pattern_observation(
        detection=detection,
        predecessor_item_id=None,
        accepted_generation=1,
    )
    event = instance.review_operational_store().append(
        family="curation",
        partition_id=observation.item_id,
        event_id=observation.event_id,
        payload=observation,
        coordinate=coordinate,
        generation=1,
        actor_context=_actor(),
        recorded_at=NOW,
        expected_latest_event_digest=None,
    )
    return observation, event


def test_accept_fixed_verifies_proposal_changeset_generation_and_member_intersection(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    first_body = instance.store_document_body(b"status: ready\n")
    initial = _document_shell(first_body.digest)
    first = service_propose_playbill_document(
        instance,
        shell=initial,
        actor_id="owner",
        proposal_name="runbook-initial",
        timestamp="2026-08-26T18:00:00.000000Z",
    )
    accept_proposal(instance, owner, first)
    observation, event = _append_document_item(instance)

    second_body = instance.store_document_body(b"status: reviewed\n")
    successor = initial.model_copy(
        update={
            "body_digest": second_body.digest,
            "predecessor_digest": document_digest(initial).tagged,
            "lifecycle": DocumentLifecycle(revision=2),
        }
    )
    second = service_propose_playbill_document(
        instance,
        shell=successor,
        actor_id="owner",
        proposal_name="runbook-successor",
        timestamp="2026-08-26T18:01:00.000000Z",
    )
    accept_proposal(instance, owner, second)
    record = instance.accepted_history()[-1].record
    assert record is not None
    accepted_before_action = instance.accepted_coordinate()
    result = service_accept_fixed_playbill_curation(
        instance,
        request=PlaybillCurationAcceptFixedRequestV1(
            item_id=observation.item_id,
            expected_latest_event_digest=event.event_digest,
            reason="the accepted runbook revision changed the evidenced document",
            accepted_proposal_id=second.proposal.admission.proposal_id,
            accepted_changeset_digest=record.changeset_digest,
        ),
        actor_context=_actor(),
    )

    assert result.item.status == "accepted_fixed"
    assert result.item.resolved_at_generation == 2
    assert result.item.accepted_changeset_digest == record.changeset_digest
    assert instance.accepted_coordinate() == accepted_before_action


def test_accept_fixed_refuses_an_unrelated_accepted_changeset(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    first_body = instance.store_document_body(b"status: ready\n")
    initial = _document_shell(first_body.digest)
    first = service_propose_playbill_document(
        instance,
        shell=initial,
        actor_id="owner",
        proposal_name="runbook-initial",
        timestamp="2026-08-26T18:00:00.000000Z",
    )
    accept_proposal(instance, owner, first)
    observation, event = _append_document_item(instance)

    other_body = instance.store_document_body(b"other\n")
    other = _document_shell(other_body.digest).model_copy(
        update={"identity": "document:other", "title": "Other"}
    )
    proposal = service_propose_playbill_document(
        instance,
        shell=other,
        actor_id="owner",
        proposal_name="other",
        timestamp="2026-08-26T18:01:00.000000Z",
    )
    accept_proposal(instance, owner, proposal)
    record = instance.accepted_history()[-1].record
    assert record is not None

    with pytest.raises(PlaybillCurationResolvingChangeUnrelated):
        service_accept_fixed_playbill_curation(
            instance,
            request=PlaybillCurationAcceptFixedRequestV1(
                item_id=observation.item_id,
                expected_latest_event_digest=event.event_digest,
                reason="unrelated change",
                accepted_proposal_id=proposal.proposal.admission.proposal_id,
                accepted_changeset_digest=record.changeset_digest,
            ),
            actor_context=_actor(),
        )


def test_dead_vocabulary_auto_resolves_to_the_accepted_retirement_changeset(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    initial = subject_shell("wi-dead")
    first = service_propose_playbill_subject(
        instance,
        shell=initial,
        actor_id="owner",
        proposal_name="dead-subject-initial",
        timestamp="2026-08-26T18:00:00.000000Z",
    )
    accept_proposal(instance, owner, first)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    path = subject_path(initial.subject_kind, initial.subject_id)
    detection = build_curation_detection(
        pattern_kind="playbill.curation.dead_vocabulary.v1",
        subject=initial.identity,
        detail={"artifact_family": "Subject"},
        coverage=CurationDetectorCoverageV1(
            pattern_kind="playbill.curation.dead_vocabulary.v1",
            status="complete",
            evaluated_fact_count=1,
        ),
        evidence_refs=(
            CurationEvidenceRefV1(
                kind="accepted_artifact",
                identity=initial.identity.qualified,
                path=path,
                generation=1,
                artifact_digest=subject_digest(initial).tagged,
            ),
        ),
    )
    observation = build_pattern_observation(
        detection=detection,
        predecessor_item_id=None,
        accepted_generation=1,
    )
    instance.review_operational_store().append(
        family="curation",
        partition_id=observation.item_id,
        event_id=observation.event_id,
        payload=observation,
        coordinate=coordinate,
        generation=1,
        actor_context=_actor(),
        recorded_at=NOW,
        expected_latest_event_digest=None,
    )
    retired = initial.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired",
                predecessor_digest=subject_digest(initial).tagged,
            )
        }
    )
    retirement = service_propose_playbill_subject(
        instance,
        shell=retired,
        actor_id="owner",
        proposal_name="dead-subject-retirement",
        timestamp="2026-08-26T18:01:00.000000Z",
    )
    accept_proposal(instance, owner, retirement)
    record = instance.accepted_history()[-1].record
    assert record is not None

    listed = service_list_playbill_curation(
        instance,
        request=PlaybillCurationListRequestV1(
            evaluation_time=NOW,
            access_profile=CoverageAccessProfileV1(profile_id="test-curation"),
        ),
        actor_context=_actor(),
    )

    assert observation.item_id not in {item.item_id for item in listed.items}
    item = next(
        item
        for item in replay_curation_items(
            instance.review_operational_store().events(family="curation")
        )
        if item.item_id == observation.item_id
    )
    assert item.status == "accepted_fixed"
    assert item.resolved_at_generation == 2
    assert item.accepted_proposal_id == retirement.proposal.admission.proposal_id
    assert item.accepted_changeset_digest == record.changeset_digest
    accepted_event = CurationAcceptedFixedV1.model_validate(
        instance.review_operational_store().events(family="curation")[-1][1]
    )
    assert accepted_event.reason == "accepted change retired the artifact"
    assert any(
        member.path == path and member.disposition == "retire"
        for member in accepted_event.affected_members
    )


def test_dead_vocabulary_retirement_scan_loads_each_generation_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, owner = initialize_local(tmp_path)
    initial = subject_shell("wi-dead")
    first = service_propose_playbill_subject(
        instance,
        shell=initial,
        actor_id="owner",
        proposal_name="dead-subject-initial",
        timestamp="2026-08-26T18:00:00.000000Z",
    )
    accept_proposal(instance, owner, first)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    path = subject_path(initial.subject_kind, initial.subject_id)
    detection = build_curation_detection(
        pattern_kind="playbill.curation.dead_vocabulary.v1",
        subject=initial.identity,
        detail={"artifact_family": "Subject"},
        coverage=CurationDetectorCoverageV1(
            pattern_kind="playbill.curation.dead_vocabulary.v1",
            status="complete",
            evaluated_fact_count=1,
        ),
        evidence_refs=(
            CurationEvidenceRefV1(
                kind="accepted_artifact",
                identity=initial.identity.qualified,
                path=path,
                generation=1,
                artifact_digest=subject_digest(initial).tagged,
            ),
        ),
    )
    observation = build_pattern_observation(
        detection=detection,
        predecessor_item_id=None,
        accepted_generation=1,
    )
    instance.review_operational_store().append(
        family="curation",
        partition_id=observation.item_id,
        event_id=observation.event_id,
        payload=observation,
        coordinate=coordinate,
        generation=1,
        actor_context=_actor(),
        recorded_at=NOW,
        expected_latest_event_digest=None,
    )
    item = replay_curation_items(instance.review_operational_store().events(family="curation"))[0]
    second_item = item.model_copy(update={"item_id": "sha256:" + "d" * 64})
    retired = initial.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired",
                predecessor_digest=subject_digest(initial).tagged,
            )
        }
    )
    retirement = service_propose_playbill_subject(
        instance,
        shell=retired,
        actor_id="owner",
        proposal_name="dead-subject-retirement",
        timestamp="2026-08-26T18:01:00.000000Z",
    )
    accept_proposal(instance, owner, retirement)
    original_tree_at = instance.tree_at
    loaded_oids: list[str] = []

    def counted_tree_at(oid: str) -> dict[str, bytes]:
        loaded_oids.append(oid)
        return original_tree_at(oid)

    monkeypatch.setattr(instance, "tree_at", counted_tree_at)

    resolutions = _accepted_retirements_for_items(instance, (item, second_item))

    assert set(resolutions) == {item.item_id, second_item.item_id}
    assert len(loaded_oids) == 2
