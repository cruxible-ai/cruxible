"""PC-G5 AuthoringIntent rebase identity and retry laws."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.claim_types import claim_type_path, parse_claim_type
from cruxible_client.contracts.query.definitions import (
    query_definition_path,
    render_query_definition,
)
from cruxible_core.playbill.authoring.coordinator import (
    AuthoringIntentCoordinator,
    AuthoringIntentRebaseError,
    AuthoringIntentRebaseSubmitted,
)
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.proposals import AuthenticatedActor
from tests.test_playbill._adoption_fixture import _query_definition
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_authoring_preflight import (
    TIMESTAMP,
    _seed_claim_surface,
    _self_source_payload,
    _working_payload,
)
from tests.test_playbill.test_claims import _claim_type
from tests.test_playbill.test_resolution_contracts import _accept_tree


def _coordinator(instance) -> AuthoringIntentCoordinator:  # type: ignore[no-untyped-def]
    return AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(
            instance.root / instance.descriptor.storage.exhaust,
            token_factory=lambda: "a" * 32,
        ),
        claim_id_factory=lambda: "CLM-" + "b" * 32,
    )


def _advance_accepted_head(instance, owner) -> None:  # type: ignore[no-untyped-def]
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = claim_type_path(_claim_type().predicate)
    claim_type = parse_claim_type(tree[path], path=path)
    query = _query_definition(91, claim_type)
    tree[query_definition_path(query.identity.name)] = render_query_definition(query)
    _accept_tree(
        instance,
        owner,
        tree,
        timestamp="2026-08-21T12:01:00.000000Z",
        proposal_name="advance-for-rebase",
    )


def test_refused_intent_rebases_without_changing_authoring_identity(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    created = coordinator.create(
        actor=actor,
        payload=_working_payload(occurrence_count=2),
        canonical_timestamp=TIMESTAMP,
    ).intent
    refused = coordinator.preflight(created.intent_id, actor=actor)
    assert refused.verdict == "refused"
    before = coordinator.get(created.intent_id, actor=actor).intent
    assert before.candidate_status.state == "preflight_refused"

    _advance_accepted_head(instance, owner)
    first = coordinator.rebase(created.intent_id, actor=actor).intent
    second = coordinator.rebase(created.intent_id, actor=actor).intent

    assert second == first
    assert first.intent_id == before.intent_id
    assert first.semantic_identity == before.semantic_identity
    assert first.payload == before.payload
    assert first.payload_digest == before.payload_digest
    assert first.create_fingerprint == before.create_fingerprint
    assert first.canonical_timestamp == before.canonical_timestamp
    assert first.intent_revision == before.intent_revision + 1
    assert first.base_coordinate.git_oid == instance.accepted_coordinate().git_oid
    assert first.last_preflight is None
    assert first.candidate_status.state == "draft"
    deduped = coordinator.create(
        actor=actor,
        payload=before.payload,
        canonical_timestamp=before.canonical_timestamp,
    ).intent
    assert deduped.intent_id == first.intent_id


def test_rebase_refuses_draft_and_submitted_intents(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    draft = coordinator.create(
        actor=actor,
        payload=_self_source_payload(),
        canonical_timestamp=TIMESTAMP,
    ).intent

    with pytest.raises(AuthoringIntentRebaseError, match="intent_rebase_not_allowed"):
        coordinator.rebase(draft.intent_id, actor=actor)

    submitted = coordinator.submit(draft.intent_id, actor=actor)
    assert submitted.status.proposal_id is not None
    with pytest.raises(AuthoringIntentRebaseSubmitted, match="intent_rebase_submitted"):
        coordinator.rebase(draft.intent_id, actor=actor)
