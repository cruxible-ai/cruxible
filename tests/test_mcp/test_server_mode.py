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


def test_public_handler_delegates_to_client(monkeypatch: pytest.MonkeyPatch):
    class StubClient:
        def query(self, instance_id, query_name, params, limit=None, offset=0):
            assert instance_id == "inst_123"
            assert query_name == "parts_for_vehicle"
            assert params == {"vehicle_id": "V-1"}
            assert limit == 5
            return contracts.QueryToolResult(
                items=[],
                receipt_id="RCPT-1",
                receipt=None,
                total=0,
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
                server_required=True,
                state_dir="/srv/cruxible-state",
                version="0.2.0",
                instance_count=2,
            )

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())
    result = handlers.handle_server_info()
    assert result.server_required is True
    assert result.instance_count == 2


def test_init_handler_delegates_kit_to_client(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, str | None] = {}

    class StubClient:
        def init(self, *, root_dir, config_path=None, config_yaml=None, data_dir=None, kit=None):
            captured["root_dir"] = root_dir
            captured["config_yaml"] = config_yaml
            captured["kit"] = kit
            return contracts.InitResult(instance_id="inst_123", status="initialized")

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())

    result = handlers.handle_init("/srv/project", kit="kev-reference")

    assert result.instance_id == "inst_123"
    assert captured == {
        "root_dir": "/srv/project",
        "config_yaml": None,
        "kit": "kev-reference",
    }


def test_query_discovery_handlers_delegate_to_client(monkeypatch: pytest.MonkeyPatch):
    class StubClient:
        def list_queries(self, instance_id, *, limit=None, offset=0):
            assert instance_id == "inst_123"
            return contracts.QueryListResult(
                items=[
                    contracts.NamedQueryInfoResult(
                        name="parts_for_vehicle",
                        mode="traversal",
                        entry_point="Vehicle",
                        required_params=["vehicle_id"],
                        returns="Part",
                        description="Find compatible parts.",
                        example_ids=["V-1"],
                    )
                ],
                total=1,
            )

        def describe_query(self, instance_id, query_name):
            assert instance_id == "inst_123"
            assert query_name == "parts_for_vehicle"
            return contracts.NamedQueryInfoResult(
                name="parts_for_vehicle",
                mode="traversal",
                entry_point="Vehicle",
                required_params=["vehicle_id"],
                returns="Part",
                description="Find compatible parts.",
                example_ids=["V-1"],
            )

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())
    listed = handlers.handle_list_queries("inst_123")
    assert listed.items[0].name == "parts_for_vehicle"
    described = handlers.handle_describe_query("inst_123", "parts_for_vehicle")
    assert described.returns == "Part"


def test_new_read_handlers_delegate_to_client(monkeypatch: pytest.MonkeyPatch):
    snapshot = contracts.SnapshotMetadata(
        snapshot_id="snap_1",
        created_at="2026-05-04T00:00:00Z",
        label="baseline",
        config_digest="sha256:cfg",
        lock_digest=None,
        graph_digest="sha256:graph",
        parent_snapshot_id=None,
        origin_snapshot_id=None,
    )

    class StubClient:
        def stats(self, instance_id):
            assert instance_id == "inst_123"
            return contracts.StatsResult(entity_count=1, edge_count=0)

        def lint(
            self,
            instance_id,
            *,
            max_findings=100,
            analysis_limit=200,
            min_support=5,
            exclude_orphan_types=None,
        ):
            assert instance_id == "inst_123"
            assert max_findings == 5
            assert analysis_limit == 10
            assert min_support == 2
            assert exclude_orphan_types == ["Log"]
            return contracts.LintResult(
                config_name="demo",
                evaluation=contracts.EvaluateResult(
                    entity_count=1,
                    edge_count=0,
                    findings=[],
                    summary={},
                ),
            )

        def inspect_entity(
            self,
            instance_id,
            entity_type,
            entity_id,
            *,
            direction="both",
            relationship_type=None,
            limit=None,
        ):
            assert (instance_id, entity_type, entity_id) == ("inst_123", "Asset", "A1")
            assert direction == "outgoing"
            assert relationship_type == "runs"
            assert limit == 3
            return contracts.InspectEntityResult(
                found=True,
                entity_type="Asset",
                entity_id="A1",
            )

        def inspect_view(self, instance_id, view, *, limit=200):
            assert instance_id == "inst_123"
            assert view == "governance"
            assert limit == 7
            return contracts.CanonicalViewResult(view=view, payload={"pending_total": 0})

        def render_wiki(
            self,
            instance_id,
            *,
            focus=None,
            include_types=None,
            scope=None,
            max_per_type=50,
            all_subjects=False,
        ):
            assert instance_id == "inst_123"
            assert focus == ["Asset:A1"]
            assert include_types == ["Asset"]
            assert scope == "local"
            assert max_per_type == 25
            assert all_subjects is False
            return contracts.WikiRenderResult(
                pages=[contracts.WikiPageResult(path="index.md", content="# Demo")],
                page_count=1,
            )

        def list_snapshots(self, instance_id, *, limit=None, offset=0):
            assert instance_id == "inst_123"
            return contracts.SnapshotListResult(items=[snapshot], total=1)

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())

    assert handlers.handle_stats("inst_123").entity_count == 1
    assert not handlers.handle_lint(
        "inst_123",
        max_findings=5,
        analysis_limit=10,
        min_support=2,
        exclude_orphan_types=["Log"],
    ).has_issues
    assert handlers.handle_inspect_entity(
        "inst_123",
        "Asset",
        "A1",
        direction="outgoing",
        relationship_type="runs",
        limit=3,
    ).found
    assert handlers.handle_inspect_view("inst_123", "governance", limit=7).view == "governance"
    assert (
        handlers.handle_render_wiki(
            "inst_123",
            focus=["Asset:A1"],
            include_types=["Asset"],
            scope="local",
            max_per_type=25,
        ).page_count
        == 1
    )
    assert handlers.handle_list_snapshots("inst_123").items[0].snapshot_id == "snap_1"


def test_new_admin_and_governed_handlers_delegate_to_client(monkeypatch: pytest.MonkeyPatch):
    snapshot = contracts.SnapshotMetadata(
        snapshot_id="snap_1",
        created_at="2026-05-04T00:00:00Z",
        label="baseline",
        config_digest="sha256:cfg",
        lock_digest=None,
        graph_digest="sha256:graph",
        parent_snapshot_id=None,
        origin_snapshot_id=None,
    )

    class StubClient:
        def workflow_test(self, instance_id, *, name=None):
            assert instance_id == "inst_123"
            assert name == "smoke"
            return contracts.WorkflowTestResult(total=1, passed=1, failed=0)

        def reload_config(self, instance_id, *, config_path=None, config_yaml=None):
            assert instance_id == "inst_123"
            assert config_path is None
            assert config_yaml == "name: demo\n"
            return contracts.ReloadConfigResult(
                config_path="/srv/project/config.yaml",
                updated=True,
            )

        def create_snapshot(self, instance_id, *, label=None):
            assert instance_id == "inst_123"
            assert label == "baseline"
            return contracts.SnapshotCreateResult(snapshot=snapshot)

        def clone_snapshot(self, instance_id, *, snapshot_id, root_dir):
            assert instance_id == "inst_123"
            assert snapshot_id == "snap_1"
            assert root_dir == "/srv/clone"
            return contracts.CloneSnapshotResult(instance_id="inst_clone", snapshot=snapshot)

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())

    assert handlers.handle_workflow_test("inst_123", "smoke").passed == 1
    assert handlers.handle_reload_config("inst_123", config_yaml="name: demo\n").updated
    assert handlers.handle_create_snapshot("inst_123", "baseline").snapshot.snapshot_id == "snap_1"
    assert (
        handlers.handle_clone_snapshot("inst_123", "snap_1", "/srv/clone").instance_id
        == "inst_clone"
    )


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
                read_metadata={"any_read_truncated": True},
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
    assert result.read_metadata == {"any_read_truncated": True}
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
                read_metadata={"any_read_truncated": True},
                trace_ids=["TRC-1"],
            )

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())
    result = handlers.handle_workflow_run("inst_123", "wf", {"id": "1"})
    assert result.receipt_id == "RCP-1"
    assert result.read_metadata == {"any_read_truncated": True}


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
                read_metadata={"any_read_truncated": True},
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
    assert result.read_metadata == {"any_read_truncated": True}


@pytest.mark.parametrize(
    ("fn", "args", "label"),
    [
        (handlers.handle_init, ("./project", None, "name: demo", None), "cruxible_init"),
        (
            handlers.handle_create_state_overlay,
            ("./overlay", "file:///tmp/release"),
            "cruxible_state_create_overlay",
        ),
        (handlers.handle_workflow_run, ("inst_123", "wf", {"id": "1"}), "cruxible_run_workflow"),
        (handlers.handle_workflow_test, ("inst_123", None), "cruxible_test_workflow"),
        (handlers.handle_reload_config, ("inst_123",), "cruxible_reload_config"),
        (handlers.handle_create_snapshot, ("inst_123", None), "cruxible_create_snapshot"),
        (
            handlers.handle_clone_snapshot,
            ("inst_123", "snap_1", "/tmp/clone"),
            "cruxible_clone_snapshot",
        ),
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
