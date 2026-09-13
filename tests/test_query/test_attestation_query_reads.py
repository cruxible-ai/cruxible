"""Queries read evidence only for the predicates their declaration can access."""

from pathlib import Path

from cruxible_client.contracts.accepted_attestations import (
    attestation_path,
    render_accepted_attestation,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claim_attestations import claim_attestation_v2_envelope_digest
from cruxible_client.contracts.claim_types import parse_claim_type
from cruxible_client.contracts.query.definitions import (
    query_definition_path,
    render_query_definition,
)
from cruxible_core.indexes.typed_state import TypedStateReader
from cruxible_core.query.engine import evaluate_claim_query
from cruxible_core.service.discovery.query import (
    build_accepted_query_facts,
    service_run_playbill_query,
)
from cruxible_core.service.discovery.query_definitions import accepted_query_definition
from tests.core_support._knowledge_loop_support import work_item_query
from tests.test_claims.test_claim_attestation_service import RECORDED_AT, _request
from tests.test_claims.test_claim_type_migrations import _accepted_claim_world
from tests.test_indexes.test_resolution_contracts import _accept_tree


def test_query_predicate_selection_preserves_results_and_skips_unrelated_attestations(
    tmp_path: Path, monkeypatch
) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    claim_type_path = next(p for p in tree if p.startswith("claim-types/"))
    claim_type = parse_claim_type(tree[claim_type_path], path=claim_type_path)
    query = work_item_query(claim_type=claim_type)
    subject_query = query.model_copy(
        update={
            "identity": ArtifactIdentity(kind="QueryDefinition", name="project.subjects"),
            "projection": query.projection.model_copy(
                update={"fields": query.projection.fields[:1]}
            ),
            "pins": (),
        }
    )
    envelope = _request(instance, owner, claim_id, tmp_path, stance="support").attestation
    path = attestation_path(claim_attestation_v2_envelope_digest(envelope))
    tree[path] = render_accepted_attestation(envelope)
    for definition in (query, subject_query):
        tree[query_definition_path(definition.identity.name)] = render_query_definition(definition)
    _accept_tree(
        instance, owner, tree, timestamp="2026-08-28T15:01:00.000000Z", proposal_name="queries"
    )
    coordinate = instance.accepted_coordinate()
    facts = build_accepted_query_facts(instance, coordinate=coordinate)
    assert any(row.attestations for row in facts.claims)
    for definition in (query, subject_query):
        accepted = accepted_query_definition(
            instance, name=definition.identity.name, coordinate=coordinate
        )
        expected = evaluate_claim_query(
            accepted, facts=facts, coordinate=coordinate, evaluation_time=RECORDED_AT
        )
        actual = service_run_playbill_query(
            instance, name=definition.identity.name, evaluation_time=RECORDED_AT
        )
        assert actual.result == expected

    read_member = TypedStateReader.member_bytes

    def refuse_unrelated(self, member_path):
        if member_path.startswith(("attestations/", "claims/")):
            raise AssertionError("subject-only query opened unrelated Claim evidence")
        return read_member(self, member_path)

    with monkeypatch.context() as patch:
        patch.setattr(TypedStateReader, "member_bytes", refuse_unrelated)
        result = service_run_playbill_query(
            instance, name=subject_query.identity.name, evaluation_time=RECORDED_AT
        )
        assert result.result.rows
        # Nonempty but unmatched predicates must also avoid signed-body reads.
        assert (
            build_accepted_query_facts(
                instance, coordinate=coordinate, predicates=("other.predicate",)
            ).claims
            == ()
        )
