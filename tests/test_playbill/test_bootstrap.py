"""Opt-in managed layout and exact genesis tests."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from cruxible_client.contracts.errors import PlaybillBootstrapError, PlaybillKeyError
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.keys import (
    DAEMON_PRIVATE_KEY_FILE,
    assert_outside_roots,
    generate_client_principal_key,
)

from ._support import FIXED_TIMESTAMP, generate_client, initialize_local


def test_opt_in_initialization_creates_managed_layout_and_exact_genesis(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    descriptor = instance.descriptor

    assert descriptor.tag == "playbill-instance-v1"
    assert descriptor.git_object_format == "sha256"
    assert descriptor.genesis.git_oid == instance.inspect().head_oid
    assert descriptor.genesis.bootstrap_root != "sha256:" + "00" * 32
    assert instance._ledger.parent_of(descriptor.genesis.git_oid) is None
    assert instance._verified_genesis.descriptor.parent_generation_root == (
        instance._verified_genesis.bootstrap_root.value
    )
    assert None not in instance._verified_genesis.descriptor.model_dump().values()

    layout = descriptor.storage.model_dump()
    assert set(layout) == {
        "ledger",
        "projections",
        "cas",
        "exhaust",
        "credentials",
        "leases",
    }
    assert all((instance.root / relative).is_dir() for relative in layout.values())
    assert owner.private_key_path.is_file()
    assert not owner.private_key_path.is_relative_to(instance.root)

    daemon_key = instance.root / descriptor.storage.credentials / DAEMON_PRIVATE_KEY_FILE
    assert stat.S_IMODE(daemon_key.stat().st_mode) == 0o600
    assert instance._ledger.durability_policy() == ("committed,reference", "fsync")


def test_reopen_requires_only_out_of_band_trust_root_and_managed_state(
    tmp_path: Path,
) -> None:
    created, _owner = initialize_local(tmp_path)
    reopened = PlaybillInstance.open(created.root, trust_root=created.trust_root)
    assert reopened.descriptor == created.descriptor
    assert reopened.inspect() == created.inspect()


def test_explicit_sha1_instance_initializes_and_reopens(tmp_path: Path) -> None:
    created, _owner = initialize_local(tmp_path, object_format="sha1")
    assert created.descriptor.git_object_format == "sha1"
    assert len(created.descriptor.genesis.git_oid) == 40
    reopened = PlaybillInstance.open(created.root, trust_root=created.trust_root)
    assert reopened.inspect().generation_root == created.inspect().generation_root


def test_inspection_exposes_posture_and_public_digests_without_private_paths(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    inspection = instance.inspect()
    rendered = inspection.model_dump_json()

    assert inspection.recovery_posture == "narrowed-no-recovery"
    assert inspection.authority.families == {
        "config": "legacy",
        "documents": "inactive",
        "graph": "legacy",
        "procedures": "legacy",
        "workflows": "legacy",
    }
    assert str(owner.private_key_path) not in rendered
    assert str(instance._ledger._signing_key_path) not in rendered
    assert DAEMON_PRIVATE_KEY_FILE not in rendered
    assert "PRIVATE KEY" not in rendered
    assert "credentials" not in inspection.storage_directories
    assert all(
        principal.public_key_digest.startswith("sha256:") for principal in inspection.principals
    )


def test_managed_root_must_be_absolute_new_and_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    owner = generate_client(
        tmp_path,
        managed_root=workspace / "managed",
        principal_id="owner",
        roles=("owner",),
    )
    with pytest.raises(PlaybillBootstrapError, match="outside"):
        PlaybillInstance.initialize(
            workspace / "managed",
            instance_id="inst_refused",
            client_principals=(owner.principal,),
            workspace_roots=(workspace,),
            timestamp=FIXED_TIMESTAMP,
        )
    assert not (workspace / "managed").exists()

    with pytest.raises(PlaybillBootstrapError, match="absolute"):
        PlaybillInstance.initialize(
            Path("relative-managed"),
            instance_id="inst_refused",
            client_principals=(owner.principal,),
            workspace_roots=(workspace,),
            timestamp=FIXED_TIMESTAMP,
        )


def test_client_key_generation_refuses_workspace_or_managed_custody(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    managed = tmp_path / "managed"
    workspace.mkdir()
    managed.mkdir()
    for forbidden in (workspace / "keys", managed / "credentials" / "keys"):
        with pytest.raises(PlaybillKeyError, match="outside"):
            generate_client_principal_key(
                forbidden,
                principal_id="owner",
                authority_roles=("owner",),
                forbidden_roots=(workspace, managed),
            )
        assert not forbidden.exists()
    with pytest.raises(PlaybillKeyError):
        assert_outside_roots(managed, (managed,))


def test_client_private_key_bytes_never_enter_workspace_or_managed_tree(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    private_bytes = owner.private_key_path.read_bytes()
    assert private_bytes.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----")

    workspace = tmp_path / "workspace"
    for root in (workspace, instance.root):
        for path in root.rglob("*"):
            if path.is_file():
                assert private_bytes not in path.read_bytes()


def test_workspace_edits_cannot_move_ledger_or_verified_coordinates(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    before = instance.inspect()
    (tmp_path / "workspace" / "untrusted-edit.yaml").write_text("authority: claimed\n")

    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    after = reopened.inspect()
    assert after.head_oid == before.head_oid
    assert after.semantic_root == before.semantic_root
    assert after.generation_root == before.generation_root


def test_bootstrap_accepts_one_owner_and_optional_recovery(tmp_path: Path) -> None:
    managed = tmp_path / "managed-single-client"
    owner = generate_client(
        tmp_path,
        managed_root=managed,
        principal_id="owner",
        roles=("owner",),
    )

    instance = PlaybillInstance.initialize(
        managed,
        instance_id="inst_single_client",
        client_principals=(owner.principal,),
        workspace_roots=(tmp_path / "workspace",),
        timestamp=FIXED_TIMESTAMP,
    )
    assert instance.inspect().recovery_posture == "narrowed-no-recovery"

    recovery_tmp = tmp_path / "with-recovery"
    recovery_tmp.mkdir()
    managed_with_recovery = recovery_tmp / "managed-owner-recovery"
    recovery_owner = generate_client(
        recovery_tmp,
        managed_root=managed_with_recovery,
        principal_id="owner",
        roles=("owner",),
    )
    recovery = generate_client(
        recovery_tmp,
        managed_root=managed_with_recovery,
        principal_id="recovery",
        roles=("recovery",),
    )
    recovered = PlaybillInstance.initialize(
        managed_with_recovery,
        instance_id="inst_owner_recovery",
        client_principals=(recovery_owner.principal, recovery.principal),
        workspace_roots=(recovery_tmp / "workspace-recovery",),
        timestamp=FIXED_TIMESTAMP,
    )
    assert recovered.inspect().recovery_posture == "recovery-configured"


def test_cloud_profile_does_not_assign_special_meaning_to_recovery_role(tmp_path: Path) -> None:
    managed = tmp_path / "managed-cloud"
    owner = generate_client(
        tmp_path,
        managed_root=managed,
        principal_id="owner",
        roles=("owner",),
    )
    instance = PlaybillInstance.initialize(
        managed,
        instance_id="inst_cloud",
        client_principals=(owner.principal,),
        workspace_roots=(tmp_path / "workspace",),
        operating_profile="cloud",
        timestamp=FIXED_TIMESTAMP,
    )
    assert instance.inspect().recovery_posture == "narrowed-no-recovery"


def test_bootstrap_without_an_owner_refuses_typed(tmp_path: Path) -> None:
    managed = tmp_path / "managed-no-owner"
    reviewer_a = generate_client(
        tmp_path, managed_root=managed, principal_id="reviewer-a", roles=("reviewer",)
    )
    reviewer_b = generate_client(
        tmp_path, managed_root=managed, principal_id="reviewer-b", roles=("reviewer",)
    )
    with pytest.raises(PlaybillBootstrapError, match="at least one owner"):
        PlaybillInstance.initialize(
            managed,
            instance_id="inst_no_owner",
            client_principals=(reviewer_a.principal, reviewer_b.principal),
            workspace_roots=(tmp_path / "workspace",),
            timestamp=FIXED_TIMESTAMP,
        )
