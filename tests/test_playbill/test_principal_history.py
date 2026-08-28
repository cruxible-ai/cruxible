"""PB-D governed principal lifecycle and exact-root replay tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.errors import (
    ApprovalIntegrityError,
    PrincipalIntegrityError,
    ProposalAdmissionError,
    SettlementIntegrityError,
)
from cruxible_client.contracts.principals import (
    PrincipalRegistrySnapshot,
    principal_registry_from_tree,
)
from cruxible_client.contracts.types import PrincipalRecord
from cruxible_core.playbill.bootstrap import render_principal
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.keys import GeneratedKeyMaterial, generate_client_principal_key
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.settlement import ChangeActorBinding, prepare_generation

from ._support import FIXED_TIMESTAMP, generate_client, initialize_local
from .test_activation import _candidate, _sign

ROOT = "sha256:" + "11" * 32


def _static_principal(principal_id: str, roles: tuple[str, ...]) -> PrincipalRecord:
    return PrincipalRecord(
        principal_id=principal_id,
        public_key=(principal_id.encode().hex() + "00" * 32)[:64],
        authority_roles=roles,
    )


def test_registry_replays_canonical_principals_at_exact_root() -> None:
    daemon = _static_principal("daemon", ("daemon",))
    owner = _static_principal("owner", ("owner",))
    snapshot = principal_registry_from_tree(
        {
            "principals/daemon.yaml": render_principal(daemon),
            "principals/owner.yaml": render_principal(owner),
            "documents/ignored.yaml": b"{}\n",
        },
        semantic_root=ROOT,
    )

    assert snapshot.require_active("owner") == owner
    assert snapshot.key_history_reference("owner") == f"principals/owner.yaml@{ROOT}"


def test_registry_refuses_path_substitution_and_noncanonical_bytes() -> None:
    daemon = _static_principal("daemon", ("daemon",))
    owner = _static_principal("owner", ("owner",))
    with pytest.raises(PrincipalIntegrityError, match="path and identity"):
        principal_registry_from_tree(
            {
                "principals/daemon.yaml": render_principal(daemon),
                "principals/other.yaml": render_principal(owner),
            },
            semantic_root=ROOT,
        )
    with pytest.raises(PrincipalIntegrityError, match="not canonical"):
        principal_registry_from_tree(
            {
                "principals/daemon.yaml": render_principal(daemon),
                "principals/owner.yaml": render_principal(owner).replace(b":", b": ", 1),
            },
            semantic_root=ROOT,
        )


def test_registry_refuses_duplicate_principal_ids() -> None:
    daemon = _static_principal("daemon", ("daemon",))
    owner = _static_principal("owner", ("owner",))
    duplicate_owner = owner.model_copy(update={"public_key": "ab" * 32})

    with pytest.raises(ValueError, match="sorted and unique"):
        PrincipalRegistrySnapshot(
            semantic_root=ROOT,
            principals=(daemon, owner, duplicate_owner),
        )


def test_principal_proposal_refuses_a_public_key_duplicated_from_the_registry(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    base = instance.accepted_coordinate()
    daemon = instance._recovered.head.principals.require_active("daemon")
    mallory = PrincipalRecord(
        principal_id="mallory",
        public_key=daemon.public_key,
        authority_roles=("reviewer",),
    )
    tree = {
        **instance._ledger.read_tree(base.git_oid),
        "principals/mallory.yaml": render_principal(mallory),
    }

    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/register-mallory",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp="2026-08-12T14:59:00.000000Z",
    )

    assert result.candidate is None
    assert tuple(item.code for item in result.evaluation.diagnostics) == (
        "playbill.principal.registry_invalid",
    )


def _cloud_instance(tmp_path: Path):
    managed = tmp_path / "managed"
    owner = generate_client(
        tmp_path,
        managed_root=managed,
        principal_id="owner",
        roles=("owner",),
    )
    recovery = generate_client(
        tmp_path,
        managed_root=managed,
        principal_id="recovery",
        roles=("recovery",),
    )
    reviewer = generate_client(
        tmp_path,
        managed_root=managed,
        principal_id="reviewer",
        roles=("reviewer",),
    )
    backup = generate_client(
        tmp_path,
        managed_root=managed,
        principal_id="backup-reviewer",
        roles=("reviewer",),
    )
    instance = PlaybillInstance.initialize(
        managed,
        instance_id="inst_principal_history",
        client_principals=(
            owner.principal,
            reviewer.principal,
            backup.principal,
            recovery.principal,
        ),
        workspace_roots=(tmp_path / "workspace",),
        operating_profile="cloud",
        timestamp=FIXED_TIMESTAMP,
    )
    return instance, owner, recovery


def _replacement_key(
    tmp_path: Path,
    instance: PlaybillInstance,
    *,
    custody_name: str,
    principal_id: str,
    roles: tuple[str, ...],
) -> GeneratedKeyMaterial:
    return generate_client_principal_key(
        tmp_path / custody_name,
        principal_id=principal_id,
        authority_roles=roles,
        forbidden_roots=(instance.root, tmp_path / "workspace"),
    )


def _settle_transition(
    instance: PlaybillInstance,
    *,
    actor: GeneratedKeyMaterial,
    approver: GeneratedKeyMaterial,
    proposed: PrincipalRecord,
    timestamp: str,
) -> PlaybillInstance:
    base = instance.accepted_coordinate()
    path = f"principals/{proposed.principal_id}.yaml"
    tree = {**instance._ledger.read_tree(base.git_oid), path: render_principal(proposed)}
    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id=actor.principal.principal_id),
        request=ProposalAdmissionRequest(
            target_ref=(
                f"refs/proposals/{actor.principal.principal_id}/principal-{proposed.principal_id}"
            ),
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp=timestamp,
    )
    assert result.candidate is not None, result.evaluation.diagnostics
    candidate = result.candidate
    # The transition the principal law classified travels in its member law
    # evidence, where every other member kind's law result travels, rather than
    # in a field only the singleton candidate shape had room for.
    assert candidate.members[0].artifact_kind == "principal-lifecycle"
    assert candidate.law_evidence[0].result["governance_operation"] in {
        "register",
        "rotate",
        "revoke",
        "recover",
    }
    with pytest.raises(SettlementIntegrityError, match="cryptographically approve"):
        prepare_generation(
            instance._ledger,
            base=base,
            candidate_tree=tree,
            candidate=candidate,
            approval_submissions=(_sign(approver, candidate.candidate_digest, base.semantic_root),),
            bodies=instance.body_store(),
            actor_binding=ChangeActorBinding(actor_id=actor.principal.principal_id),
            proposal_actor_id=actor.principal.principal_id,
            sequence=instance._recovered.head.sequence + 1,
        )
    approvals = tuple(
        sorted(
            (
                _sign(actor, candidate.candidate_digest, base.semantic_root),
                _sign(approver, candidate.candidate_digest, base.semantic_root),
            ),
            key=lambda item: item.attestation.signer_id,
        )
    )
    bundle = prepare_generation(
        instance._ledger,
        base=base,
        candidate_tree=tree,
        candidate=candidate,
        approval_submissions=approvals,
        bodies=instance.body_store(),
        actor_binding=ChangeActorBinding(actor_id=actor.principal.principal_id),
        proposal_actor_id=actor.principal.principal_id,
        sequence=instance._recovered.head.sequence + 1,
    )
    publisher = instance.activation_publisher()
    projection = publisher.prebuild(bundle, base=base)
    assert publisher.activate(bundle, projection, base=base).status == "accepted"
    return PlaybillInstance.open(instance.root, trust_root=instance.trust_root)


def test_owner_rotation_and_recovery_replacement_replay_exact_key_roots(
    tmp_path: Path,
) -> None:
    instance, owner, recovery = _cloud_instance(tmp_path)
    rotated_owner = _replacement_key(
        tmp_path,
        instance,
        custody_name="owner-rotated",
        principal_id="owner",
        roles=("owner",),
    )
    instance = _settle_transition(
        instance,
        actor=owner,
        approver=recovery,
        proposed=rotated_owner.principal,
        timestamp="2026-08-12T15:00:00.000000Z",
    )
    assert instance._recovered.head.sequence == 1
    assert instance._recovered.head.principals.require_active("owner") == rotated_owner.principal

    recovered_owner = _replacement_key(
        tmp_path,
        instance,
        custody_name="owner-recovered",
        principal_id="owner",
        roles=("owner",),
    )
    instance = _settle_transition(
        instance,
        actor=recovery,
        approver=rotated_owner,
        proposed=recovered_owner.principal,
        timestamp="2026-08-12T15:01:00.000000Z",
    )
    assert instance._recovered.head.sequence == 2
    assert instance._recovered.head.principals.require_active("owner") == recovered_owner.principal
    assert instance._recovered.history[0].principals.require_active("owner") == owner.principal
    assert (
        instance._recovered.history[1].principals.require_active("owner") == rotated_owner.principal
    )

    base, tree, document_candidate = _candidate(instance)
    bundle = prepare_generation(
        instance._ledger,
        base=base,
        candidate_tree=tree,
        candidate=document_candidate,
        approval_submissions=(
            _sign(recovery, document_candidate.candidate_digest, base.semantic_root),
        ),
        bodies=instance.body_store(),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        proposal_actor_id="owner",
        sequence=3,
    )
    assert tuple(item.signer_id for item in bundle.approvals) == ("recovery",)


def test_owner_registration_and_revocation_make_old_reviewer_key_inactive(
    tmp_path: Path,
) -> None:
    instance, owner, recovery = _cloud_instance(tmp_path)
    reviewer = _replacement_key(
        tmp_path,
        instance,
        custody_name="reviewer-custody",
        principal_id="reviewer",
        roles=("reviewer",),
    )
    instance = _settle_transition(
        instance,
        actor=recovery,
        approver=owner,
        proposed=reviewer.principal,
        timestamp="2026-08-12T16:00:00.000000Z",
    )
    assert instance._recovered.head.principals.require_active("reviewer") == reviewer.principal

    instance = _settle_transition(
        instance,
        actor=reviewer,
        approver=owner,
        proposed=reviewer.principal.model_copy(update={"status": "revoked"}),
        timestamp="2026-08-12T16:01:00.000000Z",
    )
    reviewer_record = next(
        record
        for record in instance._recovered.head.principals.principals
        if record.principal_id == "reviewer"
    )
    assert reviewer_record.status == "revoked"

    base, tree, document_candidate = _candidate(instance)
    submissions = (_sign(reviewer, document_candidate.candidate_digest, base.semantic_root),)
    with pytest.raises(ApprovalIntegrityError, match="not active"):
        prepare_generation(
            instance._ledger,
            base=base,
            candidate_tree=tree,
            candidate=document_candidate,
            approval_submissions=submissions,
            bodies=instance.body_store(),
            actor_binding=ChangeActorBinding(actor_id="owner"),
            proposal_actor_id="owner",
            sequence=3,
        )


def test_recovery_label_has_no_retention_or_unconfigured_authority(tmp_path: Path) -> None:
    instance, owner, recovery = _cloud_instance(tmp_path)
    base = instance.accepted_coordinate()
    revoked = recovery.principal.model_copy(update={"status": "revoked"})
    tree = {
        **instance._ledger.read_tree(base.git_oid),
        "principals/recovery.yaml": render_principal(revoked),
    }
    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/revoke-last-recovery",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp="2026-08-12T17:00:00.000000Z",
    )
    assert result.candidate is not None

    local_root = tmp_path / "managed-local"
    local_owner = generate_client(
        tmp_path,
        managed_root=local_root,
        principal_id="local-owner",
        roles=("owner",),
    )
    local_reviewer = generate_client(
        tmp_path,
        managed_root=local_root,
        principal_id="local-reviewer",
        roles=("reviewer",),
    )
    local = PlaybillInstance.initialize(
        local_root,
        instance_id="inst_no_recovery",
        client_principals=(local_owner.principal, local_reviewer.principal),
        workspace_roots=(tmp_path / "workspace",),
        timestamp=FIXED_TIMESTAMP,
    )
    replacement = _replacement_key(
        tmp_path,
        local,
        custody_name="local-owner-replacement",
        principal_id="local-owner",
        roles=("owner",),
    )
    local_base = local.accepted_coordinate()
    local_tree = {
        **local._ledger.read_tree(local_base.git_oid),
        "principals/local-owner.yaml": render_principal(replacement.principal),
    }
    with pytest.raises(ProposalAdmissionError, match="creator_principal_invalid"):
        local.proposal_service().submit(
            actor=AuthenticatedActor(actor_id="recovery"),
            request=ProposalAdmissionRequest(
                target_ref="refs/proposals/recovery/invented-recovery",
                proposed_base_oid=local_base.git_oid,
            ),
            candidate_tree=local_tree,
            timestamp="2026-08-12T17:01:00.000000Z",
        )


def test_revoked_key_needs_fresh_material_and_dormant_roles_stay_immutable(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed-key-invariants"
    owner = generate_client(
        tmp_path,
        managed_root=managed,
        principal_id="owner",
        roles=("owner",),
    )
    reviewer = generate_client(
        tmp_path,
        managed_root=managed,
        principal_id="reviewer",
        roles=("reviewer",),
    )
    third = generate_client(
        tmp_path,
        managed_root=managed,
        principal_id="third",
        roles=("reviewer",),
    )
    recovery = generate_client(
        tmp_path,
        managed_root=managed,
        principal_id="recovery",
        roles=("recovery",),
    )
    instance = PlaybillInstance.initialize(
        managed,
        instance_id="inst_key_invariants",
        client_principals=(
            owner.principal,
            reviewer.principal,
            third.principal,
            recovery.principal,
        ),
        workspace_roots=(tmp_path / "workspace",),
        timestamp=FIXED_TIMESTAMP,
    )

    def propose(
        label: str,
        proposed: PrincipalRecord,
        *,
        actor: GeneratedKeyMaterial = owner,
    ):
        base = instance.accepted_coordinate()
        path = f"principals/{proposed.principal_id}.yaml"
        return instance.proposal_service().submit(
            actor=AuthenticatedActor(actor_id=actor.principal.principal_id),
            request=ProposalAdmissionRequest(
                target_ref=f"refs/proposals/{actor.principal.principal_id}/{label}",
                proposed_base_oid=base.git_oid,
            ),
            candidate_tree={
                **instance.tree_at(base.git_oid),
                path: render_principal(proposed),
            },
            timestamp="2026-08-12T18:00:00.000000Z",
        )

    replacement_with_changed_roles = _replacement_key(
        tmp_path,
        instance,
        custody_name="third-role-change",
        principal_id="third",
        roles=("owner",),
    )
    role_change = propose(
        "change-dormant-role",
        replacement_with_changed_roles.principal,
        actor=third,
    )
    assert role_change.candidate is None
    assert role_change.evaluation.diagnostics[0].code == (
        "playbill.principal.transition_unauthorized"
    )

    recovery_escalation = _replacement_key(
        tmp_path,
        instance,
        custody_name="recovery-role-change",
        principal_id="recovery",
        roles=("owner",),
    )
    role_escalation = propose(
        "recovery-role-escalation",
        recovery_escalation.principal,
        actor=recovery,
    )
    assert role_escalation.candidate is None
    assert role_escalation.evaluation.diagnostics[0].code == (
        "playbill.principal.transition_unauthorized"
    )

    swapped_key = _replacement_key(
        tmp_path,
        instance,
        custody_name="third-revoke-swap",
        principal_id="third",
        roles=("reviewer",),
    )
    swapped_revocation = propose(
        "revoke-with-key-swap",
        swapped_key.principal.model_copy(update={"status": "revoked"}),
    )
    assert swapped_revocation.candidate is None
    assert swapped_revocation.evaluation.diagnostics[0].code == (
        "playbill.principal.transition_unauthorized"
    )

    revoked = third.principal.model_copy(update={"status": "revoked"})
    instance = _settle_transition(
        instance,
        actor=owner,
        approver=reviewer,
        proposed=revoked,
        timestamp="2026-08-12T18:01:00.000000Z",
    )
    revoked_record = next(
        item
        for item in instance._recovered.head.principals.principals
        if item.principal_id == "third"
    )
    assert revoked_record.status == "revoked"

    same_key_rearm = propose("rearm-revoked-key", third.principal)
    assert same_key_rearm.candidate is None
    assert same_key_rearm.evaluation.diagnostics[0].code == (
        "playbill.principal.transition_unauthorized"
    )

    fresh_third = _replacement_key(
        tmp_path,
        instance,
        custody_name="third-fresh-recovery",
        principal_id="third",
        roles=("reviewer",),
    )
    instance = _settle_transition(
        instance,
        actor=owner,
        approver=reviewer,
        proposed=fresh_third.principal,
        timestamp="2026-08-12T18:02:00.000000Z",
    )
    assert instance._recovered.head.principals.require_active("third") == (fresh_third.principal)
    assert fresh_third.principal.public_key != third.principal.public_key
