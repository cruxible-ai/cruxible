"""Server-mode MCP behavior tests."""

from __future__ import annotations

import pytest

from cruxible_client import contracts
from cruxible_core.errors import ConfigError
from cruxible_core.mcp import handlers
from cruxible_core.mcp.server import create_server


def test_create_server_fails_when_server_required_without_endpoint(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CRUXIBLE_REQUIRE_SERVER", "true")
    with pytest.raises(ConfigError):
        create_server()


def test_create_server_fails_when_agent_mode_without_endpoint(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CRUXIBLE_AGENT_MODE", "true")
    with pytest.raises(ConfigError, match="Server mode is required"):
        create_server()


def test_public_handler_delegates_to_client(monkeypatch: pytest.MonkeyPatch):
    class StubClient:
        def query(self, instance_id, query_name, params, limit=None):
            assert instance_id == "inst_123"
            assert query_name == "parts_for_vehicle"
            assert params == {"vehicle_id": "V-1"}
            assert limit == 5
            return contracts.QueryToolResult(
                results=[],
                receipt_id="RCPT-1",
                receipt=None,
                total_results=0,
                truncated=False,
                steps_executed=1,
            )

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())
    result = handlers.handle_query(
        "inst_123",
        "parts_for_vehicle",
        {"vehicle_id": "V-1"},
        limit=5,
    )
    assert result.receipt_id == "RCPT-1"


def test_server_info_handler_delegates_to_client(monkeypatch: pytest.MonkeyPatch):
    class StubClient:
        def server_info(self):
            return contracts.ServerInfoResult(
                agent_mode=True,
                state_dir="/srv/cruxible-state",
                version="0.2.0",
                instance_count=2,
            )

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())
    result = handlers.handle_server_info()
    assert result.agent_mode is True
    assert result.instance_count == 2


def test_query_discovery_handlers_delegate_to_client(monkeypatch: pytest.MonkeyPatch):
    class StubClient:
        def list_queries(self, instance_id):
            assert instance_id == "inst_123"
            return contracts.QueryListResult(
                queries=[
                    contracts.NamedQueryInfoResult(
                        name="parts_for_vehicle",
                        entry_point="Vehicle",
                        required_params=["vehicle_id"],
                        returns="Part",
                        description="Find compatible parts.",
                        example_ids=["V-1"],
                    )
                ]
            )

        def describe_query(self, instance_id, query_name):
            assert instance_id == "inst_123"
            assert query_name == "parts_for_vehicle"
            return contracts.NamedQueryInfoResult(
                name="parts_for_vehicle",
                entry_point="Vehicle",
                required_params=["vehicle_id"],
                returns="Part",
                description="Find compatible parts.",
                example_ids=["V-1"],
            )

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())
    listed = handlers.handle_list_queries("inst_123")
    assert listed.queries[0].name == "parts_for_vehicle"
    described = handlers.handle_describe_query("inst_123", "parts_for_vehicle")
    assert described.returns == "Part"


def test_workflow_propose_handler_delegates_to_client(monkeypatch: pytest.MonkeyPatch):
    class StubClient:
        def propose_workflow(self, instance_id, *, workflow_name, input_payload=None):
            assert instance_id == "inst_123"
            assert workflow_name == "wf"
            assert input_payload == {"id": "1"}
            return contracts.WorkflowProposeResult(
                workflow="wf",
                output={"members": []},
                receipt_id="RCP-1",
                group_id=None,
                group_status="suppressed",
                review_priority="review",
                suppressed=True,
                suppressed_members=[
                    contracts.SuppressedProposalMember(
                        relationship_type="recommended_for",
                        from_type="Campaign",
                        from_id="CMP-1",
                        to_type="Product",
                        to_id="SKU-123",
                        reason="pending_proposal",
                        existing_group_id="GRP-1",
                        existing_group_status="pending_review",
                        existing_signature="sig-1",
                        source_workflow_name="wf",
                    )
                ],
                trace_ids=["TRC-1"],
            )

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())
    result = handlers.handle_propose_workflow("inst_123", "wf", {"id": "1"})
    assert result.group_id is None
    assert result.suppressed is True
    assert result.suppressed_members[0].existing_group_id == "GRP-1"


def test_workflow_lock_handler_delegates_to_client(monkeypatch: pytest.MonkeyPatch):
    class StubClient:
        def workflow_lock(self, instance_id, *, force=False):
            assert instance_id == "inst_123"
            assert force is True
            return contracts.WorkflowLockResult(
                lock_path="/tmp/cruxible.lock.yaml",
                config_digest="sha256:cfg",
                providers_locked=2,
                artifacts_locked=1,
            )

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())
    result = handlers.handle_workflow_lock("inst_123", force=True)
    assert result.lock_path == "/tmp/cruxible.lock.yaml"


def test_workflow_plan_handler_delegates_to_client(monkeypatch: pytest.MonkeyPatch):
    class StubClient:
        def workflow_plan(self, instance_id, *, workflow_name, input_payload=None):
            assert instance_id == "inst_123"
            assert workflow_name == "wf"
            assert input_payload == {"id": "1"}
            return contracts.WorkflowPlanResult(plan={"workflow": "wf", "steps": []})

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())
    result = handlers.handle_workflow_plan("inst_123", "wf", {"id": "1"})
    assert result.plan["workflow"] == "wf"


def test_workflow_run_handler_delegates_to_client(monkeypatch: pytest.MonkeyPatch):
    class StubClient:
        def workflow_run(self, instance_id, *, workflow_name, input_payload=None):
            assert instance_id == "inst_123"
            assert workflow_name == "wf"
            assert input_payload == {"id": "1"}
            return contracts.WorkflowRunResult(
                workflow="wf",
                output={"ok": True},
                receipt_id="RCP-1",
                mode="run",
                canonical=False,
                trace_ids=["TRC-1"],
            )

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())
    result = handlers.handle_workflow_run("inst_123", "wf", {"id": "1"})
    assert result.receipt_id == "RCP-1"


def test_workflow_apply_handler_delegates_to_client(monkeypatch: pytest.MonkeyPatch):
    class StubClient:
        def workflow_apply(
            self,
            instance_id,
            *,
            workflow_name,
            expected_apply_digest,
            expected_head_snapshot_id=None,
            input_payload=None,
        ):
            assert instance_id == "inst_123"
            assert workflow_name == "wf"
            assert expected_apply_digest == "sha256:abc"
            assert expected_head_snapshot_id == "snap_1"
            assert input_payload == {"id": "1"}
            return contracts.WorkflowApplyResult(
                workflow="wf",
                output={"ok": True},
                receipt_id="RCP-2",
                mode="apply",
                canonical=True,
                apply_digest="sha256:abc",
                head_snapshot_id="snap_1",
                committed_snapshot_id="snap_2",
                trace_ids=["TRC-2"],
            )

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())
    result = handlers.handle_workflow_apply(
        "inst_123",
        "wf",
        expected_apply_digest="sha256:abc",
        expected_head_snapshot_id="snap_1",
        input_payload={"id": "1"},
    )
    assert result.committed_snapshot_id == "snap_2"


@pytest.mark.parametrize(
    ("fn", "args", "label"),
    [
        (handlers.handle_init, ("./project", None, "name: demo", None), "cruxible_init"),
        (handlers.handle_world_fork, ("./fork", "file:///tmp/release"), "cruxible_world_fork"),
        (handlers.handle_workflow_run, ("inst_123", "wf", {"id": "1"}), "cruxible_run_workflow"),
    ],
)
def test_local_mutation_handlers_require_server(
    monkeypatch: pytest.MonkeyPatch,
    fn,
    args,
    label: str,
):
    monkeypatch.setattr(handlers, "_get_client", lambda: None)
    with pytest.raises(ConfigError, match=f"{label}"):
        fn(*args)
