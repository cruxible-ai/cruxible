"""A refused intent whose only fault is a stale base advances on preflight."""

from __future__ import annotations

from pathlib import Path

from cruxible_client.contracts.claim_types import claim_type_path, parse_claim_type
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.query.definitions import (
    query_definition_path,
    render_query_definition,
)
from cruxible_core.proposals.proposals import AuthenticatedActor
from tests.core_support._adoption_fixture import _query_definition
from tests.core_support._support import initialize_local
from tests.test_authoring.test_authoring_change_set_intents import (
    _accept,
    _change_set,
    _claim,
    _coordinator,
    _create,
    _seed,
)
from tests.test_authoring.test_authoring_preflight import (
    TIMESTAMP,
    _seed_claim_surface,
    _working_payload,
)
from tests.test_claims.test_claims import _claim_type
from tests.test_indexes.test_resolution_contracts import _accept_tree


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


def test_stale_base_refusal_advances_the_intent_when_it_passes_at_head(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    intent = coordinator.create(
        actor=actor,
        payload=_change_set(
            _claim(subject_id="wi-late", qualifier="late"),
            _claim(qualifier="here"),
        ),
        canonical_timestamp=TIMESTAMP,
    ).intent
    assert coordinator.preflight(intent.intent_id, actor=actor).verdict == "refused"
    # The missing Subject is accepted underneath the intent's base.
    advance = coordinator.create(
        actor=actor, payload=_change_set(_create("wi-late")), canonical_timestamp=TIMESTAMP
    ).intent
    _accept(instance, owner, coordinator, advance.intent_id, actor)
    head = AcceptedCoordinate.from_internal(instance.accepted_coordinate())

    first = coordinator.preflight(intent.intent_id, actor=actor)
    advanced = coordinator.get(intent.intent_id, actor=actor).intent
    again = coordinator.preflight(intent.intent_id, actor=actor)

    assert first.verdict == again.verdict == "passed"
    assert first.certificate.accepted_coordinate == head
    assert advanced.base_coordinate == head
    assert advanced.intent_id == intent.intent_id
    assert advanced.semantic_identity == intent.semantic_identity
    assert advanced.payload_digest == intent.payload_digest
    assert advanced.canonical_timestamp == intent.canonical_timestamp
    assert advanced.intent_revision == intent.intent_revision + 1
    # A second preflight at the same head is not a second advance.
    assert coordinator.get(intent.intent_id, actor=actor).intent.intent_revision == (
        advanced.intent_revision
    )


def test_payload_fault_keeps_the_intent_on_its_base(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    created = coordinator.create(
        actor=actor,
        payload=_working_payload(occurrence_count=2),
        canonical_timestamp=TIMESTAMP,
    ).intent
    assert coordinator.preflight(created.intent_id, actor=actor).verdict == "refused"
    _advance_accepted_head(instance, owner)

    refused = coordinator.preflight(created.intent_id, actor=actor)

    assert refused.verdict == "refused"
    assert "cruxible.authoring.working_selection_ambiguous" in {
        diagnostic.code for diagnostic in refused.frontier.diagnostics
    }
    after = coordinator.get(created.intent_id, actor=actor).intent
    assert after.base_coordinate == created.base_coordinate
    assert after.intent_revision == created.intent_revision
