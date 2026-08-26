"""Accepted-fixed lifecycle links only exact related accepted ChangeSets."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    document_digest,
    document_path,
    parse_document,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.curation import (
    CurationDetectorCoverageV1,
    CurationEvidenceRefV1,
    build_curation_detection,
    build_pattern_observation,
)
from cruxible_core.playbill.service.documents import service_propose_playbill_document
from cruxible_core.service.playbill_curation import (
    PlaybillCurationAcceptFixedRequestV1,
    PlaybillCurationResolvingChangeUnrelated,
    service_accept_fixed_playbill_curation,
)
from tests.test_playbill._knowledge_loop_support import accept_proposal
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
            approval_roles=("owner",),
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
    accept_proposal(instance, owner, first, sequence=1)
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
    accept_proposal(instance, owner, second, sequence=2)
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
    accept_proposal(instance, owner, first, sequence=1)
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
    accept_proposal(instance, owner, proposal, sequence=2)
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
