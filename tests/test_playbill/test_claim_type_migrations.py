"""PC-G3b composed ClaimType migration laws."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.authoring.inputs import (
    ClaimInput,
    LiteralObjectInput,
    SelfSourceInput,
)
from cruxible_client.contracts.artifacts import ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.claim_types import (
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
)
from cruxible_client.contracts.claims import claim_path, parse_claim
from cruxible_client.contracts.query.definitions import (
    parse_query_definition,
    query_definition_path,
    render_query_definition,
)
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.claim_type_inputs import ClaimTypeInputV1
from cruxible_core.playbill.claim_type_migrations import (
    ClaimTypeDependentDispositionV1,
    ClaimTypeDependentDispositionV2,
    ClaimTypeMigrationDependentSetMismatch,
    ClaimTypeMigrationError,
    ClaimTypeMigrationIncomplete,
    ClaimTypeMigrationRequestV1,
    ClaimTypeMigrationRequestV2,
    ClaimTypeMigrationResultV2,
    service_migrate_claim_type,
)
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from tests.test_playbill._adoption_fixture import _query_definition
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_authoring_preflight import TIMESTAMP, _seed_claim_surface
from tests.test_playbill.test_claims import _claim_type
from tests.test_playbill.test_resolution_contracts import _accept_tree


def _accepted_claim_world(tmp_path: Path):  # type: ignore[no-untyped-def]
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    intent = coordinator.create_input(
        actor=actor,
        input=ClaimInput(
            kind="claim",
            subject="project.work_item/wi-42",
            predicate=_claim_type().predicate,
            object=LiteralObjectInput(kind="literal", value="ready"),
            role="observation",
            rationale="The work item is ready.",
            source=SelfSourceInput(kind="self_source", body="status: ready\n"),
        ),
        canonical_timestamp=TIMESTAMP,
    ).intent
    submitted = coordinator.submit(intent.intent_id, actor=actor)
    assert submitted.status.proposal_id is not None
    assert submitted.status.candidate_digest is not None
    approval = _sign(
        owner,
        submitted.status.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=submitted.status.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="owner",
    )
    assert (
        service_activate_playbill_proposal(
            instance,
            proposal_id=submitted.status.proposal_id,
        ).status
        == "accepted"
    )
    return instance, intent.semantic_identity


def _successor(instance):  # type: ignore[no-untyped-def]
    path = claim_type_path(_claim_type().predicate)
    current = parse_claim_type(
        instance.tree_at(instance.accepted_coordinate().git_oid)[path],
        path=path,
    )
    return current.model_copy(
        update={
            "literal_schema": {"type": "string", "enum": ["blocked", "ready"]},
            "lifecycle": ArtifactLifecycle(predecessor_digest=claim_type_digest(current).tagged),
        }
    )


def test_migration_updates_type_and_dependent_in_one_idempotent_candidate(
    tmp_path: Path,
) -> None:
    instance, claim_id = _accepted_claim_world(tmp_path)
    request = ClaimTypeMigrationRequestV1(
        successor=_successor(instance),
        dependents=(
            ClaimTypeDependentDispositionV1(
                claim_id=claim_id,
                disposition="successor",
            ),
        ),
    )
    actor = AuthenticatedActor(actor_id="owner")

    first = service_migrate_claim_type(instance, request=request, actor=actor)
    second = service_migrate_claim_type(instance, request=request, actor=actor)

    assert first.operation_digest == second.operation_digest
    assert first.proposal.proposal.admission.proposal_id == (
        second.proposal.proposal.admission.proposal_id
    )
    assert first.proposal.proposal.candidate is not None
    tree_oid = first.proposal.proposal.evaluation.evaluated_tree_oid
    assert tree_oid is not None
    candidate_tree = instance.proposal_tree(tree_oid)
    claim = parse_claim(candidate_tree[claim_path(claim_id)], path=claim_path(claim_id))
    assert claim.statement.object.value == "ready"  # type: ignore[union-attr]
    assert claim.statement.claim_type_digest == claim_type_digest(_successor(instance)).tagged
    assert claim.lifecycle.predecessor_digest is not None


def test_migration_refuses_an_omitted_current_dependent(tmp_path: Path) -> None:
    instance, claim_id = _accepted_claim_world(tmp_path)

    with pytest.raises(ClaimTypeMigrationError, match=claim_id):
        service_migrate_claim_type(
            instance,
            request=ClaimTypeMigrationRequestV1(
                successor=_successor(instance),
                dependents=(),
            ),
            actor=AuthenticatedActor(actor_id="owner"),
        )


def test_invalidation_normalizes_to_a_retired_candidate_member(tmp_path: Path) -> None:
    instance, claim_id = _accepted_claim_world(tmp_path)
    result = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequestV1(
            successor=_successor(instance),
            dependents=(
                ClaimTypeDependentDispositionV1(
                    claim_id=claim_id,
                    disposition="invalidation",
                ),
            ),
        ),
        actor=AuthenticatedActor(actor_id="owner"),
    )

    assert result.dependents[0].disposition == "retire"
    tree_oid = result.proposal.proposal.evaluation.evaluated_tree_oid
    assert tree_oid is not None
    claim = parse_claim(
        instance.proposal_tree(tree_oid)[claim_path(claim_id)],
        path=claim_path(claim_id),
    )
    assert claim.lifecycle.state == "retired"


def test_v1_refuses_when_complete_closure_contains_a_query_definition(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    current_tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    current_type = parse_claim_type(
        current_tree[claim_type_path(_claim_type().predicate)],
        path=claim_type_path(_claim_type().predicate),
    )
    query = _query_definition(1, current_type)
    current_tree[query_definition_path(query.identity.name)] = render_query_definition(query)
    _accept_tree(instance, owner, current_tree, timestamp=TIMESTAMP, proposal_name="seed-query")

    with pytest.raises(ClaimTypeMigrationDependentSetMismatch, match="query-definition"):
        service_migrate_claim_type(
            instance,
            request=ClaimTypeMigrationRequestV1(successor=_successor(instance), dependents=()),
            actor=AuthenticatedActor(actor_id="owner"),
        )


def test_v2_preflight_and_submit_cover_claim_and_query_dependents(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    current_tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    current_type = parse_claim_type(
        current_tree[claim_type_path(_claim_type().predicate)],
        path=claim_type_path(_claim_type().predicate),
    )
    query = _query_definition(1, current_type)
    current_tree[query_definition_path(query.identity.name)] = render_query_definition(query)
    _accept_tree(instance, owner, current_tree, timestamp=TIMESTAMP, proposal_name="seed-query")

    preflight = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequestV2(
            mode="preflight",
            successor=_successor(instance),
        ),
        actor=AuthenticatedActor(actor_id="owner"),
    )
    assert preflight.tag == "playbill-claim-type-migration-preflight-v1"
    assert [(item.artifact_kind, item.identity) for item in preflight.dependents] == [
        ("query-definition", query.identity),
    ]

    result = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequestV2(
            mode="submit",
            successor=_successor(instance),
            dependents=(
                ClaimTypeDependentDispositionV2(
                    identity=query.identity,
                    disposition="successor",
                ),
            ),
        ),
        actor=AuthenticatedActor(actor_id="owner"),
    )
    assert isinstance(result, ClaimTypeMigrationResultV2)
    tree_oid = result.proposal.proposal.evaluation.evaluated_tree_oid
    assert tree_oid is not None
    migrated_query = parse_query_definition(
        instance.proposal_tree(tree_oid)[query_definition_path(query.identity.name)],
        path=query_definition_path(query.identity.name),
    )
    assert migrated_query.pins[0].artifact_digest == claim_type_digest(_successor(instance)).tagged
    assert migrated_query.lifecycle.predecessor_digest is not None


def _decision_only_successor(instance, *, enum: list[str]):  # type: ignore[no-untyped-def]
    path = claim_type_path(_claim_type().predicate)
    current = parse_claim_type(
        instance.tree_at(instance.accepted_coordinate().git_oid)[path],
        path=path,
    )
    values = current.model_dump(mode="json")
    for mechanical in ("artifact_format", "identity", "lifecycle"):
        values.pop(mechanical)
    values["literal_schema"] = {"type": "string", "enum": enum}
    return ClaimTypeInputV1.model_validate(values)


def test_migration_refuses_hand_authored_predecessor_artifact_pins(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    path = claim_type_path(_claim_type().predicate)
    predecessor = parse_claim_type(
        instance.tree_at(instance.accepted_coordinate().git_oid)[path],
        path=path,
    )
    successor = _decision_only_successor(instance, enum=["blocked", "ready"]).model_copy(
        update={
            "pins": (
                {
                    "role": "predecessor",
                    "target": predecessor.identity.model_dump(mode="json"),
                    "artifact_digest": claim_type_digest(predecessor).tagged,
                },
            )
        }
    )

    with pytest.raises(ClaimTypeMigrationError, match="predecessor.*machine-owned"):
        service_migrate_claim_type(
            instance,
            request=ClaimTypeMigrationRequestV2(mode="preflight", successor=successor),
            actor=AuthenticatedActor(actor_id="owner"),
        )


@pytest.mark.parametrize("mode", ["preflight", "submit"])
def test_preflight_and_submit_refuse_the_same_unresolved_successor_pin(
    tmp_path: Path,
    mode: str,
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    predecessor = _successor(instance)
    successor = predecessor.model_copy(
        update={
            "pins": (
                ArtifactPin(
                    role="other-dependency",
                    target=predecessor.identity,
                    artifact_digest=predecessor.lifecycle.predecessor_digest,
                ),
            )
        }
    )

    with pytest.raises(ClaimTypeMigrationIncomplete, match="unresolved_pin"):
        service_migrate_claim_type(
            instance,
            request=ClaimTypeMigrationRequestV2(mode=mode, successor=successor),  # type: ignore[arg-type]
            actor=AuthenticatedActor(actor_id="owner"),
        )


def test_decision_only_claim_type_completes_two_successions_without_predecessor_pins(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    actor = AuthenticatedActor(actor_id="owner")
    path = claim_type_path(_claim_type().predicate)

    for enum in (["blocked", "ready"], ["blocked", "closed", "ready"]):
        predecessor = parse_claim_type(
            instance.tree_at(instance.accepted_coordinate().git_oid)[path],
            path=path,
        )
        successor = _decision_only_successor(instance, enum=enum)
        assert successor.pins == ()
        preflight = service_migrate_claim_type(
            instance,
            request=ClaimTypeMigrationRequestV2(mode="preflight", successor=successor),
            actor=actor,
        )
        result = service_migrate_claim_type(
            instance,
            request=ClaimTypeMigrationRequestV2(mode="submit", successor=successor),
            actor=actor,
        )
        assert isinstance(result, ClaimTypeMigrationResultV2)
        candidate = result.proposal.proposal.candidate
        assert candidate is not None
        approval = _sign(
            owner, candidate.candidate_digest, instance.accepted_coordinate().semantic_root
        )
        service_submit_playbill_approval(
            instance,
            proposal_id=result.proposal.proposal.admission.proposal_id,
            attestation=approval.attestation,
            authenticated_submitter="owner",
        )
        assert (
            service_activate_playbill_proposal(
                instance,
                proposal_id=result.proposal.proposal.admission.proposal_id,
            ).status
            == "accepted"
        )
        accepted = parse_claim_type(
            instance.tree_at(instance.accepted_coordinate().git_oid)[path],
            path=path,
        )
        assert accepted.lifecycle.predecessor_digest == claim_type_digest(predecessor).tagged
        assert preflight.successor_artifact_digest == claim_type_digest(accepted).tagged  # type: ignore[union-attr]


@pytest.mark.parametrize("mode", ["preflight", "submit"])
def test_migration_surfaces_nonblocking_policy_and_source_lint(
    tmp_path: Path,
    mode: str,
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    successor = _decision_only_successor(instance, enum=["blocked", "ready"]).model_copy(
        update={
            "evidence_admission_policy": {"rules": []},
            "anticipated_source_ids": ("corpus.runbook",),
        }
    )

    result = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequestV2(mode=mode, successor=successor),  # type: ignore[arg-type]
        actor=AuthenticatedActor(actor_id="owner"),
    )

    assert result.lint is not None
    assert {warning.code for warning in result.lint.warnings} == {
        "playbill.claim_type.evidence_policy_admits_no_accepted_contract",
        "playbill.claim_type.anticipated_source_contract_omitted",
    }
