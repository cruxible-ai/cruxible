"""MCP Claim-attestation tools use the same real local signing composition."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_core.errors import DataValidationError
from cruxible_core.mcp import handlers
from cruxible_core.mcp.server import create_server
from tests.test_claims.test_claim_type_migrations import _accepted_claim_world
from tests.test_client._attestation_support import ServiceAttestationClient
from tests.test_mcp.test_playbill_protocol_curation import _protocol_session, _run


def test_mcp_examined_existing_signs_with_real_key_and_appends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    client = ServiceAttestationClient(
        instance,
        actor_id="owner",
        state_dir=tmp_path / "server-state",
    )
    monkeypatch.setattr(handlers, "_get_client", lambda: client)
    monkeypatch.setenv("CRUXIBLE_PRINCIPAL_KEY", str(owner.private_key_path))
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "full")
    monkeypatch.setenv("CRUXIBLE_MODE", "governed_write")
    server = create_server()

    async def exercise() -> tuple[bool, str]:
        async with _protocol_session(server) as session:
            await session.initialize()
            result = await session.call_tool(
                "cruxible_claim_attest",
                {
                    "instance_id": instance.descriptor.instance_id,
                    "claim_id": claim_id,
                    "stance": "unsure",
                    "note": "examined through MCP",
                },
            )
            text = " ".join(block.text for block in result.content if hasattr(block, "text"))
            return bool(result.isError), text

    is_error, output = _run(exercise())
    assert not is_error, output
    assert "playbill-claim-attestation-append-result-v1" in output
    assert str(owner.private_key_path) not in output
    assert len(instance.claim_attestation_evidence_store().events()) == 1


def _captured_attestation(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    prepared: list[Any] = []

    def capture(_client: Any, _instance_id: str, request: Any) -> str:
        prepared.append(request)
        return "appended"

    monkeypatch.setattr(handlers, "_handle_claim_attestation", capture)
    monkeypatch.setattr(handlers, "_get_client", lambda: object())
    return prepared


def test_capture_digests_turn_the_attestation_into_a_new_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _captured_attestation(monkeypatch)
    later, earlier = "sha256:" + "b" * 64, "sha256:" + "a" * 64

    handlers.handle_playbill_claim_attest(
        "inst_test",
        "Claim:CLM-1",
        "support",
        None,
        capture_digests=[later, earlier, later],
    )
    handlers.handle_playbill_claim_attest("inst_test", "CLM-1", "unsure", None)

    new_capture, examined = prepared
    assert new_capture.attestation_basis == "new_capture"
    assert new_capture.claim_id == "CLM-1"
    assert [item.capture_digest for item in new_capture.capture_references] == [earlier, later]
    assert examined.attestation_basis == "examined_existing"
    assert examined.capture_references == ()


def test_referent_coordinate_needs_capture_digests(monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = _captured_attestation(monkeypatch)

    with pytest.raises(DataValidationError, match="only with capture_digests"):
        handlers.handle_playbill_claim_attest(
            "inst_test", "CLM-1", "support", None, referent_coordinate={}
        )
    with pytest.raises(DataValidationError, match="cruxible_claim_attest"):
        handlers.handle_playbill_claim_attest(
            "inst_test", "CLM-1", "support", None, capture_digests=["not-a-digest"]
        )
    assert prepared == []


def test_an_empty_capture_list_refuses_instead_of_attesting_the_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _captured_attestation(monkeypatch)

    with pytest.raises(DataValidationError, match="at least one new Capture"):
        handlers.handle_playbill_claim_attest(
            "inst_test", "CLM-1", "support", None, capture_digests=[]
        )
    assert prepared == []


def test_a_new_capture_keeps_the_caller_observation_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _captured_attestation(monkeypatch)
    observed = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

    handlers.handle_playbill_claim_attest(
        "inst_test",
        "CLM-1",
        "support",
        None,
        capture_digests=["sha256:" + "a" * 64],
        attested_at=observed,
    )
    with pytest.raises(DataValidationError, match="attested_at applies only"):
        handlers.handle_playbill_claim_attest(
            "inst_test", "CLM-1", "support", None, attested_at=observed
        )

    (new_capture,) = prepared
    assert new_capture.attested_at == observed
