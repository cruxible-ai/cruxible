"""Agent-facing Claim-attestation composition retains local key custody."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client import AccessProfile, Playbill
from cruxible_client.authoring.attestations import (
    LocalClaimAttestationKeyUnavailable,
    LocalEd25519ClaimAttestationSigner,
    local_attestation_signer_from_environment,
)
from cruxible_client.contracts.errors import PlaybillKeyError
from tests.test_client._attestation_support import ServiceAttestationClient
from tests.test_playbill.test_claim_type_migrations import _accepted_claim_world
from tests.test_server.test_playbill_sdk_demo_world import _catalog


def test_sdk_attest_signs_with_a_real_local_key_and_appends_once(tmp_path: Path) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    client = ServiceAttestationClient(
        instance,
        actor_id="owner",
        state_dir=tmp_path / "server-state",
    )
    signer = LocalEd25519ClaimAttestationSigner.open(
        signer="owner",
        signing_key_id=owner.principal.public_key_digest,
        private_key_path=owner.private_key_path,
        expected_public_key=owner.principal.public_key,
        forbidden_roots=(instance.root,),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    _catalog(workspace)
    pb = Playbill(  # type: ignore[arg-type]
        client=client,
        instance_id=instance.descriptor.instance_id,
        workspace=workspace,
        access_profile=AccessProfile(
            profile_id="attestation-door-sdk",
            permitted_access_classes=("instance", "public"),
            disclose_restricted_existence=True,
        ),
        clock=lambda: datetime(2026, 8, 28, 18, tzinfo=UTC),
    )

    first = pb.attest(claim_id, stance="support", signer=signer, note="examined")
    retry = pb.attest(claim_id, stance="support", signer=signer, note="examined")

    assert retry == first
    assert len(instance.claim_attestation_evidence_store().events()) == 1


@pytest.mark.parametrize("failure", ["relative", "permissions", "mismatch", "workspace"])
def test_environment_key_custody_refuses_unsafe_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    instance, _claim_id, owner = _accepted_claim_world(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    client = ServiceAttestationClient(
        instance,
        actor_id="owner",
        state_dir=tmp_path / "server-state",
    )
    key_path = owner.private_key_path
    if failure == "relative":
        configured = key_path.name
    elif failure == "permissions":
        os.chmod(key_path, 0o644)
        configured = str(key_path)
    elif failure == "workspace":
        key_path = workspace / "owner.ed25519"
        key_path.write_bytes(owner.private_key_path.read_bytes())
        os.chmod(key_path, 0o600)
        configured = str(key_path)
    else:
        other_root = tmp_path / "other"
        other_root.mkdir()
        _other_instance, _other_claim, other = _accepted_claim_world(other_root)
        configured = str(other.private_key_path)
    monkeypatch.setenv("CRUXIBLE_PRINCIPAL_KEY_PATH", configured)

    with pytest.raises(
        (LocalClaimAttestationKeyUnavailable, PlaybillKeyError),
        match="playbill.claim_attestation.local_signing_key_unavailable|forbidden|match|absolute|permissions",
    ):
        local_attestation_signer_from_environment(
            client,
            instance.descriptor.instance_id,
            workspace_root=workspace,
        )
