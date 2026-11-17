"""S1: a Claim's `--detail proof` is its verdict and cited Captures; proof-full is whole."""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client.contracts.get_reads import GetRequest, GetResult
from cruxible_core.cli.main import cli

BASE = ["--server-url", "https://proof.invalid", "--instance-id", "inst"]
CAPTURE = "sha256:" + "ab" * 32
PROOF: dict[str, Any] = {
    "envelope": {"big": "x" * 2000},
    "facts": [
        {"schema_id": "cruxible.claim.current_verdict", "value": {"verdict": "supported"}},
        {"schema_id": "cruxible.claim.statement", "value": {"subject": "acme/widget"}},
    ],
    "admission_accounts": [
        {
            "capture_digest": CAPTURE,
            "status": "admitted",
            "capture_contract_identity": "CaptureContract:acme.catalog",
            "citation_role": "evidence",
        }
    ],
}


def _result(detail: str) -> GetResult:
    return GetResult.model_validate(
        {
            "ref": "CLM-0123456789ab",
            "kind": "claim",
            "detail": detail,
            "coordinate": {"git_oid": "a" * 12, "generation": 3},
            "evaluation_time": "2026-11-16T00:00:00Z",
            "proof": PROOF,
        }
    )


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[GetRequest]:
    seen: list[GetRequest] = []

    class Client:
        def get(self, instance_id: str, *, request: GetRequest) -> GetResult:
            seen.append(request)
            return _result(request.detail)

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: Client())
    return seen


def test_cli_proof_on_a_claim_is_its_verdict_and_cited_captures(sent: list[GetRequest]) -> None:
    brief = CliRunner().invoke(cli, [*BASE, "get", "CLM-0123456789ab", "--detail", "proof"])
    full = CliRunner().invoke(cli, [*BASE, "get", "CLM-0123456789ab", "--detail", "proof-full"])
    as_json = CliRunner().invoke(
        cli, [*BASE, "get", "CLM-0123456789ab", "--detail", "proof", "--json"]
    )

    assert brief.exit_code == full.exit_code == as_json.exit_code == 0, brief.output
    assert [request.detail for request in sent] == ["proof", "proof", "proof"]
    assert "verdict: supported" in brief.output
    assert "CAP-abababababab  admitted  CaptureContract:acme.catalog  evidence" in brief.output
    assert "x" * 50 not in brief.output
    assert "x" * 50 in full.output
    payload = json.loads(as_json.output[as_json.output.index("{") :])
    assert payload["proof"]["verdict"] == "supported" and "envelope" not in payload["proof"]


def test_mcp_proof_on_a_claim_is_a_summary_and_proof_full_is_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_core.mcp import handlers

    monkeypatch.setattr(
        handlers,
        "_daemon_call",
        lambda call, **_: _result("proof"),
    )
    brief = handlers.handle_playbill_get("inst", ref="CLM-0123456789ab", detail="proof")
    full = handlers.handle_playbill_get("inst", ref="CLM-0123456789ab", detail="proof_full")

    assert brief.proof is not None and brief.proof["captures"][0].startswith("CAP-abababababab")
    assert full.proof == PROOF
