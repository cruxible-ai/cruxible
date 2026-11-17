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
            proposal_id=proposal_id,
            statement=statement.model_dump(mode="json"),
            signer_principal=principals[signer_id].model_dump(mode="json"),
        )

    def submit(
        instance_id: str, proposal_id: str, attestation: dict[str, Any], *, hold: bool = False
    ) -> Any:
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


def _no_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No key directory, no principal variables, nothing remembered, no default key."""

    for name in ("CRUXIBLE_MCP_KEY_DIR", "CRUXIBLE_PRINCIPAL_ID", "CRUXIBLE_PRINCIPAL_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(
        handlers,
        "_get_client",
        lambda: SimpleNamespace(base_url=None, socket_path=str(tmp_path / "daemon.sock")),
    )


def test_approve_refuses_a_server_that_acts_as_no_principal(tmp_path, monkeypatch) -> None:
    _no_identity(tmp_path, monkeypatch)
    monkeypatch.setattr(
        handlers,
        "handle_playbill_prepare_approval",
        lambda *args, **kwargs: pytest.fail("an unconfigured approval reached the daemon"),
    )

    with pytest.raises(ConfigError) as refused:
        handlers.handle_playbill_approve("inst_1", "PROP-1", signer_id=None, candidate_digest=None)

    message = str(refused.value)
    assert "acts as no principal" in message
    assert "CRUXIBLE_PRINCIPAL_ID and CRUXIBLE_PRINCIPAL_KEY" in message


def test_approve_reads_the_principal_key_a_settings_file_sets(tmp_path, monkeypatch) -> None:
    """coldmcp-03, psw F12, sec N6: a server started with the reviewer's cruxible.env
    (CRUXIBLE_PRINCIPAL_ID and CRUXIBLE_PRINCIPAL_KEY) was refused for lack of
    CRUXIBLE_MCP_KEY_DIR and told to run `cruxible init`."""

    world = _world(tmp_path, monkeypatch, "owner", "reviewer")
    _no_identity(tmp_path, monkeypatch)
    monkeypatch.setenv("CRUXIBLE_PRINCIPAL_ID", "reviewer")
    monkeypatch.setenv("CRUXIBLE_PRINCIPAL_KEY", str(world["keys"] / "reviewer.ed25519"))

    handlers.handle_playbill_approve("inst_1", "PROP-1", signer_id=None, candidate_digest=None)

    (attestation,) = world["calls"]["submitted"]
    assert attestation["signer_id"] == "reviewer"


def test_approve_never_signs_as_one_principal_with_anothers_key(tmp_path, monkeypatch) -> None:
    """The server acts as owner (its key in the environment); signer_id names the reviewer,
    whose key is nowhere here: refused with this server's repair, nothing prepared."""

    world = _world(tmp_path, monkeypatch, "owner", "reviewer")
    _no_identity(tmp_path, monkeypatch)
    monkeypatch.setenv("CRUXIBLE_PRINCIPAL_ID", "owner")
    monkeypatch.setenv("CRUXIBLE_PRINCIPAL_KEY", str(world["keys"] / "owner.ed25519"))

    with pytest.raises(ConfigError) as refused:
        handlers.handle_playbill_approve(
            "inst_1", "PROP-1", signer_id="reviewer", candidate_digest=None
        )

    message = str(refused.value)
    assert "no signing key for principal reviewer here" in message
    assert "CRUXIBLE_MCP_KEY_DIR" in message and "cruxible_proposal_approve_prepare" in message
    assert "cruxible context use" not in message
    assert world["calls"]["prepared"] == []


def test_approve_finds_a_named_signers_remembered_settings(tmp_path, monkeypatch) -> None:
    """The lookup the CLI uses: the settings file remembered for the signer on this instance."""

    import json

    world = _world(tmp_path, monkeypatch, "owner", "reviewer")
    _no_identity(tmp_path, monkeypatch)
    socket = tmp_path / "daemon.sock"
    settings = world["keys"] / "cruxible.env"
    settings.write_text(
        f"export CRUXIBLE_SERVER_SOCKET={socket}\n"
        "export CRUXIBLE_INSTANCE_ID=inst_1\n"
        "export CRUXIBLE_PRINCIPAL_ID=reviewer\n"
        f"export CRUXIBLE_PRINCIPAL_KEY={world['keys'] / 'reviewer.ed25519'}\n"
    )
    binding = f"inst_1@unix://{socket.resolve()}"
    (tmp_path / "context.json").write_text(
        json.dumps({"principals": {binding: {"settings": {"reviewer": str(settings)}}}})
    )
    monkeypatch.setenv("CRUXIBLE_PRINCIPAL_ID", "owner")
    monkeypatch.setenv("CRUXIBLE_PRINCIPAL_KEY", str(world["keys"] / "owner.ed25519"))

    handlers.handle_playbill_approve(
        "inst_1", "PROP-1", signer_id="reviewer", candidate_digest=None
    )

    (attestation,) = world["calls"]["submitted"]
    assert attestation["signer_id"] == "reviewer"
    Ed25519PublicKey.from_public_bytes(
        bytes.fromhex(world["principals"]["reviewer"].public_key)
    ).verify(
        bytes.fromhex(attestation["sig"]),
        approval_statement_bytes(ApprovalAttestation.model_validate(attestation)),
    )


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


def test_approve_signs_the_landed_candidate_next(tmp_path, monkeypatch) -> None:
    """Nothing it touches changed since its base: the daemon landed it at the head.

    An approval signs one candidate, so the landed one (identical members) is
    challenged and signed in the same call; its receipt is the answer.
    """

    from cruxible_client import contracts

    world = _world(tmp_path, monkeypatch, "reviewer")
    landed = contracts.ProposalRecompiled(
        status="pending_review",
        source_proposal_id="PROP-1",
        proposal_id="PROP-2",
        candidate_digest="sha256:" + "4" * 64,
        members_identical=True,
        accepted_coordinate=contracts.AcceptedCoordinate(
            git_oid="1" * 40,
            semantic_root="sha256:" + "5" * 64,
            generation_root="sha256:" + "6" * 64,
            compiler_digest="sha256:" + "7" * 64,
        ),
    )
    answers = iter([landed, "receipt"])

    def submit(
        instance_id: str, proposal_id: str, attestation: dict[str, Any], *, hold: bool = False
    ) -> Any:
        world["calls"]["submitted"].append((proposal_id, attestation))
        return next(answers)

    monkeypatch.setattr(handlers, "handle_playbill_submit_approval", submit)

    receipt = handlers.handle_playbill_approve(
        "inst_1", "PROP-1", signer_id=None, candidate_digest=CANDIDATE
    )

    assert receipt == "receipt"
    assert world["calls"]["prepared"] == [
        ("inst_1", "PROP-1", "reviewer"),
        ("inst_1", "PROP-2", "reviewer"),
    ]
    assert [proposal_id for proposal_id, _ in world["calls"]["submitted"]] == ["PROP-1", "PROP-2"]


def test_approve_submits_to_the_proposal_the_challenge_landed(tmp_path, monkeypatch) -> None:
    """F3 / coldmcp-01: for a signer registered after the proposal's base, the
    challenge lands the proposal at the head and names the landed one; the
    signature is submitted for that proposal, not the stale id."""

    _world(tmp_path, monkeypatch, "late")
    original = handlers.handle_playbill_prepare_approval
    submitted_to: list[str] = []

    def landed_prepare(instance_id: str, proposal_id: str, **options: Any) -> Any:
        challenge = original(instance_id, proposal_id, **options)
        return SimpleNamespace(**{**vars(challenge), "proposal_id": "PROP-LANDED"})

    def submit(instance_id: str, proposal_id: str, attestation: Any, *, hold: bool = False) -> Any:
        submitted_to.append(proposal_id)
        return "receipt"

    monkeypatch.setattr(handlers, "handle_playbill_prepare_approval", landed_prepare)
    monkeypatch.setattr(handlers, "handle_playbill_submit_approval", submit)

    receipt = handlers.handle_playbill_approve(
        "inst_1", "PROP-1", signer_id="late", candidate_digest=None
    )
    assert receipt == "receipt"
    assert submitted_to == ["PROP-LANDED"]
