"""PC-G3b composed ClaimType migration laws."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.authoring.inputs import (
    ClaimInput,
    LiteralObjectInput,
    SelfSourceInput,
)
from cruxible_client.contracts.artifacts import (
    ArtifactAuthority,
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.claim_types import (
    ClaimAttestationConsequencePolicyV1,
    ClaimAttestationConsequenceRuleV1,
    ClaimEvidenceFreshnessV1,
    ClaimFreshnessDurationV1,
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
    render_claim_type,
)
from cruxible_client.contracts.claim_verdicts import (
    claim_adjudication_rule,
    claim_adjudication_rule_digest,
)
from cruxible_client.contracts.claims import (
    claim_artifact_digest,
    claim_path,
    parse_claim,
    render_claim,
)
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.query.definitions import (
    parse_query_definition,
    query_definition_path,
    render_query_definition,
)
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.claim_type_inputs import ClaimTypeInputV1, lower_claim_type_input
from cruxible_core.playbill.claim_type_migrations import (
    ClaimTypeDependentDispositionV1,
    ClaimTypeDependentDispositionV2,
    ClaimTypeMigrationDependentSetMismatch,
    ClaimTypeMigrationError,
    ClaimTypeMigrationIncomplete,
    ClaimTypeMigrationPreflightV1,
    ClaimTypeMigrationRequestV1,
    ClaimTypeMigrationRequestV2,
    ClaimTypeMigrationResultV2,
    service_migrate_claim_type,
)
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.service.playbill_claims import _claim_law_evidence
from cruxible_core.service.playbill_evidence import _queue_only_claim_type_successor
from cruxible_core.service.playbill_next import PlaybillNextRequestV1, service_playbill_next
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
    return instance, intent.semantic_identity, owner


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
    instance, claim_id, _owner = _accepted_claim_world(tmp_path)
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
    instance, claim_id, _owner = _accepted_claim_world(tmp_path)

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
    instance, claim_id, _owner = _accepted_claim_world(tmp_path)
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
    for mechanical in ("artifact_format", "identity", "lifecycle", "subject_scope", "slot_policy"):
        values.pop(mechanical, None)
    values["literal_schema"] = {"type": "string", "enum": enum}
    return ClaimTypeInputV1.model_validate(values)


def _activate_migration(instance, owner, successor, dependents):  # type: ignore[no-untyped-def]
    result = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequestV2(
            mode="submit",
            successor=successor,
            dependents=dependents,
        ),
        actor=AuthenticatedActor(actor_id="owner"),
    )
    assert isinstance(result, ClaimTypeMigrationResultV2)
    candidate = result.proposal.proposal.candidate
    assert candidate is not None
    approval = _sign(
        owner,
        candidate.candidate_digest,
        instance.accepted_coordinate().semantic_root,
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
    return result


def _accept_claim_type_only(
    instance,
    owner,
    successor: ClaimTypeInputV1,
    *,
    proposal_name: str,
):  # type: ignore[no-untyped-def]
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    lowered = lower_claim_type_input(successor, tree=tree)
    tree[claim_type_path(lowered.predicate)] = render_claim_type(lowered)
    _accept_tree(
        instance,
        owner,
        tree,
        timestamp=TIMESTAMP,
        proposal_name=proposal_name,
    )
    return lowered


def _next(instance):  # type: ignore[no-untyped-def]
    return service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(
            evaluation_time=datetime(2026, 8, 26, 12, tzinfo=UTC),
            access_profile=CoverageAccessProfileV1(
                profile_id="migration-test",
                permitted_access_classes=("instance", "public"),
            ),
        ),
    )


def _policy(threshold: int) -> ClaimAttestationConsequencePolicyV1:
    return ClaimAttestationConsequencePolicyV1(
        rules=(
            ClaimAttestationConsequenceRuleV1(
                rule_id="independent-unsure",
                stance="unsure",
                minimum_independent_control_components=threshold,
            ),
        )
    )


def _assert_current_adjudication_evidence(instance, claim_id: str) -> None:  # type: ignore[no-untyped-def]
    coordinate = instance.accepted_coordinate()
    tree = instance.tree_at(coordinate.git_oid)
    path = claim_type_path(_claim_type().predicate)
    claim_type = parse_claim_type(tree[path], path=path)
    evidence = _claim_law_evidence(instance, path=claim_path(claim_id), at=coordinate)
    rule = claim_adjudication_rule(
        claim_type,
        claim_type_digest=claim_type_digest(claim_type).tagged,
    )
    assert evidence.adjudication_rule_digest == claim_adjudication_rule_digest(rule)


def test_retired_query_definition_does_not_block_later_claim_type_migration(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    type_path = claim_type_path(_claim_type().predicate)
    query = _query_definition(1, parse_claim_type(tree[type_path], path=type_path))
    query_path = query_definition_path(query.identity.name)
    tree[query_path] = render_query_definition(query)
    _accept_tree(instance, owner, tree, timestamp=TIMESTAMP, proposal_name="seed-query")

    _activate_migration(
        instance,
        owner,
        _decision_only_successor(instance, enum=["blocked", "ready"]),
        (ClaimTypeDependentDispositionV2(identity=query.identity, disposition="retire"),),
    )
    retired_tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert parse_query_definition(retired_tree[query_path], path=query_path).lifecycle.state == (
        "retired"
    )

    successor = _decision_only_successor(instance, enum=["blocked", "ready"]).model_copy(
        update={"attestation_consequence_policy": _policy(2)}
    )
    preflight = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequestV2(mode="preflight", successor=successor),
        actor=AuthenticatedActor(actor_id="owner"),
    )
    assert isinstance(preflight, ClaimTypeMigrationPreflightV1)
    assert preflight.dependents == ()

    _activate_migration(instance, owner, successor, ())
    accepted_tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert parse_query_definition(accepted_tree[query_path], path=query_path) == (
        parse_query_definition(retired_tree[query_path], path=query_path)
    )
    _next(instance)


def test_queue_only_equivalence_requires_legal_authority_widening() -> None:
    predecessor = _claim_type()
    widened = predecessor.model_copy(
        update={
            "authority": ArtifactAuthority(
                propose_roles=predecessor.authority.propose_roles,
                approve_roles=tuple(sorted((*predecessor.authority.approve_roles, "reviewer"))),
            )
        }
    )
    changed_proposer = widened.model_copy(
        update={
            "authority": ArtifactAuthority(
                propose_roles=("reviewer",),
                approve_roles=widened.authority.approve_roles,
            )
        }
    )

    assert _queue_only_claim_type_successor(predecessor, widened)
    assert not _queue_only_claim_type_successor(predecessor, changed_proposer)
    assert not _queue_only_claim_type_successor(widened, predecessor)


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


def test_decision_only_successor_migrates_freshness_and_its_live_claim(
    tmp_path: Path,
) -> None:
    instance, claim_id, _owner = _accepted_claim_world(tmp_path)
    freshness = ClaimEvidenceFreshnessV1(
        stale_after=ClaimFreshnessDurationV1(microseconds=2_592_000_000_000)
    )
    successor = _decision_only_successor(instance, enum=["blocked", "ready"]).model_copy(
        update={"evidence_freshness": freshness}
    )
    dependent = ClaimTypeDependentDispositionV2(
        identity=ArtifactIdentity(kind="Claim", name=claim_id),
        disposition="successor",
    )
    actor = AuthenticatedActor(actor_id="owner")

    preflight = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequestV2(
            mode="preflight",
            successor=successor,
            dependents=(dependent,),
        ),
        actor=actor,
    )
    result = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequestV2(
            mode="submit",
            successor=successor,
            dependents=(dependent,),
        ),
        actor=actor,
    )

    assert isinstance(result, ClaimTypeMigrationResultV2)
    tree_oid = result.proposal.proposal.evaluation.evaluated_tree_oid
    assert tree_oid is not None
    path = claim_type_path(_claim_type().predicate)
    governed = parse_claim_type(instance.proposal_tree(tree_oid)[path], path=path)
    assert governed.artifact_format == "playbill-claim-type-v3"
    assert governed.evidence_freshness == freshness
    assert preflight.successor_artifact_digest == claim_type_digest(governed).tagged  # type: ignore[union-attr]


def test_claim_type_v3_to_v4_migration_preserves_freshness_and_accepts_policy(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    actor = AuthenticatedActor(actor_id="owner")
    path = claim_type_path(_claim_type().predicate)

    v3_input = _decision_only_successor(instance, enum=["blocked", "ready"]).model_copy(
        update={
            "evidence_freshness": ClaimEvidenceFreshnessV1(
                stale_after=ClaimFreshnessDurationV1(microseconds=2_592_000_000_000)
            )
        }
    )
    v3_result = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequestV2(mode="submit", successor=v3_input),
        actor=actor,
    )
    assert isinstance(v3_result, ClaimTypeMigrationResultV2)
    v3_candidate = v3_result.proposal.proposal.candidate
    assert v3_candidate is not None
    v3_approval = _sign(
        owner,
        v3_candidate.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=v3_result.proposal.proposal.admission.proposal_id,
        attestation=v3_approval.attestation,
        authenticated_submitter="owner",
    )
    assert (
        service_activate_playbill_proposal(
            instance,
            proposal_id=v3_result.proposal.proposal.admission.proposal_id,
        ).status
        == "accepted"
    )
    accepted_v3 = parse_claim_type(
        instance.tree_at(instance.accepted_coordinate().git_oid)[path], path=path
    )
    assert accepted_v3.artifact_format == "playbill-claim-type-v3"

    policy = ClaimAttestationConsequencePolicyV1(
        rules=(
            ClaimAttestationConsequenceRuleV1(
                rule_id="two-independent-unsure",
                stance="unsure",
                minimum_independent_control_components=2,
            ),
        )
    )
    v4_input = _decision_only_successor(instance, enum=["blocked", "ready"]).model_copy(
        update={"attestation_consequence_policy": policy}
    )
    v4_result = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequestV2(mode="submit", successor=v4_input),
        actor=actor,
    )
    assert isinstance(v4_result, ClaimTypeMigrationResultV2)
    v4_candidate = v4_result.proposal.proposal.candidate
    assert v4_candidate is not None
    v4_approval = _sign(
        owner,
        v4_candidate.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=v4_result.proposal.proposal.admission.proposal_id,
        attestation=v4_approval.attestation,
        authenticated_submitter="owner",
    )
    assert (
        service_activate_playbill_proposal(
            instance,
            proposal_id=v4_result.proposal.proposal.admission.proposal_id,
        ).status
        == "accepted"
    )
    accepted_v4 = parse_claim_type(
        instance.tree_at(instance.accepted_coordinate().git_oid)[path], path=path
    )
    assert accepted_v4.artifact_format == "playbill-claim-type-v4"
    assert accepted_v4.evidence_freshness == accepted_v3.evidence_freshness
    assert accepted_v4.attestation_consequence_policy == policy
    assert accepted_v4.lifecycle.predecessor_digest == claim_type_digest(accepted_v3).tagged


def test_retired_dependent_is_rederived_byte_exactly_and_next_remains_live(
    tmp_path: Path,
) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    identity = ArtifactIdentity(kind="Claim", name=claim_id)
    retire = ClaimTypeDependentDispositionV2(identity=identity, disposition="retire")
    freshness = ClaimEvidenceFreshnessV1(
        stale_after=ClaimFreshnessDurationV1(microseconds=2_592_000_000_000)
    )
    _activate_migration(
        instance,
        owner,
        _decision_only_successor(instance, enum=["blocked", "ready"]).model_copy(
            update={"evidence_freshness": freshness}
        ),
        (retire,),
    )
    before_tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    before = parse_claim(before_tree[claim_path(claim_id)], path=claim_path(claim_id))
    assert before.lifecycle.state == "retired"
    successor = _decision_only_successor(instance, enum=["blocked", "ready"]).model_copy(
        update={"attestation_consequence_policy": _policy(2)}
    )
    dependent = ClaimTypeDependentDispositionV2(identity=identity, disposition="successor")
    result = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequestV2(
            mode="submit",
            successor=successor,
            dependents=(dependent,),
        ),
        actor=AuthenticatedActor(actor_id="owner"),
    )
    assert isinstance(result, ClaimTypeMigrationResultV2)
    tree_oid = result.proposal.proposal.evaluation.evaluated_tree_oid
    assert tree_oid is not None
    candidate_tree = instance.proposal_tree(tree_oid)
    type_path = claim_type_path(_claim_type().predicate)
    successor_type = parse_claim_type(candidate_tree[type_path], path=type_path)
    successor_digest = claim_type_digest(successor_type).tagged
    expected_pins = tuple(
        pin.model_copy(update={"artifact_digest": successor_digest})
        if pin.role == "claim-type" and pin.target == successor_type.identity
        else pin
        for pin in before.pins
    )
    expected = before.model_copy(
        update={
            "statement": before.statement.model_copy(
                update={"claim_type_digest": successor_digest}
            ),
            "authority": successor_type.authority,
            "pins": expected_pins,
            "lifecycle": ArtifactLifecycle(
                state="retired",
                predecessor_digest=claim_artifact_digest(before).tagged,
            ),
        }
    )
    assert candidate_tree[claim_path(claim_id)] == render_claim(expected)
    candidate = result.proposal.proposal.candidate
    assert candidate is not None
    approval = _sign(
        owner,
        candidate.candidate_digest,
        instance.accepted_coordinate().semantic_root,
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
    _assert_current_adjudication_evidence(instance, claim_id)
    _next(instance)


def test_already_broken_retired_claim_walks_two_policy_only_predecessors(
    tmp_path: Path,
) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    dependent = ClaimTypeDependentDispositionV2(
        identity=ArtifactIdentity(kind="Claim", name=claim_id),
        disposition="retire",
    )
    _activate_migration(
        instance,
        owner,
        _decision_only_successor(instance, enum=["blocked", "ready"]),
        (dependent,),
    )
    for threshold in (2, 3):
        _accept_claim_type_only(
            instance,
            owner,
            _decision_only_successor(instance, enum=["blocked", "ready"]).model_copy(
                update={"attestation_consequence_policy": _policy(threshold)}
            ),
            proposal_name=f"policy-only-{threshold}",
        )
    _next(instance)


def test_retired_claim_rederives_on_freshness_migration(tmp_path: Path) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    identity = ArtifactIdentity(kind="Claim", name=claim_id)
    _activate_migration(
        instance,
        owner,
        _decision_only_successor(instance, enum=["blocked", "ready"]),
        (ClaimTypeDependentDispositionV2(identity=identity, disposition="retire"),),
    )
    freshness = ClaimEvidenceFreshnessV1(
        stale_after=ClaimFreshnessDurationV1(microseconds=2_592_000_000_000)
    )
    _activate_migration(
        instance,
        owner,
        _decision_only_successor(instance, enum=["blocked", "ready"]).model_copy(
            update={"evidence_freshness": freshness}
        ),
        (ClaimTypeDependentDispositionV2(identity=identity, disposition="successor"),),
    )
    _assert_current_adjudication_evidence(instance, claim_id)
    _next(instance)


def test_already_broken_retired_claim_accepts_policy_plus_approval_widen(
    tmp_path: Path,
) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    dependent = ClaimTypeDependentDispositionV2(
        identity=ArtifactIdentity(kind="Claim", name=claim_id),
        disposition="retire",
    )
    _activate_migration(
        instance,
        owner,
        _decision_only_successor(instance, enum=["blocked", "ready"]),
        (dependent,),
    )
    current = parse_claim_type(
        instance.tree_at(instance.accepted_coordinate().git_oid)[
            claim_type_path(_claim_type().predicate)
        ],
        path=claim_type_path(_claim_type().predicate),
    )
    _accept_claim_type_only(
        instance,
        owner,
        _decision_only_successor(instance, enum=["blocked", "ready"]).model_copy(
            update={
                "attestation_consequence_policy": _policy(2),
                "authority": ArtifactAuthority(
                    propose_roles=current.authority.propose_roles,
                    approve_roles=tuple(sorted((*current.authority.approve_roles, "reviewer"))),
                ),
            }
        ),
        proposal_name="policy-plus-approval-widen",
    )
    _next(instance)


def test_already_broken_retired_claim_refuses_schema_divergence(tmp_path: Path) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    dependent = ClaimTypeDependentDispositionV2(
        identity=ArtifactIdentity(kind="Claim", name=claim_id),
        disposition="retire",
    )
    _activate_migration(
        instance,
        owner,
        _decision_only_successor(instance, enum=["blocked", "ready"]),
        (dependent,),
    )
    _accept_claim_type_only(
        instance,
        owner,
        _decision_only_successor(instance, enum=["blocked", "closed", "ready"]),
        proposal_name="schema-divergence",
    )
    with pytest.raises(ProposalIntegrityError, match="adjudication rule does not reproduce"):
        _next(instance)


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
