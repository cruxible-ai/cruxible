"""PB-E MCP explanation binds a typed subject to an accepted coordinate."""

from __future__ import annotations

import asyncio
from typing import Any

from cruxible_client import contracts
from cruxible_core.mcp import handlers
from cruxible_core.mcp.server import create_server
from tests.test_client.test_playbill_documents import COORDINATE


def test_playbill_explain_schema_has_no_mutation_or_key_inputs() -> None:
    tools = {tool.name: tool for tool in asyncio.run(create_server().list_tools())}
    schema = tools["cruxible_playbill_explain"].inputSchema
    assert set(schema["properties"]) == {
        "instance_id",
        "subject",
        "at",
        "detail",
        "include_body",
    }
    serialized = str(schema)
    for forbidden in ("private_key", "local_path", "proposal", "activate"):
        assert forbidden not in serialized


def test_playbill_explain_handler_forwards_exact_validated_values(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    class StubClient:
        def explain_playbill_subject(self, instance_id: str, **kwargs: Any) -> Any:
            calls.append({"instance_id": instance_id, **kwargs})
            return contracts.PlaybillExplainUnsupportedDetail(
                tag="playbill-explain-unsupported-detail-v1",
                subject=kwargs["subject"],
                coordinate=contracts.PlaybillAcceptedCoordinate.model_validate(COORDINATE),
                requested_detail="proof",
                code="playbill.explain.detail_unsupported",
                message="deferred",
                supported_details=["summary", "evidence"],
            )

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())
    subject = {
        "tag": "playbill-semantic-address-v1",
        "artifact_path": "documents/design.yaml",
        "selector": {"scheme": "artifact-v1", "value": ""},
    }
    result = handlers.handle_playbill_explain(
        "inst_1",
        subject,
        COORDINATE,
        detail="proof",
        include_body=False,
    )
    assert isinstance(result, contracts.PlaybillExplainUnsupportedDetail)
    assert calls == [
        {
            "instance_id": "inst_1",
            "subject": subject,
            "at": COORDINATE,
            "detail": "proof",
            "include_body": False,
        }
    ]
