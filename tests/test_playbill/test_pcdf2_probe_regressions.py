"""Direct regressions for the PC-DF2 reviewer reproduction probes."""

from __future__ import annotations

import shutil
from pathlib import Path

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claim_types import claim_type_path, parse_claim_type
from cruxible_client.contracts.claims import claim_path, parse_claim
from cruxible_client.contracts.laws import CLAIM_LAW_V3_IDENTIFIER, _artifact_law_coordinate
from cruxible_core.playbill import proposals as proposals_module
from cruxible_core.playbill.claim_type_inputs import ClaimTypeInputV1
from cruxible_core.playbill.claim_type_migrations import (
    ClaimTypeDependentDispositionV3,
    ClaimTypeMigrationPreflightV1,
    ClaimTypeMigrationRequestV3,
    ClaimTypeMigrationResultV3,
    service_migrate_claim_type,
)
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.proposals import (
    AuthenticatedActor,
    claim_type_expansions_from_candidate,
    evaluate_proposal_tree,
)
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.playbill.settlement import ChangeActorBinding
from tests.test_playbill._support import client_material
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_claim_type_migrations import (
    _accepted_affects_package_world,
    _subject_valued_affects_package_successor,
)


def _migration(instance: PlaybillInstance, claim_id: str) -> ClaimTypeMigrationResultV3:
    result = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequestV3(
            mode="submit",
            successor=_subject_valued_affects_package_successor(instance),
            dependents=(
                ClaimTypeDependentDispositionV3(
                    identity=ArtifactIdentity(kind="Claim", name=claim_id),
                    disposition="retire",
                    claim_retirement_reason="was-rescinded",
                ),
            ),
        ),
        actor=AuthenticatedActor(actor_id="owner"),
    )
    assert isinstance(result, ClaimTypeMigrationResultV3)
    return result


def test_probe_alias_move_keeps_rev8_tombstone_settlement_reproducible(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    instance, claim_id, _owner = _accepted_affects_package_world(tmp_path)
    actor = AuthenticatedActor(actor_id="owner")
    result = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequestV3(
            mode="submit",
            successor=_subject_valued_affects_package_successor(instance),
            dependents=(
                ClaimTypeDependentDispositionV3(
                    identity=ArtifactIdentity(kind="Claim", name=claim_id),
                    disposition="retire",
                    claim_retirement_reason="was-rescinded",
                ),
            ),
        ),
        actor=actor,
    )
    assert isinstance(result, ClaimTypeMigrationResultV3)
    proposal = result.proposal.proposal
    assert proposal.candidate is not None
    tree_oid = proposal.evaluation.evaluated_tree_oid
    assert tree_oid is not None
    candidate_tree = instance.proposal_tree(tree_oid)
    base = instance.accepted_coordinate()
    reevaluated = evaluate_proposal_tree(
        base_tree=instance.tree_at(base.git_oid),
        current_tree=instance.tree_at(base.git_oid),
        proposed_tree=candidate_tree,
        current=base,
        bodies=instance.body_store(),
        timestamp=proposal.candidate.candidate.timestamp,
        rebased=False,
        actor_id=actor.actor_id,
        claim_type_expansions=claim_type_expansions_from_candidate(proposal.candidate),
    )
    record = reevaluated.candidate
    assert record is not None, reevaluated.diagnostics
    revision_9 = _artifact_law_coordinate(
        CLAIM_LAW_V3_IDENTIFIER, "playbill-claim-v3", semantic_revision=9
    )
    monkeypatch.setattr(proposals_module, "CLAIM_LAW_V3", revision_9)

    bundle = instance.prepare_generation(
        base=base,
        candidate_tree=candidate_tree,
        candidate=record,
        approvals=(),
        actor_binding=ChangeActorBinding(actor_id=actor.actor_id),
        proposal_actor_id=actor.actor_id,
        sequence=instance.accepted_history()[-1].sequence + 1,
    )
    assert bundle.settlement.base_oid == base.git_oid


def test_probe_reopen_after_law_bump_survives_checkpoint_and_genesis_replay(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    instance, claim_id, _owner = _accepted_affects_package_world(tmp_path)
    result = _migration(instance, claim_id)
    proposal = result.proposal.proposal
    assert proposal.candidate is not None
    approval = _sign(
        client_material(instance.root.parent, instance),
        proposal.candidate.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=proposal.admission.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="owner",
    )
    assert (
        service_activate_playbill_proposal(
            instance,
            proposal_id=proposal.admission.proposal_id,
            activated_by="owner",
        ).status
        == "accepted"
    )
    revision_9 = _artifact_law_coordinate(
        CLAIM_LAW_V3_IDENTIFIER, "playbill-claim-v3", semantic_revision=9
    )
    monkeypatch.setattr(proposals_module, "CLAIM_LAW_V3", revision_9)

    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.accepted_coordinate() == instance.accepted_coordinate()
    assert reopened.refresh() == instance.accepted_coordinate()
    checkpoint = PlaybillInstance._checkpoint_directory(instance.root)
    assert checkpoint.exists()
    shutil.rmtree(checkpoint)
    genesis_replay = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert genesis_replay.accepted_coordinate() == instance.accepted_coordinate()
    assert (
        parse_claim(
            genesis_replay.tree_at(genesis_replay.accepted_coordinate().git_oid)[
                claim_path(claim_id)
            ],
            path=claim_path(claim_id),
        ).lifecycle.state
        == "retired"
    )


def test_probe_double_migration_rederives_an_existing_tombstone(tmp_path: Path) -> None:
    instance, claim_id, _owner = _accepted_affects_package_world(tmp_path)
    result = _migration(instance, claim_id)
    proposal = result.proposal.proposal
    assert proposal.candidate is not None
    approval = _sign(
        client_material(instance.root.parent, instance),
        proposal.candidate.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=proposal.admission.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="owner",
    )
    assert (
        service_activate_playbill_proposal(
            instance,
            proposal_id=proposal.admission.proposal_id,
            activated_by="owner",
        ).status
        == "accepted"
    )
    path = claim_type_path("sec.vuln.affects_package")
    current = parse_claim_type(
        instance.tree_at(instance.accepted_coordinate().git_oid)[path], path=path
    )
    values = current.model_dump(mode="json")
    for mechanical in ("artifact_format", "identity", "lifecycle", "subject_scope", "slot_policy"):
        values.pop(mechanical, None)
    values["anticipated_source_ids"] = tuple(values.get("anticipated_source_ids") or ()) + (
        "extra-source",
    )
    second = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequestV3(
            mode="preflight", successor=ClaimTypeInputV1.model_validate(values)
        ),
        actor=AuthenticatedActor(actor_id="owner"),
    )
    assert isinstance(second, ClaimTypeMigrationPreflightV1)
    assert any(item.identity.name == claim_id for item in second.dependents)
