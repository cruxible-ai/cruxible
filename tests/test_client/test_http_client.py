"""Tests for the HTTP client."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from cruxible_client import CruxibleClient
from cruxible_client.errors import ConstraintViolationError, DataValidationError


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
    result = client.init("/srv/project", kit="kev-reference")

    assert result.instance_id == "inst_123"
    assert captured["payload"]["kit"] == "kev-reference"
    assert captured["payload"]["config_yaml"] is None


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


def test_trace_methods_call_trace_routes():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        if request.url.path.endswith("/TRC-1"):
            return httpx.Response(200, json={"trace_id": "TRC-1", "workflow_name": "wf"})
        return httpx.Response(
            200,
            json={
                "traces": [
                    {
                        "trace_id": "TRC-1",
                        "workflow_name": "wf",
                        "provider_name": "provider",
                    }
                ],
                "count": 1,
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
    assert listed.traces[0]["provider_name"] == "provider"
    assert seen[0] == ("GET", "http://cruxible/api/v1/inst_123/traces/TRC-1")
    expected_url = (
        "http://cruxible/api/v1/inst_123/traces?"
        "workflow_name=wf&provider_name=provider&limit=25&offset=5"
    )
    assert seen[1][1] == expected_url


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
                "trace_ids": ["TRC-1"],
                "prior_resolution": None,
                "receipt": None,
                "traces": [],
            },
        )

    client = _build_client(handler)
    result = client.propose_workflow("inst_123", workflow_name="wf", input_payload={"id": "1"})
    assert result.group_id == "GRP-1"
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
            return httpx.Response(200, json={"records": [record]})
        if request.method == "GET" and path.endswith("/decision-records/events"):
            return httpx.Response(
                200,
                json={
                    "events": [
                        {
                            "decision_event_id": "DE-1",
                            "decision_record_id": "DR-1",
                            "sequence": 1,
                            "command": "query:q",
                            "status": "success",
                        }
                    ]
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
    assert listed.records[0]["decision_record_id"] == "DR-1"
    assert events.events[0]["decision_record_id"] == "DR-1"
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
        if path.endswith("/query"):
            return httpx.Response(
                200,
                json={
                    "results": [],
                    "receipt_id": "RCP-query",
                    "receipt": None,
                    "total_results": 0,
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
                "workflow_type": "utility"
                if path.endswith("/workflows/run")
                else "canonical",
                "canonical": path.endswith("/workflows/apply"),
                "apply_digest": "sha256:abc",
                "head_snapshot_id": "snap_1",
                "committed_snapshot_id": None,
                "apply_previews": {},
                "query_receipt_ids": [],
                "trace_ids": [],
                "receipt": None,
                "traces": [],
            },
        )

    client = _build_client(handler)
    client.query("inst_123", "q", {}, decision_record_id="DR-1")
    client.workflow_run("inst_123", workflow_name="wf", decision_record_id="DR-1")
    client.workflow_apply(
        "inst_123",
        workflow_name="wf",
        expected_apply_digest="sha256:abc",
        expected_head_snapshot_id="snap_1",
        decision_record_id="DR-1",
    )
    client.propose_workflow("inst_123", workflow_name="wf", decision_record_id="DR-1")

    assert [payload["decision_record_id"] for _, payload in captured] == [
        "DR-1",
        "DR-1",
        "DR-1",
        "DR-1",
    ]


def test_render_wiki_sends_scope_and_max_per_type():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = str(request.url)
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"pages": [], "page_count": 0})

    client = _build_client(handler)
    result = client.render_wiki(
        "inst_123",
        focus=["Asset:A1"],
        include_types=["Asset"],
        scope="local",
        max_per_type=25,
    )

    assert result.page_count == 0
    assert captured["path"].endswith("/api/v1/inst_123/wiki/render")
    assert captured["payload"] == {
        "focus": ["Asset:A1"],
        "include_types": ["Asset"],
        "scope": "local",
        "max_per_type": 25,
        "all_subjects": False,
    }


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
                "relationship": {"relationship_type": "fits"},
                "_provenance": {"source_ref": "group:GRP-1"},
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
    assert result.group == {"group_id": "GRP-1"}
    assert "relationships/lineage" in captured["path"]
    assert "edge_key=7" in captured["path"]


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
                {"groups": [], "total": 0}
                if request.url.path.endswith("/groups")
                else {"resolutions": [], "total": 0}
            ),
        )

    client = _build_client(handler)
    groups_result = client.list_groups("inst_123", status=None, relationship_type=None, limit=25)
    resolutions_result = client.list_resolutions(
        "inst_123",
        action=None,
        relationship_type=None,
        limit=25,
    )

    assert groups_result.total == 0
    assert resolutions_result.total == 0
    assert captured["groups"].endswith("/api/v1/inst_123/groups?limit=25")
    assert captured["resolutions"].endswith("/api/v1/inst_123/resolutions?limit=25")


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
    result = client.evaluate("inst_123", max_findings=5)
    assert result.quality_summary == {"check_ok": 0}
    assert captured["path"].endswith("/api/v1/inst_123/evaluate")
    assert captured["payload"]["max_findings"] == 5


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


def test_world_endpoints_use_expected_routes():
    captured: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.content.decode() if request.content else None
        captured.append((str(request.url), payload))
        if request.url.path == "/api/v1/worlds/overlays":
            return httpx.Response(
                200,
                json={
                    "instance_id": "inst_overlay",
                    "manifest": {
                        "format_version": 1,
                        "world_id": "case-law",
                        "release_id": "v1.0.0",
                        "snapshot_id": "snap_1",
                        "compatibility": "data_only",
                        "owned_entity_types": ["Case"],
                        "owned_relationship_types": ["cites"],
                        "parent_release_id": None,
                    },
                },
            )
        if request.url.path.endswith("/world/publish"):
            return httpx.Response(
                200,
                json={
                    "manifest": {
                        "format_version": 1,
                        "world_id": "case-law",
                        "release_id": "v1.0.0",
                        "snapshot_id": "snap_1",
                        "compatibility": "data_only",
                        "owned_entity_types": ["Case"],
                        "owned_relationship_types": ["cites"],
                        "parent_release_id": None,
                    }
                },
            )
        if request.url.path.endswith("/world/status"):
            return httpx.Response(
                200,
                json={
                    "upstream": {
                        "transport_ref": "file:///tmp/releases/current",
                        "requested_source_ref": "case-law@v1.0.0",
                        "requested_transport_ref": "file:///tmp/releases/v1.0.0",
                        "world_id": "case-law",
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
        if request.url.path.endswith("/world/pull/preview"):
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
    assert client.create_world_overlay(
        transport_ref="file:///tmp/releases/current",
        root_dir="/tmp/overlay",
    ).instance_id == "inst_overlay"
    assert client.world_publish(
        "inst_123",
        transport_ref="file:///tmp/releases/current",
        world_id="case-law",
        release_id="v1.0.0",
        compatibility="data_only",
    ).manifest.release_id == "v1.0.0"
    upstream = client.world_status("inst_123").upstream
    assert upstream is not None
    assert upstream.requested_source_ref == "case-law@v1.0.0"
    assert upstream.requested_transport_ref == "file:///tmp/releases/v1.0.0"
    assert client.world_pull_preview("inst_123").apply_digest == "sha256:apply"
    assert client.world_pull_apply(
        "inst_123",
        expected_apply_digest="sha256:apply",
    ).pre_pull_snapshot_id == "snap_pre"

    assert captured[0][0].endswith("/api/v1/worlds/overlays")
    assert captured[1][0].endswith("/api/v1/inst_123/world/publish")
    assert captured[2][0].endswith("/api/v1/inst_123/world/status")
    assert captured[3][0].endswith("/api/v1/inst_123/world/pull/preview")
    assert captured[4][0].endswith("/api/v1/inst_123/world/pull/apply")


def test_create_world_overlay_serializes_world_ref():
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
                    "world_id": "kev-reference",
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
    result = client.create_world_overlay(
        root_dir="/tmp/overlay",
        world_ref="kev-reference",
        kit="kev-triage",
    )

    assert result.instance_id == "inst_overlay"
    assert captured["path"].endswith("/api/v1/worlds/overlays")
    assert captured["payload"] == {
        "transport_ref": None,
        "world_ref": "kev-reference",
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
    assert captured["path"].endswith("/api/v1/inst_123/stats")

    inspect_result = client.inspect_entity("inst_123", "Vehicle", "V-1", direction="both")
    assert inspect_result.found is True
    assert "/api/v1/inst_123/inspect/entity/Vehicle/V-1" in captured["path"]

    reload_result = client.reload_config("inst_123", config_yaml='name: governed\nversion: "1.0"\n')
    assert reload_result.updated is True
    assert captured["path"].endswith("/api/v1/inst_123/config/reload")
    assert captured["payload"]["config_path"] is None
    assert captured["payload"]["config_yaml"] == 'name: governed\nversion: "1.0"\n'


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
