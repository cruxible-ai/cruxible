"""PC-G5 deterministic Playbill next queue laws."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.contracts.authoring.models import AuthoringExistingClaimDispositionV1
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.captures import parse_capture_envelope
from cruxible_client.contracts.claims import claim_citation_references
from cruxible_client.contracts.knowledge_briefs import KnowledgeBriefValueV1
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.service.playbill_claims import _claim_from_view, service_list_playbill_claims
from cruxible_core.service.playbill_next import (
    PlaybillNextAccessProfileInvalid,
    PlaybillNextDriftObservationV1,
    PlaybillNextRequestV1,
    PlaybillNextWorkspaceObservationInvalid,
    PlaybillNextWorkspaceObservationV1,
    service_playbill_next,
    validate_playbill_next_request,
)
from tests.test_playbill._knowledge_loop_support import seed_claims
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_authoring_preflight import _seed_claim_surface
from tests.test_playbill.test_brief_health import _accepted_brief, _ref

EVALUATION_TIME = datetime(2026, 8, 24, 18, tzinfo=UTC)


def _access() -> CoverageAccessProfileV1:
    return CoverageAccessProfileV1(
        profile_id="next-test",
        permitted_access_classes=("instance", "public"),
    )


def test_next_is_deterministic_and_reuses_the_canonical_brief_health_evaluator(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    source_id, source_digest = _accepted_brief(
        instance,
        owner,
        coordinator,
        actor,
        value=KnowledgeBriefValueV1(
            purpose="What is the release rule?",
            kind="guidance",
            prose="Use checklist A.",
        ),
        timestamp="2026-08-24T17:00:00.000000Z",
    )
    consumer_id, _consumer_digest = _accepted_brief(
        instance,
        owner,
        coordinator,
        actor,
        value=KnowledgeBriefValueV1(
            purpose="How should the agent release?",
            kind="brief",
            claim_refs=(_ref(source_id, source_digest),),
            prose="Follow the referenced guidance.",
        ),
        timestamp="2026-08-24T17:00:01.000000Z",
    )
    _accepted_brief(
        instance,
        owner,
        coordinator,
        actor,
        value=KnowledgeBriefValueV1(
            purpose="What is the release rule?",
            kind="guidance",
            prose="Use checklist B.",
        ),
        timestamp="2026-08-24T17:00:02.000000Z",
        claim_ref=source_id,
        dispositions=(
            AuthoringExistingClaimDispositionV1(
                claim_id=source_id,
                disposition="not_tested",
            ),
        ),
    )
    request = PlaybillNextRequestV1(
        evaluation_time=EVALUATION_TIME,
        access_profile=_access(),
    )

    first = service_playbill_next(instance, request=request)
    retry = service_playbill_next(instance, request=request)

    assert retry == first
    assert first.observed_domains == ("accepted_state",)
    assert first.unobserved_domains == ("workspace_floor", "workspace_sources")
    brief = next(item for item in first.items if item.reason == "brief_unhealthy")
    assert brief.subject_identity == f"Claim:{consumer_id}"
    assert brief.related_identities == (source_id,)
    assert brief.repair.operation == "playbill.authoring.create"
    assert brief.item_id.startswith("sha256:")
    assert first.result_digest.startswith("sha256:")


def test_workspace_drift_is_verified_against_the_accepted_citation(
    tmp_path: Path,
) -> None:
    instance, _owner = seed_claims(tmp_path)
    listed = service_list_playbill_claims(instance)
    claim = _claim_from_view(listed.claims[0])
    citation = claim_citation_references(claim)[0]
    envelope = parse_capture_envelope(
        instance.body_store().read(
            citation.capture_digest,
            access=BodyAccessContext(principal_id="next-test", can_read_body=True),
        )
    )
    observed = typed_digest(
        Sha256Value,
        "playbill-next-test-observed-v1",
        {"citation_id": citation.citation_id},
    ).tagged
    request = PlaybillNextRequestV1(
        at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        evaluation_time=EVALUATION_TIME,
        access_profile=_access(),
        workspace_observation=PlaybillNextWorkspaceObservationV1(
            floor_status="missing",
            drift_observations=(
                PlaybillNextDriftObservationV1(
                    citation_id=citation.citation_id,
                    expected_commitment_digest=envelope.commitment.digest,
                    observed_commitment_digest=observed,
                ),
            ),
        ),
    )

    result = service_playbill_next(instance, request=request)

    assert result.observed_domains == (
        "accepted_state",
        "workspace_floor",
        "workspace_sources",
    )
    assert result.unobserved_domains == ()
    assert {item.reason for item in result.items}.issuperset({"citation_drifted", "floor_missing"})
    drift = next(item for item in result.items if item.reason == "citation_drifted")
    assert drift.related_identities == (citation.citation_id,)
    assert drift.repair.operation == "playbill.authoring.bind"

    workspace = request.workspace_observation
    assert workspace is not None
    drifts = workspace.drift_observations
    assert drifts is not None
    substituted = request.model_copy(
        update={
            "workspace_observation": workspace.model_copy(
                update={
                    "drift_observations": (
                        drifts[0].model_copy(
                            update={
                                "expected_commitment_digest": typed_digest(
                                    Sha256Value,
                                    "playbill-next-test-substitution-v1",
                                    {},
                                ).tagged
                            }
                        ),
                    )
                }
            )
        }
    )
    with pytest.raises(PlaybillNextWorkspaceObservationInvalid):
        service_playbill_next(instance, request=substituted)


def test_unknown_access_profile_value_has_the_frozen_refusal() -> None:
    with pytest.raises(PlaybillNextAccessProfileInvalid) as raised:
        validate_playbill_next_request(
            {
                "evaluation_time": EVALUATION_TIME,
                "access_profile": {
                    "tag": "playbill-coverage-access-profile-v1",
                    "profile_id": "next-test",
                    "permitted_access_classes": ["secret"],
                    "disclose_restricted_existence": False,
                },
            }
        )

    assert raised.value.code == "playbill.next.access_profile_invalid"
