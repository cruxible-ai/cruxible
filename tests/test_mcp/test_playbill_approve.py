"""MCP approval with a local key: challenge, sign and submit in one call."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from cruxible_client.contracts.attestations import (
    ApprovalAttestation,
    ApprovalStatement,
    approval_statement_bytes,
)
from cruxible_client.contracts.errors import SigningKeyError
from cruxible_core.errors import ConfigError, DataValidationError
from cruxible_core.governance.keys import generate_client_principal_key
from cruxible_core.mcp import handlers

CANDIDATE = "sha256:" + "3" * 64


def _world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *signers: str) -> dict[str, Any]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    keys = tmp_path / "keys"
    principals = {
        signer: generate_client_principal_key(
            keys, principal_id=signer, kind="ordinary", forbidden_roots=()
        ).principal
        for signer in signers
    }
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("CRUXIBLE_MCP_KEY_DIR", str(keys))
    calls: dict[str, Any] = {"prepared": [], "submitted": []}

    def prepare(instance_id: str, proposal_id: str, *, signer_id: str, include_body: bool) -> Any:
        calls["prepared"].append((instance_id, proposal_id, signer_id))
        statement = ApprovalStatement(
            signer_id=signer_id,
            signing_semantic_root="sha256:" + "2" * 64,
            payload_digest=CANDIDATE,
        )
        return SimpleNamespace(
            statement=statement.model_dump(mode="json"),
            signer_principal=principals[signer_id].model_dump(mode="json"),
        )

    def submit(instance_id: str, proposal_id: str, attestation: dict[str, Any]) -> Any:
        calls["submitted"].append(attestation)
        return "receipt"

    monkeypatch.setattr(handlers, "handle_playbill_prepare_approval", prepare)
    monkeypatch.setattr(handlers, "handle_playbill_submit_approval", submit)
    return {"keys": keys, "workspace": workspace, "principals": principals, "calls": calls}


def test_approve_signs_the_challenge_with_the_configured_key(tmp_path, monkeypatch) -> None:
    world = _world(tmp_path, monkeypatch, "reviewer")

    receipt = handlers.handle_playbill_approve(
        "inst_1", "PROP-1", signer_id=None, candidate_digest=CANDIDATE
    )

    assert receipt == "receipt"
    assert world["calls"]["prepared"] == [("inst_1", "PROP-1", "reviewer")]
    (attestation,) = world["calls"]["submitted"]
    assert set(attestation) == {
        "tag",
        "signer_id",
        "signing_semantic_root",
        "payload_digest",
        "sig",
    }
    public_key = world["principals"]["reviewer"].public_key
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key)).verify(
        bytes.fromhex(attestation["sig"]),
        approval_statement_bytes(ApprovalAttestation.model_validate(attestation)),
    )
    private = (world["keys"] / "reviewer.ed25519").read_text()
    assert private not in str(attestation) and str(world["keys"]) not in str(attestation)


def test_approve_refuses_without_a_configured_key_directory(monkeypatch) -> None:
    monkeypatch.delenv("CRUXIBLE_MCP_KEY_DIR", raising=False)
    monkeypatch.setattr(
        handlers,
        "handle_playbill_prepare_approval",
        lambda *args, **kwargs: pytest.fail("an unconfigured approval reached the daemon"),
    )

    with pytest.raises(ConfigError) as refused:
        handlers.handle_playbill_approve("inst_1", "PROP-1", signer_id=None, candidate_digest=None)

    message = str(refused.value)
    assert "set CRUXIBLE_MCP_KEY_DIR" in message
    assert "cruxible playbill principal add --key-dir" in message


def test_approve_needs_a_signer_when_the_directory_holds_several(tmp_path, monkeypatch) -> None:
    world = _world(tmp_path, monkeypatch, "alice", "reviewer")

    with pytest.raises(DataValidationError, match=r"pass signer_id.*alice, reviewer"):
        handlers.handle_playbill_approve("inst_1", "PROP-1", signer_id=None, candidate_digest=None)
    assert world["calls"]["prepared"] == []

    handlers.handle_playbill_approve("inst_1", "PROP-1", signer_id="alice", candidate_digest=None)
    assert world["calls"]["submitted"][0]["signer_id"] == "alice"

    with pytest.raises(DataValidationError, match="signer_id"):
        handlers.handle_playbill_approve(
            "inst_1", "PROP-1", signer_id="../alice", candidate_digest=None
        )


def test_approve_refuses_a_candidate_that_changed_since_review(tmp_path, monkeypatch) -> None:
    world = _world(tmp_path, monkeypatch, "reviewer")

    with pytest.raises(DataValidationError, match="review it again"):
        handlers.handle_playbill_approve(
            "inst_1", "PROP-1", signer_id=None, candidate_digest="sha256:" + "9" * 64
        )
    assert world["calls"]["submitted"] == []


def test_approve_refuses_a_key_directory_inside_the_workspace(tmp_path, monkeypatch) -> None:
    world = _world(tmp_path, monkeypatch, "reviewer")
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(tmp_path))

    with pytest.raises(SigningKeyError, match="outside workspaces"):
        handlers.handle_playbill_approve("inst_1", "PROP-1", signer_id=None, candidate_digest=None)
    assert world["calls"]["submitted"] == []
