"""Signed-genesis, cross-format, and tamper-refusal tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import (
    PlaybillBootstrapError,
    PlaybillFormatError,
    PlaybillKeyError,
)
from cruxible_client.contracts.types import PlaybillTrustRoot, PrincipalRecord
from cruxible_core.playbill.bootstrap import prepare_genesis
from cruxible_core.playbill.git import GitLedger
from cruxible_core.playbill.instance import DESCRIPTOR_FILE, PlaybillInstance
from cruxible_core.playbill.keys import (
    ALLOWED_SIGNERS_FILE,
    generate_daemon_key,
)

from ._support import FIXED_TIMESTAMP, generate_client, initialize_local


def test_sha1_and_sha256_ledgers_share_semantic_roots_but_not_generation_roots(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed-coordinate-source"
    owner = generate_client(
        tmp_path,
        managed_root=managed,
        principal_id="owner",
        roles=("owner",),
    )
    credentials = tmp_path / "daemon-custody"
    daemon = generate_daemon_key(credentials)
    trust = PlaybillTrustRoot(
        instance_id="inst_cross_format",
        daemon_public_key=daemon.principal.public_key,
        principals=tuple(
            sorted(
                (daemon.principal, owner.principal),
                key=lambda item: item.principal_id,
            )
        ),
    )

    verified = []
    for object_format in ("sha1", "sha256"):
        ledger = GitLedger.initialize(
            tmp_path / f"ledger-{object_format}.git",
            object_format=object_format,
            signing_key_path=daemon.private_key_path,
            allowed_signers_path=credentials / ALLOWED_SIGNERS_FILE,
        )
        verified.append(prepare_genesis(ledger, trust_root=trust, timestamp=FIXED_TIMESTAMP))

    sha1, sha256 = verified
    assert len(sha1.oid) == 40
    assert len(sha256.oid) == 64
    assert sha1.tree == sha256.tree
    assert sha1.bootstrap_root == sha256.bootstrap_root
    assert sha1.changeset_digest == sha256.changeset_digest
    assert sha1.semantic_root == sha256.semantic_root
    assert sha1.generation_root != sha256.generation_root


def test_git_operations_ignore_ambient_repository_and_config_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poison_objects = tmp_path / "poison-objects"
    poison_objects.mkdir()
    poison_config = tmp_path / "poison.gitconfig"
    poison_config.write_text(
        '[gpg "ssh"]\n\tprogram = /usr/bin/false\n[core]\n\thooksPath = /does/not/exist\n'
    )
    poison = {
        "GIT_DIR": str(tmp_path / "wrong.git"),
        "GIT_COMMON_DIR": str(tmp_path / "wrong-common.git"),
        "GIT_WORK_TREE": str(tmp_path / "wrong-worktree"),
        "GIT_OBJECT_DIRECTORY": str(poison_objects),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(poison_objects),
        "GIT_INDEX_FILE": str(tmp_path / "wrong-index"),
        "GIT_NAMESPACE": "wrong-namespace",
        "GIT_EXEC_PATH": str(tmp_path / "wrong-exec-path"),
        "GIT_CONFIG_GLOBAL": str(poison_config),
        "GIT_CONFIG_SYSTEM": str(poison_config),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "gpg.ssh.program",
        "GIT_CONFIG_VALUE_0": "/usr/bin/false",
    }
    for name, value in poison.items():
        monkeypatch.setenv(name, value)

    instance, _owner = initialize_local(tmp_path)
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)

    assert reopened.inspect().head_oid == instance.inspect().head_oid
    assert list(poison_objects.iterdir()) == []
    assert not (tmp_path / "wrong-index").exists()


def test_reopen_refuses_out_of_band_instance_id_or_principal_substitution(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    wrong_instance = instance.trust_root.model_copy(update={"instance_id": "inst_other"})
    with pytest.raises(PlaybillBootstrapError, match="instance ID"):
        PlaybillInstance.open(instance.root, trust_root=wrong_instance)

    substitute = generate_client(
        tmp_path,
        managed_root=instance.root,
        principal_id="substitute",
        roles=("owner",),
    )
    substituted_owner = PrincipalRecord(
        principal_id="owner",
        public_key=substitute.principal.public_key,
        authority_roles=("owner",),
    )
    changed_principals = tuple(
        substituted_owner if record.principal_id == "owner" else record
        for record in instance.trust_root.principals
    )
    changed_principals = tuple(sorted(changed_principals, key=lambda item: item.principal_id))
    changed_trust = PlaybillTrustRoot(
        instance_id=instance.trust_root.instance_id,
        daemon_public_key=instance.trust_root.daemon_public_key,
        principals=changed_principals,
    )
    with pytest.raises(PlaybillBootstrapError, match="differs from trust root"):
        PlaybillInstance.open(instance.root, trust_root=changed_trust)


def test_reopen_refuses_out_of_band_daemon_substitution(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    replacement = generate_daemon_key(tmp_path / "replacement-daemon")
    changed_principals = tuple(
        replacement.principal if record.principal_id == "daemon" else record
        for record in instance.trust_root.principals
    )
    changed_trust = PlaybillTrustRoot(
        instance_id=instance.trust_root.instance_id,
        daemon_public_key=replacement.principal.public_key,
        principals=changed_principals,
    )
    with pytest.raises(PlaybillBootstrapError, match="descriptor daemon key"):
        PlaybillInstance.open(instance.root, trust_root=changed_trust)


def test_reopen_refuses_daemon_private_key_replacement(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    replacement = generate_daemon_key(tmp_path / "replacement-daemon")
    private_path = instance._ledger._signing_key_path
    private_path.write_bytes(replacement.private_key_path.read_bytes())
    os.chmod(private_path, 0o600)

    with pytest.raises(PlaybillKeyError, match="does not match"):
        PlaybillInstance.open(instance.root, trust_root=instance.trust_root)


def test_reopen_refuses_allowed_signer_replacement(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    generate_daemon_key(tmp_path / "replacement-daemon")
    replacement_line = (tmp_path / "replacement-daemon" / ALLOWED_SIGNERS_FILE).read_bytes()
    instance._ledger._allowed_signers_path.write_bytes(replacement_line)
    os.chmod(instance._ledger._allowed_signers_path, 0o600)

    with pytest.raises(PlaybillBootstrapError, match="allowed daemon signer"):
        PlaybillInstance.open(instance.root, trust_root=instance.trust_root)


def test_descriptor_rejects_unknown_version_and_object_format_mismatch(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    descriptor_path = instance.root / DESCRIPTOR_FILE
    original = json.loads(descriptor_path.read_bytes())

    unknown = dict(original)
    unknown["format_version"] = 2
    descriptor_path.write_bytes(canonical_bytes(unknown) + b"\n")
    with pytest.raises(PlaybillFormatError, match="unsupported"):
        PlaybillInstance.open(instance.root, trust_root=instance.trust_root)

    mismatch = dict(original)
    mismatch["git_object_format"] = "sha1"
    descriptor_path.write_bytes(canonical_bytes(mismatch) + b"\n")
    with pytest.raises(PlaybillFormatError, match="object format differs"):
        PlaybillInstance.open(instance.root, trust_root=instance.trust_root)


def test_descriptor_root_tampering_is_refused(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    descriptor_path = instance.root / DESCRIPTOR_FILE
    payload = json.loads(descriptor_path.read_bytes())
    payload["genesis"]["semantic_root"] = "sha256:" + "00" * 32
    descriptor_path.write_bytes(canonical_bytes(payload) + b"\n")

    with pytest.raises(PlaybillBootstrapError, match="do not reproduce"):
        PlaybillInstance.open(instance.root, trust_root=instance.trust_root)


@pytest.mark.parametrize(
    ("field", "expected_error", "match"),
    (
        ("compiler", PlaybillFormatError, "compiler coordinate"),
        ("authority", PlaybillBootstrapError, "authority matrix"),
        ("storage", PlaybillFormatError, "storage layout"),
        ("recovery", PlaybillBootstrapError, "recovery posture"),
    ),
)
def test_unsigned_operational_descriptor_fields_are_cross_verified(
    tmp_path: Path,
    field: str,
    expected_error: type[Exception],
    match: str,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    descriptor_path = instance.root / DESCRIPTOR_FILE
    payload = json.loads(descriptor_path.read_bytes())
    if field == "compiler":
        payload["compiler"]["rule_digest"] = "sha256:" + "00" * 32
    elif field == "authority":
        payload["authority"]["families"]["documents"] = "ledger"
    elif field == "storage":
        payload["storage"]["cas"] = "cache"
    else:
        payload["recovery_posture"] = "recovery-configured"
    descriptor_path.write_bytes(canonical_bytes(payload) + b"\n")

    with pytest.raises(expected_error, match=match):
        PlaybillInstance.open(instance.root, trust_root=instance.trust_root)


def test_noncanonical_descriptor_is_refused(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    descriptor_path = instance.root / DESCRIPTOR_FILE
    payload = json.loads(descriptor_path.read_bytes())
    descriptor_path.write_text(json.dumps(payload, indent=2) + "\n")

    with pytest.raises(PlaybillFormatError, match="not canonical"):
        PlaybillInstance.open(instance.root, trust_root=instance.trust_root)


def test_reopen_refuses_world_readable_daemon_private_key(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    private_path = instance._ledger._signing_key_path
    os.chmod(private_path, 0o644)
    with pytest.raises(PlaybillKeyError, match="permissions"):
        PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
