"""PB-E MCP Document registration, dispatch, permission, and custody tests."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import pytest

from cruxible_client import contracts
from cruxible_core.errors import DataValidationError
from cruxible_core.mcp import handlers
from cruxible_core.mcp.server import create_server
from cruxible_core.runtime.permissions import TOOL_PERMISSIONS, PermissionMode

PLAYBILL_DOCUMENT_TOOLS = {
    "cruxible_init",
    "cruxible_body_store",
    "cruxible_document_propose",
    "cruxible_inspect_proposal",
    "cruxible_review",
    "cruxible_prepare_approval",
    "cruxible_submit_approval",
    "cruxible_activate",
    "cruxible_source_context",
    "cruxible_source_check",
    "cruxible_propose_source_bundle",
    "cruxible_propose_principal_change",
}


def test_playbill_tools_register_without_private_key_or_local_path_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "full")
    tools = {tool.name: tool for tool in asyncio.run(create_server().list_tools())}
    assert PLAYBILL_DOCUMENT_TOOLS <= set(tools)
    approval = tools["cruxible_submit_approval"].inputSchema
    assert set(approval["properties"]) == {"instance_id", "proposal_id", "attestation"}
    source = tools["cruxible_propose_source_bundle"].inputSchema
    assert set(source["properties"]) == {
        "instance_id",
        "bundle",
        "source_name",
        "proposal_name",
        "dry_run",
        "at",
    }
    serialized = str({name: tools[name].inputSchema for name in PLAYBILL_DOCUMENT_TOOLS})
    assert "private_key" not in serialized
    assert "private_key_path" not in serialized
    assert "local_path" not in serialized


def test_playbill_permission_tiers_separate_inert_proposal_approval_and_activation() -> None:
    assert TOOL_PERMISSIONS["cruxible_body_store"] == PermissionMode.GOVERNED_WRITE
    assert TOOL_PERMISSIONS["cruxible_document_propose"] == PermissionMode.GOVERNED_WRITE
    assert TOOL_PERMISSIONS["cruxible_prepare_approval"] == PermissionMode.READ_ONLY
    assert TOOL_PERMISSIONS["cruxible_submit_approval"] == PermissionMode.GRAPH_WRITE
    assert TOOL_PERMISSIONS["cruxible_activate"] == PermissionMode.GRAPH_WRITE
    assert TOOL_PERMISSIONS["cruxible_get"] == PermissionMode.READ_ONLY
    assert TOOL_PERMISSIONS["cruxible_init"] == PermissionMode.ADMIN


def test_playbill_handlers_decode_bytes_and_submit_only_public_attestation(monkeypatch) -> None:
    calls: list[tuple[str, Any]] = []
    proposal_result = object()
    approval_result = object()

    class StubClient:
        def store_body(self, instance_id: str, content: bytes) -> contracts.CasObjectResult:
            calls.append(("store", (instance_id, content)))
            return contracts.CasObjectResult(
                digest="sha256:" + "1" * 64,
                present=True,
                byte_length=len(content),
                redacted=False,
            )

        def propose_document(self, instance_id: str, **kwargs: Any) -> Any:
            calls.append(("propose", (instance_id, kwargs)))
            return proposal_result

        def submit_approval(self, instance_id: str, proposal_id: str, **kwargs: Any) -> Any:
            calls.append(("approve", (instance_id, proposal_id, kwargs)))
            return approval_result

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())
    body = b"# MCP bytes\n"
    stored = handlers.handle_playbill_store_body("inst_1", base64.b64encode(body).decode("ascii"))
    assert stored.byte_length == len(body)
    with pytest.raises(DataValidationError, match="base64"):
        handlers.handle_playbill_store_body("inst_1", "not base64!")

    shell = {
        "tag": "playbill-document-v1",
        "identity": "document:design",
        "document_kind": "design",
        "title": "Design",
        "media_type": "text/markdown",
        "body_digest": "sha256:" + "1" * 64,
        "links": [],
        "pins": [],
        "authority": {
            "required_tier": "graph_write",
        },
        "governance_scope": ["project:playbill"],
        "predecessor_digest": None,
        "lifecycle": {"revision": 1},
    }
    assert (
        handlers.handle_playbill_propose_document("inst_1", shell, "design", None)
        is proposal_result
    )
    attestation = {
        "tag": "playbill-attest-v1",
        "signer_id": "owner",
        "signing_semantic_root": "sha256:" + "2" * 64,
        "payload_digest": "sha256:" + "3" * 64,
        "sig": "4" * 128,
    }
    assert (
        handlers.handle_playbill_submit_approval("inst_1", "sha256:" + "5" * 64, attestation)
        is approval_result
    )
    assert calls[0] == ("store", ("inst_1", body))
    approve_payload = calls[-1][1][2]
    assert approve_payload == {"attestation": attestation}
    assert "private" not in str(approve_payload)
