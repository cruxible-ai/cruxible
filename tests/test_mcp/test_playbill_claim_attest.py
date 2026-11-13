"""MCP Claim-attestation tools use the same real local signing composition."""

from __future__ import annotations

from pathlib import Path

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
                    "because": "examined through MCP",
                },
            )
            text = " ".join(block.text for block in result.content if hasattr(block, "text"))
            return bool(result.isError), text

    is_error, output = _run(exercise())
    assert not is_error, output
    assert "playbill-claim-attestation-append-result-v1" in output
    assert str(owner.private_key_path) not in output
    assert len(instance.claim_attestation_evidence_store().events()) == 1


def test_mcp_attest_takes_write_evidence_and_refuses_a_malformed_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-R42: evidence is write's union; capture_digests and note are gone."""

    monkeypatch.setattr(handlers, "_get_client", lambda: object())
    with pytest.raises(DataValidationError, match="invalid evidence"):
        handlers.handle_playbill_claim_attest(
            "inst_test", "CLM-1", "support", evidence={"kind": "capture", "capture": "nope"}
        )
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "full")
    tools = {tool.name: tool for tool in _run(create_server().list_tools())}
    properties = tools["cruxible_claim_attest"].inputSchema["properties"]
    assert {"because", "evidence"} <= set(properties)
    assert not {"note", "capture_digests", "attested_at", "referent_coordinate"} & set(properties)
