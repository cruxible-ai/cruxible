"""PC-G12c attributed Claim-retirement operation laws."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.claim_types import (
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
)
from cruxible_client.contracts.claims import (
    ClaimArtifactV3,
    ClaimRetireDependentV1,
    ClaimRetireRequestV1,
    LiteralClaimObject,
    claim_artifact_digest,
    claim_path,
    parse_claim,
    render_claim,
)
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.query.definitions import (
    query_definition_path,
    render_query_definition,
)
from cruxible_core.playbill.claim_retirement import (
    ClaimRetireClosureMismatch,
    ClaimRetireDependentUnsupported,
    ClaimRetireResultV1,
    ClaimRetireStale,
    service_retire_claim,
)
from cruxible_core.playbill.claim_type_inputs import ClaimTypeInputV1
from cruxible_core.playbill.claim_type_migrations import (
    ClaimTypeDependentDispositionV3,
    ClaimTypeMigrationRequestV3,
    ClaimTypeMigrationResultV3,
    service_migrate_claim_type,
)
from cruxible_core.playbill.proposals import AuthenticatedActor, evaluate_proposal_tree
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.service.playbill_claims import (
    DirectClaimAuthoringV1,
    service_propose_playbill_claim,
    service_propose_playbill_claims,
)
from tests.test_playbill._adoption_fixture import _query_definition
from tests.test_playbill._support import client_material, initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_claim_type_migrations import _accepted_claim_world
from tests.test_playbill.test_claims import _claim_type
from tests.test_playbill.test_multi_claim_authoring import (
    STATUS_CLAIM_ID,
    SUMMARY_CLAIM_ID,
    TIMESTAMP,
    _status_authoring,
    _summary_authoring,
)
from tests.test_playbill.test_multi_claim_authoring import (
    _activate as _activate_batch,
)
from tests.test_playbill.test_resolution_contracts import _accept_tree


def _request(
    instance,  # type: ignore[no-untyped-def]
    *,
    mode: str,
    reason: str = "was-rescinded",
    effective_until: datetime | None = None,
    dependents: tuple[ClaimRetireDependentV1, ...] = (),
) -> ClaimRetireRequestV1:
    return ClaimRetireRequestV1(
        mode=mode,  # type: ignore[arg-type]
        reason=reason,  # type: ignore[arg-type]
        effective_until=effective_until,
        expected_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        dependents=dependents,
    )


def _activate(instance, owner, result: ClaimRetireResultV1) -> None:  # type: ignore[no-untyped-def]
    assert result.proposal is not None
    proposal = result.proposal.proposal
    candidate = proposal.candidate
    assert candidate is not None
    if candidate.approval_requirements:
        approval = _sign(
            client_material(instance.root.parent, instance),
            candidate.candidate_digest,
            instance.accepted_coordinate().semantic_root,
        )
        service_submit_playbill_approval(
            instance,
            proposal_id=proposal.admission.proposal_id,
            attestation=approval.attestation,
            authenticated_submitter=owner.principal.principal_id,
        )
    assert (
        service_activate_playbill_proposal(
            instance,
            proposal_id=proposal.admission.proposal_id,
        ).status
        == "accepted"
    )


def _derivation_capable(authoring, *, value: str | None = None):  # type: ignore[no-untyped-def]
    claim_type = authoring.claim_type_artifact
    assert claim_type is not None
    evidence_policy = claim_type.evidence_admission_policy.model_copy(
        update={
            "rules": tuple(
                rule.model_copy(
                    update={"claim_roles": tuple(sorted((*rule.claim_roles, "derivation")))}
                )
                for rule in claim_type.evidence_admission_policy.rules
            )
        }
    )
    claim_type = claim_type.model_copy(
        update={
            "permitted_roles": tuple(sorted((*claim_type.permitted_roles, "derivation"))),
            "evidence_admission_policy": evidence_policy,
        }
    )
    statement_updates: dict[str, object] = {
        "claim_type_digest": claim_type_digest(claim_type).tagged
    }
    if value is not None:
        statement_updates["object"] = LiteralClaimObject(value=value)
    return authoring.model_copy(
        update={
            "claim_type_artifact": claim_type,
            "statement": authoring.statement.model_copy(update=statement_updates),
        }
    )


def _accepted_dependency_world(tmp_path: Path):  # type: ignore[no-untyped-def]
    """Accept root -> middle -> leaf using historical inputs and a Claim pin."""

    instance, owner = initialize_local(tmp_path)
    leaf_id = "CLM-" + "3" * 32
    seeded = service_propose_playbill_claims(
        instance,
        authorings=(
            _status_authoring(),
            _derivation_capable(_summary_authoring()),
            _derivation_capable(
                _summary_authoring(claim_id=leaf_id),
                value="A second derived summary",
            ),
        ),
        actor_id="owner",
        proposal_name="retirement-chain-seed",
        timestamp=TIMESTAMP,
    )
    _activate_batch(instance, owner, seeded)

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    root = parse_claim(tree[claim_path(STATUS_CLAIM_ID)], path=claim_path(STATUS_CLAIM_ID))
    middle_path = claim_path(SUMMARY_CLAIM_ID)
    middle = parse_claim(tree[middle_path], path=middle_path)
    middle = middle.model_copy(
        update={
            "statement": middle.statement.model_copy(update={"role": "derivation"}),
            "backing": middle.backing.model_copy(
                update={
                    "input_claim_digests": (claim_artifact_digest(root).tagged,),
                    "reducer_digest": "sha256:" + "8" * 64,
                }
            ),
            "lifecycle": middle.lifecycle.model_copy(
                update={"predecessor_digest": claim_artifact_digest(middle).tagged}
            ),
        }
    )
    tree[middle_path] = render_claim(middle)
    _accept_tree(
        instance,
        owner,
        tree,
        timestamp=TIMESTAMP,
        proposal_name="retirement-chain-middle",
    )

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    middle = parse_claim(tree[middle_path], path=middle_path)
    leaf_path = claim_path(leaf_id)
    leaf = parse_claim(tree[leaf_path], path=leaf_path)
    leaf = leaf.model_copy(
        update={
            "statement": leaf.statement.model_copy(update={"role": "derivation"}),
            "backing": leaf.backing.model_copy(
                update={
                    "input_claim_digests": (claim_artifact_digest(middle).tagged,),
                    "reducer_digest": "sha256:" + "9" * 64,
                }
            ),
            "pins": tuple(
                sorted(
                    (
                        *leaf.pins,
                        ArtifactPin(
                            role="input-claim",
                            target=middle.identity,
                            artifact_digest=claim_artifact_digest(middle).tagged,
                        ),
                    ),
                    key=lambda pin: (
                        pin.role.encode("utf-8"),
                        pin.target.qualified.encode("utf-8"),
                    ),
                )
            ),
            "lifecycle": leaf.lifecycle.model_copy(
                update={"predecessor_digest": claim_artifact_digest(leaf).tagged}
            ),
        }
    )
    tree[leaf_path] = render_claim(leaf)
    _accept_tree(
        instance,
        owner,
        tree,
        timestamp=TIMESTAMP,
        proposal_name="retirement-chain-leaf",
    )

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    root = parse_claim(tree[claim_path(STATUS_CLAIM_ID)], path=claim_path(STATUS_CLAIM_ID))
    root_revision = service_propose_playbill_claim(
        instance,
        authoring=DirectClaimAuthoringV1(
            statement=root.statement.model_copy(
                update={"object": LiteralClaimObject(value="blocked")}
            ),
            rationale="Advance the source lineage without rewriting historical derivation pins.",
            claim_id=STATUS_CLAIM_ID,
            predecessor_artifact_digest=claim_artifact_digest(root).tagged,
        ),
        actor_id="owner",
        proposal_name="retirement-chain-root-revision",
        timestamp=TIMESTAMP,
    )
    _activate_batch(
        instance,
        owner,
        root_revision,
        sequence=len(instance.accepted_history()),
    )
    return instance, owner, STATUS_CLAIM_ID, SUMMARY_CLAIM_ID, leaf_id


@pytest.mark.parametrize("reason", ["was-rescinded", "was-wrong"])
def test_root_only_retirement_is_idempotent_and_post_activation_terminal(
    tmp_path: Path,
    reason: str,
) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    actor = AuthenticatedActor(actor_id="owner")
    preflight_request = _request(instance, mode="preflight", reason=reason)

    preflight = service_retire_claim(
        instance,
        claim_id=claim_id,
        request=preflight_request,
        actor=actor,
    )
    assert preflight.tag == "playbill-claim-retire-preflight-v1"
    assert preflight.required_dependents == ()
    assert preflight.submit_ready is True

    submit_request = preflight_request.model_copy(update={"mode": "submit"})
    first = service_retire_claim(
        instance,
        claim_id=claim_id,
        request=submit_request,
        actor=actor,
    )
    second = service_retire_claim(
        instance,
        claim_id=claim_id,
        request=submit_request,
        actor=actor,
    )
    assert isinstance(first, ClaimRetireResultV1)
    assert isinstance(second, ClaimRetireResultV1)
    assert first.operation_digest == preflight.operation_digest == second.operation_digest
    assert first.proposal is not None and second.proposal is not None
    assert (
        first.proposal.proposal.admission.proposal_id
        == second.proposal.proposal.admission.proposal_id
    )

    tree_oid = first.proposal.proposal.evaluation.evaluated_tree_oid
    assert tree_oid is not None
    retired = parse_claim(
        instance.proposal_tree(tree_oid)[claim_path(claim_id)],
        path=claim_path(claim_id),
    )
    assert isinstance(retired, ClaimArtifactV3)
    assert retired.retirement.reason == reason
    _activate(instance, owner, first)

    terminal = service_retire_claim(
        instance,
        claim_id=claim_id,
        request=submit_request,
        actor=actor,
    )
    assert isinstance(terminal, ClaimRetireResultV1)
    assert terminal.outcome == "already_retired"
    assert terminal.proposal is None
    assert terminal.retirements[0].successor_digest == claim_artifact_digest(retired).tagged


def test_effective_until_is_caller_supplied_or_preserved_without_clock_substitution(
    tmp_path: Path,
) -> None:
    until = datetime(2026, 9, 1, 12, tzinfo=UTC)
    instance, claim_id, _owner = _accepted_claim_world(tmp_path)
    actor = AuthenticatedActor(actor_id="owner")
    original = parse_claim(
        instance.tree_at(instance.accepted_coordinate().git_oid)[claim_path(claim_id)],
        path=claim_path(claim_id),
    )
    with_until = service_retire_claim(
        instance,
        claim_id=claim_id,
        request=_request(instance, mode="submit", effective_until=until),
        actor=actor,
    )
    assert isinstance(with_until, ClaimRetireResultV1)
    assert with_until.proposal is not None
    tree_oid = with_until.proposal.proposal.evaluation.evaluated_tree_oid
    assert tree_oid is not None
    retired = parse_claim(
        instance.proposal_tree(tree_oid)[claim_path(claim_id)],
        path=claim_path(claim_id),
    )
    assert retired.statement.model_copy(
        update={"effective_until": original.statement.effective_until}
    ) == (original.statement)
    assert retired.statement.effective_until == until

    preserve_root = tmp_path / "preserve"
    preserve_root.mkdir()
    second, second_id, _second_owner = _accepted_claim_world(preserve_root)
    preserved = service_retire_claim(
        second,
        claim_id=second_id,
        request=_request(second, mode="submit"),
        actor=actor,
    )
    assert isinstance(preserved, ClaimRetireResultV1)
    assert preserved.retirements[0].effective_until is None


def test_transitive_dual_edge_closure_freezes_inputs_and_advances_only_claim_pin(
    tmp_path: Path,
) -> None:
    instance, owner, root_id, middle_id, leaf_id = _accepted_dependency_world(tmp_path)
    actor = AuthenticatedActor(actor_id="owner")
    accepted_tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    root = parse_claim(accepted_tree[claim_path(root_id)], path=claim_path(root_id))
    middle = parse_claim(accepted_tree[claim_path(middle_id)], path=claim_path(middle_id))
    leaf = parse_claim(accepted_tree[claim_path(leaf_id)], path=claim_path(leaf_id))
    assert middle.backing.input_claim_digests != (claim_artifact_digest(root).tagged,)

    preflight = service_retire_claim(
        instance,
        claim_id=root_id,
        request=_request(instance, mode="preflight"),
        actor=actor,
    )
    assert preflight.tag == "playbill-claim-retire-preflight-v1"
    assert [item.artifact_identity for item in preflight.required_dependents] == [
        middle.identity,
        leaf.identity,
    ]
    assert preflight.required_dependents[0].triggering_identity == root.identity
    assert preflight.required_dependents[0].triggering_edge_roles == ("backing-input",)
    assert preflight.required_dependents[1].triggering_identity == middle.identity
    assert preflight.required_dependents[1].triggering_edge_roles == (
        "backing-input",
        "input-claim",
    )

    dependents = tuple(
        ClaimRetireDependentV1(
            artifact_identity=item.artifact_identity,
            predecessor_digest=item.predecessor_digest,
            reason="was-wrong" if item.artifact_identity == middle.identity else "was-rescinded",
        )
        for item in preflight.required_dependents
    )
    submit_request = _request(instance, mode="submit", dependents=dependents)
    result = service_retire_claim(
        instance,
        claim_id=root_id,
        request=submit_request,
        actor=actor,
    )
    assert isinstance(result, ClaimRetireResultV1)
    assert result.proposal is not None
    tree_oid = result.proposal.proposal.evaluation.evaluated_tree_oid
    assert tree_oid is not None
    candidate_tree = instance.proposal_tree(tree_oid)
    retired_root = parse_claim(candidate_tree[claim_path(root_id)], path=claim_path(root_id))
    retired_middle = parse_claim(candidate_tree[claim_path(middle_id)], path=claim_path(middle_id))
    retired_leaf = parse_claim(candidate_tree[claim_path(leaf_id)], path=claim_path(leaf_id))
    assert isinstance(retired_root, ClaimArtifactV3)
    assert isinstance(retired_middle, ClaimArtifactV3)
    assert isinstance(retired_leaf, ClaimArtifactV3)
    assert retired_middle.backing.input_claim_digests == middle.backing.input_claim_digests
    assert retired_leaf.backing.input_claim_digests == leaf.backing.input_claim_digests
    before_pin = next(pin for pin in leaf.pins if pin.target == middle.identity)
    after_pin = next(pin for pin in retired_leaf.pins if pin.target == middle.identity)
    assert before_pin.role == after_pin.role == "input-claim"
    assert after_pin.artifact_digest == claim_artifact_digest(retired_middle).tagged
    assert after_pin.artifact_digest != before_pin.artifact_digest
    assert [item.artifact_identity for item in result.retirements] == [
        root.identity,
        middle.identity,
        leaf.identity,
    ]

    with pytest.raises(ClaimRetireClosureMismatch, match="expected"):
        service_retire_claim(
            instance,
            claim_id=root_id,
            request=_request(instance, mode="submit", dependents=dependents[:-1]),
            actor=actor,
        )

    _activate(instance, owner, result)
    terminal = service_retire_claim(
        instance,
        claim_id=root_id,
        request=submit_request,
        actor=actor,
    )
    assert isinstance(terminal, ClaimRetireResultV1)
    assert terminal.outcome == "already_retired"
    assert terminal.operation_digest == result.operation_digest
    assert terminal.retirements == result.retirements
    reattributed = submit_request.model_copy(
        update={
            "dependents": (
                dependents[0].model_copy(update={"reason": "was-rescinded"}),
                dependents[1],
            )
        }
    )
    with pytest.raises(ClaimRetireClosureMismatch, match="attribution"):
        service_retire_claim(
            instance,
            claim_id=root_id,
            request=reattributed,
            actor=actor,
        )


@pytest.mark.parametrize("leaf_retirement", ["attributed-v3", "legacy-v2"])
def test_terminal_replay_uses_only_the_original_retirement_changeset(
    tmp_path: Path,
    leaf_retirement: str,
) -> None:
    instance, owner, _root_id, middle_id, leaf_id = _accepted_dependency_world(tmp_path)
    actor = AuthenticatedActor(actor_id="owner")
    if leaf_retirement == "attributed-v3":
        leaf_request = _request(instance, mode="submit")
        leaf_result = service_retire_claim(
            instance,
            claim_id=leaf_id,
            request=leaf_request,
            actor=actor,
        )
        assert isinstance(leaf_result, ClaimRetireResultV1)
        _activate(instance, owner, leaf_result)
    else:
        tree = instance.tree_at(instance.accepted_coordinate().git_oid)
        leaf = parse_claim(tree[claim_path(leaf_id)], path=claim_path(leaf_id))
        legacy_leaf = leaf.model_copy(
            update={
                "lifecycle": ArtifactLifecycle(
                    state="retired",
                    predecessor_digest=claim_artifact_digest(leaf).tagged,
                )
            }
        )
        tree[claim_path(leaf_id)] = render_claim(legacy_leaf)
        _accept_tree(
            instance,
            owner,
            tree,
            timestamp=TIMESTAMP,
            proposal_name="legacy-retire-leaf",
        )

    middle_request = _request(instance, mode="submit")
    middle_result = service_retire_claim(
        instance,
        claim_id=middle_id,
        request=middle_request,
        actor=actor,
    )
    assert isinstance(middle_result, ClaimRetireResultV1)
    assert [item.artifact_identity.name for item in middle_result.retirements] == [middle_id]
    _activate(instance, owner, middle_result)

    replayed = service_retire_claim(
        instance,
        claim_id=middle_id,
        request=middle_request,
        actor=actor,
    )
    assert isinstance(replayed, ClaimRetireResultV1)
    assert replayed.outcome == "already_retired"
    assert replayed.operation_digest == middle_result.operation_digest
    assert replayed.retirements == middle_result.retirements


def test_migration_can_succeed_one_claim_and_retire_its_dependent(tmp_path: Path) -> None:
    instance, _owner, _root_id, middle_id, leaf_id = _accepted_dependency_world(tmp_path)
    actor = AuthenticatedActor(actor_id="owner")
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    middle_before = parse_claim(tree[claim_path(middle_id)], path=claim_path(middle_id))
    type_path = claim_type_path(middle_before.statement.predicate)
    current_type = parse_claim_type(tree[type_path], path=type_path)
    successor_values = current_type.model_dump(mode="json")
    for mechanical in (
        "artifact_format",
        "identity",
        "lifecycle",
        "subject_scope",
        "slot_policy",
    ):
        successor_values.pop(mechanical, None)
    successor_values["literal_schema"] = {"type": "string", "minLength": 1}
    successor = ClaimTypeInputV1.model_validate(successor_values)
    dispositions = (
        ClaimTypeDependentDispositionV3(
            identity=middle_before.identity,
            disposition="successor",
        ),
        ClaimTypeDependentDispositionV3(
            identity=ArtifactIdentity(kind="Claim", name=leaf_id),
            disposition="retire",
            claim_retirement_reason="was-rescinded",
        ),
    )
    result = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequestV3(
            mode="submit",
            successor=successor,
            dependents=dispositions,
        ),
        actor=actor,
    )
    assert isinstance(result, ClaimTypeMigrationResultV3)
    tree_oid = result.proposal.proposal.evaluation.evaluated_tree_oid
    assert tree_oid is not None
    candidate_tree = instance.proposal_tree(tree_oid)
    middle = parse_claim(candidate_tree[claim_path(middle_id)], path=claim_path(middle_id))
    leaf = parse_claim(candidate_tree[claim_path(leaf_id)], path=claim_path(leaf_id))
    assert middle.lifecycle.state == "live"
    assert isinstance(leaf, ClaimArtifactV3)
    assert leaf.lifecycle.state == "retired"
    middle_pin = next(pin for pin in leaf.pins if pin.target == middle.identity)
    assert middle_pin.artifact_digest == claim_artifact_digest(middle).tagged

    bad_digest_leaf = leaf.model_copy(
        update={
            "pins": tuple(
                pin.model_copy(update={"artifact_digest": "sha256:" + "f" * 64})
                if pin.target == middle.identity
                else pin
                for pin in leaf.pins
            )
        }
    )
    invalid_digest_tree = dict(candidate_tree)
    invalid_digest_tree[claim_path(leaf_id)] = render_claim(bad_digest_leaf)
    missing_target_change_tree = dict(candidate_tree)
    missing_target_change_tree[claim_path(middle_id)] = tree[claim_path(middle_id)]
    for proposed_tree in (invalid_digest_tree, missing_target_change_tree):
        evaluation = evaluate_proposal_tree(
            base_tree=tree,
            current_tree=tree,
            proposed_tree=proposed_tree,
            current=instance.accepted_coordinate(),
            bodies=instance.body_store(),
            timestamp=TIMESTAMP,
            rebased=False,
            actor_id="owner",
            promotion_verifier=instance.proposal_service().promotion_verifier,
        )
        assert evaluation.candidate is None


def test_retire_refuses_stale_coordinate_and_extra_dependent(tmp_path: Path) -> None:
    instance, claim_id, _owner = _accepted_claim_world(tmp_path)
    actor = AuthenticatedActor(actor_id="owner")
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    stale = coordinate.model_copy(update={"git_oid": "0" * 40})
    with pytest.raises(ClaimRetireStale, match="accepted head"):
        service_retire_claim(
            instance,
            claim_id=claim_id,
            request=ClaimRetireRequestV1(
                mode="submit",
                reason="was-rescinded",
                expected_coordinate=stale,
            ),
            actor=actor,
        )

    with pytest.raises(ClaimRetireClosureMismatch, match="supplied"):
        service_retire_claim(
            instance,
            claim_id=claim_id,
            request=_request(
                instance,
                mode="submit",
                dependents=(
                    ClaimRetireDependentV1(
                        artifact_identity=ArtifactIdentity(
                            kind="Claim",
                            name="CLM-ffffffffffffffffffffffffffffffff",
                        ),
                        predecessor_digest="sha256:" + "f" * 64,
                        reason="was-wrong",
                    ),
                ),
            ),
            actor=actor,
        )


def test_terminal_replay_refuses_a_different_coordinate(tmp_path: Path) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    actor = AuthenticatedActor(actor_id="owner")
    request = _request(instance, mode="submit")
    result = service_retire_claim(instance, claim_id=claim_id, request=request, actor=actor)
    assert isinstance(result, ClaimRetireResultV1)
    _activate(instance, owner, result)

    changed_coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    with pytest.raises(ClaimRetireClosureMismatch, match="no accepted retirement follows"):
        service_retire_claim(
            instance,
            claim_id=claim_id,
            request=request.model_copy(update={"expected_coordinate": changed_coordinate}),
            actor=actor,
        )


def test_retire_refuses_a_live_non_claim_dependent(tmp_path: Path) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    claim = parse_claim(tree[claim_path(claim_id)], path=claim_path(claim_id))
    type_path = claim_type_path(_claim_type().predicate)
    claim_type = parse_claim_type(tree[type_path], path=type_path)
    query = _query_definition(1, claim_type)
    query = query.model_copy(
        update={
            "pins": tuple(
                sorted(
                    (
                        *query.pins,
                        ArtifactPin(
                            role="input-claim",
                            target=claim.identity,
                            artifact_digest=claim_artifact_digest(claim).tagged,
                        ),
                    ),
                    key=lambda pin: (
                        pin.role.encode("utf-8"),
                        pin.target.qualified.encode("utf-8"),
                    ),
                )
            )
        }
    )
    tree[query_definition_path(query.identity.name)] = render_query_definition(query)
    _accept_tree(
        instance,
        owner,
        tree,
        timestamp=TIMESTAMP,
        proposal_name="retirement-unsupported-query",
    )

    with pytest.raises(ClaimRetireDependentUnsupported, match=query.identity.qualified):
        service_retire_claim(
            instance,
            claim_id=claim_id,
            request=_request(instance, mode="preflight"),
            actor=AuthenticatedActor(actor_id="owner"),
        )


def test_direct_retire_warns_for_v1_and_refuses_v3_wire_downgrade(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    seeded = service_propose_playbill_claims(
        instance,
        authorings=(_status_authoring(),),
        actor_id="owner",
        proposal_name="legacy-direct-seed",
        timestamp=TIMESTAMP,
    )
    _activate_batch(instance, owner, seeded)
    claim_id = STATUS_CLAIM_ID
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    predecessor = parse_claim(tree[claim_path(claim_id)], path=claim_path(claim_id))
    direct = DirectClaimAuthoringV1(
        statement=predecessor.statement,
        rationale="The legacy caller is closing the Claim.",
        claim_id=claim_id,
        predecessor_artifact_digest=claim_artifact_digest(predecessor).tagged,
        retire=True,
    )
    proposed = service_propose_playbill_claim(
        instance,
        authoring=direct,
        actor_id="owner",
        proposal_name="legacy-direct-retire",
        timestamp=TIMESTAMP,
    )
    assert [warning.model_dump(mode="json") for warning in proposed.warnings] == [
        {
            "code": "playbill.claim.direct_retire_deprecated",
            "field_path": "$.retire",
            "repair_operation": "playbill.claim.retire",
        }
    ]

    v3_root = tmp_path / "v3"
    v3_root.mkdir()
    v3_instance, v3_claim_id, v3_owner = _accepted_claim_world(v3_root)
    result = service_retire_claim(
        v3_instance,
        claim_id=v3_claim_id,
        request=_request(v3_instance, mode="submit"),
        actor=AuthenticatedActor(actor_id="owner"),
    )
    assert isinstance(result, ClaimRetireResultV1)
    _activate(v3_instance, v3_owner, result)
    accepted_v3 = parse_claim(
        v3_instance.tree_at(v3_instance.accepted_coordinate().git_oid)[claim_path(v3_claim_id)],
        path=claim_path(v3_claim_id),
    )
    with pytest.raises(ProposalIntegrityError, match="direct_retire_wire_downgrade"):
        service_propose_playbill_claim(
            v3_instance,
            authoring=DirectClaimAuthoringV1(
                statement=accepted_v3.statement,
                rationale="A legacy caller must not downgrade v3.",
                claim_id=v3_claim_id,
                predecessor_artifact_digest=claim_artifact_digest(accepted_v3).tagged,
                retire=True,
            ),
            actor_id="owner",
            proposal_name="legacy-direct-v3-retire",
            timestamp=TIMESTAMP,
        )
