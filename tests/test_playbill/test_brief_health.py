"""Canonical knowledge.brief health evaluation and receipt laws."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cruxible_client.contracts.authoring.models import AuthoringExistingClaimDispositionV1
from cruxible_client.contracts.claims import claim_path, claim_statement_digest, parse_claim
from cruxible_client.contracts.knowledge_briefs import (
    KnowledgeBriefClaimExpectationV1,
    KnowledgeBriefClaimRefV1,
    KnowledgeBriefQueryRefV1,
    KnowledgeBriefValueV1,
)
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.brief_health import (
    KnowledgeBriefHealthRequestV1,
    evaluate_knowledge_brief_health,
)
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.service.playbill_briefs import (
    service_list_playbill_brief_reauthor_queue,
)
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_authoring_preflight import (
    _seed_claim_surface,
    _self_source_payload,
)
from tests.test_playbill.test_knowledge_briefs import _activate, _brief_payload

EVALUATION_TIME = datetime(2026, 8, 21, 13, tzinfo=UTC)


def _accepted_brief(
    instance,
    owner,
    coordinator: AuthoringIntentCoordinator,
    actor: AuthenticatedActor,
    *,
    value: KnowledgeBriefValueV1,
    timestamp: str,
    claim_ref: str | None = None,
    dispositions: tuple[AuthoringExistingClaimDispositionV1, ...] = (),
) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    intent = coordinator.create(
        actor=actor,
        payload=_brief_payload(
            value,
            claim_ref=claim_ref,
            dispositions=dispositions,
        ),
        canonical_timestamp=timestamp,
    ).intent
    submitted = coordinator.submit(intent.intent_id, actor=actor)
    _activate(instance, owner, submitted)
    path = claim_path(intent.semantic_identity)
    claim = parse_claim(instance.tree_at(instance.accepted_coordinate().git_oid)[path], path=path)
    return intent.semantic_identity, claim_statement_digest(claim.statement).tagged


def _request(instance, statement_digest: str) -> KnowledgeBriefHealthRequestV1:  # type: ignore[no-untyped-def]
    return KnowledgeBriefHealthRequestV1(
        brief_statement_digest=statement_digest,
        accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        evaluation_time=EVALUATION_TIME,
        access_profile=CoverageAccessProfileV1(profile_id="brief-health-test"),
    )


def _ref(claim_id: str, statement_digest: str) -> KnowledgeBriefClaimRefV1:
    return KnowledgeBriefClaimRefV1(
        claim_id=claim_id,
        statement_digest=statement_digest,
        expect=KnowledgeBriefClaimExpectationV1(),
    )


def test_health_receipt_is_byte_deterministic_and_tracks_semantic_succession(
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
        timestamp="2026-08-21T12:00:00.000000Z",
    )
    _consumer_id, consumer_digest = _accepted_brief(
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
        timestamp="2026-08-21T12:00:01.000000Z",
    )

    request = _request(instance, consumer_digest)
    first = evaluate_knowledge_brief_health(instance, request)
    retry = evaluate_knowledge_brief_health(instance, request)

    assert retry == first
    assert first.result.healthy is True
    assert [item.state for item in first.result.ref_states] == ["accepted_current"]
    assert first.receipt.result_digest == first.result.result_digest

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
        timestamp="2026-08-21T12:00:02.000000Z",
        claim_ref=source_id,
        dispositions=(
            AuthoringExistingClaimDispositionV1(
                claim_id=source_id,
                disposition="not_tested",
            ),
        ),
    )
    after_successor = evaluate_knowledge_brief_health(
        instance,
        _request(instance, consumer_digest),
    )
    assert after_successor.result.healthy is False
    assert [item.state for item in after_successor.result.ref_states] == ["superseded_semantically"]
    queue = service_list_playbill_brief_reauthor_queue(
        instance,
        evaluation_time=EVALUATION_TIME,
        access_profile=CoverageAccessProfileV1(profile_id="brief-health-test"),
    )
    assert [item.identity for item in queue.entries] == [_consumer_id]
    assert queue.entries[0].health == after_successor


def test_health_depth_budget_truncates_and_can_never_report_healthy(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    prior: tuple[str, str] | None = None
    for index in range(6):
        refs = () if prior is None else (_ref(*prior),)
        prior = _accepted_brief(
            instance,
            owner,
            coordinator,
            actor,
            value=KnowledgeBriefValueV1(
                purpose=f"Depth {index}?",
                kind="brief",
                claim_refs=refs,
                prose=f"Depth {index}.",
            ),
            timestamp=f"2026-08-21T12:00:{index:02d}.000000Z",
        )
    assert prior is not None

    evaluation = evaluate_knowledge_brief_health(instance, _request(instance, prior[1]))

    assert evaluation.result.truncated is True
    assert evaluation.result.healthy is False
    assert evaluation.result.verdict == "completed"


def test_health_refuses_lineage_cycle_without_partial_traversal(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    first_id, first_digest = _accepted_brief(
        instance,
        owner,
        coordinator,
        actor,
        value=KnowledgeBriefValueV1(
            purpose="What is A?",
            kind="brief",
            prose="A initially stands alone.",
        ),
        timestamp="2026-08-21T12:00:00.000000Z",
    )
    second_id, second_digest = _accepted_brief(
        instance,
        owner,
        coordinator,
        actor,
        value=KnowledgeBriefValueV1(
            purpose="What is B?",
            kind="brief",
            claim_refs=(_ref(first_id, first_digest),),
            prose="B depends on A.",
        ),
        timestamp="2026-08-21T12:00:01.000000Z",
    )
    _first_id, successor_digest = _accepted_brief(
        instance,
        owner,
        coordinator,
        actor,
        value=KnowledgeBriefValueV1(
            purpose="What is A?",
            kind="brief",
            claim_refs=(_ref(second_id, second_digest),),
            prose="A now depends on B.",
        ),
        timestamp="2026-08-21T12:00:02.000000Z",
        claim_ref=first_id,
        dispositions=(
            AuthoringExistingClaimDispositionV1(
                claim_id=first_id,
                disposition="not_tested",
            ),
        ),
    )

    evaluation = evaluate_knowledge_brief_health(
        instance,
        _request(instance, successor_digest),
    )

    assert evaluation.result.verdict == "refused"
    assert evaluation.result.ref_states == ()
    assert evaluation.result.query_states == ()
    assert evaluation.result.cycle_refusal is not None
    assert evaluation.result.cycle_refusal.path == (first_id, second_id, first_id)


def test_missing_pinned_query_is_an_unhealthy_refusal_not_a_health_error(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    _brief_id, statement_digest = _accepted_brief(
        instance,
        owner,
        coordinator,
        actor,
        value=KnowledgeBriefValueV1(
            purpose="What did the missing query say?",
            kind="brief",
            query_refs=(
                KnowledgeBriefQueryRefV1(
                    query_id="missing-query",
                    definition_digest="sha256:" + "0" * 64,
                    parameters={},
                    render_field="answer",
                ),
            ),
            prose="Answer: {missing-query.answer}",
        ),
        timestamp="2026-08-21T12:00:00.000000Z",
    )

    evaluation = evaluate_knowledge_brief_health(
        instance,
        _request(instance, statement_digest),
    )

    assert evaluation.result.verdict == "completed"
    assert evaluation.result.healthy is False
    assert [item.state for item in evaluation.result.query_states] == ["refused"]


def test_supported_same_value_slot_keeps_the_original_brief_ref_current(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")

    original = coordinator.create(
        actor=actor,
        payload=_self_source_payload(),
        canonical_timestamp="2026-08-21T12:00:00.000000Z",
    ).intent
    _activate(instance, owner, coordinator.submit(original.intent_id, actor=actor))
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    original_path = claim_path(original.semantic_identity)
    original_claim = parse_claim(tree[original_path], path=original_path)
    original_digest = claim_statement_digest(original_claim.statement).tagged

    supporting = coordinator.create(
        actor=actor,
        payload=_self_source_payload().model_copy(
            update={
                "existing_claim_dispositions": (
                    AuthoringExistingClaimDispositionV1(
                        claim_id=original.semantic_identity,
                        disposition="support",
                    ),
                )
            }
        ),
        canonical_timestamp="2026-08-21T12:00:01.000000Z",
    ).intent
    _activate(instance, owner, coordinator.submit(supporting.intent_id, actor=actor))

    _brief_id, brief_digest = _accepted_brief(
        instance,
        owner,
        coordinator,
        actor,
        value=KnowledgeBriefValueV1(
            purpose="What is the current work status?",
            kind="brief",
            claim_refs=(_ref(original.semantic_identity, original_digest),),
            prose="The referenced status is current.",
        ),
        timestamp="2026-08-21T12:00:02.000000Z",
    )

    evaluation = evaluate_knowledge_brief_health(instance, _request(instance, brief_digest))

    assert evaluation.result.healthy is True
    assert [item.state for item in evaluation.result.ref_states] == ["accepted_current"]
