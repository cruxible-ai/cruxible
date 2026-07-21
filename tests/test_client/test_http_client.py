"""Tests for the HTTP client."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from cruxible_client import CruxibleClient, contracts
from cruxible_client.errors import (
    AuthenticationError,
    ConstraintViolationError,
    DataValidationError,
    RuntimeCredentialNotFoundError,
)


def _build_client(handler):
    transport = httpx.MockTransport(handler)
    client = CruxibleClient(base_url="http://cruxible")
    client._client = httpx.Client(base_url="http://cruxible", transport=transport)  # type: ignore[attr-defined]
    return client


def test_successful_call_returns_contract_model():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "instance_id": "inst_123",
                "status": "initialized",
                "warnings": [],
            },
        )

    client = _build_client(handler)
    result = client.init("/srv/project", config_yaml="name: demo")
    assert result.instance_id == "inst_123"
    assert result.status == "initialized"


def test_init_serializes_kit():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "instance_id": "inst_123",
                "status": "initialized",
                "warnings": [],
            },
        )

    client = _build_client(handler)
    result = client.init("/srv/project", kits=["kev-reference"])

    assert result.instance_id == "inst_123"
    assert captured["payload"]["kits"] == ["kev-reference"]
    assert captured["payload"]["config_yaml"] is None
    assert "bare" not in captured["payload"]


def test_init_serializes_explicit_bare_opt_out():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "instance_id": "inst_123",
                "status": "initialized",
                "warnings": [],
                "base_kit_id": None,
            },
        )

    client = _build_client(handler)
    client.init("/srv/project", kits=["kev-reference"], bare=True)

    assert captured["payload"]["bare"] is True


def test_error_response_rehydrates_correct_exception():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "error_type": "ConstraintViolationError",
                "message": "constraint failed",
                "errors": [],
                "context": {"violations": ["mismatch"]},
                "mutation_receipt_id": "RCPT-1",
            },
        )

    client = _build_client(handler)
    with pytest.raises(ConstraintViolationError) as exc_info:
        client.query("inst_123", "parts_for_vehicle")

    assert exc_info.value.violations == ["mismatch"]
    assert exc_info.value.mutation_receipt_id == "RCPT-1"


def test_validation_error_preserves_errors_list():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error_type": "DataValidationError",
                "message": "bad data",
                "errors": ["wrong type"],
                "context": {},
                "mutation_receipt_id": None,
            },
        )

    client = _build_client(handler)
    with pytest.raises(DataValidationError) as exc_info:
        client.query("inst_123", "parts_for_vehicle")

    assert exc_info.value.errors == ["wrong type"]


def test_query_inline_uses_expected_route_and_payload():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "items": [],
                "receipt_id": "RCP-inline",
                "receipt": None,
                "total": 0,
                "limit": 50,
                "truncated": False,
                "steps_executed": 0,
            },
        )

    client = _build_client(handler)
    result = client.query_inline(
        "inst_123",
        contracts.InlineQueryDefinition(
            name="brake_parts",
            mode="collection",
            returns="Part",
            result_shape="entity",
            where={"result.properties.category": {"eq": "brakes"}},
        ),
        {},
        relationship_state="reviewable",
        decision_record_id="DR-1",
    )

    assert result.receipt_id == "RCP-inline"
    assert captured == {
        "path": "/api/v1/inst_123/queries/run-inline",
        "payload": {
            "definition": {
                "name": "brake_parts",
                "mode": "collection",
                "traversal": [],
                "returns": "Part",
                "result_shape": "entity",
                "relationship_state": "live",
                "allow_relationship_state_override": False,
                "where": {"result.properties.category": {"eq": "brakes"}},
                "order_by": [],
                "include": {},
            },
            "params": {},
            "limit": None,
            "relationship_state": "reviewable",
            "decision_record_id": "DR-1",
        },
    }


def test_view_uses_expected_route_and_query_string():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "items": [],
                "receipt_id": "RCP-view",
                "receipt": None,
                "total": 0,
                "limit": 25,
                "offset": 25,
                "truncated": False,
                "steps_executed": 0,
            },
        )

    client = _build_client(handler)
    result = client.view(
        "inst_123",
        "review_queue",
        params={"work_item_id": "wi-1"},
        limit=25,
        offset=25,
        relationship_state="reviewable",
    )

    assert result.receipt_id == "RCP-view"
    assert result.offset == 25
    assert captured == {
        "path": "/api/v1/inst_123/views/review_queue",
        "params": {
            "work_item_id": "wi-1",
            "limit": "25",
            "offset": "25",
            "relationship_state": "reviewable",
        },
    }


def test_view_rejects_reserved_param_keys():
    client = _build_client(lambda request: httpx.Response(500))

    with pytest.raises(ValueError, match="reserved view keys"):
        client.view("inst_123", "review_queue", params={"limit": "10"})


def test_gate_check_uses_expected_route_and_payload():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "gate_name": "merge-review",
                "kind": "git-pre-push",
                "candidates": ["abc"],
                "candidate_outcomes": [],
                "verdict": "error",
                "reason": "malformed pre-push stdin",
                "instance_id": "inst_123",
                "read_revision": 7,
                "receipt_id": "RCP-gate",
            },
        )

    client = _build_client(handler)
    result = client.gate_check(
        "inst_123",
        "merge-review",
        ["abc"],
        error_reason="malformed pre-push stdin",
    )

    assert result.verdict == "error"
    assert result.receipt_id == "RCP-gate"
    assert captured == {
        "path": "/api/v1/inst_123/gates/merge-review/check",
        "payload": {
            "candidates": ["abc"],
            "error_reason": "malformed pre-push stdin",
        },
    }


def test_query_sends_offset_in_payload():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "items": [],
                "receipt_id": None,
                "receipt": None,
                "total": 0,
                "limit": 10,
                "offset": 10,
                "truncated": False,
                "steps_executed": 0,
            },
        )

    client = _build_client(handler)
    client.query("inst_123", "parts_for_vehicle", {"vehicle_id": "V-1"}, limit=10, offset=10)

    assert captured["payload"]["offset"] == 10
    assert captured["payload"]["limit"] == 10
    # `layout` is opt-in: omitted from the payload unless requested.
    assert "layout" not in captured["payload"]


def test_query_graph_layout_sends_param_and_parses_graph_model():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "layout": "graph",
                "nodes": [
                    {
                        "entity_type": "Vehicle",
                        "entity_id": "V-1",
                        "properties": {"make": "Honda"},
                        "metadata": {},
                    },
                    {
                        "entity_type": "Part",
                        "entity_id": "P-1",
                        "properties": {"name": "Pads"},
                        "metadata": {},
                    },
                ],
                "edges": [
                    {
                        "relationship_type": "fits",
                        "from_type": "Part",
                        "from_id": "P-1",
                        "to_type": "Vehicle",
                        "to_id": "V-1",
                        "edge_key": None,
                        "properties": {"verified": True},
                        "metadata": {},
                    }
                ],
                "results": [{"entry": 0, "result": 1, "paths": [0], "includes": {}}],
                "paths": [[{"edge": 0, "alias": "fit"}]],
                "receipt_id": "RCP-1",
                "receipt": None,
                "total": 1,
                "limit": None,
                "offset": 0,
                "truncated": False,
                "steps_executed": 1,
                "result_shape": "path",
                "dedupe": "path",
            },
        )

    client = _build_client(handler)
    result = client.query("inst_123", "parts_for_vehicle", {"vehicle_id": "V-1"}, layout="graph")

    assert captured["payload"]["layout"] == "graph"
    assert isinstance(result, contracts.QueryGraphToolResult)
    assert result.layout == "graph"
    assert [node.entity_id for node in result.nodes] == ["V-1", "P-1"]
    assert result.edges[0].edge_key is None
    ref = result.results[0]
    assert isinstance(ref, contracts.QueryGraphPathRef)
    assert (ref.entry, ref.result, ref.paths) == (0, 1, [0])
    assert result.paths == [[contracts.QueryGraphPathStepRef(edge=0, alias="fit")]]


def test_query_inline_graph_layout_sends_param_and_parses_graph_model():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "layout": "graph",
                "nodes": [],
                "edges": [],
                "results": [],
                "paths": [],
                "receipt_id": None,
                "receipt": None,
                "total": 0,
                "steps_executed": 0,
            },
        )

    client = _build_client(handler)
    definition = contracts.InlineQueryDefinition(
        name="q",
        mode="collection",
        returns="Part",
        result_shape="entity",
    )
    result = client.query_inline("inst_123", definition, layout="graph")

    assert captured["payload"]["layout"] == "graph"
    assert isinstance(result, contracts.QueryGraphToolResult)
    assert result.results == []


def test_batch_direct_write_uses_expected_route_and_payload():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "dry_run": True,
                "valid": True,
                "entities_added": 1,
                "entities_updated": 0,
                "relationships_added": 0,
                "relationships_updated": 0,
                "validation_errors": [],
                "validation_warnings": [],
                "evidence_sources_used": [],
                "pending_conflicts": [
                    {
                        "relationship_type": "fits",
                        "from_type": "Part",
                        "from_id": "BP-1",
                        "to_type": "Vehicle",
                        "to_id": "V-1",
                        "group_id": "GRP-pending",
                        "group_status": "pending_review",
                        "group_signature": "sig-pending",
                        "source_workflow_name": "wf",
                        "edge_key": None,
                    }
                ],
                "updated_group_backed_edges": [],
                "receipt_id": None,
            },
        )

    client = _build_client(handler)
    result = client.batch_direct_write(
        "inst_123",
        contracts.BatchDirectWritePayload(
            entities=[
                contracts.EntityInput(
                    entity_type="Vehicle",
                    entity_id="V-BATCH",
                    properties={"vehicle_id": "V-BATCH"},
                )
            ]
        ),
        dry_run=True,
    )

    assert result.valid is True
    assert result.pending_conflicts[0].group_id == "GRP-pending"
    assert captured == {
        "path": "/api/v1/inst_123/direct-writes/batch",
        "payload": {
            "payload": {
                "entities": [
                    {
                        "entity_type": "Vehicle",
                        "entity_id": "V-BATCH",
                        "properties": {"vehicle_id": "V-BATCH"},
                        "metadata": {},
                        "lifecycle": None,
                    }
                ],
                "relationships": [],
                "shared_evidence": {},
            },
            "dry_run": True,
        },
    }


def test_trace_methods_call_trace_routes():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        if request.url.path.endswith("/TRC-1"):
            return httpx.Response(200, json={"trace_id": "TRC-1", "workflow_name": "wf"})
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "trace_id": "TRC-1",
                        "workflow_name": "wf",
                        "provider_name": "provider",
                    }
                ],
                "total": 1,
            },
        )

    client = _build_client(handler)

    trace = client.get_trace("inst_123", "TRC-1")
    listed = client.list_traces(
        "inst_123",
        workflow_name="wf",
        provider_name="provider",
        limit=25,
        offset=5,
    )

    assert trace["trace_id"] == "TRC-1"
    assert listed.items[0]["provider_name"] == "provider"
    assert seen[0] == ("GET", "http://cruxible/api/v1/inst_123/traces/TRC-1")
    expected_url = (
        "http://cruxible/api/v1/inst_123/traces?"
        "workflow_name=wf&provider_name=provider&limit=25&offset=5"
    )
    assert seen[1][1] == expected_url


def test_explain_receipt_calls_explain_route():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        return httpx.Response(
            200,
            json={
                "receipt_id": "RCP-1",
                "format": "mermaid",
                "content": "graph TD\n",
            },
        )

    client = _build_client(handler)

    result = client.explain_receipt("inst_123", "RCP-1", format="mermaid")

    assert result.receipt_id == "RCP-1"
    assert result.format == "mermaid"
    assert result.content == "graph TD\n"
    assert seen == [
        (
            "GET",
            "http://cruxible/api/v1/inst_123/receipts/RCP-1/explain?format=mermaid",
        )
    ]


def test_paginated_client_methods_serialize_offset():
    captured: dict[str, dict[str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured[request.url.path] = dict(request.url.params)
        return httpx.Response(200, json={"items": [], "total": 0})

    client = _build_client(handler)

    client.list("inst_123", resource_type="entities", limit=10, offset=3)
    client.list_decision_records("inst_123", limit=10, offset=4)
    client.list_decision_events("inst_123", limit=10, offset=5)
    client.list_queries("inst_123", limit=10, offset=6)
    client.list_snapshots("inst_123", limit=10, offset=7)
    client.list_groups("inst_123", limit=10, offset=8)
    client.list_resolutions("inst_123", limit=10, offset=9)

    assert captured["/api/v1/inst_123/list/entities"]["offset"] == "3"
    assert captured["/api/v1/inst_123/decision-records"]["offset"] == "4"
    assert captured["/api/v1/inst_123/decision-records/events"]["offset"] == "5"
    assert captured["/api/v1/inst_123/queries"]["offset"] == "6"
    assert captured["/api/v1/inst_123/snapshots"]["offset"] == "7"
    assert captured["/api/v1/inst_123/groups"]["offset"] == "8"
    assert captured["/api/v1/inst_123/resolutions"]["offset"] == "9"


def test_list_queries_detail_branches_parse_typed_models():
    summary_item = {
        "name": "parts_for_vehicle",
        "description": "Find compatible parts.",
        "mode": "traversal",
        "entry_point": "Vehicle",
        "returns": "Part",
        "result_shape": "path",
        "required_params": ["vehicle_id"],
        "allow_relationship_state_override": False,
    }
    full_item = {
        **summary_item,
        "dedupe": "path",
        "relationship_state": "live",
        "select": None,
        "order_by": [],
        "include": {},
        "limit": None,
        "max_paths": None,
        "max_paths_per_result": None,
        "example_ids": ["V-1"],
    }
    captured: dict[str, dict[str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        item = full_item if request.url.params.get("detail") == "full" else summary_item
        return httpx.Response(200, json={"items": [item], "total": 1})

    client = _build_client(handler)

    summary = client.list_queries("inst_123")
    assert captured["params"]["detail"] == "summary"
    assert isinstance(summary, contracts.QueryListResult)
    assert isinstance(summary.items[0], contracts.QueryDefinitionSummary)

    full = client.list_queries("inst_123", detail="full")
    assert captured["params"]["detail"] == "full"
    assert isinstance(full, contracts.QueryListDetailResult)
    assert isinstance(full.items[0], contracts.NamedQueryInfoResult)
    assert full.items[0].example_ids == ["V-1"]


def test_entity_list_and_sample_serialize_projection_fields():
    captured: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured[request.url.path] = request.url.params.get_list("fields")
        if request.url.path.endswith("/sample/Part"):
            return httpx.Response(200, json={"items": [], "entity_type": "Part", "total": 0})
        return httpx.Response(200, json={"items": [], "total": 0})

    client = _build_client(handler)

    client.list(
        "inst_123",
        resource_type="entities",
        entity_type="Part",
        fields=["name", "category"],
    )
    client.sample("inst_123", "Part", fields=["name"])

    assert captured["/api/v1/inst_123/list/entities"] == ["name", "category"]
    assert captured["/api/v1/inst_123/sample/Part"] == ["name"]


def test_list_serializes_where_filter():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"items": [], "total": 0})

    client = _build_client(handler)

    client.list(
        "inst_123",
        resource_type="entities",
        entity_type="Part",
        where={"name": {"contains": "Brake"}},
    )

    assert json.loads(captured["where"]) == {"name": {"contains": "Brake"}}
    assert "property_filter" not in captured


def test_feedback_from_query_uses_expected_route_and_payload():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "feedback_id": "FB-1",
                "applied": True,
                "receipt_id": "RCP-FB-1",
            },
        )

    client = _build_client(handler)
    result = client.feedback_from_query(
        "inst_123",
        receipt_id="RCP-QUERY-1",
        result_index=2,
        action="reject",
        source="agent",
        reason="stale evidence",
        reason_code="vendor_mismatch",
        scope_hints={"vendor": "acme"},
        group_override=True,
        path_alias="exposure",
    )

    assert result.feedback_id == "FB-1"
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/inst_123/feedback/from-query"
    assert captured["payload"] == {
        "receipt_id": "RCP-QUERY-1",
        "result_index": 2,
        "action": "reject",
        "source": "agent",
        "reason": "stale evidence",
        "reason_code": "vendor_mismatch",
        "scope_hints": {"vendor": "acme"},
        "corrections": None,
        "group_override": True,
        "path_index": None,
        "path_alias": "exposure",
    }


def test_source_artifact_methods_use_expected_routes_and_payloads():
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {
                "method": request.method,
                "path": request.url.path,
                "payload": json.loads(request.content.decode()),
            }
        )
        if request.url.path.endswith("/source-artifacts/register"):
            return httpx.Response(
                200,
                json={
                    "source_artifact_id": "SRC-1",
                    "source_kind": "markdown",
                    "source_retention": "archive",
                    "content_hash": "sha256:abc",
                    "byte_count": 10,
                    "parser_version": "markdown_chunks_v1",
                    "archived": True,
                    "archive_content_hash": "sha256:abc",
                    "chunks": [],
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "available",
                "source_artifact_id": "SRC-1",
                "chunk_id": "mdchunk_1",
                "content_hash": "sha256:def",
                "expected_artifact_hash": "sha256:abc",
                "body_origin": "archive",
                "body": "source text",
            },
        )

    client = _build_client(handler)
    registered = client.register_source_artifact(
        "inst_123",
        source_path="docs/evidence.md",
        source_retention="archive",
        original_uri="docs/evidence.md",
        label="Evidence",
    )
    dereferenced = client.dereference_source_evidence(
        "inst_123",
        source_artifact_id=registered.source_artifact_id,
        heading_path=["Evidence"],
        block_selector="paragraph:1",
        expected_content_hash="sha256:def",
    )

    assert registered.archived is True
    assert dereferenced.body == "source text"
    assert captured[0] == {
        "method": "POST",
        "path": "/api/v1/inst_123/source-artifacts/register",
        "payload": {
            "source_path": "docs/evidence.md",
            "source_kind": "markdown",
            "source_retention": "archive",
            "original_uri": "docs/evidence.md",
            "label": "Evidence",
        },
    }
    assert captured[1] == {
        "method": "POST",
        "path": "/api/v1/inst_123/source-evidence/dereference",
        "payload": {
            "source_artifact_id": "SRC-1",
            "chunk_id": None,
            "heading_path": ["Evidence"],
            "block_selector": "paragraph:1",
            "expected_content_hash": "sha256:def",
        },
    }


def test_source_artifact_read_methods_use_expected_routes_and_params():
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {
                "method": request.method,
                "path": request.url.path,
                "params": dict(request.url.params),
            }
        )
        if request.url.path.endswith("/source-artifacts"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "source_artifact_id": "SRC-1",
                            "kind": "markdown",
                            "retention": "archive",
                            "original_uri": "docs/evidence.md",
                            "label": "Evidence",
                            "content_hash": "sha256:abc",
                            "registered_at": "2026-06-05T12:00:00Z",
                            "chunk_count": 1,
                            "byte_count": 10,
                        }
                    ],
                    "total": 1,
                    "limit": 10,
                    "offset": 4,
                    "truncated": False,
                },
            )
        return httpx.Response(
            200,
            json={
                "source_artifact_id": "SRC-1",
                "kind": "markdown",
                "retention": "archive",
                "original_uri": "docs/evidence.md",
                "label": "Evidence",
                "content_hash": "sha256:abc",
                "registered_at": "2026-06-05T12:00:00Z",
                "chunk_count": 1,
                "byte_count": 10,
                "parser_version": "markdown_chunks_v1",
                "archived": True,
                "archive_content_hash": "sha256:abc",
                "content_available": True,
                "body_origin": "archive",
                "current_artifact_hash": "sha256:abc",
                "chunks": [
                    {
                        "chunk_id": "mdchunk_1",
                        "heading_path": ["Evidence"],
                        "block_selector": "paragraph:1",
                        "block_type": "paragraph",
                        "line_start": 3,
                        "line_end": 3,
                        "content_hash": "sha256:def",
                        "text": "source text",
                    }
                ],
            },
        )

    client = _build_client(handler)
    listed = client.list_source_artifacts("inst_123", limit=10, offset=4)
    detail = client.get_source_artifact("inst_123", "SRC-1")

    assert listed.items[0].source_artifact_id == "SRC-1"
    assert listed.items[0].kind == "markdown"
    assert detail.content_available is True
    assert detail.chunks[0].text == "source text"
    assert captured == [
        {
            "method": "GET",
            "path": "/api/v1/inst_123/source-artifacts",
            "params": {"offset": "4", "limit": "10"},
        },
        {
            "method": "GET",
            "path": "/api/v1/inst_123/source-artifacts/SRC-1",
            "params": {},
        },
    ]


def test_add_relationships_serializes_evidence_fields():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "added": 1,
                "updated": 0,
                "pending_conflicts": [],
                "updated_group_backed_edges": [
                    {
                        "relationship_type": "fits",
                        "from_type": "Part",
                        "from_id": "BP-1",
                        "to_type": "Vehicle",
                        "to_id": "V-1",
                        "group_id": "GRP-resolved",
                        "group_status": "resolved",
                        "group_signature": "sig-resolved",
                        "source_workflow_name": "wf",
                        "edge_key": 7,
                    }
                ],
                "receipt_id": "RCP-add",
            },
        )

    client = _build_client(handler)
    result = client.add_relationships(
        "inst_123",
        [
            contracts.RelationshipInput(
                from_type="Part",
                from_id="BP-1",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-1",
                properties={"verified": True},
                pending=True,
                evidence_refs=[
                    contracts.EvidenceRef(
                        source="roadmap_doc",
                        source_record_id="section-p0",
                    )
                ],
                source_evidence=[
                    contracts.SourceEvidenceInput(
                        source_artifact_id="SRC-1",
                        chunk_id="CHK-1",
                    )
                ],
                evidence_rationale="Accepted direct source-backed assertion.",
            )
        ],
    )

    assert result.added == 1
    assert result.updated_group_backed_edges[0].group_id == "GRP-resolved"
    assert result.updated_group_backed_edges[0].edge_key == 7
    assert captured == {
        "path": "/api/v1/inst_123/relationships",
        "payload": {
            "relationships": [
                {
                    "from_type": "Part",
                    "from_id": "BP-1",
                    "relationship_type": "fits",
                    "to_type": "Vehicle",
                    "to_id": "V-1",
                    "properties": {"verified": True},
                    "pending": True,
                    "evidence_refs": [
                        {
                            "source": "roadmap_doc",
                            "source_record_id": "section-p0",
                            "artifact_id": None,
                            "table": None,
                            "row_index": None,
                            "label": None,
                            "metadata": {},
                        }
                    ],
                    "source_evidence": [
                        {
                            "source_artifact_id": "SRC-1",
                            "chunk_id": "CHK-1",
                            "heading_path": None,
                            "block_selector": None,
                            "label": None,
                            "expected_content_hash": None,
                        }
                    ],
                    "evidence_rationale": "Accepted direct source-backed assertion.",
                    "lifecycle": None,
                }
            ],
            "dry_run": False,
        },
    }


def test_client_includes_bearer_token_header_when_configured():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "instance_id": "inst_123",
                "status": "initialized",
                "warnings": [],
            },
        )

    transport = httpx.MockTransport(handler)
    client = CruxibleClient(base_url="http://cruxible", token="local-secret")
    client._client.close()  # type: ignore[attr-defined]
    client._client = httpx.Client(  # type: ignore[attr-defined]
        base_url="http://cruxible",
        headers={"Authorization": "Bearer local-secret"},
        transport=transport,
    )

    result = client.init("/srv/project", config_yaml="name: demo")

    assert result.instance_id == "inst_123"
    assert captured["authorization"] == "Bearer local-secret"


def test_workflow_propose_uses_expected_route():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = str(request.url)
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "workflow": "wf",
                "output": {"members": []},
                "receipt_id": "RCP-1",
                "group_id": "GRP-1",
                "group_status": "pending_review",
                "review_priority": "review",
                "query_receipt_ids": [],
                "read_metadata": {"any_read_truncated": False},
                "trace_ids": ["TRC-1"],
                "prior_resolution": None,
                "receipt": None,
                "traces": [],
            },
        )

    client = _build_client(handler)
    result = client.propose_workflow("inst_123", workflow_name="wf", input_payload={"id": "1"})
    assert result.group_id == "GRP-1"
    assert result.read_metadata == {"any_read_truncated": False}
    assert captured["path"].endswith("/api/v1/inst_123/workflows/propose")
    assert captured["payload"]["workflow_name"] == "wf"


def test_decision_record_client_routes_round_trip():
    captured: list[tuple[str, str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        payload = json.loads(request.content.decode()) if request.content else None
        captured.append((request.method, path, payload))
        record = {
            "decision_record_id": "DR-1",
            "question": "Should we act?",
            "subject_type": "Incident",
            "subject_id": "I-1",
            "status": "open",
        }
        if request.method == "GET" and path.endswith("/decision-records"):
            return httpx.Response(200, json={"items": [record], "total": 1})
        if request.method == "GET" and path.endswith("/decision-records/events"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "decision_event_id": "DE-1",
                            "decision_record_id": "DR-1",
                            "sequence": 1,
                            "command": "query:q",
                            "status": "success",
                        }
                    ],
                    "total": 1,
                },
            )
        return httpx.Response(200, json={"record": record, "events": []})

    client = _build_client(handler)
    created = client.create_decision_record(
        "inst_123",
        question="Should we act?",
        subject_type="Incident",
        subject_id="I-1",
        opened_by="agent",
    )
    fetched = client.get_decision_record("inst_123", "DR-1", include_events=False)
    listed = client.list_decision_records("inst_123", status="open", subject_type="Incident")
    events = client.list_decision_events("inst_123", decision_record_id="DR-1")
    finalized = client.finalize_decision_record(
        "inst_123",
        "DR-1",
        final_decision="Act",
        decision_class="recommended",
        rationale="Evidence supports it",
    )
    abandoned = client.abandon_decision_record("inst_123", "DR-2", reason="Superseded")

    assert created.record["decision_record_id"] == "DR-1"
    assert fetched.record["decision_record_id"] == "DR-1"
    assert listed.items[0]["decision_record_id"] == "DR-1"
    assert events.items[0]["decision_record_id"] == "DR-1"
    assert finalized.record["decision_record_id"] == "DR-1"
    assert abandoned.record["decision_record_id"] == "DR-1"
    assert captured[0] == (
        "POST",
        "/api/v1/inst_123/decision-records",
        {
            "question": "Should we act?",
            "subject_type": "Incident",
            "subject_id": "I-1",
            "opened_by": "agent",
        },
    )
    assert captured[1][0:2] == ("GET", "/api/v1/inst_123/decision-records/DR-1")
    assert captured[2][0:2] == ("GET", "/api/v1/inst_123/decision-records")
    assert captured[3][0:2] == ("GET", "/api/v1/inst_123/decision-records/events")
    assert captured[4] == (
        "POST",
        "/api/v1/inst_123/decision-records/DR-1/finalize",
        {
            "final_decision": "Act",
            "decision_class": "recommended",
            "rationale": "Evidence supports it",
        },
    )
    assert captured[5] == (
        "POST",
        "/api/v1/inst_123/decision-records/DR-2/abandon",
        {"reason": "Superseded"},
    )


def test_decision_record_id_is_sent_on_query_and_workflow_requests():
    captured: list[tuple[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        payload = json.loads(request.content.decode())
        captured.append((path, payload))
        if path.endswith("/queries/run"):
            return httpx.Response(
                200,
                json={
                    "items": [],
                    "receipt_id": "RCP-query",
                    "receipt": None,
                    "total": 0,
                    "truncated": False,
                    "steps_executed": 1,
                    "policy_summary": {},
                },
            )
        if path.endswith("/workflows/propose"):
            return httpx.Response(
                200,
                json={
                    "workflow": "wf",
                    "output": {},
                    "receipt_id": "RCP-propose",
                    "mode": "proposal",
                    "workflow_type": "proposal",
                    "canonical": False,
                    "group_id": None,
                    "group_status": "suppressed",
                    "review_priority": "review",
                    "query_receipt_ids": [],
                    "read_metadata": {"any_read_truncated": False},
                    "trace_ids": [],
                    "prior_resolution": None,
                    "policy_summary": {},
                    "receipt": None,
                    "traces": [],
                },
            )
        return httpx.Response(
            200,
            json={
                "workflow": "wf",
                "output": {},
                "receipt_id": "RCP-workflow",
                "mode": "run" if path.endswith("/workflows/run") else "apply",
                "workflow_type": "utility" if path.endswith("/workflows/run") else "canonical",
                "canonical": path.endswith("/workflows/apply"),
                "apply_digest": "sha256:abc",
                "head_snapshot_id": "snap_1",
                "committed_snapshot_id": None,
                "apply_previews": {},
                "query_receipt_ids": [],
                "read_metadata": {"any_read_truncated": False},
                "trace_ids": [],
                "receipt": None,
                "traces": [],
            },
        )

    client = _build_client(handler)
    client.query("inst_123", "q", {}, decision_record_id="DR-1")
    run_result = client.workflow_run("inst_123", workflow_name="wf", decision_record_id="DR-1")
    apply_result = client.workflow_apply(
        "inst_123",
        workflow_name="wf",
        expected_apply_digest="sha256:abc",
        expected_head_snapshot_id="snap_1",
        decision_record_id="DR-1",
    )
    propose_result = client.propose_workflow(
        "inst_123", workflow_name="wf", decision_record_id="DR-1"
    )

    assert run_result.read_metadata == {"any_read_truncated": False}
    assert apply_result.read_metadata == {"any_read_truncated": False}
    assert propose_result.read_metadata == {"any_read_truncated": False}

    assert [payload["decision_record_id"] for _, payload in captured] == [
        "DR-1",
        "DR-1",
        "DR-1",
        "DR-1",
    ]


def test_inspect_view_uses_expected_route():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = str(request.url)
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={"view": "ontology", "payload": {"entity_count": 2}},
        )

    client = _build_client(handler)
    result = client.inspect_view("inst_123", "ontology", limit=25)

    assert result.view == "ontology"
    assert result.payload["entity_count"] == 2
    assert captured["path"].endswith("/api/v1/inst_123/inspect/ontology?limit=25")
    assert captured["params"] == {"limit": "25"}


def test_workflow_apply_uses_expected_route():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = str(request.url)
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "workflow": "wf",
                "output": {"total_results": 1},
                "receipt_id": "RCP-2",
                "mode": "apply",
                "workflow_type": "canonical",
                "canonical": True,
                "apply_digest": "sha256:abc",
                "head_snapshot_id": None,
                "committed_snapshot_id": "snap_2",
                "apply_previews": {},
                "query_receipt_ids": [],
                "read_metadata": {"any_read_truncated": False},
                "trace_ids": ["TRC-2"],
                "receipt": None,
                "traces": [],
            },
        )

    client = _build_client(handler)
    result = client.workflow_apply(
        "inst_123",
        workflow_name="wf",
        expected_apply_digest="sha256:abc",
        expected_head_snapshot_id=None,
        input_payload={"id": "1"},
    )
    assert result.committed_snapshot_id == "snap_2"
    assert result.read_metadata == {"any_read_truncated": False}
    assert captured["path"].endswith("/api/v1/inst_123/workflows/apply")
    assert captured["payload"]["workflow_name"] == "wf"
    assert captured["payload"]["expected_apply_digest"] == "sha256:abc"


def test_workflow_lock_sends_force_flag():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = str(request.url)
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "lock_path": "/tmp/cruxible.lock.yaml",
                "config_digest": "sha256:cfg",
                "providers_locked": 1,
                "artifacts_locked": 1,
            },
        )

    client = _build_client(handler)
    result = client.workflow_lock("inst_123", force=True)

    assert result.lock_path == "/tmp/cruxible.lock.yaml"
    assert captured["path"].endswith("/api/v1/inst_123/workflows/lock")
    assert captured["payload"] == {"force": True}


def test_resolve_group_sends_expected_pending_version():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = str(request.url)
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "group_id": "GRP-1",
                "action": "approve",
                "edges_created": 1,
                "edges_skipped": 0,
                "resolution_id": "RES-1",
                "receipt_id": "RCPT-1",
            },
        )

    client = _build_client(handler)
    result = client.resolve_group(
        "inst_123",
        "GRP-1",
        action="approve",
        rationale="looks good",
        expected_pending_version=3,
    )

    assert result.group_id == "GRP-1"
    assert captured["path"].endswith("/api/v1/inst_123/groups/GRP-1/resolve")
    assert captured["payload"]["expected_pending_version"] == 3
    assert captured["payload"]["action"] == "approve"


def test_get_group_preserves_review_payload():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "group": {"group_id": "GRP-1", "status": "pending_review"},
                "members": [{"from_id": "BP-1", "to_id": "V-1"}],
                "resolution": None,
                "bucket_status": {
                    "signature": "sigv1:abc",
                    "relationship_type": "fits",
                    "thesis_text": "",
                    "thesis_facts": {},
                    "latest_trust_status": None,
                    "accepted_tuple_count": 0,
                    "pending_delta_count": 1,
                    "pending_group_id": "GRP-1",
                    "pending_version": 1,
                    "latest_approved_resolution_id": None,
                    "approved_history": [],
                },
                "member_review": [
                    {
                        "proposed_tuple": {"from_id": "BP-1"},
                        "proposed_properties": {},
                        "current_edge_count": 0,
                        "property_delta": {
                            "added": [],
                            "removed": [],
                            "changed": [],
                            "unchanged": [],
                        },
                    }
                ],
            },
        )

    client = _build_client(handler)
    result = client.get_group("inst_123", "GRP-1")

    assert result.bucket_status is not None
    assert result.bucket_status["pending_group_id"] == "GRP-1"
    assert result.member_review[0]["current_edge_count"] == 0


def test_get_relationship_lineage_uses_expected_route():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "found": True,
                "relationship": {
                    "relationship_type": "fits",
                    "metadata": {
                        "assertion": {
                            "review": {"status": "approved", "source": "group"},
                        },
                    },
                },
                "provenance": {"source_ref": "group:GRP-1"},
                "group": {"group_id": "GRP-1"},
                "resolution": {"resolution_id": "RES-1"},
                "source_workflow_receipt_id": "RCP-1",
                "source_trace_ids": ["TRC-1"],
                "warnings": [],
            },
        )

    client = _build_client(handler)
    result = client.get_relationship_lineage(
        "inst_123",
        from_type="Part",
        from_id="BP-1",
        relationship_type="fits",
        to_type="Vehicle",
        to_id="V-1",
        edge_key=7,
    )

    assert result.provenance == {"source_ref": "group:GRP-1"}
    assert "assertion" not in result.model_dump()
    assert result.relationship is not None
    assert result.relationship["metadata"]["assertion"]["review"] == {
        "status": "approved",
        "source": "group",
    }
    assert result.group == {"group_id": "GRP-1"}
    assert "relationships/lineage" in captured["path"]
    assert "edge_key=7" in captured["path"]


def test_relationship_lookup_omits_none_edge_key_query_param():
    captured: dict[str, dict[str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured[request.url.path] = dict(request.url.params)
        if request.url.path.endswith("/relationships/lookup"):
            return httpx.Response(
                200,
                json={
                    "found": True,
                    "from_type": "Part",
                    "from_id": "BP-1",
                    "relationship_type": "fits",
                    "to_type": "Vehicle",
                    "to_id": "V-1",
                    "edge_key": None,
                    "properties": {},
                    "metadata": {},
                },
            )
        return httpx.Response(
            200,
            json={
                "found": True,
                "relationship": {"relationship_type": "fits"},
                "provenance": None,
                "group": None,
                "resolution": None,
                "source_workflow_receipt_id": None,
                "source_trace_ids": [],
                "warnings": [],
            },
        )

    client = _build_client(handler)
    lookup = client.get_relationship(
        "inst_123",
        from_type="Part",
        from_id="BP-1",
        relationship_type="fits",
        to_type="Vehicle",
        to_id="V-1",
        edge_key=None,
    )
    lineage = client.get_relationship_lineage(
        "inst_123",
        from_type="Part",
        from_id="BP-1",
        relationship_type="fits",
        to_type="Vehicle",
        to_id="V-1",
        edge_key=None,
    )

    assert lookup.found is True
    assert lineage.found is True
    assert captured["/api/v1/inst_123/relationships/lookup"] == {
        "from_type": "Part",
        "from_id": "BP-1",
        "relationship_type": "fits",
        "to_type": "Vehicle",
        "to_id": "V-1",
    }
    assert captured["/api/v1/inst_123/relationships/lineage"] == {
        "from_type": "Part",
        "from_id": "BP-1",
        "relationship_type": "fits",
        "to_type": "Vehicle",
        "to_id": "V-1",
    }


def test_get_relationship_preserves_provenance_and_evidence_with_newer_metadata():
    """Regression: a version-skewed client must NOT silently null audit data.

    ``GetRelationshipResult.metadata`` is a free-form ``dict[str, Any]`` pass-through,
    so a newer server's relationship metadata -- populated provenance
    (source/source_ref/created_at) and evidence, alongside metadata keys the client
    predates -- must survive parsing AND round-trip unchanged. Unknown sibling keys
    must never cause the known provenance/evidence blocks to be dropped or nulled.

    Root-cause history (wi-provenance-null-investigation): the original bug lived in a
    client whose contract model parsed provenance/evidence into TYPED sub-fields that
    nulled out when newer keys appeared. This checkout's model keeps metadata as an
    opaque dict, so it is already tolerant -- this test pins that behavior so a future
    refactor to typed sub-fields can't silently reintroduce the audit-data loss.
    """

    server_metadata = {
        "provenance": {
            "source": "manual",
            "source_ref": "add_relationship",
            "created_at": "2026-06-11T00:00:00Z",
            # Newer provenance keys an older client would not recognize:
            "created_actor_context": {
                "actor_type": "human_user",
                "actor_id": "u1",
                "org_id": "o1",
                "operation_id": "op1",
                "timestamp": "2026-06-11T00:00:00Z",
            },
            "future_provenance_field": "NEWER",
        },
        "assertion": {"review_state": "accepted"},
        "evidence": {
            "refs": [{"source": "doc", "source_record_id": "section"}],
            "rationale": "Observed in docs.",
            "future_evidence_field": "NEWER",
        },
        # A brand-new sibling metadata block the client has never heard of:
        "future_metadata_block": {"foo": "bar"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/relationships/lookup")
        return httpx.Response(
            200,
            json={
                "found": True,
                "from_type": "Part",
                "from_id": "BP-1",
                "relationship_type": "fits",
                "to_type": "Vehicle",
                "to_id": "V-1",
                "edge_key": 0,
                "properties": {"verified": True},
                "metadata": server_metadata,
            },
        )

    client = _build_client(handler)
    result = client.get_relationship(
        "inst_123",
        from_type="Part",
        from_id="BP-1",
        relationship_type="fits",
        to_type="Vehicle",
        to_id="V-1",
        edge_key=0,
    )

    # Known audit blocks survive parsing, with their core fields intact.
    provenance = result.metadata["provenance"]
    assert provenance["source"] == "manual"
    assert provenance["source_ref"] == "add_relationship"
    assert provenance["created_at"] == "2026-06-11T00:00:00Z"
    evidence = result.metadata["evidence"]
    assert evidence["rationale"] == "Observed in docs."
    assert evidence["refs"] == [{"source": "doc", "source_record_id": "section"}]

    # Unknown/newer keys must be preserved, never dropped -- and their presence must
    # not have nulled the known blocks above.
    assert provenance["future_provenance_field"] == "NEWER"
    assert evidence["future_evidence_field"] == "NEWER"
    assert result.metadata["future_metadata_block"] == {"foo": "bar"}

    # The whole metadata block round-trips byte-for-byte through the contract model:
    # nothing the server sent is lost on the client read path.
    assert result.model_dump(mode="json")["metadata"] == server_metadata


def test_optional_get_query_params_omit_none_and_preserve_falsey_values():
    captured: dict[str, dict[str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured[request.url.path] = dict(request.url.params)
        path = request.url.path
        if path.endswith("/decision-records/events"):
            return httpx.Response(200, json={"items": [], "total": 0})
        if path.endswith("/decision-records/DR-1"):
            return httpx.Response(
                200,
                json={
                    "record": {"decision_record_id": "DR-1", "status": "open"},
                    "events": [],
                },
            )
        if path.endswith("/decision-records"):
            return httpx.Response(200, json={"items": [], "total": 0})
        if path.endswith("/traces"):
            return httpx.Response(200, json={"items": [], "total": 0})
        if path.endswith("/list/entities"):
            return httpx.Response(200, json={"items": [], "total": 0})
        if path.endswith("/outcome/profile"):
            return httpx.Response(200, json={"found": False, "anchor_type": "receipt"})
        if path.endswith("/inspect/entity-history/WorkItem"):
            return httpx.Response(
                200,
                json={
                    "entity_type": "WorkItem",
                    "items": [],
                    "total": 0,
                    "legacy_entity_write_count": 0,
                    "warnings": [],
                },
            )
        if "/inspect/entity/" in path:
            return httpx.Response(
                200,
                json={
                    "found": True,
                    "entity_type": "Vehicle",
                    "entity_id": "V-1",
                    "properties": {},
                    "metadata": {},
                    "neighbors": [],
                    "total_neighbors": 0,
                },
            )
        return httpx.Response(404)

    client = _build_client(handler)

    client.list_decision_records(
        "inst_123",
        status=None,
        subject_type=None,
        subject_id=None,
        decision_class=None,
        limit=25,
        offset=0,
    )
    client.list_decision_events(
        "inst_123",
        decision_record_id=None,
        receipt_id=None,
        trace_id=None,
        status=None,
        limit=25,
        offset=0,
    )
    client.list_traces("inst_123", workflow_name=None, provider_name=None, limit=25, offset=0)
    client.list(
        "inst_123",
        resource_type="entities",
        entity_type=None,
        relationship_type=None,
        query_name=None,
        receipt_id=None,
        operation_type=None,
        property_filter=None,
        limit=25,
        offset=0,
    )
    client.get_outcome_profile(
        "inst_123",
        anchor_type="receipt",
        relationship_type=None,
        workflow_name=None,
        surface_type=None,
        surface_name=None,
    )
    client.inspect_entity(
        "inst_123",
        "Vehicle",
        "V-1",
        direction="both",
        relationship_type=None,
        limit=None,
    )
    client.inspect_entity_history("inst_123", "WorkItem", entity_id=None, limit=25, offset=0)
    client.get_decision_record("inst_123", "DR-1", include_events=False)

    assert captured["/api/v1/inst_123/decision-records"] == {"limit": "25", "offset": "0"}
    assert captured["/api/v1/inst_123/decision-records/events"] == {
        "limit": "25",
        "offset": "0",
    }
    assert captured["/api/v1/inst_123/traces"] == {"limit": "25", "offset": "0"}
    assert captured["/api/v1/inst_123/list/entities"] == {"limit": "25", "offset": "0"}
    assert captured["/api/v1/inst_123/outcome/profile"] == {"anchor_type": "receipt"}
    assert captured["/api/v1/inst_123/inspect/entity/Vehicle/V-1"] == {"direction": "both"}
    assert captured["/api/v1/inst_123/inspect/entity-history/WorkItem"] == {
        "limit": "25",
        "offset": "0",
    }
    assert captured["/api/v1/inst_123/decision-records/DR-1"] == {"include_events": "false"}


def test_get_group_status_by_group_uses_expected_route():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "signature": "sigv1:abc",
                "relationship_type": "fits",
                "thesis_text": "fit rule",
                "thesis_facts": {"rule_id": "fit_rule"},
                "latest_trust_status": "watch",
                "accepted_tuple_count": 2,
                "pending_delta_count": 1,
                "pending_group_id": "GRP-1",
                "pending_version": 4,
                "latest_approved_resolution_id": "RES-1",
                "approved_history": [
                    {
                        "resolution_id": "RES-1",
                        "action": "approve",
                        "trust_status": "watch",
                        "confirmed": True,
                        "resolved_at": "2026-04-20T12:00:00+00:00",
                        "tuple_count": 2,
                    }
                ],
            },
        )

    client = _build_client(handler)
    result = client.get_group_status("inst_123", group_id="GRP-1")

    assert result.signature == "sigv1:abc"
    assert result.pending_version == 4
    assert captured["path"].endswith("/api/v1/inst_123/groups/GRP-1/status")


def test_get_group_status_by_signature_uses_expected_route():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "signature": "sigv1:def",
                "relationship_type": "fits",
                "thesis_text": "",
                "thesis_facts": {"rule_id": "fit_rule", "rule_version": 2},
                "latest_trust_status": None,
                "accepted_tuple_count": 0,
                "pending_delta_count": 0,
                "pending_group_id": None,
                "pending_version": None,
                "latest_approved_resolution_id": None,
                "approved_history": [],
            },
        )

    client = _build_client(handler)
    result = client.get_group_status("inst_123", signature="sigv1:def")

    assert result.signature == "sigv1:def"
    assert result.accepted_tuple_count == 0
    assert captured["path"].endswith("/api/v1/inst_123/group-status/sigv1:def")


def test_group_routes_omit_none_query_params():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/groups"):
            captured["groups"] = str(request.url)
        elif request.url.path.endswith("/resolutions"):
            captured["resolutions"] = str(request.url)
        return httpx.Response(
            200,
            json=(
                {"items": [], "total": 0}
                if request.url.path.endswith("/groups")
                else {"items": [], "total": 0}
            ),
        )

    client = _build_client(handler)
    groups_result = client.list_groups(
        "inst_123",
        status=None,
        relationship_type=None,
        limit=25,
        offset=5,
    )
    resolutions_result = client.list_resolutions(
        "inst_123",
        action=None,
        relationship_type=None,
        limit=25,
        offset=5,
    )

    assert groups_result.total == 0
    assert resolutions_result.total == 0
    assert captured["groups"].endswith("/api/v1/inst_123/groups?limit=25&offset=5")
    assert captured["resolutions"].endswith("/api/v1/inst_123/resolutions?limit=25&offset=5")


def test_evaluate_uses_expected_route():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = str(request.url)
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "entity_count": 4,
                "edge_count": 3,
                "findings": [],
                "summary": {},
                "quality_summary": {"check_ok": 0},
            },
        )

    client = _build_client(handler)
    result = client.evaluate(
        "inst_123",
        max_findings=5,
        severity_filter=["error"],
        category_filter=["quality_check_failed"],
    )
    assert result.quality_summary == {"check_ok": 0}
    assert captured["path"].endswith("/api/v1/inst_123/evaluate")
    assert captured["payload"]["max_findings"] == 5
    assert captured["payload"]["severity_filter"] == ["error"]
    assert captured["payload"]["category_filter"] == ["quality_check_failed"]


def test_lint_uses_expected_route():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = str(request.url)
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "config_name": "car_parts_compatibility",
                "config_warnings": [],
                "compatibility_warnings": [],
                "evaluation": {
                    "entity_count": 4,
                    "edge_count": 3,
                    "findings": [],
                    "summary": {},
                    "constraint_summary": {},
                    "quality_summary": {},
                },
                "feedback_reports": [],
                "outcome_reports": [],
                "summary": {
                    "config_warning_count": 0,
                    "compatibility_warning_count": 0,
                    "evaluation_finding_count": 0,
                    "feedback_report_count": 0,
                    "feedback_issue_count": 0,
                    "outcome_report_count": 0,
                    "outcome_issue_count": 0,
                },
                "has_issues": False,
            },
        )

    client = _build_client(handler)
    result = client.lint(
        "inst_123",
        max_findings=5,
        analysis_limit=50,
        min_support=2,
        exclude_orphan_types=["Vehicle"],
    )
    assert result.config_name == "car_parts_compatibility"
    assert result.has_issues is False
    assert captured["path"].endswith("/api/v1/inst_123/lint")
    assert captured["payload"] == {
        "max_findings": 5,
        "analysis_limit": 50,
        "min_support": 2,
        "exclude_orphan_types": ["Vehicle"],
    }


def test_snapshot_create_uses_expected_route():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = str(request.url)
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "snapshot": {
                    "snapshot_id": "snap_1",
                    "created_at": "2026-03-21T00:00:00Z",
                    "label": "baseline",
                    "config_digest": "sha256:abc",
                    "lock_digest": None,
                    "graph_digest": "sha256:def",
                    "parent_snapshot_id": None,
                    "origin_snapshot_id": None,
                }
            },
        )

    client = _build_client(handler)
    result = client.create_snapshot("inst_123", label="baseline")
    assert result.snapshot.snapshot_id == "snap_1"
    assert captured["path"].endswith("/api/v1/inst_123/snapshots")
    assert captured["payload"]["label"] == "baseline"


def test_instance_backup_and_restore_use_expected_routes():
    captured: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        captured.append((request.url.path, payload))
        manifest = {
            "format_version": 1,
            "instance_id": "inst_123",
            "created_at": "2026-03-21T00:00:00Z",
            "cruxible_version": "0.2.0",
            "label": "pre-release",
            "original_config_path": "/srv/project/config.yaml",
            "restored_config_path": "config.yaml",
            "instance_mode": "governed",
            "artifacts": {"state.db": "sha256:abc"},
        }
        if request.url.path.endswith("/instance/backup"):
            return httpx.Response(
                200,
                json={
                    "instance_id": "inst_123",
                    "artifact_path": "/tmp/backup.zip",
                    "manifest": manifest,
                },
            )
        return httpx.Response(
            200,
            json={
                "instance_id": "inst_123",
                "root_dir": "/srv/restored",
                "manifest": manifest,
                "registry_status": "registered",
            },
        )

    client = _build_client(handler)
    snap = client.backup_instance(
        "inst_123",
        artifact_path="/tmp/backup.zip",
        label="pre-release",
    )
    restored = client.restore_instance(artifact_path="/tmp/backup.zip", root_dir="/srv/restored")

    assert snap.instance_id == "inst_123"
    assert restored.root_dir == "/srv/restored"
    assert captured == [
        (
            "/api/v1/inst_123/instance/backup",
            {"artifact_path": "/tmp/backup.zip", "label": "pre-release"},
        ),
        (
            "/api/v1/instances/restore",
            {"artifact_path": "/tmp/backup.zip", "root_dir": "/srv/restored"},
        ),
    ]


def test_instance_relocate_uses_expected_route():
    captured: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        captured.append((request.url.path, payload))
        manifest = {
            "format_version": 1,
            "instance_id": "inst_123",
            "created_at": "2026-03-21T00:00:00Z",
            "cruxible_version": "0.2.0",
            "label": "relocate",
            "original_config_path": "/srv/old/config.yaml",
            "restored_config_path": "config.yaml",
            "instance_mode": "governed",
            "artifacts": {"state.db": "sha256:abc"},
        }
        return httpx.Response(
            200,
            json={
                "instance_id": "inst_123",
                "from_dir": "/srv/old",
                "to_dir": "/srv/new",
                "manifest": manifest,
                "source_removed": True,
                "registry_status": "registered",
            },
        )

    client = _build_client(handler)
    relocated = client.relocate_instance("inst_123", to_dir="/srv/new", remove_source=True)

    assert relocated.instance_id == "inst_123"
    assert relocated.from_dir == "/srv/old"
    assert relocated.to_dir == "/srv/new"
    assert relocated.source_removed is True
    assert captured == [
        (
            "/api/v1/inst_123/instance/relocate",
            {"to_dir": "/srv/new", "remove_source": True},
        ),
    ]


def test_state_endpoints_use_expected_routes():
    captured: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.content.decode() if request.content else None
        captured.append((str(request.url), payload))
        if request.url.path == "/api/v1/states/overlays":
            return httpx.Response(
                200,
                json={
                    "instance_id": "inst_overlay",
                    "manifest": {
                        "format_version": 1,
                        "state_id": "case-law",
                        "release_id": "v1.0.0",
                        "snapshot_id": "snap_1",
                        "compatibility": "data_only",
                        "owned_entity_types": ["Case"],
                        "owned_relationship_types": ["cites"],
                        "parent_release_id": None,
                    },
                },
            )
        if request.url.path.endswith("/state/publish"):
            return httpx.Response(
                200,
                json={
                    "manifest": {
                        "format_version": 1,
                        "state_id": "case-law",
                        "release_id": "v1.0.0",
                        "snapshot_id": "snap_1",
                        "compatibility": "data_only",
                        "owned_entity_types": ["Case"],
                        "owned_relationship_types": ["cites"],
                        "parent_release_id": None,
                    }
                },
            )
        if request.url.path.endswith("/state/status"):
            return httpx.Response(
                200,
                json={
                    "upstream": {
                        "transport_ref": "file:///tmp/releases/current",
                        "requested_source_ref": "case-law@v1.0.0",
                        "requested_transport_ref": "file:///tmp/releases/v1.0.0",
                        "state_id": "case-law",
                        "release_id": "v1.0.0",
                        "snapshot_id": "snap_1",
                        "compatibility": "data_only",
                        "owned_entity_types": ["Case"],
                        "owned_relationship_types": ["cites"],
                        "overlay_config_path": "config.yaml",
                        "manifest_path": ".cruxible/upstream/current/manifest.json",
                        "graph_path": ".cruxible/upstream/current/graph.json",
                        "upstream_config_path": ".cruxible/upstream/current/config.yaml",
                        "lock_path": ".cruxible/upstream/current/cruxible.lock.yaml",
                        "manifest_digest": "sha256:abc",
                        "graph_digest": "sha256:def",
                    }
                },
            )
        if request.url.path.endswith("/state/pull/preview"):
            return httpx.Response(
                200,
                json={
                    "current_release_id": "v1.0.0",
                    "target_release_id": "v1.1.0",
                    "compatibility": "data_only",
                    "apply_digest": "sha256:apply",
                    "warnings": [],
                    "conflicts": [],
                    "lock_changed": True,
                    "upstream_entity_delta": 1,
                    "upstream_edge_delta": 0,
                },
            )
        return httpx.Response(
            200,
            json={
                "release_id": "v1.1.0",
                "apply_digest": "sha256:apply",
                "pre_pull_snapshot_id": "snap_pre",
            },
        )

    client = _build_client(handler)
    assert (
        client.create_state_overlay(
            transport_ref="file:///tmp/releases/current",
            root_dir="/tmp/overlay",
        ).instance_id
        == "inst_overlay"
    )
    assert (
        client.state_publish(
            "inst_123",
            transport_ref="file:///tmp/releases/current",
            state_id="case-law",
            release_id="v1.0.0",
            compatibility="data_only",
        ).manifest.release_id
        == "v1.0.0"
    )
    upstream = client.state_status("inst_123").upstream
    assert upstream is not None
    assert upstream.requested_source_ref == "case-law@v1.0.0"
    assert upstream.requested_transport_ref == "file:///tmp/releases/v1.0.0"
    assert client.state_pull_preview("inst_123").apply_digest == "sha256:apply"
    assert (
        client.state_pull_apply(
            "inst_123",
            expected_apply_digest="sha256:apply",
        ).pre_pull_snapshot_id
        == "snap_pre"
    )

    assert captured[0][0].endswith("/api/v1/states/overlays")
    assert captured[1][0].endswith("/api/v1/inst_123/state/publish")
    assert captured[2][0].endswith("/api/v1/inst_123/state/status")
    assert captured[3][0].endswith("/api/v1/inst_123/state/pull/preview")
    assert captured[4][0].endswith("/api/v1/inst_123/state/pull/apply")


def test_create_state_overlay_serializes_state_ref():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = str(request.url)
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "instance_id": "inst_overlay",
                "manifest": {
                    "format_version": 1,
                    "state_id": "kev-reference",
                    "release_id": "2026-03-27",
                    "snapshot_id": "snap_1",
                    "compatibility": "data_only",
                    "owned_entity_types": ["Vulnerability"],
                    "owned_relationship_types": ["affects_product"],
                    "parent_release_id": None,
                },
            },
        )

    client = _build_client(handler)
    result = client.create_state_overlay(
        root_dir="/tmp/overlay",
        state_ref="kev-reference",
        kit="kev-triage",
    )

    assert result.instance_id == "inst_overlay"
    assert captured["path"].endswith("/api/v1/states/overlays")
    assert captured["payload"] == {
        "transport_ref": None,
        "state_ref": "kev-reference",
        "kit": "kev-triage",
        "no_kit": False,
        "root_dir": "/tmp/overlay",
    }


def test_stats_inspect_and_reload_use_expected_routes():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = str(request.url)
        if request.url.path.endswith("/stats"):
            return httpx.Response(
                200,
                json={
                    "entity_count": 4,
                    "edge_count": 3,
                    "entity_counts": {"Vehicle": 2},
                    "relationship_counts": {"fits": 3},
                    "status_counts": {"WorkItem": {"planned": 1, "closed": 0}},
                    "head_snapshot_id": "snap_1",
                },
            )
        if "/inspect/entity/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "found": True,
                    "entity_type": "Vehicle",
                    "entity_id": "V-1",
                    "properties": {"vehicle_id": "V-1"},
                    "neighbors": [],
                    "total_neighbors": 0,
                },
            )
        if "/inspect/entity-history/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "entity_type": "WorkItem",
                    "entity_id": "wi-1",
                    "items": [
                        {
                            "entity_type": "WorkItem",
                            "entity_id": "wi-1",
                            "change_kind": "updated",
                            "property_changes": [
                                {
                                    "property": "status",
                                    "from_value": "planned",
                                    "to_value": "closed",
                                }
                            ],
                            "changed_at": "2026-06-15T12:00:00Z",
                            "receipt_id": "RCP-1",
                            "operation_type": "add_entity",
                            "actor_context": {"actor_id": "agent"},
                        }
                    ],
                    "total": 1,
                    "legacy_entity_write_count": 0,
                    "warnings": [],
                },
            )
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "config_path": "/srv/project/config.yaml",
                "updated": True,
                "warnings": [],
            },
        )

    client = _build_client(handler)

    stats_result = client.stats("inst_123")
    assert stats_result.entity_count == 4
    assert stats_result.status_counts == {"WorkItem": {"planned": 1, "closed": 0}}
    assert captured["path"].endswith("/api/v1/inst_123/stats")

    inspect_result = client.inspect_entity("inst_123", "Vehicle", "V-1", direction="both")
    assert inspect_result.found is True
    assert "/api/v1/inst_123/inspect/entity/Vehicle/V-1" in captured["path"]

    history_result = client.inspect_entity_history("inst_123", "WorkItem", entity_id="wi-1")
    assert history_result.total == 1
    assert history_result.items[0].property_changes[0].to_value == "closed"
    assert "entity_id=wi-1" in captured["path"]

    reload_result = client.reload_config("inst_123", config_yaml='name: governed\nversion: "1.0"\n')
    assert reload_result.updated is True
    assert captured["path"].endswith("/api/v1/inst_123/config/reload")
    assert captured["payload"]["config_path"] is None
    assert captured["payload"]["config_yaml"] == 'name: governed\nversion: "1.0"\n'


def test_config_provenance_round_trips_for_reload_and_status() -> None:
    captured: list[tuple[str, dict[str, Any]]] = []
    source = contracts.ConfigSourceManifest(
        root_path="/repo/overlay.yaml",
        layers=[
            contracts.ConfigSourceDigest(path="/repo/base.yaml", digest="sha256:base"),
            contracts.ConfigSourceDigest(path="/repo/overlay.yaml", digest="sha256:overlay"),
        ],
        composed_digest="sha256:composed",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        captured.append((request.url.path, payload))
        if request.url.path.endswith("/config/status"):
            return httpx.Response(
                200,
                json={
                    "status": "in_sync",
                    "config_path": "/daemon/config.yaml",
                    "materialized_matches": True,
                    "sources_checked": True,
                    "composed_matches": True,
                    "changed_sources": [],
                    "provenance": {
                        **source.model_dump(mode="json"),
                        "active_config_digest": "sha256:composed",
                        "materialized_digest": "sha256:active",
                        "recorded_at": "2026-07-15T12:00:00Z",
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "config_path": "/daemon/config.yaml",
                "updated": True,
                "warnings": [],
            },
        )

    client = _build_client(handler)
    client.reload_config(
        "inst_123",
        config_yaml='name: governed\nversion: "1.0"\n',
        config_source_manifest=source,
    )
    status = client.config_status("inst_123", current_source_manifest=source)

    assert captured[0][0].endswith("/config/reload")
    assert captured[0][1]["config_source_manifest"] == source.model_dump(mode="json")
    assert captured[1][0].endswith("/config/status")
    assert captured[1][1]["current_source_manifest"] == source.model_dump(mode="json")
    assert status.status == "in_sync"
    assert status.provenance is not None
    assert status.provenance.materialized_digest == "sha256:active"


def test_feedback_analysis_and_policy_routes():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = str(request.url)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "found": True,
                    "relationship_type": "fits",
                    "profile": {"version": 2},
                },
            )
        captured["payload"] = json.loads(request.content.decode())
        if request.url.path.endswith("/feedback/analyze"):
            return httpx.Response(
                200,
                json={
                    "relationship_type": "fits",
                    "feedback_count": 2,
                    "action_counts": {"reject": 2},
                    "source_counts": {"agent": 2},
                    "reason_code_counts": {"legacy_unsupported": 2},
                    "coded_groups": [],
                    "uncoded_feedback_count": 0,
                    "uncoded_examples": [],
                    "constraint_suggestions": [],
                    "decision_policy_suggestions": [],
                    "quality_check_candidates": [],
                    "provider_fix_candidates": [],
                    "warnings": [],
                },
            )
        return httpx.Response(
            200,
            json={
                "name": "suppress_brakes",
                "added": True,
                "config_updated": True,
                "warnings": [],
            },
        )

    client = _build_client(handler)

    profile = client.get_feedback_profile("inst_123", "fits")
    assert profile.found is True
    assert captured["path"].endswith("/api/v1/inst_123/feedback/profiles/fits")

    analysis = client.analyze_feedback(
        "inst_123",
        relationship_type="fits",
        min_support=2,
    )
    assert analysis.feedback_count == 2
    assert captured["path"].endswith("/api/v1/inst_123/feedback/analyze")
    assert captured["payload"]["relationship_type"] == "fits"

    add_result = client.add_decision_policy(
        "inst_123",
        name="suppress_brakes",
        applies_to="query",
        relationship_type="fits",
        effect="suppress",
    )
    assert add_result.added is True
    assert captured["path"].endswith("/api/v1/inst_123/decision-policies")
    assert captured["payload"]["name"] == "suppress_brakes"


def test_outcome_routes():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = str(request.url)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "found": True,
                    "profile_key": "query_quality",
                    "anchor_type": "receipt",
                    "profile": {"version": 1},
                },
            )
        captured["payload"] = json.loads(request.content.decode())
        if request.url.path.endswith("/outcome"):
            return httpx.Response(200, json={"outcome_id": "OUT-1"})
        return httpx.Response(
            200,
            json={
                "anchor_type": "receipt",
                "outcome_count": 2,
                "outcome_counts": {"incorrect": 2},
                "outcome_code_counts": {"bad_result": 2},
                "coded_groups": [],
                "uncoded_outcome_count": 0,
                "uncoded_examples": [],
                "trust_adjustment_suggestions": [],
                "workflow_review_policy_suggestions": [],
                "query_policy_suggestions": [],
                "provider_fix_candidates": [],
                "debug_packages": [],
                "workflow_debug_packages": [],
                "warnings": [],
            },
        )

    client = _build_client(handler)

    outcome = client.outcome(
        "inst_123",
        receipt_id="RCP-1",
        outcome="incorrect",
        source="agent",
        outcome_code="bad_result",
        scope_hints={"surface": "parts_for_vehicle"},
        outcome_profile_key="query_quality",
    )
    assert outcome.outcome_id == "OUT-1"
    assert captured["path"].endswith("/api/v1/inst_123/outcome")
    assert captured["payload"]["outcome_code"] == "bad_result"

    profile = client.get_outcome_profile(
        "inst_123",
        anchor_type="receipt",
        surface_type="query",
        surface_name="parts_for_vehicle",
    )
    assert profile.profile_key == "query_quality"
    assert "/api/v1/inst_123/outcome/profile" in captured["path"]

    analysis = client.analyze_outcomes(
        "inst_123",
        anchor_type="receipt",
        query_name="parts_for_vehicle",
        min_support=2,
    )
    assert analysis.outcome_count == 2
    assert captured["path"].endswith("/api/v1/inst_123/outcomes/analyze")
    assert captured["payload"]["anchor_type"] == "receipt"


def test_claim_runtime_bootstrap_uses_expected_route_and_contract():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "credential_id": "rcred_123",
                "instance_id": "inst_123",
                "permission_mode": "admin",
                "token": "crt_rcred_123_secret",
            },
        )

    client = _build_client(handler)
    result = client.claim_runtime_bootstrap("inst_123", "bootstrap-secret")

    assert result.credential_id == "rcred_123"
    assert result.instance_id == "inst_123"
    assert result.permission_mode == "admin"
    assert result.token == "crt_rcred_123_secret"
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/inst_123/runtime/bootstrap/claim"
    assert captured["payload"] == {"bootstrap_secret": "bootstrap-secret"}


def test_claim_runtime_bootstrap_rehydrates_authentication_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error_type": "AuthenticationError",
                "message": "Invalid bootstrap secret",
                "errors": [],
                "context": {},
                "mutation_receipt_id": None,
            },
        )

    client = _build_client(handler)

    with pytest.raises(AuthenticationError, match="Invalid bootstrap secret"):
        client.claim_runtime_bootstrap("inst_123", "wrong-secret")


def test_init_hosted_instance_uses_expected_route_and_contract():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "instance_id": "inst_hosted",
                "status": "initialized",
                "source_type": "reference_model",
                "source_ref": "kev-reference@v1",
                "resolved_source_ref": "oci://ghcr.io/cruxible-ai/models/kev-reference:v1",
                "overlay_kit_ref": "kev-triage",
                "manifest": {
                    "format_version": 1,
                    "state_id": "kev-reference",
                    "release_id": "v1",
                    "snapshot_id": "snap_1",
                    "compatibility": "data_only",
                    "owned_entity_types": ["Vulnerability"],
                    "owned_relationship_types": ["affects_product"],
                    "parent_release_id": None,
                },
                "warnings": [],
            },
        )

    client = _build_client(handler)
    result = client.init_hosted_instance(
        instance_id="inst_hosted",
        source_type="reference_model",
        state_ref="kev-reference@v1",
        overlay_kit_ref="kev-triage",
    )

    assert result.instance_id == "inst_hosted"
    assert result.status == "initialized"
    assert result.manifest is not None
    assert result.manifest.release_id == "v1"
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/runtime/instances"
    assert captured["payload"] == {
        "instance_id": "inst_hosted",
        "source_type": "reference_model",
        "kit_refs": None,
        "transport_ref": None,
        "state_ref": "kev-reference@v1",
        "overlay_kit_ref": "kev-triage",
        "no_overlay_kit": False,
    }


def test_runtime_credential_client_routes_round_trip():
    captured: list[tuple[str, str, Any]] = []

    metadata = {
        "credential_id": "rcred_123",
        "instance_id": "inst_123",
        "label": "dispatch",
        "permission_mode": "graph_write",
        "created_at": "2026-06-01T12:00:00Z",
        "created_by": "rcred_admin",
        "revoked_at": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        payload = json.loads(request.content.decode()) if request.content else None
        captured.append((request.method, path, payload))
        if request.method == "POST" and path == "/api/v1/inst_123/runtime/credentials":
            return httpx.Response(200, json={"credential": metadata, "token": "crt_new"})
        if request.method == "GET" and path == "/api/v1/inst_123/runtime/credentials":
            return httpx.Response(200, json={"credentials": [metadata]})
        if path == "/api/v1/inst_123/runtime/credentials/rcred_123/revoke":
            revoked = {**metadata, "revoked_at": "2026-06-01T12:01:00Z"}
            return httpx.Response(200, json={"credential": revoked})
        if path == "/api/v1/inst_123/runtime/credentials/rcred_123/rotate":
            rotated = {**metadata, "credential_id": "rcred_456"}
            return httpx.Response(200, json={"credential": rotated, "token": "crt_rotated"})
        return httpx.Response(404, json={"error_type": "CoreError", "message": "not found"})

    client = _build_client(handler)

    created = client.create_runtime_credential(
        "inst_123",
        label="dispatch",
        permission_mode="graph_write",
    )
    listed = client.list_runtime_credentials("inst_123")
    revoked = client.revoke_runtime_credential("inst_123", "rcred_123")
    rotated = client.rotate_runtime_credential("inst_123", "rcred_123")

    assert created.credential.permission_mode == "graph_write"
    assert created.token == "crt_new"
    assert listed.credentials[0].credential_id == "rcred_123"
    assert revoked.credential.revoked_at == "2026-06-01T12:01:00Z"
    assert rotated.credential.credential_id == "rcred_456"
    assert rotated.token == "crt_rotated"
    assert captured == [
        (
            "POST",
            "/api/v1/inst_123/runtime/credentials",
            {"label": "dispatch", "permission_mode": "graph_write"},
        ),
        ("GET", "/api/v1/inst_123/runtime/credentials", None),
        ("POST", "/api/v1/inst_123/runtime/credentials/rcred_123/revoke", None),
        ("POST", "/api/v1/inst_123/runtime/credentials/rcred_123/rotate", None),
    ]


def test_runtime_credential_not_found_error_rehydrates():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "error_type": "RuntimeCredentialNotFoundError",
                "message": "Runtime credential 'rcred_missing' not found",
                "errors": [],
                "context": {"credential_id": "rcred_missing"},
                "mutation_receipt_id": None,
            },
        )

    client = _build_client(handler)

    with pytest.raises(RuntimeCredentialNotFoundError) as exc_info:
        client.rotate_runtime_credential("inst_123", "rcred_missing")

    assert exc_info.value.credential_id == "rcred_missing"


def test_snapshot_create_serializes_actor_context_when_supplied():
    captured: dict[str, Any] = {}
    actor_context = {
        "actor_type": "human_user",
        "actor_id": "usr_1",
        "org_id": "org_1",
        "operation_id": "op_1",
        "timestamp": "2026-06-05T12:00:00Z",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "snapshot": {
                    "snapshot_id": "snap_1",
                    "created_at": "2026-03-21T00:00:00Z",
                    "label": "baseline",
                    "config_digest": "sha256:abc",
                    "lock_digest": None,
                    "graph_digest": "sha256:def",
                    "parent_snapshot_id": None,
                    "origin_snapshot_id": None,
                }
            },
        )

    client = _build_client(handler)
    result = client.create_snapshot(
        "inst_123",
        label="baseline",
        actor_context=actor_context,
    )

    assert result.snapshot.snapshot_id == "snap_1"
    assert captured["payload"]["actor_context"] == actor_context


def test_governed_write_clients_serialize_actor_context_when_supplied():
    captured: dict[str, dict[str, Any]] = {}
    actor_context = {
        "actor_type": "human_user",
        "actor_id": "usr_1",
        "org_id": "org_1",
        "operation_id": "op_1",
        "timestamp": "2026-06-05T12:00:00Z",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured[request.url.path] = json.loads(request.content.decode())
        path = request.url.path
        if path.endswith("/feedback"):
            return httpx.Response(200, json={"feedback_id": "FB-1", "applied": True})
        if path.endswith("/workflows/run"):
            return httpx.Response(
                200,
                json={
                    "workflow": "wf",
                    "output": {},
                    "receipt_id": "RCP-1",
                    "mode": "run",
                    "workflow_type": "utility",
                    "canonical": False,
                },
            )
        if path.endswith("/source-artifacts/register"):
            return httpx.Response(
                200,
                json={
                    "source_artifact_id": "SRC-1",
                    "source_kind": "markdown",
                    "source_retention": "manifest_only",
                    "content_hash": "sha256:abc",
                    "byte_count": 10,
                    "parser_version": "markdown-it-py@test",
                    "archived": False,
                    "chunks": [],
                },
            )
        if path.endswith("/groups/propose"):
            return httpx.Response(
                200,
                json={
                    "group_id": "GRP-1",
                    "signature": "sigv1:test",
                    "status": "pending_review",
                    "review_priority": "normal",
                    "member_count": 0,
                },
            )
        return httpx.Response(500, json={"error": f"unexpected path {path}"})

    client = _build_client(handler)
    client.feedback(
        "inst_123",
        receipt_id="RCP-1",
        action="approve",
        source="human",
        from_type="Part",
        from_id="P-1",
        relationship_type="fits",
        to_type="Vehicle",
        to_id="V-1",
        actor_context=actor_context,
    )
    client.workflow_run(
        "inst_123",
        workflow_name="wf",
        actor_context=actor_context,
    )
    client.register_source_artifact(
        "inst_123",
        source_path="docs/evidence.md",
        actor_context=actor_context,
    )
    client.propose_group(
        "inst_123",
        relationship_type="fits",
        members=[],
        actor_context=actor_context,
    )

    assert captured["/api/v1/inst_123/feedback"]["actor_context"] == actor_context
    assert captured["/api/v1/inst_123/workflows/run"]["actor_context"] == actor_context
    assert captured["/api/v1/inst_123/source-artifacts/register"]["actor_context"] == actor_context
    assert captured["/api/v1/inst_123/groups/propose"]["actor_context"] == actor_context


def test_feedback_omits_source_receipt_by_default():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "feedback_id": "FB-1",
                "applied": True,
                "receipt_id": "RCP-feedback",
            },
        )

    client = _build_client(handler)
    result = client.feedback(
        "inst_123",
        action="approve",
        source="human",
        from_type="Part",
        from_id="P-1",
        relationship_type="fits",
        to_type="Vehicle",
        to_id="V-1",
    )

    assert result.applied is True
    assert captured["payload"]["receipt_id"] is None


def test_register_source_artifact_sends_caller_supplied_id():
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode()))
        return httpx.Response(
            200,
            json={
                "source_artifact_id": "opinion_text_op_loper_bright",
                "source_kind": "markdown",
                "source_retention": "manifest_only",
                "original_uri": None,
                "label": None,
                "content_hash": "sha256:abc",
                "byte_count": 3,
                "parser_version": "markdown_chunks_v1",
                "archived": False,
                "archive_content_hash": None,
                "chunks": [],
            },
        )

    client = _build_client(handler)
    result = client.register_source_artifact(
        "inst_1",
        source_path="docs/opinion.md",
        source_artifact_id="opinion_text_op_loper_bright",
    )
    assert result.source_artifact_id == "opinion_text_op_loper_bright"
    assert captured[0]["source_artifact_id"] == "opinion_text_op_loper_bright"


def test_read_profile_params_are_threaded_and_omitted_when_none():
    """`profile` reaches the wire when set and is absent otherwise (additive)."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        if request.method == "POST":
            captured["payload"] = json.loads(request.content.decode())
        if "queries/run" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "items": [],
                    "receipt_id": None,
                    "receipt": None,
                    "total": 0,
                    "limit": None,
                    "offset": 0,
                    "truncated": False,
                    "steps_executed": 0,
                },
            )
        if "/entities/" in request.url.path:
            return httpx.Response(
                200,
                json={"found": False, "entity_type": "Part", "entity_id": "BP-1"},
            )
        if "/inspect/entity/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "found": False,
                    "entity_type": "Part",
                    "entity_id": "BP-1",
                    "properties": {},
                    "metadata": {},
                    "neighbors": [],
                    "total_neighbors": 0,
                },
            )
        if "/sample/" in request.url.path:
            return httpx.Response(
                200,
                json={"items": [], "entity_type": "Part", "total": 0, "limit": 5},
            )
        return httpx.Response(
            200,
            json={"items": [], "total": 0, "limit": 50, "offset": 0, "truncated": False},
        )

    client = _build_client(handler)

    client.query("inst_1", "q", profile="compact")
    assert captured["payload"]["profile"] == "compact"
    client.query("inst_1", "q")
    assert "profile" not in captured["payload"]

    client.list("inst_1", resource_type="edges", profile="compact")
    assert captured["params"]["profile"] == "compact"
    client.list("inst_1", resource_type="edges")
    assert "profile" not in captured["params"]

    client.get_entity("inst_1", "Part", "BP-1", profile="compact")
    assert captured["params"]["profile"] == "compact"
    client.get_entity("inst_1", "Part", "BP-1")
    assert "profile" not in captured["params"]

    client.inspect_entity("inst_1", "Part", "BP-1", profile="standard")
    assert captured["params"]["profile"] == "standard"

    client.sample("inst_1", "Part", profile="compact")
    assert captured["params"]["profile"] == "compact"
