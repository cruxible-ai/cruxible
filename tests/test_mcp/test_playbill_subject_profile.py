"""The MCP Subject profile carries the same incoming-relation shape as the CLI."""

from __future__ import annotations

import asyncio

import pytest

from cruxible_client import contracts
from cruxible_core.mcp import handlers
from cruxible_core.mcp.server import create_server

COORDINATE = contracts.PlaybillAcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)
INCOMING = contracts.PlaybillSubjectIncomingGroupV1(
    predicate="sec.vuln.affects_package",
    claims=[
        contracts.PlaybillSubjectIncomingClaimV1(
            claim_identity="Claim:CLM-" + "a" * 32,
            subject_identity="subjects/sec.vuln/cve-2026-69247.json",
        )
    ],
)


class _StubClient:
    def get_playbill_subject(
        self, instance_id: str, subject_kind: str, subject_id: str
    ) -> contracts.PlaybillSubjectView:
        assert (instance_id, subject_kind, subject_id) == ("inst_mcp", "sec.package", "click")
        return contracts.PlaybillSubjectView(
            coordinate=COORDINATE,
            envelope={"identity": "Subject:sec.package/click"},
            facts=[],
            incoming=[INCOMING],
        )


def test_mcp_get_subject_returns_the_incoming_relations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handlers, "_get_client", lambda: _StubClient())

    view = handlers.handle_playbill_get_subject("inst_mcp", "sec.package", "click")

    assert view.incoming == [INCOMING]
    assert view.model_dump(mode="json")["incoming"] == [
        {
            "tag": "playbill-subject-incoming-group-v1",
            "predicate": "sec.vuln.affects_package",
            "claims": [
                {
                    "tag": "playbill-subject-incoming-claim-v1",
                    "claim_identity": "Claim:CLM-" + "a" * 32,
                    "subject_identity": "subjects/sec.vuln/cve-2026-69247.json",
                }
            ],
        }
    ]


def test_the_mcp_subject_tool_publishes_incoming_in_its_output_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "full")
    tools = {tool.name: tool for tool in asyncio.run(create_server().list_tools())}

    schema = tools["cruxible_playbill_get_subject"].outputSchema

    assert schema is not None
    assert "incoming" in schema["properties"]
