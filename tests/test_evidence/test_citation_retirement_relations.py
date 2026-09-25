"""Shared-Capture retirement context through real authoring and Claim explanation surfaces."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cruxible_core.claims.claim_retirement import service_retire_claim
from cruxible_core.coverage.contracts import (
    CoverageAccessProfileV1,
)
from cruxible_core.evidence.citation_relations import (
    RELATION_RETIRED_CONFLICT_SCHEMA,
    retired_activation_live_candidates,
)
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.service.claims.claims import service_explain_playbill_claim
from cruxible_core.service.claims.retirement_context import ClaimRetirementContextV1
from cruxible_core.service.discovery.next import (
    PlaybillNextRequestV1,
    service_playbill_next,
)
from tests.core_support._citation_relations_oracle import (
    RELATION_USE_SCHEMA,
    build_citation_relation_facts,
)
from tests.test_authoring.test_authoring_existing_capture import shared_capture_world
from tests.test_claims.test_claim_retirement import (
    _activate as _activate_retirement,
)
from tests.test_claims.test_claim_retirement import (
    _request as _retirement_request,
)
from tests.test_proposals.test_retirement_citing_advisory import (
    COPY_CLAIM_ID,
    SOURCE_CLAIM_ID,
    copied_from_world,
)

EVALUATION_TIME = datetime(2026, 8, 24, 18, tzinfo=UTC)


def _access() -> CoverageAccessProfileV1:
    return CoverageAccessProfileV1(
        profile_id="citation-retirement-test",
        permitted_access_classes=("instance", "public"),
    )


def _retire_claim(instance, owner, claim_id: str) -> None:  # type: ignore[no-untyped-def]
    result = service_retire_claim(
        instance,
        claim_id=claim_id,
        request=_retirement_request(instance, mode="submit"),
        actor=AuthenticatedActor(actor_id="owner"),
    )
    _activate_retirement(instance, owner, result)
    instance.refresh()


def _next(instance):  # type: ignore[no-untyped-def]
    return service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(evaluation_time=EVALUATION_TIME, access_profile=_access()),
    )


def retirement_context(instance, claim_id: str) -> ClaimRetirementContextV1 | None:  # type: ignore[no-untyped-def]
    explanation = service_explain_playbill_claim(
        instance, identity=claim_id, evaluation_time=EVALUATION_TIME
    )
    return explanation.retirement_context


def test_shared_capture_is_explain_context_not_queue_work_and_retirement_clears_it(
    tmp_path: Path,
) -> None:
    instance, owner, _actor, first, live_claim_id, *_rest = shared_capture_world(tmp_path)
    _retire_claim(instance, owner, first)

    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        conflicts = projection.citations.conflicts()
    assert len(conflicts) == 1
    assert conflicts[0].value["relation_kind"] == "capture"  # type: ignore[index]
    assert conflicts[0].value["live_claim_identity"] == f"Claim:{live_claim_id}"  # type: ignore[index]

    # Sharing evidence with a retired Claim is context for its reviewer, not work.
    assert not [
        item for item in _next(instance).items if item.subject_identity == f"Claim:{live_claim_id}"
    ]
    context = retirement_context(instance, live_claim_id)
    assert context is not None
    (relation,) = context.shared_with_retired
    assert relation.relation_kind == "capture"
    assert relation.relation_key == f"capture:{relation.live_capture_digest}"
    assert relation.retired_claim_count == 1
    assert relation.retired_claim_witnesses == (f"Claim:{first}",)
    assert relation.retired_citation_count == 1
    assert len(relation.retired_citation_witnesses) == 1
    # The retired side shares nothing live, and the section is absent on the wire.
    assert retirement_context(instance, first) is None
    explanation = service_explain_playbill_claim(
        instance, identity=first, evaluation_time=EVALUATION_TIME
    )
    assert "retirement_context" not in explanation.model_dump(mode="json")

    _retire_claim(instance, owner, live_claim_id)
    assert retirement_context(instance, live_claim_id) is None


def test_published_retirement_conflicts_match_complete_source_oracle(
    tmp_path: Path,
) -> None:
    instance, owner, _actor, first, second, *_rest = shared_capture_world(tmp_path)
    _retire_claim(instance, owner, first)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    full = build_citation_relation_facts(tree, bodies=instance.body_store())
    full_uses = [fact for fact in full if fact.schema_id == RELATION_USE_SCHEMA]
    assert len(full_uses) == 2
    assert {
        (
            fact.value["claim_identity"],  # type: ignore[index]
            fact.value["claim_lifecycle"],  # type: ignore[index]
        )
        for fact in full_uses
    } == {
        (f"Claim:{first}", "retired"),
        (f"Claim:{second}", "live"),
    }
    full_conflicts = [fact for fact in full if fact.schema_id == RELATION_RETIRED_CONFLICT_SCHEMA]
    assert len(full_conflicts) == 1
    assert full_conflicts[0].value == {
        "live_capture_digest": {"$digest": full_uses[0].value["capture_digest"]["$digest"]},  # type: ignore[index]
        "live_citation_id": next(
            fact.value["citation_id"]  # type: ignore[index]
            for fact in full_uses
            if fact.value["claim_identity"] == f"Claim:{second}"  # type: ignore[index]
        ),
        "live_claim_artifact_digest": next(
            fact.value["claim_artifact_digest"]  # type: ignore[index]
            for fact in full_uses
            if fact.value["claim_identity"] == f"Claim:{second}"  # type: ignore[index]
        ),
        "live_claim_identity": f"Claim:{second}",
        "relation_key": "capture:" + full_uses[0].value["capture_digest"]["$digest"],  # type: ignore[index]
        "relation_kind": "capture",
        "retired_citation_count": 1,
        "retired_citation_witnesses": [
            next(
                fact.value["citation_id"]  # type: ignore[index]
                for fact in full_uses
                if fact.value["claim_identity"] == f"Claim:{first}"  # type: ignore[index]
            )
        ],
        "retired_claim_count": 1,
        "retired_claim_witnesses": [f"Claim:{first}"],
    }

    def key(fact):  # type: ignore[no-untyped-def]
        return fact.schema_id, fact.subject_identity, fact.fact_key

    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        assert sorted(projection.citations.conflicts(), key=key) == sorted(
            full_conflicts,
            key=key,
        )


def test_span_sweep_scans_active_live_set_once_per_retired_activation_epoch() -> None:
    active_live = {f"live-{index:04d}": index for index in range(256)}
    active_retired: dict[str, object] = {}
    visits = 0

    for index in range(1024):
        visits += len(retired_activation_live_candidates(active_retired, active_live))
        active_retired[f"retired-{index:04d}"] = index

    assert visits == len(active_live)


def test_a_copied_claim_shares_its_retired_source_span(tmp_path: Path) -> None:
    instance, _owner, _coordinator, _actor = copied_from_world(tmp_path)

    assert retirement_context(instance, SOURCE_CLAIM_ID) is None
    context = retirement_context(instance, COPY_CLAIM_ID)
    assert context is not None
    assert [relation.relation_kind for relation in context.shared_with_retired] == [
        "same_version_span"
    ]
