"""Admission clustering reads both durable proposal attempt identities."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.service.playbill_claims import service_propose_playbill_claim
from cruxible_core.service.playbill_curation import (
    PlaybillCurationListRequestV1,
    service_list_playbill_curation,
)
from tests.test_playbill._knowledge_loop_support import TIMESTAMP, authoring, seed_claims

NOW = datetime(2026, 8, 26, 17, tzinfo=UTC)


def test_two_distinct_refused_proposals_cluster_by_claim_type_and_code(
    tmp_path: Path,
) -> None:
    instance, _owner = seed_claims(tmp_path)
    first = service_propose_playbill_claim(
        instance,
        authoring=authoring("wi-44", "ready", with_claim_type=False),
        actor_id="unregistered",
        proposal_name="refused-one",
        timestamp=TIMESTAMP,
    )
    second = service_propose_playbill_claim(
        instance,
        authoring=authoring("wi-45", "ready", with_claim_type=False),
        actor_id="unregistered",
        proposal_name="refused-two",
        timestamp=TIMESTAMP,
    )
    assert first.proposal.proposal.evaluation.verdict == "refused"
    assert second.proposal.proposal.evaluation.verdict == "refused"

    result = service_list_playbill_curation(
        instance,
        request=PlaybillCurationListRequestV1(evaluation_time=NOW),
        actor_context=GovernedActorContext(
            actor_type="human_user",
            actor_id="curator",
            org_id="org-test",
            operation_id="op-list",
            timestamp=NOW,
        ),
    )

    clusters = [
        item
        for item in result.items
        if item.pattern_kind == "playbill.curation.admission_failure_cluster.v1"
    ]
    assert len(clusters) == 1
    assert clusters[0].subject.qualified == "ClaimType:project.work_item.status"
    assert clusters[0].detail == {"diagnostic_code": "playbill.claim.actor_unauthorized"}
    attempts = [ref for ref in clusters[0].latest_evidence_refs if ref.kind == "proposal_attempt"]
    assert len(attempts) == 2
    assert len({ref.identity for ref in attempts}) == 2
