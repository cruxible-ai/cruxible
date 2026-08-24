"""ClaimType-v2 and knowledge.brief authoring profile laws."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from cruxible_client.contracts.authoring.models import (
    AuthoringClaimStatementV1,
    AuthoringExistingClaimDispositionV1,
    ClaimAuthoringPayloadV1,
    SelfSourceBodyV1,
)
from cruxible_client.contracts.claim_types import (
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
    render_claim_type,
)
from cruxible_client.contracts.claims import (
    LiteralClaimObject,
    claim_path,
    parse_claim,
)
from cruxible_client.contracts.knowledge_briefs import (
    KNOWLEDGE_BRIEF_CLAIM_TYPE,
    KNOWLEDGE_BRIEF_PREDICATE,
    KnowledgeBriefValueV1,
    knowledge_brief_purpose_digest,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import subject_path
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.service.playbill_floor import service_export_playbill_floor
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_authoring_preflight import TIMESTAMP, _seed_claim_surface
from tests.test_playbill.test_claims import _claim_type, _subject


def _brief_payload(
    value: KnowledgeBriefValueV1,
    *,
    claim_ref: str | None = None,
    dispositions: tuple[AuthoringExistingClaimDispositionV1, ...] = (),
    qualifier: str | None = None,
) -> ClaimAuthoringPayloadV1:
    return ClaimAuthoringPayloadV1(
        statement=AuthoringClaimStatementV1(
            subject=SemanticAddress.whole_artifact(
                subject_path(_subject().subject_kind, _subject().subject_id)
            ),
            predicate=KNOWLEDGE_BRIEF_PREDICATE,
            qualifier=qualifier,
            object=LiteralClaimObject(value=value.model_dump(mode="json")),
            role="normative",
        ),
        rationale="Preserve concise governed guidance.",
        source=SelfSourceBodyV1(
            content_base64=base64.b64encode(value.prose.encode()).decode("ascii")
        ),
        claim_ref=claim_ref,
        existing_claim_dispositions=dispositions,
    )


def _activate(instance, owner, result) -> None:  # type: ignore[no-untyped-def]
    assert result.status.proposal_id is not None
    assert result.status.candidate_digest is not None
    approval = _sign(
        owner,
        result.status.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=result.status.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="owner",
    )
    assert (
        service_activate_playbill_proposal(instance, proposal_id=result.status.proposal_id).status
        == "accepted"
    )


def test_claim_type_v1_wire_and_digest_are_unchanged_by_mixed_v2_parser() -> None:
    original = _claim_type()
    content = render_claim_type(original)
    parsed = parse_claim_type(content, path=claim_type_path(original.predicate))

    assert b"subject_scope" not in content
    assert b"slot_policy" not in content
    assert parsed == original
    assert claim_type_digest(parsed) == claim_type_digest(original)


def test_first_brief_installs_exact_builtin_type_and_derives_purpose_slot(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    value = KnowledgeBriefValueV1(
        purpose="How should this work item be released?",
        kind="guidance",
        prose="Use the release checklist.",
    )
    intent = coordinator.create(
        actor=actor,
        payload=_brief_payload(value),
        canonical_timestamp=TIMESTAMP,
    ).intent

    submitted = coordinator.submit(intent.intent_id, actor=actor)
    assert submitted.status.state == "awaiting_external_approval"
    _activate(instance, owner, submitted)

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    type_path = claim_type_path(KNOWLEDGE_BRIEF_PREDICATE)
    assert parse_claim_type(tree[type_path], path=type_path) == KNOWLEDGE_BRIEF_CLAIM_TYPE
    path = claim_path(intent.semantic_identity)
    claim = parse_claim(tree[path], path=path)
    assert claim.statement.qualifier == knowledge_brief_purpose_digest(value.purpose)
    assert claim.statement.claim_type_digest == claim_type_digest(KNOWLEDGE_BRIEF_CLAIM_TYPE).tagged
    floor = service_export_playbill_floor(instance)
    card = floor[f"briefs/{intent.semantic_identity}.card.json"]
    payload = json.loads(card)
    assert payload["health_receipt_digest"].startswith("sha256:")
    assert payload["prose"] == "Use the release checklist."
    assert payload["slot_state"] == "accepted"
    assert payload["source_handles"]


def test_brief_qualifier_mismatch_refuses_with_its_repair(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    value = KnowledgeBriefValueV1(
        purpose="What is current?",
        kind="brief",
        prose="The work item is ready.",
    )
    intent = coordinator.create(
        actor=actor,
        payload=_brief_payload(value, qualifier="sha256:" + "0" * 64),
        canonical_timestamp=TIMESTAMP,
    ).intent

    result = coordinator.preflight(intent.intent_id, actor=actor)

    assert result.verdict == "refused"
    diagnostic = next(
        item
        for item in result.frontier.diagnostics
        if item.code == "playbill.authoring.knowledge_brief_qualifier_mismatch"
    )
    assert diagnostic.repairs[0].kind == "omit_qualifier"


def test_brief_dispositions_are_partitioned_by_purpose_slot(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    first_value = KnowledgeBriefValueV1(
        purpose="How should this work item be released?",
        kind="guidance",
        prose="Use checklist A.",
    )
    first = coordinator.create(
        actor=actor,
        payload=_brief_payload(first_value),
        canonical_timestamp=TIMESTAMP,
    ).intent
    _activate(instance, owner, coordinator.submit(first.intent_id, actor=actor))

    distinct = coordinator.create(
        actor=actor,
        payload=_brief_payload(
            KnowledgeBriefValueV1(
                purpose="Who approves this work item?",
                kind="faq",
                prose="The owner approves it.",
            )
        ),
        canonical_timestamp="2026-08-21T12:00:01.000000Z",
    ).intent
    assert coordinator.preflight(distinct.intent_id, actor=actor).verdict == "passed"

    same_slot = coordinator.create(
        actor=actor,
        payload=_brief_payload(
            KnowledgeBriefValueV1(
                purpose=first_value.purpose,
                kind="guidance",
                prose="Use checklist B.",
            )
        ),
        canonical_timestamp="2026-08-21T12:00:02.000000Z",
    ).intent
    refused = coordinator.preflight(same_slot.intent_id, actor=actor)
    assert refused.verdict == "refused"
    assert {item.code for item in refused.frontier.diagnostics} == {
        "playbill.authoring.existing_claim_dispositions_incomplete"
    }

    repaired = coordinator.replace_payload(
        same_slot.intent_id,
        actor=actor,
        payload=_brief_payload(
            KnowledgeBriefValueV1(
                purpose=first_value.purpose,
                kind="guidance",
                prose="Use checklist B.",
            ),
            dispositions=(
                AuthoringExistingClaimDispositionV1(
                    claim_id=first.semantic_identity,
                    disposition="not_tested",
                ),
            ),
        ),
    )
    assert coordinator.preflight(repaired.intent.intent_id, actor=actor).verdict == "passed"


__all__ = ["_activate", "_brief_payload"]
