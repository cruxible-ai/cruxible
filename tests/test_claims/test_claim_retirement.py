"""Attributed Claim-retirement laws, through the change-set retirement member.

A retirement is one ``ClaimRetirementMember`` naming the Claim it ``retires``
and its exact live dependent closure; the write verbs lower ``retire`` to it.
"""

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
    ClaimArtifact,
    ClaimRetireDependent,
    LiteralClaimObject,
    _is_attributed_retirement,
    claim_artifact_digest,
    claim_path,
    parse_claim,
    render_claim,
)
from cruxible_client.contracts.query.definitions import (
    query_definition_path,
    render_query_definition,
)
from cruxible_core.claims.claim_retirement import ClaimRetireDependentUnsupported
from cruxible_core.claims.claim_type_migrations import (
    ClaimTypeDependentDisposition,
    ClaimTypeMigrationDependentInvalid,
    ClaimTypeMigrationIncomplete,
    ClaimTypeMigrationRequest,
    service_migrate_claim_type,
)
from cruxible_core.proposals.proposals import AuthenticatedActor, evaluate_proposal_tree
from cruxible_core.proposals.settlement import ChangeActorBinding
from tests.core_support._adoption_fixture import _query_definition
from tests.core_support._claim_authoring_support import (
    STATUS_CLAIM_ID,
    SUMMARY_CLAIM_ID,
    TIMESTAMP,
    DirectClaimAuthoringV1,
    _activate_direct_claim,
    _status_authoring,
    _summary_authoring,
    service_propose_playbill_claim,
)
from tests.core_support._retirement_support import (
    activate_submitted,
    refusal_codes,
    refusal_messages,
    retirement_inventory,
    retirement_member,
    submit_retirement,
)
from tests.core_support._retirement_support import candidate_tree as _candidate_tree
from tests.core_support._support import client_material, initialize_local
from tests.test_claims.test_claim_type_migrations import (
    _accepted_claim_world,
    _decision_only_input,
    _decision_only_successor,
)
from tests.test_claims.test_claims import _claim_type
from tests.test_indexes.test_resolution_contracts import _accept_tree
from tests.test_ledger.test_activation import _sign


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


def _accept_historical_derivation_tree(instance, tree):  # type: ignore[no-untyped-def]
    """Seed old signed history under frozen laws, not today's live submission gate.

    These retirement fixtures deliberately contain legacy, asserted reducer
    digests. New submissions may no longer create those assertions. Retirement
    and replay must still work on records accepted before that gate existed.
    """

    base = instance.accepted_coordinate()
    current_tree = instance.tree_at(base.git_oid)
    outcome = evaluate_proposal_tree(
        base_tree=current_tree,
        current_tree=current_tree,
        proposed_tree=tree,
        current=base,
        bodies=instance.body_store(),
        timestamp=TIMESTAMP,
        rebased=False,
        actor_id="owner",
    )
    candidate = outcome.candidate
    assert candidate is not None, outcome.diagnostics
    bundle = instance.prepare_generation(
        base=base,
        candidate_tree=outcome.tree,
        candidate=candidate,
        approvals=(
            _sign(
                client_material(instance.root.parent, instance),
                candidate.candidate_digest,
                base.semantic_root,
            ),
        ),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        proposal_actor_id="owner",
        sequence=instance.accepted_history()[-1].sequence + 1,
    )
    publisher = instance.activation_publisher()
    projection = publisher.prebuild(bundle, base=base)
    assert publisher.activate(bundle, projection, base=base).status == "accepted"
    instance.refresh()


def _accepted_dependency_world(tmp_path: Path):  # type: ignore[no-untyped-def]
    """Accept root -> middle -> leaf using historical inputs and a Claim pin."""

    instance, owner = initialize_local(tmp_path)
    leaf_id = "CLM-" + "3" * 32
    for authoring, name in (
        (_status_authoring(), "retirement-chain-root"),
        (_derivation_capable(_summary_authoring()), "retirement-chain-middle-seed"),
        (
            _derivation_capable(
                _summary_authoring(claim_id=leaf_id),
                value="A second derived summary",
            ),
            "retirement-chain-leaf-seed",
        ),
    ):
        seeded = service_propose_playbill_claim(
            instance,
            authoring=authoring,
            actor_id="owner",
            proposal_name=name,
            timestamp=TIMESTAMP,
        )
        _activate_direct_claim(
            instance,
            owner,
            seeded,
        )

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
    _accept_historical_derivation_tree(instance, tree)

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
    _accept_historical_derivation_tree(instance, tree)

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
    _activate_direct_claim(
        instance,
        owner,
        root_revision,
    )
    return instance, owner, STATUS_CLAIM_ID, SUMMARY_CLAIM_ID, leaf_id


@pytest.mark.parametrize("reason", ["was-rescinded", "was-wrong", "superseded"])
def test_root_only_retirement_records_its_reason_and_is_terminal(
    tmp_path: Path,
    reason: str,
) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    assert retirement_inventory(instance, claim_id) == ()

    submitted = submit_retirement(instance, retirement_member(instance, claim_id, reason=reason))
    retired = parse_claim(
        _candidate_tree(instance, submitted)[claim_path(claim_id)], path=claim_path(claim_id)
    )
    assert isinstance(retired, ClaimArtifact)
    assert retired.retirement.reason == reason
    activate_submitted(instance, owner, submitted)

    again = submit_retirement(instance, retirement_member(instance, claim_id, dependents=()))
    assert again.status.proposal_id is None
    assert "playbill.authoring.claim_terminal" in refusal_codes(again)


def test_effective_until_is_caller_supplied_or_preserved_without_clock_substitution(
    tmp_path: Path,
) -> None:
    until = datetime(2026, 9, 1, 12, tzinfo=UTC)
    instance, claim_id, _owner = _accepted_claim_world(tmp_path)
    original = parse_claim(
        instance.tree_at(instance.accepted_coordinate().git_oid)[claim_path(claim_id)],
        path=claim_path(claim_id),
    )
    with_until = submit_retirement(
        instance, retirement_member(instance, claim_id, effective_until=until)
    )
    retired = parse_claim(
        _candidate_tree(instance, with_until)[claim_path(claim_id)], path=claim_path(claim_id)
    )
    assert retired.statement.model_copy(
        update={"effective_until": original.statement.effective_until}
    ) == (original.statement)
    assert retired.statement.effective_until == until

    preserve_root = tmp_path / "preserve"
    preserve_root.mkdir()
    second, second_id, _second_owner = _accepted_claim_world(preserve_root)
    preserved = submit_retirement(second, retirement_member(second, second_id))
    kept = parse_claim(
        _candidate_tree(second, preserved)[claim_path(second_id)], path=claim_path(second_id)
    )
    assert kept.statement.effective_until is None


def test_invalid_effective_interval_is_typed_for_retirement_and_migration(
    tmp_path: Path,
) -> None:
    effective_from = datetime(2026, 8, 16, 17, tzinfo=UTC)
    invalid_until = datetime(2026, 8, 16, 16, tzinfo=UTC)
    instance, owner = initialize_local(tmp_path)
    authoring = _status_authoring()
    seeded = service_propose_playbill_claim(
        instance,
        authoring=authoring.model_copy(
            update={
                "statement": authoring.statement.model_copy(
                    update={"effective_from": effective_from}
                )
            }
        ),
        actor_id="owner",
        proposal_name="effective-interval-seed",
        timestamp=TIMESTAMP,
    )
    _activate_direct_claim(
        instance,
        owner,
        seeded,
    )
    claim_id = STATUS_CLAIM_ID
    actor = AuthenticatedActor(actor_id="owner")

    refused = submit_retirement(
        instance, retirement_member(instance, claim_id, effective_until=invalid_until)
    )
    assert refused.status.proposal_id is None
    assert "invalid Claim effective interval" in refusal_messages(refused)

    with pytest.raises(
        ClaimTypeMigrationDependentInvalid,
        match="invalid Claim effective interval",
    ):
        service_migrate_claim_type(
            instance,
            request=ClaimTypeMigrationRequest(
                mode="submit",
                successor=_decision_only_successor(
                    instance,
                    enum=["blocked", "ready", "waiting"],
                ),
                dependents=(
                    ClaimTypeDependentDisposition(
                        identity=ArtifactIdentity(kind="Claim", name=claim_id),
                        disposition="retire",
                        claim_retirement_reason="was-rescinded",
                        claim_effective_until=invalid_until,
                    ),
                ),
            ),
            actor=actor,
        )


def test_transitive_dual_edge_closure_freezes_inputs_and_advances_only_claim_pin(
    tmp_path: Path,
) -> None:
    instance, owner, root_id, middle_id, leaf_id = _accepted_dependency_world(tmp_path)
    accepted_tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    root = parse_claim(accepted_tree[claim_path(root_id)], path=claim_path(root_id))
    middle = parse_claim(accepted_tree[claim_path(middle_id)], path=claim_path(middle_id))
    leaf = parse_claim(accepted_tree[claim_path(leaf_id)], path=claim_path(leaf_id))
    assert middle.backing.input_claim_digests != (claim_artifact_digest(root).tagged,)

    inventory = retirement_inventory(instance, root_id)
    assert [item.artifact_identity for item in inventory] == [middle.identity, leaf.identity]
    assert inventory[0].triggering_identity == root.identity
    assert inventory[0].triggering_edge_roles == ("backing-input",)
    assert inventory[1].triggering_identity == middle.identity
    assert inventory[1].triggering_edge_roles == ("backing-input", "input-claim")

    dependents = tuple(
        ClaimRetireDependent(
            artifact_identity=item.artifact_identity,
            predecessor_digest=item.predecessor_digest,
            reason="was-wrong" if item.artifact_identity == middle.identity else "was-rescinded",
        )
        for item in inventory
    )
    result = submit_retirement(
        instance, retirement_member(instance, root_id, dependents=dependents)
    )
    candidate_tree_value = _candidate_tree(instance, result)
    candidate_tree_mapping = dict(candidate_tree_value)
    retired_root = parse_claim(
        candidate_tree_mapping[claim_path(root_id)], path=claim_path(root_id)
    )
    retired_middle = parse_claim(
        candidate_tree_mapping[claim_path(middle_id)], path=claim_path(middle_id)
    )
    retired_leaf = parse_claim(
        candidate_tree_mapping[claim_path(leaf_id)], path=claim_path(leaf_id)
    )
    assert isinstance(retired_root, ClaimArtifact)
    assert isinstance(retired_middle, ClaimArtifact)
    assert isinstance(retired_leaf, ClaimArtifact)
    assert retired_middle.retirement.reason == "was-wrong"
    assert retired_middle.backing.input_claim_digests == middle.backing.input_claim_digests
    assert retired_leaf.backing.input_claim_digests == leaf.backing.input_claim_digests
    before_pin = next(pin for pin in leaf.pins if pin.target == middle.identity)
    after_pin = next(pin for pin in retired_leaf.pins if pin.target == middle.identity)
    assert before_pin.role == after_pin.role == "input-claim"
    assert after_pin.artifact_digest == claim_artifact_digest(retired_middle).tagged
    assert after_pin.artifact_digest != before_pin.artifact_digest

    candidate_tree = candidate_tree_mapping
    statement_rewrite = retired_leaf.model_copy(
        update={
            "statement": retired_leaf.statement.model_copy(
                update={"object": LiteralClaimObject(value="rewritten while retiring")}
            )
        }
    )
    statement_tree = {**candidate_tree, claim_path(leaf_id): render_claim(statement_rewrite)}
    statement_result = evaluate_proposal_tree(
        base_tree=accepted_tree,
        current_tree=accepted_tree,
        proposed_tree=statement_tree,
        current=instance.accepted_coordinate(),
        bodies=instance.body_store(),
        timestamp=TIMESTAMP,
        rebased=False,
        actor_id="owner",
        promotion_verifier=instance.proposal_service().promotion_verifier,
    )
    assert statement_result.candidate is None
    assert "playbill.claim.retirement_delta_invalid" in {
        item.code for item in statement_result.diagnostics
    }

    backing_rewrite = retired_leaf.model_copy(
        update={
            "backing": retired_leaf.backing.model_copy(
                update={"input_claim_digests": (claim_artifact_digest(retired_root).tagged,)}
            )
        }
    )
    backing_tree = {**candidate_tree, claim_path(leaf_id): render_claim(backing_rewrite)}
    backing_result = evaluate_proposal_tree(
        base_tree=accepted_tree,
        current_tree=accepted_tree,
        proposed_tree=backing_tree,
        current=instance.accepted_coordinate(),
        bodies=instance.body_store(),
        timestamp=TIMESTAMP,
        rebased=False,
        actor_id="owner",
        promotion_verifier=instance.proposal_service().promotion_verifier,
    )
    assert backing_result.candidate is None
    assert "playbill.claim.retirement_delta_invalid" in {
        item.code for item in backing_result.diagnostics
    }

    non_claim_pin = ArtifactPin(
        role="claim-type",
        target=leaf.statement.claim_type,
        artifact_digest=leaf.statement.claim_type_digest,
    )
    predecessor_with_non_claim_pin = leaf.model_copy(
        update={
            "pins": tuple(
                sorted(
                    (*leaf.pins, non_claim_pin),
                    key=lambda pin: (
                        pin.role.encode("utf-8"),
                        pin.target.qualified.encode("utf-8"),
                    ),
                )
            )
        }
    )
    attributed_retirement_with_non_claim_pin = retired_leaf.model_copy(
        update={
            "statement": predecessor_with_non_claim_pin.statement.model_copy(
                update={"effective_until": retired_leaf.statement.effective_until}
            ),
            "backing": predecessor_with_non_claim_pin.backing,
            "pins": predecessor_with_non_claim_pin.pins,
        }
    )
    assert isinstance(attributed_retirement_with_non_claim_pin, ClaimArtifact)
    assert _is_attributed_retirement(
        attributed_retirement_with_non_claim_pin,
        predecessor=predecessor_with_non_claim_pin,
    )
    retirement_with_non_claim_pin_rewrite = attributed_retirement_with_non_claim_pin.model_copy(
        update={
            "pins": tuple(
                pin.model_copy(update={"artifact_digest": "sha256:" + "a" * 64})
                if pin.target.kind != "Claim"
                else pin
                for pin in attributed_retirement_with_non_claim_pin.pins
            ),
        }
    )
    assert not _is_attributed_retirement(
        retirement_with_non_claim_pin_rewrite,
        predecessor=predecessor_with_non_claim_pin,
    )

    target_not_in_changeset = dict(candidate_tree)
    target_not_in_changeset[claim_path(middle_id)] = accepted_tree[claim_path(middle_id)]
    missing_target = evaluate_proposal_tree(
        base_tree=accepted_tree,
        current_tree=accepted_tree,
        proposed_tree=target_not_in_changeset,
        current=instance.accepted_coordinate(),
        bodies=instance.body_store(),
        timestamp=TIMESTAMP,
        rebased=False,
        actor_id="owner",
        promotion_verifier=instance.proposal_service().promotion_verifier,
    )
    assert missing_target.candidate is None
    assert "playbill.change_set.unresolved_pin" in {
        item.code for item in missing_target.diagnostics
    }

    skipped_middle = retired_middle.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired",
                predecessor_digest="sha256:" + "f" * 64,
            )
        }
    )
    skipped_middle_digest = claim_artifact_digest(skipped_middle).tagged
    skipped_leaf = retired_leaf.model_copy(
        update={
            "pins": tuple(
                pin.model_copy(update={"artifact_digest": skipped_middle_digest})
                if pin.target == middle.identity
                else pin
                for pin in retired_leaf.pins
            )
        }
    )
    skipped_hop_tree = dict(candidate_tree)
    skipped_hop_tree[claim_path(middle_id)] = render_claim(skipped_middle)
    skipped_hop_tree[claim_path(leaf_id)] = render_claim(skipped_leaf)
    skipped_hop = evaluate_proposal_tree(
        base_tree=accepted_tree,
        current_tree=accepted_tree,
        proposed_tree=skipped_hop_tree,
        current=instance.accepted_coordinate(),
        bodies=instance.body_store(),
        timestamp=TIMESTAMP,
        rebased=False,
        actor_id="owner",
        promotion_verifier=instance.proposal_service().promotion_verifier,
    )
    assert skipped_hop.candidate is None
    assert "playbill.claim.retirement_pin_delta_invalid" in {
        item.code for item in skipped_hop.diagnostics
    }

    incomplete = submit_retirement(
        instance, retirement_member(instance, root_id, dependents=dependents[:-1])
    )
    assert incomplete.status.proposal_id is None
    assert "playbill.authoring.claim_retirement_closure_incomplete" in refusal_codes(incomplete)

    activate_submitted(instance, owner, result)
    terminal = submit_retirement(instance, retirement_member(instance, root_id, dependents=()))
    assert "playbill.authoring.claim_terminal" in refusal_codes(terminal)


@pytest.mark.parametrize("leaf_retirement", ["attributed-v3", "legacy-v2"])
def test_a_retired_dependent_leaves_the_closure(
    tmp_path: Path,
    leaf_retirement: str,
) -> None:
    instance, owner, _root_id, middle_id, leaf_id = _accepted_dependency_world(tmp_path)
    if leaf_retirement == "attributed-v3":
        activate_submitted(
            instance, owner, submit_retirement(instance, retirement_member(instance, leaf_id))
        )
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
        _accept_historical_derivation_tree(instance, tree)

    assert retirement_inventory(instance, middle_id) == ()
    accepted = dict(instance.tree_at(instance.accepted_coordinate().git_oid))
    submitted = submit_retirement(instance, retirement_member(instance, middle_id))
    candidate = dict(_candidate_tree(instance, submitted))
    assert isinstance(
        parse_claim(candidate[claim_path(middle_id)], path=claim_path(middle_id)),
        ClaimArtifact,
    )
    assert candidate[claim_path(leaf_id)] == accepted[claim_path(leaf_id)]
    activate_submitted(instance, owner, submitted)


def test_live_target_successor_cannot_advance_a_retiring_dependent_pin(tmp_path: Path) -> None:
    instance, _owner, _root_id, middle_id, leaf_id = _accepted_dependency_world(tmp_path)
    actor = AuthenticatedActor(actor_id="owner")
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    middle_before = parse_claim(tree[claim_path(middle_id)], path=claim_path(middle_id))
    type_path = claim_type_path(middle_before.statement.predicate)
    current_type = parse_claim_type(tree[type_path], path=type_path)
    successor = _decision_only_input(current_type).model_copy(
        update={"literal_schema": {"type": "string", "minLength": 1}}
    )
    dispositions = (
        ClaimTypeDependentDisposition(
            identity=middle_before.identity,
            disposition="successor",
        ),
        ClaimTypeDependentDisposition(
            identity=ArtifactIdentity(kind="Claim", name=leaf_id),
            disposition="retire",
            claim_retirement_reason="was-rescinded",
        ),
    )
    with pytest.raises(
        ClaimTypeMigrationIncomplete,
        match="playbill.claim.retirement_pin_delta_invalid",
    ):
        service_migrate_claim_type(
            instance,
            request=ClaimTypeMigrationRequest(
                mode="submit",
                successor=successor,
                dependents=dispositions,
            ),
            actor=actor,
        )


def test_retire_refuses_an_extra_dependent(tmp_path: Path) -> None:
    instance, claim_id, _owner = _accepted_claim_world(tmp_path)
    refused = submit_retirement(
        instance,
        retirement_member(
            instance,
            claim_id,
            dependents=(
                ClaimRetireDependent(
                    artifact_identity=ArtifactIdentity(
                        kind="Claim",
                        name="CLM-ffffffffffffffffffffffffffffffff",
                    ),
                    predecessor_digest="sha256:" + "f" * 64,
                    reason="was-wrong",
                ),
            ),
        ),
    )
    assert refused.status.proposal_id is None
    assert "playbill.authoring.claim_retirement_closure_incomplete" in refusal_codes(refused)


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
        retirement_inventory(instance, claim_id)
    refused = submit_retirement(instance, retirement_member(instance, claim_id, dependents=()))
    assert refused.status.proposal_id is None
    assert "playbill.authoring.claim_retirement_closure_unsupported" in refusal_codes(refused)
    assert query.identity.qualified in refusal_messages(refused)
