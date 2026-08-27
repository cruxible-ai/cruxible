"""Admission clustering reads both durable proposal attempt identities."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_types import claim_type_path, parse_claim_type
from cruxible_client.contracts.claims import LiteralClaimObject
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.service.playbill_claims import service_propose_playbill_claim
from cruxible_core.service.playbill_curation import (
    PlaybillCurationListRequestV1,
    service_list_playbill_curation,
)
from cruxible_core.service.playbill_next import PlaybillNextWorkspaceObservationV1
from tests.test_playbill._knowledge_loop_support import TIMESTAMP, authoring, seed_claims

NOW = datetime(2026, 8, 26, 17, tzinfo=UTC)


def test_two_distinct_refused_proposals_cluster_by_claim_type_and_code(
    tmp_path: Path,
) -> None:
    instance, _owner = seed_claims(tmp_path)
    first_authoring = authoring("wi-44", "ready", with_claim_type=False)
    second_authoring = authoring("wi-45", "ready", with_claim_type=False)
    first = service_propose_playbill_claim(
        instance,
        authoring=first_authoring.model_copy(
            update={
                "statement": first_authoring.statement.model_copy(
                    update={"object": LiteralClaimObject(value=1)}
                )
            }
        ),
        actor_id="owner",
        proposal_name="refused-one",
        timestamp=TIMESTAMP,
    )
    second = service_propose_playbill_claim(
        instance,
        authoring=second_authoring.model_copy(
            update={
                "statement": second_authoring.statement.model_copy(
                    update={"object": LiteralClaimObject(value=1)}
                )
            }
        ),
        actor_id="owner",
        proposal_name="refused-two",
        timestamp=TIMESTAMP,
    )
    assert first.proposal.proposal.evaluation.verdict == "refused"
    assert second.proposal.proposal.evaluation.verdict == "refused"

    result = service_list_playbill_curation(
        instance,
        request=PlaybillCurationListRequestV1(
            evaluation_time=NOW,
            access_profile=CoverageAccessProfileV1(profile_id="test-curation"),
        ),
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
    assert clusters[0].detail == {
        "diagnostic_code": "playbill.claim.literal_schema_invalid",
        "refusal_direction": "payload_side",
    }
    attempts = [ref for ref in clusters[0].latest_evidence_refs if ref.kind == "proposal_attempt"]
    assert len(attempts) == 2
    assert len({ref.identity for ref in attempts}) == 2


def test_claim_type_refusals_are_labeled_schema_side(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    base = instance.accepted_coordinate()
    path = claim_type_path("project.work_item.status")
    tree = instance.tree_at(base.git_oid)
    payload = parse_claim_type(tree[path], path=path).model_dump(mode="json")
    payload["artifact_format"] = "playbill-claim-type-v3"
    payload["evidence_freshness"] = {
        "tag": "playbill-claim-evidence-freshness-v1",
        "stale_after": {"tag": "playbill-duration-v1", "microseconds": 0},
    }
    tree[path] = canonical_bytes(payload) + b"\n"
    for suffix in ("one", "two"):
        result = instance.proposal_service().submit(
            actor=AuthenticatedActor(actor_id="owner"),
            request=ProposalAdmissionRequest(
                target_ref=f"refs/proposals/owner/schema-refusal-{suffix}",
                proposed_base_oid=base.git_oid,
            ),
            candidate_tree=tree,
            timestamp="2026-08-26T17:00:00.000000Z",
        )
        assert result.evaluation.diagnostics[0].code == (
            "playbill.claim_type.freshness_horizon_invalid"
        )

    result = service_list_playbill_curation(
        instance,
        request=PlaybillCurationListRequestV1(
            evaluation_time=NOW,
            access_profile=CoverageAccessProfileV1(profile_id="test-curation"),
        ),
        actor_context=GovernedActorContext(
            actor_type="human_user",
            actor_id="curator",
            org_id="org-test",
            operation_id="op-schema-list",
            timestamp=NOW,
        ),
    )

    schema = next(
        item
        for item in result.items
        if item.detail.get("diagnostic_code") == "playbill.claim_type.freshness_horizon_invalid"
    )
    assert schema.subject.qualified == "ClaimType:project.work_item.status"
    assert schema.detail["refusal_direction"] == "schema_side"


def test_restricted_curation_profile_short_circuits_without_count_leakage(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    instance, _owner = seed_claims(tmp_path)

    def must_not_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("restricted curation read reached detectors")

    monkeypatch.setattr(
        "cruxible_core.service.playbill_curation.run_curation_detectors",
        must_not_run,
    )
    result = service_list_playbill_curation(
        instance,
        request=PlaybillCurationListRequestV1(
            evaluation_time=NOW,
            access_profile=CoverageAccessProfileV1(
                profile_id="public-only",
                permitted_access_classes=("public",),
                disclose_restricted_existence=False,
            ),
            workspace_observation=PlaybillNextWorkspaceObservationV1(source_observations=()),
        ),
        actor_context=GovernedActorContext(
            actor_type="human_user",
            actor_id="curator",
            org_id="org-test",
            operation_id="op-restricted-list",
            timestamp=NOW,
        ),
    )

    assert result.items == ()
    assert result.detector_coverage == ()
    assert result.observation_coverage.model_dump(mode="json") == {
        "tag": "playbill-curation-observation-coverage-v1",
        "source_count": 0,
        "observed_block_count": 0,
        "omitted_source_count": 0,
        "omissions": [],
    }
    assert instance.review_operational_store().events() == ()
