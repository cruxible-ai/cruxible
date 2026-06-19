"""CLI server-mode tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from cruxible_client import contracts
from cruxible_core.cli.main import cli
from cruxible_core.server.request_models import RenderWikiRequest
from tests.test_cli.conftest import CAR_PARTS_YAML


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def cli_context_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))


def test_cli_fails_when_server_required_without_endpoint(monkeypatch, runner: CliRunner):
    monkeypatch.setenv("CRUXIBLE_REQUIRE_SERVER", "true")
    result = runner.invoke(cli, ["query", "run", "parts_for_vehicle"])
    assert result.exit_code == 2
    assert "Server mode is required" in result.output


def test_server_mode_sample_json_emits_envelope(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))

    class StubClient:
        def sample(self, _instance_id, entity_type, *, limit):
            return contracts.SampleResult(
                items=[
                    {
                        "entity_type": entity_type,
                        "entity_id": "V-1",
                        "properties": {"vehicle_id": "V-1"},
                    }
                ],
                entity_type=entity_type,
                total=1,
                limit=limit,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_x",
            "sample",
            "--type",
            "Vehicle",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["items"][0]["entity_id"] == "V-1"
    assert payload["total"] == 1
    assert payload["entity_type"] == "Vehicle"


def test_server_mode_inspect_entity_history_json(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))
    captured: dict[str, object] = {}

    class StubClient:
        def inspect_entity_history(
            self,
            instance_id,
            entity_type,
            *,
            entity_id=None,
            limit=50,
            offset=0,
        ):
            captured.update(
                {
                    "instance_id": instance_id,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "limit": limit,
                    "offset": offset,
                }
            )
            return contracts.EntityChangeHistoryResult(
                entity_type=entity_type,
                entity_id=entity_id,
                items=[
                    contracts.EntityChangeHistoryItem(
                        entity_type=entity_type,
                        entity_id=entity_id or "T-1",
                        change_kind="updated",
                        property_changes=[
                            contracts.PropertyChangeItem(
                                property="status",
                                from_value="planned",
                                to_value="active",
                            )
                        ],
                        changed_at="2026-06-15T12:00:00Z",
                        receipt_id="RCP-1",
                        operation_type="add_entity",
                    )
                ],
                total=4,
                limit=limit,
                offset=offset,
                truncated=offset + 1 < 4,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_x",
            "inspect",
            "entity-history",
            "--type",
            "Task",
            "--id",
            "T-1",
            "--limit",
            "5",
            "--offset",
            "1",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert captured == {
        "instance_id": "inst_x",
        "entity_type": "Task",
        "entity_id": "T-1",
        "limit": 5,
        "offset": 1,
    }
    payload = json.loads(result.output)
    assert payload["items"][0]["property_changes"][0]["to_value"] == "active"
    # Server mode emits the contract envelope verbatim (R1).
    assert payload["total"] == 4
    assert payload["limit"] == 5
    assert payload["offset"] == 1
    assert payload["truncated"] is True


def test_server_mode_evaluate_forwards_filters(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))
    captured: dict[str, object] = {}

    class StubClient:
        def evaluate(
            self,
            instance_id,
            *,
            max_findings=100,
            exclude_orphan_types=None,
            severity_filter=None,
            category_filter=None,
        ):
            captured.update(
                {
                    "instance_id": instance_id,
                    "max_findings": max_findings,
                    "exclude_orphan_types": exclude_orphan_types,
                    "severity_filter": severity_filter,
                    "category_filter": category_filter,
                }
            )
            return contracts.EvaluateResult(
                entity_count=1,
                edge_count=0,
                findings=[],
                summary={},
                constraint_summary={},
                quality_summary={},
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_x",
            "evaluate",
            "--limit",
            "1",
            "--severity",
            "error",
            "--category",
            "quality_check_failed",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert captured == {
        "instance_id": "inst_x",
        "max_findings": 1,
        "exclude_orphan_types": None,
        "severity_filter": ["error"],
        "category_filter": ["quality_check_failed"],
    }


def test_server_mode_query_not_found_prints_list_guidance(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    # Remote query failures raise client-package errors; the CLI must catch
    # them and point agents to query discovery without leaking a traceback.
    from cruxible_client.errors import QueryNotFoundError as ClientQueryNotFoundError

    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))

    class StubClient:
        def query(self, *_args, **_kwargs):
            raise ClientQueryNotFoundError("parts_for_vehicle")

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_x",
            "query",
            "run",
            "parts_for_vehicle",
        ],
    )

    assert result.exit_code == 1
    assert "Run: cruxible query list" in result.output
    assert "Error: QueryNotFoundError:" in result.output
    assert "Param hints:" not in result.output
    assert "Traceback" not in result.output


def test_server_mode_client_errors_render_friendly_not_traceback(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    # Client-package errors are a distinct hierarchy from core errors; the CLI
    # must catch both or every server-mode failure leaks a traceback.
    from cruxible_client.errors import DataValidationError as ClientDataValidationError

    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))

    class StubClient:
        def init(self, **_kwargs):
            raise ClientDataValidationError(
                "Request validation failed",
                errors=["query.offset: Input should be a valid integer"],
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        ["--server-url", "http://server", "init", "--root-dir", "/srv/project"],
    )

    assert result.exit_code == 1
    assert "Error: DataValidationError: Request validation failed: query.offset" in result.output
    assert "Traceback" not in result.output


def test_server_mode_init_reads_local_config_and_prints_instance_id(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CAR_PARTS_YAML)
    captured: dict[str, object] = {}

    class StubClient:
        def init(self, *, root_dir, config_path=None, config_yaml=None, data_dir=None):
            captured["root_dir"] = root_dir
            captured["config_path"] = config_path
            captured["config_yaml"] = config_yaml
            captured["data_dir"] = data_dir
            return contracts.InitResult(instance_id="inst_abc123", status="initialized")

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "init",
            "--root-dir",
            "/srv/project",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 0
    assert captured["root_dir"] == "/srv/project"
    assert captured["config_path"] is None
    assert isinstance(captured["config_yaml"], str)
    assert "Instance ID: inst_abc123" in result.output
    assert "Active instance: inst_abc123" in result.output

    shown = runner.invoke(cli, ["context", "show", "--json"])
    assert shown.exit_code == 0
    assert json.loads(shown.output) == {
        "instance_id": "inst_abc123",
        "server_url": "http://server",
    }


def test_server_mode_init_reports_previous_active_instance(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    runner.invoke(
        cli,
        [
            "context",
            "connect",
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_old",
        ],
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CAR_PARTS_YAML)

    class StubClient:
        def init(self, *, root_dir, config_path=None, config_yaml=None, data_dir=None):
            return contracts.InitResult(instance_id="inst_new", status="initialized")

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(cli, ["init", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Active instance: inst_new" in result.output
    assert "Previous active instance: inst_old" in result.output
    shown = runner.invoke(cli, ["context", "show", "--json"])
    assert json.loads(shown.output)["instance_id"] == "inst_new"


def test_server_mode_init_no_activate_leaves_active_instance(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    runner.invoke(
        cli,
        [
            "context",
            "connect",
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_old",
        ],
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CAR_PARTS_YAML)

    class StubClient:
        def init(self, *, root_dir, config_path=None, config_yaml=None, data_dir=None):
            return contracts.InitResult(instance_id="inst_new", status="initialized")

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(cli, ["init", "--config", str(config_path), "--no-activate"])

    assert result.exit_code == 0
    assert "Instance ID: inst_new" in result.output
    assert "Active instance unchanged: inst_old" in result.output
    shown = runner.invoke(cli, ["context", "show", "--json"])
    assert json.loads(shown.output)["instance_id"] == "inst_old"


def test_server_mode_init_defaults_root_dir_to_cwd(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CAR_PARTS_YAML)
    captured: dict[str, object] = {}

    class StubClient:
        def init(self, *, root_dir, config_path=None, config_yaml=None, data_dir=None):
            captured["root_dir"] = root_dir
            return contracts.InitResult(instance_id="inst_abc123", status="initialized")

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "init",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 0
    assert captured["root_dir"] == str(tmp_path)


def test_server_mode_init_sends_kit(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    monkeypatch.chdir(tmp_path)
    captured: dict[str, object] = {}

    class StubClient:
        def init(self, *, root_dir, config_path=None, config_yaml=None, data_dir=None, kit=None):
            captured["root_dir"] = root_dir
            captured["config_yaml"] = config_yaml
            captured["kit"] = kit
            return contracts.InitResult(instance_id="inst_abc123", status="initialized")

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "init",
            "--kit",
            "kev-reference",
        ],
    )

    assert result.exit_code == 0
    assert captured["root_dir"] == str(tmp_path)
    assert captured["config_yaml"] is None
    assert captured["kit"] == "kev-reference"


def test_context_commands_persist_and_show_governed_context(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))

    connect = runner.invoke(
        cli,
        [
            "context",
            "connect",
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
        ],
    )
    assert connect.exit_code == 0
    assert "Remembered governed CLI context." in connect.output

    shown = runner.invoke(cli, ["context", "show", "--json"])
    assert shown.exit_code == 0
    payload = json.loads(shown.output)
    assert payload == {
        "instance_id": "inst_123",
        "server_url": "http://server",
    }

    used = runner.invoke(cli, ["context", "use", "inst_456"])
    assert used.exit_code == 0
    assert "Active instance: inst_456" in used.output

    cleared = runner.invoke(cli, ["context", "clear"])
    assert cleared.exit_code == 0
    assert "Cleared remembered CLI context." in cleared.output


def test_server_info_uses_live_server_surface(
    monkeypatch,
    runner: CliRunner,
):
    captured: dict[str, object] = {}

    class StubClient:
        def server_info(self):
            captured["called"] = True
            return contracts.ServerInfoResult(
                server_required=True,
                state_dir="/srv/cruxible-state",
                version="0.2.0",
                instance_count=3,
                auth_enabled=True,
                auth_required=True,
            )

    monkeypatch.setattr("cruxible_core.cli.commands.server._get_client", lambda: StubClient())
    result = runner.invoke(cli, ["--server-url", "http://server", "server", "info", "--json"])

    assert result.exit_code == 0
    assert captured["called"] is True
    payload = json.loads(result.output)
    assert payload["server_required"] is True
    assert payload["auth_enabled"] is True
    assert payload["auth_required"] is True
    assert payload["state_dir"] == "/srv/cruxible-state"
    assert payload["instance_count"] == 3


def test_cli_uses_persisted_context_for_server_calls(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))
    runner.invoke(
        cli,
        [
            "context",
            "connect",
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
        ],
    )
    captured: dict[str, object] = {}

    class StubClient:
        def __init__(self, *, base_url=None, socket_path=None, token=None):
            captured["base_url"] = base_url
            captured["socket_path"] = socket_path

        def stats(self, instance_id):
            captured["instance_id"] = instance_id
            return contracts.StatsResult(
                entity_count=1,
                edge_count=2,
                entity_counts={"Part": 1},
                relationship_counts={"fits": 2},
                head_snapshot_id=None,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common.CruxibleClient", StubClient)
    result = runner.invoke(cli, ["stats", "--json"])

    assert result.exit_code == 0
    assert captured["base_url"] == "http://server"
    assert captured["instance_id"] == "inst_123"
    payload = json.loads(result.output)
    assert payload["entity_count"] == 1


def test_query_discovery_commands_delegate_to_client_in_server_mode(
    monkeypatch,
    runner: CliRunner,
):
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
                        example_ids=["V-2024-CIVIC-EX"],
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
                example_ids=["V-2024-CIVIC-EX"],
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    runner.invoke(
        cli,
        [
            "context",
            "connect",
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
        ],
    )

    listed = runner.invoke(cli, ["query", "list", "--json"])
    assert listed.exit_code == 0
    list_payload = json.loads(listed.output)
    assert list_payload["items"][0]["name"] == "parts_for_vehicle"
    assert list_payload["items"][0]["required_params"] == ["vehicle_id"]

    bare = runner.invoke(cli, ["query"])
    assert bare.exit_code == 0
    assert "Named Queries" in bare.output
    assert "parts_for_vehicle" in bare.output

    described = runner.invoke(
        cli,
        ["query", "describe", "--query", "parts_for_vehicle", "--json"],
    )
    assert described.exit_code == 0
    describe_payload = json.loads(described.output)
    assert describe_payload["entry_point"] == "Vehicle"
    assert describe_payload["returns"] == "Part"


def test_query_decision_record_requires_explicit_flag(
    monkeypatch,
    runner: CliRunner,
):
    monkeypatch.setenv("CRUXIBLE_DECISION_RECORD_ID", "DR-env")
    captured: dict[str, object] = {}

    class StubClient:
        def query(self, instance_id, query_name, params, limit=None, decision_record_id=None):
            captured["instance_id"] = instance_id
            captured["query_name"] = query_name
            captured["params"] = params
            captured["limit"] = limit
            captured["decision_record_id"] = decision_record_id
            return contracts.QueryToolResult(
                items=[],
                receipt_id="RCP-1",
                receipt=None,
                total=0,
                truncated=False,
                steps_executed=1,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "query",
            "run",
            "parts_for_vehicle",
            "--param",
            "vehicle_id=V-1",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert captured == {
        "instance_id": "inst_123",
        "query_name": "parts_for_vehicle",
        "params": {"vehicle_id": "V-1"},
        "limit": None,
        "decision_record_id": None,
    }


def test_query_uses_explicit_decision_record_flag(
    monkeypatch,
    runner: CliRunner,
):
    captured: dict[str, object] = {}

    class StubClient:
        def query(self, instance_id, query_name, params, limit=None, decision_record_id=None):
            captured["decision_record_id"] = decision_record_id
            return contracts.QueryToolResult(
                items=[],
                receipt_id="RCP-1",
                receipt=None,
                total=0,
                truncated=False,
                steps_executed=1,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "query",
            "run",
            "parts_for_vehicle",
            "--param",
            "vehicle_id=V-1",
            "--decision-record",
            "DR-flag",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert captured["decision_record_id"] == "DR-flag"


def test_query_inline_uses_server_client(monkeypatch, runner: CliRunner):
    captured: dict[str, object] = {}

    class StubClient:
        def query_inline(
            self,
            instance_id,
            definition,
            params,
            *,
            limit=None,
            relationship_state=None,
            decision_record_id=None,
        ):
            captured["instance_id"] = instance_id
            captured["definition"] = definition
            captured["params"] = params
            captured["limit"] = limit
            captured["relationship_state"] = relationship_state
            captured["decision_record_id"] = decision_record_id
            return contracts.QueryToolResult(
                items=[],
                receipt_id="RCP-inline",
                receipt=None,
                total=0,
                limit=50,
                truncated=False,
                steps_executed=0,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "query",
            "inline",
            "--definition-json",
            ('{"name":"brake_parts","mode":"collection","returns":"Part","result_shape":"entity"}'),
            "--param",
            "category=brakes",
            "--limit",
            "25",
            "--relationship-state",
            "reviewable",
            "--decision-record",
            "DR-1",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    definition = captured["definition"]
    assert isinstance(definition, contracts.InlineQueryDefinition)
    assert captured == {
        "instance_id": "inst_123",
        "definition": definition,
        "params": {"category": "brakes"},
        "limit": 25,
        "relationship_state": "reviewable",
        "decision_record_id": "DR-1",
    }
    assert definition.name == "brake_parts"
    assert json.loads(result.output)["receipt_id"] == "RCP-inline"


def test_query_inline_rejects_malformed_definition(runner: CliRunner):
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "query",
            "inline",
            "--definition-json",
            '{"name":"broken","mode":"collection"}',
        ],
    )

    assert result.exit_code == 2
    assert "inline query definition is invalid" in result.output


def test_decision_record_commands_delegate_to_client_in_server_mode(
    monkeypatch,
    runner: CliRunner,
):
    captured: dict[str, object] = {}

    class StubClient:
        def create_decision_record(
            self,
            instance_id,
            *,
            question,
            subject_type=None,
            subject_id=None,
            opened_by="human",
        ):
            captured["create"] = (instance_id, question, subject_type, subject_id, opened_by)
            return contracts.DecisionRecordResult(
                record={
                    "decision_record_id": "DR-1",
                    "question": question,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "status": "open",
                }
            )

        def get_decision_record(self, instance_id, decision_record_id, *, include_events=True):
            captured["get"] = (instance_id, decision_record_id, include_events)
            return contracts.DecisionRecordResult(
                record={
                    "decision_record_id": decision_record_id,
                    "question": "Should we act?",
                    "status": "open",
                },
                events=[
                    {
                        "sequence": 1,
                        "command": "query:impact",
                        "status": "success",
                        "receipt_id": "RCP-1",
                    }
                ],
            )

        def list_decision_records(
            self,
            instance_id,
            *,
            status=None,
            subject_type=None,
            subject_id=None,
            decision_class=None,
            limit=100,
            offset=0,
        ):
            captured["list"] = (
                instance_id,
                status,
                subject_type,
                subject_id,
                decision_class,
                limit,
            )
            return contracts.DecisionRecordListResult(
                items=[
                    {
                        "decision_record_id": "DR-1",
                        "question": "Should we act?",
                        "status": "open",
                    }
                ],
                total=1,
            )

        def finalize_decision_record(
            self,
            instance_id,
            decision_record_id,
            *,
            final_decision,
            decision_class,
            rationale="",
        ):
            captured["finalize"] = (
                instance_id,
                decision_record_id,
                final_decision,
                decision_class,
                rationale,
            )
            return contracts.DecisionRecordResult(
                record={
                    "decision_record_id": decision_record_id,
                    "question": "Should we act?",
                    "status": "finalized",
                    "final_decision": final_decision,
                    "decision_class": decision_class,
                    "rationale": rationale,
                }
            )

        def abandon_decision_record(self, instance_id, decision_record_id, *, reason=""):
            captured["abandon"] = (instance_id, decision_record_id, reason)
            return contracts.DecisionRecordResult(
                record={
                    "decision_record_id": decision_record_id,
                    "question": "Should we act?",
                    "status": "abandoned",
                    "abandoned_reason": reason,
                }
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())

    create = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "decision-record",
            "create",
            "--question",
            "Should we act?",
            "--subject-type",
            "Incident",
            "--subject-id",
            "I-1",
            "--opened-by",
            "agent",
            "--json",
        ],
    )
    assert create.exit_code == 0
    assert json.loads(create.output)["record"]["decision_record_id"] == "DR-1"

    get = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "decision-record",
            "get",
            "--id",
            "DR-1",
            "--json",
        ],
    )
    assert get.exit_code == 0
    assert json.loads(get.output)["events"][0]["receipt_id"] == "RCP-1"

    listed = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "decision-record",
            "list",
            "--status",
            "open",
            "--limit",
            "5",
            "--json",
        ],
    )
    assert listed.exit_code == 0
    assert json.loads(listed.output)["items"][0]["decision_record_id"] == "DR-1"

    finalized = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "decision-record",
            "finalize",
            "--id",
            "DR-1",
            "--final-decision",
            "Take action",
            "--decision-class",
            "recommended",
            "--rationale",
            "Evidence supports it",
            "--json",
        ],
    )
    assert finalized.exit_code == 0
    assert json.loads(finalized.output)["record"]["decision_class"] == "recommended"

    abandoned = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "decision-record",
            "abandon",
            "--id",
            "DR-2",
            "--reason",
            "Superseded",
            "--json",
        ],
    )
    assert abandoned.exit_code == 0
    assert json.loads(abandoned.output)["record"]["status"] == "abandoned"

    assert captured["create"] == (
        "inst_123",
        "Should we act?",
        "Incident",
        "I-1",
        "agent",
    )
    assert captured["get"] == ("inst_123", "DR-1", True)
    assert captured["list"] == ("inst_123", "open", None, None, None, 5)
    assert captured["finalize"] == (
        "inst_123",
        "DR-1",
        "Take action",
        "recommended",
        "Evidence supports it",
    )
    assert captured["abandon"] == ("inst_123", "DR-2", "Superseded")


def test_inspect_ontology_uses_server_inspect_view_surface(
    monkeypatch,
    runner: CliRunner,
):
    class StubClient:
        def inspect_view(self, instance_id, view, *, limit=200):
            assert instance_id == "inst_123"
            assert view == "ontology"
            assert limit == 200
            return contracts.CanonicalViewResult(
                view=view,
                payload={
                    "entity_count": 2,
                    "relationship_count": 2,
                    "governed_relationship_count": 0,
                    "entity_types": [],
                    "relationships": [
                        {
                            "name": "fits",
                            "from_entity": "Part",
                            "to_entity": "Vehicle",
                            "mode": "deterministic",
                            "cardinality": "many_to_many",
                            "reverse_name": None,
                            "description": None,
                            "instance_count": 3,
                        }
                    ],
                },
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    runner.invoke(
        cli,
        [
            "context",
            "connect",
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
        ],
    )

    result = runner.invoke(cli, ["inspect", "ontology", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["entity_count"] == 2
    assert payload["relationship_count"] == 2
    assert payload["relationships"][0]["instance_count"] in (1, 3)


def test_inspect_overview_uses_server_inspect_view_surface(
    monkeypatch,
    runner: CliRunner,
):
    class StubClient:
        def inspect_view(self, instance_id, view, *, limit=200):
            assert instance_id == "inst_123"
            assert view == "overview"
            assert limit == 200
            return contracts.CanonicalViewResult(
                view=view,
                payload={
                    "ontology": {
                        "entity_count": 2,
                        "relationship_count": 1,
                        "governed_relationship_count": 0,
                        "entity_types": [
                            {
                                "name": "Vehicle",
                                "primary_key": "vehicle_id",
                                "property_count": 1,
                                "description": None,
                            }
                        ],
                        "relationships": [
                            {
                                "name": "fits",
                                "from_entity": "Part",
                                "to_entity": "Vehicle",
                                "mode": "deterministic",
                                "cardinality": "many_to_many",
                                "reverse_name": None,
                                "description": None,
                                "instance_count": 3,
                            }
                        ],
                    },
                    "workflows": {
                        "workflow_count": 1,
                        "workflows": [
                            {
                                "name": "sync_catalog",
                                "mode": "utility",
                                "step_count": 1,
                                "queries": [],
                                "providers": [],
                                "provider_details": [],
                                "consumes_relationships": [],
                                "proposes_relationships": [],
                                "applies_relationships": ["fits"],
                                "steps": [],
                            }
                        ],
                        "dependencies": [],
                    },
                    "queries": {
                        "query_count": 1,
                        "queries": [
                            {
                                "name": "parts_for_vehicle",
                                "mode": "traversal",
                                "entry_point": "Vehicle",
                                "required_params": ["vehicle_id"],
                                "returns": "Part",
                                "description": "Find compatible parts.",
                                "example_ids": ["V-2024-CIVIC-EX"],
                                "traversal_summary": ["Vehicle", "Part"],
                            }
                        ],
                    },
                    "governance": {
                        "governed_relationship_count": 0,
                        "pending_group_count": 0,
                        "total_pending_groups": 0,
                        "approved_resolution_count": 0,
                        "total_resolutions": 0,
                        "pending_truncated": False,
                        "resolutions_truncated": False,
                        "relationships": [],
                        "pending_buckets": [],
                    },
                },
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    runner.invoke(
        cli,
        [
            "context",
            "connect",
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
        ],
    )

    result = runner.invoke(cli, ["inspect", "overview"])
    assert result.exit_code == 0
    assert "# Config Overview" in result.output
    assert "## Workflow Chain" in result.output
    assert "parts_for_vehicle" in result.output


def test_explicit_transport_overrides_remembered_opposite_transport(
    monkeypatch,
    runner: CliRunner,
):
    runner.invoke(
        cli,
        [
            "context",
            "connect",
            "--server-socket",
            "/tmp/cruxible.sock",
            "--instance-id",
            "inst_socket",
        ],
    )
    captured: dict[str, object] = {}

    class StubClient:
        def __init__(self, *, base_url=None, socket_path=None, token=None):
            captured["base_url"] = base_url
            captured["socket_path"] = socket_path

        def stats(self, instance_id):
            captured["instance_id"] = instance_id
            return contracts.StatsResult(
                entity_count=1,
                edge_count=0,
                entity_counts={},
                relationship_counts={},
                head_snapshot_id=None,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common.CruxibleClient", StubClient)
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_http",
            "stats",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert captured["base_url"] == "http://server"
    assert captured["socket_path"] is None
    assert captured["instance_id"] == "inst_http"


def test_instance_id_env_is_ignored_in_favor_of_cli_context(
    monkeypatch,
    runner: CliRunner,
):
    runner.invoke(
        cli,
        [
            "context",
            "connect",
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_context",
        ],
    )
    monkeypatch.setenv("CRUXIBLE_INSTANCE_ID", "inst_env")
    captured: dict[str, object] = {}

    class StubClient:
        def stats(self, instance_id):
            captured["instance_id"] = instance_id
            return contracts.StatsResult(
                entity_count=1,
                edge_count=0,
                entity_counts={},
                relationship_counts={},
                head_snapshot_id=None,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(cli, ["stats", "--json"])

    assert result.exit_code == 0
    assert captured["instance_id"] == "inst_context"


def test_context_connect_clears_instance_when_transport_changes(
    runner: CliRunner,
):
    runner.invoke(
        cli,
        [
            "context",
            "connect",
            "--server-url",
            "http://server-a",
            "--instance-id",
            "inst_a",
        ],
    )

    switched = runner.invoke(
        cli,
        [
            "context",
            "connect",
            "--server-url",
            "http://server-b",
        ],
    )
    assert switched.exit_code == 0
    assert "Server URL: http://server-b" in switched.output
    assert "Instance ID:" not in switched.output

    shown = runner.invoke(cli, ["context", "show", "--json"])
    assert shown.exit_code == 0
    assert json.loads(shown.output) == {"server_url": "http://server-b"}


def test_server_mode_validate_composes_overlay_before_upload(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    base = tmp_path / "base.yaml"
    base.write_text(
        'version: "1.0"\n'
        "name: base\n"
        "entity_types:\n"
        "  Case:\n"
        "    properties:\n"
        "      case_id: {type: string, primary_key: true}\n"
        "relationships:\n"
        "  - name: cites\n"
        "    from: Case\n"
        "    to: Case\n"
    )
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        'version: "1.0"\n'
        "name: overlay\n"
        "extends: base.yaml\n"
        "entity_types: {}\n"
        "relationships:\n"
        "  - name: follows\n"
        "    from: Case\n"
        "    to: Case\n"
    )
    captured: dict[str, object] = {}

    class StubClient:
        def validate(self, *, config_path=None, config_yaml=None):
            captured["config_path"] = config_path
            captured["config_yaml"] = config_yaml
            return contracts.ValidateResult(
                valid=True,
                name="overlay",
                entity_types=["Case"],
                relationships=["cites", "follows"],
                named_queries=[],
                warnings=[],
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "validate",
            "--config",
            str(overlay),
        ],
    )

    assert result.exit_code == 0
    assert captured["config_path"] is None
    assert isinstance(captured["config_yaml"], str)
    assert "extends:" not in captured["config_yaml"]
    assert "Case:" in captured["config_yaml"]
    assert "follows" in captured["config_yaml"]
    assert "Config 'overlay' is valid." in result.output


def test_server_mode_lint_delegates_to_client_and_exits_one_on_issues(
    monkeypatch,
    runner: CliRunner,
):
    captured: dict[str, object] = {}

    class StubClient:
        def lint(
            self,
            instance_id,
            *,
            max_findings=100,
            analysis_limit=200,
            min_support=5,
            exclude_orphan_types=None,
        ):
            captured["instance_id"] = instance_id
            captured["payload"] = {
                "max_findings": max_findings,
                "analysis_limit": analysis_limit,
                "min_support": min_support,
                "exclude_orphan_types": exclude_orphan_types,
            }
            return contracts.LintResult(
                config_name="car_parts_compatibility",
                config_warnings=[],
                compatibility_warnings=[],
                evaluation=contracts.EvaluateResult(
                    entity_count=4,
                    edge_count=3,
                    findings=[
                        {
                            "severity": "warning",
                            "message": "Unreviewed relationship found",
                        }
                    ],
                    summary={"unreviewed": 1},
                    constraint_summary={},
                    quality_summary={},
                ),
                feedback_reports=[],
                outcome_reports=[],
                summary=contracts.LintSummary(
                    evaluation_finding_count=1,
                ),
                has_issues=True,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "lint",
            "--max-findings",
            "5",
            "--analysis-limit",
            "50",
            "--min-support",
            "2",
            "--exclude-orphan-type",
            "Vehicle",
        ],
    )

    assert result.exit_code == 1
    assert captured["instance_id"] == "inst_123"
    assert captured["payload"] == {
        "max_findings": 5,
        "analysis_limit": 50,
        "min_support": 2,
        "exclude_orphan_types": ["Vehicle"],
    }
    assert "Lint report for 'car_parts_compatibility'" in result.output
    assert "Graph findings:" in result.output
    assert "Lint found issues." in result.output


def test_server_mode_lint_json_exits_zero_when_clean(
    monkeypatch,
    runner: CliRunner,
):
    class StubClient:
        def lint(
            self,
            instance_id,
            *,
            max_findings=100,
            analysis_limit=200,
            min_support=5,
            exclude_orphan_types=None,
        ):
            return contracts.LintResult(
                config_name="car_parts_compatibility",
                config_warnings=[],
                compatibility_warnings=[],
                evaluation=contracts.EvaluateResult(
                    entity_count=0,
                    edge_count=0,
                    findings=[],
                    summary={},
                    constraint_summary={},
                    quality_summary={},
                ),
                feedback_reports=[],
                outcome_reports=[],
                summary=contracts.LintSummary(),
                has_issues=False,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "lint",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["config_name"] == "car_parts_compatibility"
    assert payload["has_issues"] is False
    assert payload["summary"]["evaluation_finding_count"] == 0


def test_explain_delegates_to_client_in_server_mode(monkeypatch, runner: CliRunner):
    captured: dict[str, object] = {}

    class StubClient:
        def explain_receipt(self, instance_id, receipt_id, *, format="markdown"):
            captured["instance_id"] = instance_id
            captured["receipt_id"] = receipt_id
            captured["format"] = format
            return contracts.ReceiptExplanationResult(
                receipt_id=receipt_id,
                format=format,
                content="# Receipt R1\n",
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "explain",
            "--receipt",
            "R1",
        ],
    )
    assert result.exit_code == 0
    assert captured == {
        "instance_id": "inst_123",
        "receipt_id": "R1",
        "format": "markdown",
    }
    assert "# Receipt R1" in result.output


def test_local_only_server_mode_error_uses_current_wording(monkeypatch, runner: CliRunner):
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: object())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "export",
            "edges",
            "--output",
            "edges.csv",
        ],
    )
    assert result.exit_code == 2
    assert "export edges is local-only and is not available in server mode" in result.output
    assert "wait for v2" not in result.output


def test_render_wiki_delegates_to_client_and_writes_files(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    captured: dict[str, object] = {}

    class StubClient:
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
            captured["instance_id"] = instance_id
            captured["focus"] = focus
            captured["include_types"] = include_types
            captured["scope"] = scope
            captured["max_per_type"] = max_per_type
            captured["all_subjects"] = all_subjects
            return contracts.WikiRenderResult(
                pages=[
                    contracts.WikiPageResult(path="index.md", content="# Demo Wiki\n"),
                    contracts.WikiPageResult(
                        path="subjects/asset/a1.md",
                        content="# Asset A1\n",
                    ),
                ],
                page_count=2,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    output_dir = tmp_path / "wiki"
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "render-wiki",
            "--output",
            str(output_dir),
            "--focus",
            "Asset:A1",
            "--include-type",
            "Asset",
        ],
    )

    assert result.exit_code == 0
    assert captured == {
        "instance_id": "inst_123",
        "focus": ["Asset:A1"],
        "include_types": ["Asset"],
        "scope": "local",
        "max_per_type": 50,
        "all_subjects": False,
    }
    assert "Rendered" in result.output
    assert (output_dir / "index.md").read_text() == "# Demo Wiki\n"
    assert (output_dir / "subjects" / "asset" / "a1.md").read_text() == "# Asset A1\n"


def test_render_wiki_all_subjects_alias_delegates_as_all_scope(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    captured: dict[str, object] = {}

    class StubClient:
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
            captured["instance_id"] = instance_id
            captured["focus"] = focus
            captured["include_types"] = include_types
            captured["scope"] = scope
            captured["max_per_type"] = max_per_type
            captured["all_subjects"] = all_subjects
            return contracts.WikiRenderResult(
                pages=[contracts.WikiPageResult(path="index.md", content="# Demo Wiki\n")],
                page_count=1,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "render-wiki",
            "--output",
            str(tmp_path / "wiki"),
            "--all-subjects",
        ],
    )

    assert result.exit_code == 0
    assert captured["scope"] == "all"
    assert captured["all_subjects"] is True
    assert "deprecated" in result.output


def test_render_wiki_request_rejects_conflicting_scope_and_all_subjects() -> None:
    with pytest.raises(ValidationError, match="all_subjects=true"):
        RenderWikiRequest(scope="local", all_subjects=True)


def test_workflow_commands_delegate_to_client_in_server_mode(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    input_path = tmp_path / "input.yaml"
    input_path.write_text("sku: SKU-123\n")
    captured: dict[str, bool] = {}

    class StubClient:
        def workflow_lock(self, instance_id, *, force=False):
            assert instance_id == "inst_123"
            captured["force"] = force
            return contracts.WorkflowLockResult(
                lock_path="/srv/project/.cruxible/cruxible.lock.yaml",
                config_digest="sha256:abc",
                providers_locked=1,
                artifacts_locked=0,
            )

        def workflow_plan(self, instance_id, *, workflow_name, input_payload=None):
            assert instance_id == "inst_123"
            assert workflow_name == "wf"
            assert input_payload == {"sku": "SKU-123"}
            return contracts.WorkflowPlanResult(plan={"workflow": "wf", "steps": []})

        def workflow_run(self, instance_id, *, workflow_name, input_payload=None):
            assert instance_id == "inst_123"
            return contracts.WorkflowRunResult(
                workflow=workflow_name,
                output={"decision": "approve"},
                receipt_id="RCP-1",
                read_metadata={"any_read_truncated": True},
                trace_ids=["TRC-1"],
            )

        def workflow_test(self, instance_id, *, name=None):
            assert instance_id == "inst_123"
            assert name == "smoke"
            return contracts.WorkflowTestResult(
                total=1,
                passed=1,
                failed=0,
                cases=[
                    contracts.WorkflowTestCaseResult(
                        name="smoke",
                        workflow="wf",
                        passed=True,
                        receipt_id="RCP-1",
                    )
                ],
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())

    lock = runner.invoke(
        cli, ["--server-url", "http://server", "--instance-id", "inst_123", "lock", "--force"]
    )
    assert lock.exit_code == 0
    assert "Workflow lock updated on server." in lock.output
    assert captured["force"] is True
    assert "digest=sha256:abc" in lock.output
    assert "/srv/project/.cruxible/cruxible.lock.yaml" not in lock.output

    plan = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "plan",
            "--workflow",
            "wf",
            "--input-file",
            str(input_path),
        ],
    )
    assert plan.exit_code == 0
    assert '"workflow": "wf"' in plan.output

    run = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "run",
            "--workflow",
            "wf",
            "--input-file",
            str(input_path),
        ],
    )
    assert run.exit_code == 0
    assert "Receipt ID: RCP-1" in run.output

    run_json = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "run",
            "--workflow",
            "wf",
            "--input-file",
            str(input_path),
            "--json",
        ],
    )
    assert run_json.exit_code == 0
    assert json.loads(run_json.output)["read_metadata"] == {"any_read_truncated": True}

    test = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "test",
            "--name",
            "smoke",
        ],
    )
    assert test.exit_code == 0
    assert "1 passed, 0 failed, 1 total" in test.output


def test_run_apply_shortcut_is_removed(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.yaml"
    input_path.write_text("sku: SKU-123\n")
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "run",
            "--workflow",
            "wf",
            "--input-file",
            str(input_path),
            "--apply",
        ],
    )

    assert result.exit_code == 2
    assert "No such option: --apply" in result.output


def test_workflow_apply_explicit_digest_delegates_to_client(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    captured: dict[str, object] = {}

    class StubClient:
        def workflow_apply(
            self,
            instance_id,
            *,
            workflow_name,
            expected_apply_digest,
            expected_head_snapshot_id,
            input_payload,
        ):
            assert instance_id == "inst_123"
            captured["workflow_name"] = workflow_name
            captured["expected_apply_digest"] = expected_apply_digest
            captured["expected_head_snapshot_id"] = expected_head_snapshot_id
            captured["input_payload"] = input_payload
            return contracts.WorkflowApplyResult(
                workflow=workflow_name,
                output={"ok": True},
                receipt_id="RCP-apply",
                apply_digest=expected_apply_digest,
                head_snapshot_id=expected_head_snapshot_id,
                committed_snapshot_id="snap_committed",
                read_metadata={"any_read_truncated": True},
                trace_ids=[],
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "apply",
            "--workflow",
            "build_reference",
            "--input",
            '{"vendor": "acme"}',
            "--apply-digest",
            "sha256:manual",
            "--head-snapshot",
            "snap_manual",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert captured == {
        "workflow_name": "build_reference",
        "expected_apply_digest": "sha256:manual",
        "expected_head_snapshot_id": "snap_manual",
        "input_payload": {"vendor": "acme"},
    }
    payload = json.loads(result.output)
    assert payload["read_metadata"] == {"any_read_truncated": True}


def test_workflow_apply_preview_file_delegates_to_client(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    preview_file = tmp_path / "preview.json"
    preview_file.write_text(
        json.dumps(
            {
                "kind": "workflow_preview",
                "version": 1,
                "workflow": "build_reference",
                "input": {"vendor": "acme"},
                "apply_digest": "sha256:preview",
                "head_snapshot_id": "snap_preview",
            }
        )
    )

    class StubClient:
        def workflow_apply(
            self,
            instance_id,
            *,
            workflow_name,
            expected_apply_digest,
            expected_head_snapshot_id,
            input_payload,
        ):
            assert instance_id == "inst_123"
            captured["workflow_name"] = workflow_name
            captured["expected_apply_digest"] = expected_apply_digest
            captured["expected_head_snapshot_id"] = expected_head_snapshot_id
            captured["input_payload"] = input_payload
            return contracts.WorkflowApplyResult(
                workflow=workflow_name,
                output={"ok": True},
                receipt_id="RCP-apply",
                apply_digest=expected_apply_digest,
                head_snapshot_id=expected_head_snapshot_id,
                committed_snapshot_id="snap_committed",
                trace_ids=[],
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "apply",
            "--preview-file",
            str(preview_file),
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert captured == {
        "workflow_name": "build_reference",
        "expected_apply_digest": "sha256:preview",
        "expected_head_snapshot_id": "snap_preview",
        "input_payload": {"vendor": "acme"},
    }


def test_workflow_apply_from_last_preview_uses_stored_preview(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    captured: dict[str, object] = {}

    class StubClient:
        def list(
            self,
            instance_id,
            *,
            resource_type,
            query_name=None,
            operation_type=None,
            limit=50,
        ):
            assert instance_id == "inst_123"
            assert resource_type == "receipts"
            assert query_name == "build_reference"
            assert operation_type == "workflow"
            assert limit == 50
            return contracts.ListResult(
                items=[{"receipt_id": "RCP-preview"}],
                total=1,
            )

        def receipt(self, instance_id, receipt_id):
            assert instance_id == "inst_123"
            assert receipt_id == "RCP-preview"
            return {
                "receipt_id": "RCP-preview",
                "query_name": "build_reference",
                "parameters": {"vendor": "acme"},
                "operation_type": "workflow",
                "workflow_mode": "preview",
                "head_snapshot_id": "snap_preview",
                "created_at": "2026-05-12T12:00:00Z",
                "nodes": [
                    {
                        "node_id": "root",
                        "node_type": "workflow",
                        "detail": {"apply_digest": "sha256:preview"},
                    }
                ],
                "edges": [],
                "results": [],
            }

        def workflow_apply(
            self,
            instance_id,
            *,
            workflow_name,
            expected_apply_digest,
            expected_head_snapshot_id,
            input_payload,
        ):
            assert instance_id == "inst_123"
            captured["workflow_name"] = workflow_name
            captured["expected_apply_digest"] = expected_apply_digest
            captured["expected_head_snapshot_id"] = expected_head_snapshot_id
            captured["input_payload"] = input_payload
            return contracts.WorkflowApplyResult(
                workflow=workflow_name,
                output={"ok": True},
                receipt_id="RCP-apply",
                apply_digest=expected_apply_digest,
                head_snapshot_id=expected_head_snapshot_id,
                committed_snapshot_id="snap_committed",
                trace_ids=[],
            )

    monkeypatch.setattr("cruxible_core.cli.commands.workflows._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "apply",
            "--workflow",
            "build_reference",
            "--from-last-preview",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert captured == {
        "workflow_name": "build_reference",
        "expected_apply_digest": "sha256:preview",
        "expected_head_snapshot_id": "snap_preview",
        "input_payload": {"vendor": "acme"},
    }
    payload = json.loads(result.output)
    assert payload["committed_snapshot_id"] == "snap_committed"


def test_workflow_apply_requires_explicit_preview_in_noninteractive_mode(
    runner: CliRunner,
) -> None:
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "apply",
            "--workflow",
            "build_reference",
        ],
    )

    assert result.exit_code == 2
    assert "--apply-digest, --preview-file, or --from-last-preview is required" in result.output


def test_propose_json_includes_suppressed_members(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    input_path = tmp_path / "input.yaml"
    input_path.write_text("campaign_id: CMP-1\n")

    class StubClient:
        def propose_workflow(self, instance_id, *, workflow_name, input_payload=None):
            assert instance_id == "inst_123"
            assert workflow_name == "wf"
            assert input_payload == {"campaign_id": "CMP-1"}
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
                read_metadata={"any_read_truncated": True},
                trace_ids=["TRC-1"],
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "propose",
            "--workflow",
            "wf",
            "--input-file",
            str(input_path),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["suppressed"] is True
    assert payload["read_metadata"] == {"any_read_truncated": True}
    assert payload["suppressed_members"] == [
        {
            "relationship_type": "recommended_for",
            "from_type": "Campaign",
            "from_id": "CMP-1",
            "to_type": "Product",
            "to_id": "SKU-123",
            "reason": "pending_proposal",
            "existing_group_id": "GRP-1",
            "existing_group_status": "pending_review",
            "existing_signature": "sig-1",
            "source_workflow_name": "wf",
        }
    ]


def test_propose_json_includes_no_candidates_status(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    input_path = tmp_path / "input.yaml"
    input_path.write_text("campaign_id: CMP-1\n")

    class StubClient:
        def propose_workflow(self, instance_id, *, workflow_name, input_payload=None):
            assert instance_id == "inst_123"
            assert workflow_name == "wf"
            assert input_payload == {"campaign_id": "CMP-1"}
            return contracts.WorkflowProposeResult(
                workflow="wf",
                output={
                    "status": "no_candidates",
                    "candidate_count": 0,
                    "group_created": False,
                },
                receipt_id="RCP-1",
                group_id=None,
                group_status="no_candidates",
                review_priority="normal",
                trace_ids=[],
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "propose",
            "--workflow",
            "wf",
            "--input-file",
            str(input_path),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["group_id"] is None
    assert payload["group_status"] == "no_candidates"
    assert payload["output"]["group_created"] is False


def test_propose_human_output_prints_suppressed_members(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    input_path = tmp_path / "input.yaml"
    input_path.write_text("campaign_id: CMP-1\n")

    class StubClient:
        def propose_workflow(self, instance_id, *, workflow_name, input_payload=None):
            return contracts.WorkflowProposeResult(
                workflow=workflow_name,
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
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "propose",
            "--workflow",
            "wf",
            "--input-file",
            str(input_path),
        ],
    )

    assert result.exit_code == 0
    assert "Workflow wf produced no reviewable group." in result.output
    assert "Suppressed members: 1" in result.output
    assert "Campaign:CMP-1 -[recommended_for]-> Product:SKU-123" in result.output


def test_propose_human_output_prints_no_candidates_status(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    input_path = tmp_path / "input.yaml"
    input_path.write_text("campaign_id: CMP-1\n")

    class StubClient:
        def propose_workflow(self, instance_id, *, workflow_name, input_payload=None):
            return contracts.WorkflowProposeResult(
                workflow=workflow_name,
                output={"status": "no_candidates", "group_created": False},
                receipt_id="RCP-1",
                group_id=None,
                group_status="no_candidates",
                review_priority="normal",
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "propose",
            "--workflow",
            "wf",
            "--input-file",
            str(input_path),
        ],
    )

    assert result.exit_code == 0
    assert "Workflow wf completed with no candidates." in result.output
    assert "No candidate group was created." in result.output


def test_propose_snapshot_and_clone_delegate_to_client_in_server_mode(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    input_path = tmp_path / "input.yaml"
    input_path.write_text("campaign_id: CMP-1\n")

    class StubClient:
        def propose_workflow(self, instance_id, *, workflow_name, input_payload=None):
            assert instance_id == "inst_123"
            assert workflow_name == "wf"
            assert input_payload == {"campaign_id": "CMP-1"}
            return contracts.WorkflowProposeResult(
                workflow="wf",
                output={"members": []},
                receipt_id="RCP-1",
                group_id="GRP-1",
                group_status="pending_review",
                review_priority="review",
                trace_ids=["TRC-1"],
            )

        def create_snapshot(self, instance_id, *, label=None):
            assert instance_id == "inst_123"
            assert label == "baseline"
            return contracts.SnapshotCreateResult(
                snapshot=contracts.SnapshotMetadata(
                    snapshot_id="snap_1",
                    created_at="2026-03-21T00:00:00Z",
                    label="baseline",
                    config_digest="sha256:abc",
                    lock_digest=None,
                    graph_digest="sha256:def",
                    parent_snapshot_id=None,
                    origin_snapshot_id=None,
                )
            )

        def list_snapshots(self, instance_id, *, limit=None, offset=0):
            assert instance_id == "inst_123"
            return contracts.SnapshotListResult(
                items=[
                    contracts.SnapshotMetadata(
                        snapshot_id="snap_1",
                        created_at="2026-03-21T00:00:00Z",
                        label="baseline",
                        config_digest="sha256:abc",
                        lock_digest=None,
                        graph_digest="sha256:def",
                        parent_snapshot_id=None,
                        origin_snapshot_id=None,
                    )
                ],
                total=1,
            )

        def clone_snapshot(self, instance_id, *, snapshot_id, root_dir):
            assert instance_id == "inst_123"
            assert snapshot_id == "snap_1"
            return contracts.CloneSnapshotResult(
                instance_id="inst_clone",
                snapshot=contracts.SnapshotMetadata(
                    snapshot_id="snap_1",
                    created_at="2026-03-21T00:00:00Z",
                    label="baseline",
                    config_digest="sha256:abc",
                    lock_digest=None,
                    graph_digest="sha256:def",
                    parent_snapshot_id=None,
                    origin_snapshot_id=None,
                ),
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())

    propose = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "propose",
            "--workflow",
            "wf",
            "--input-file",
            str(input_path),
        ],
    )
    assert propose.exit_code == 0
    assert "group GRP-1" in propose.output

    create = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "snapshot",
            "create",
            "--label",
            "baseline",
        ],
    )
    assert create.exit_code == 0
    assert "Created snapshot snap_1" in create.output

    listed = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "snapshot",
            "list",
        ],
    )
    assert listed.exit_code == 0
    assert "snap_1" in listed.output

    clone = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "clone",
            "--snapshot",
            "snap_1",
            "--root-dir",
            str(tmp_path / "cloned"),
        ],
    )
    assert clone.exit_code == 0
    assert "instance inst_clone" in clone.output
    assert "Active instance: inst_clone" in clone.output
    assert "Previous active instance: inst_123" in clone.output
    shown = runner.invoke(cli, ["context", "show", "--json"])
    assert json.loads(shown.output)["instance_id"] == "inst_clone"


def test_clone_snapshot_no_activate_leaves_active_instance(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    runner.invoke(
        cli,
        [
            "context",
            "connect",
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_old",
        ],
    )

    class StubClient:
        def clone_snapshot(self, instance_id, *, snapshot_id, root_dir):
            assert instance_id == "inst_old"
            return contracts.CloneSnapshotResult(
                instance_id="inst_clone",
                snapshot=contracts.SnapshotMetadata(
                    snapshot_id=snapshot_id,
                    created_at="2026-03-21T00:00:00Z",
                    label=None,
                    config_digest="sha256:abc",
                    lock_digest=None,
                    graph_digest="sha256:def",
                    parent_snapshot_id=None,
                    origin_snapshot_id=None,
                ),
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "clone",
            "--snapshot",
            "snap_1",
            "--root-dir",
            str(tmp_path / "clone"),
            "--no-activate",
        ],
    )

    assert result.exit_code == 0
    assert "instance inst_clone" in result.output
    assert "Active instance unchanged: inst_old" in result.output
    shown = runner.invoke(cli, ["context", "show", "--json"])
    assert json.loads(shown.output)["instance_id"] == "inst_old"


def test_governed_write_commands_delegate_to_client_in_server_mode(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    feedback_items = tmp_path / "feedback.json"
    feedback_items.write_text(
        """[
  {
    "receipt_id": "RCP-1",
    "action": "approve",
    "target": {
      "from_type": "Part",
      "from_id": "BP-1",
      "relationship_type": "fits",
      "to_type": "Vehicle",
      "to_id": "V-1"
    }
  }
]"""
    )

    class StubClient:
        def feedback_batch(self, instance_id, *, items, source):
            assert instance_id == "inst_123"
            assert source == "human"
            assert len(items) == 1
            return contracts.FeedbackBatchResult(
                feedback_ids=["FB-1"],
                applied_count=1,
                total=1,
                receipt_id="RCP-BATCH-1",
            )

        def feedback_from_query(
            self,
            instance_id,
            *,
            receipt_id,
            result_index,
            action,
            source,
            reason,
            reason_code,
            scope_hints,
            corrections,
            group_override,
            path_index,
            path_alias,
        ):
            assert instance_id == "inst_123"
            assert receipt_id == "RCP-QUERY-1"
            assert result_index == 0
            assert action == "approve"
            assert source == "human"
            assert reason == "looks valid"
            assert reason_code == "vendor_mismatch"
            assert scope_hints == {"vendor": "acme"}
            assert corrections is None
            assert group_override is False
            assert path_index == 1
            assert path_alias is None
            return contracts.FeedbackResult(
                feedback_id="FB-QUERY-1",
                applied=True,
                receipt_id="RCP-FB-1",
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())

    feedback = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "feedback-batch",
            "--items-file",
            str(feedback_items),
        ],
    )
    assert feedback.exit_code == 0
    assert "Batch feedback recorded for 1/1 item(s)." in feedback.output

    feedback_from_query = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "feedback-from-query",
            "--receipt",
            "RCP-QUERY-1",
            "--result-index",
            "0",
            "--path-index",
            "1",
            "--action",
            "approve",
            "--reason",
            "looks valid",
            "--reason-code",
            "vendor_mismatch",
            "--scope-hints",
            '{"vendor":"acme"}',
        ],
    )
    assert feedback_from_query.exit_code == 0
    assert "Feedback FB-QUERY-1 applied to graph." in feedback_from_query.output


def test_feedback_explicit_coordinates_without_receipt_forwards_none(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    captured: dict[str, object] = {}

    class StubClient:
        def feedback(self, instance_id, **kwargs):
            captured["instance_id"] = instance_id
            captured.update(kwargs)
            return contracts.FeedbackResult(
                feedback_id="FB-no-receipt",
                applied=True,
                receipt_id="RCP-feedback",
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "feedback",
            "--action",
            "approve",
            "--from-type",
            "Part",
            "--from-id",
            "BP-1",
            "--relationship",
            "fits",
            "--to-type",
            "Vehicle",
            "--to-id",
            "V-1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["instance_id"] == "inst_123"
    assert captured["receipt_id"] is None
    assert captured["action"] == "approve"
    assert "Feedback FB-no-receipt applied to graph." in result.output


def test_reload_config_uploads_composed_yaml_in_server_mode(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    base = tmp_path / "base.yaml"
    base.write_text(
        'version: "1.0"\n'
        "name: base\n"
        "entity_types:\n"
        "  Case:\n"
        "    properties:\n"
        "      case_id: {type: string, primary_key: true}\n"
        "relationships:\n"
        "  - name: cites\n"
        "    from: Case\n"
        "    to: Case\n"
    )
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        'version: "1.0"\n'
        "name: overlay\n"
        "extends: base.yaml\n"
        "entity_types: {}\n"
        "relationships:\n"
        "  - name: follows\n"
        "    from: Case\n"
        "    to: Case\n"
    )
    captured: dict[str, object] = {}

    class StubClient:
        def reload_config(self, instance_id, *, config_path=None, config_yaml=None):
            captured["instance_id"] = instance_id
            captured["config_path"] = config_path
            captured["config_yaml"] = config_yaml
            return contracts.ReloadConfigResult(
                config_path="/daemon/instances/inst_123/config.yaml",
                updated=True,
                warnings=[],
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "reload-config",
            "--config",
            str(overlay),
        ],
    )

    assert result.exit_code == 0
    assert captured["instance_id"] == "inst_123"
    assert captured["config_path"] is None
    assert isinstance(captured["config_yaml"], str)
    assert "extends:" not in captured["config_yaml"]
    assert "follows" in captured["config_yaml"]
    assert "Config updated on server." in result.output


@pytest.mark.parametrize("command", ["add", "update"])
def test_top_level_write_verb_groups_are_not_registered(
    runner: CliRunner,
    command: str,
) -> None:
    result = runner.invoke(cli, [command, "--help"])

    assert result.exit_code == 2
    assert f"No such command '{command}'" in result.output


@pytest.mark.parametrize(
    ("args", "label"),
    [
        (["init", "--config", "config.yaml"], "init"),
        (["run", "--workflow", "wf"], "run"),
        (["entity", "add", "--type", "Vehicle", "--id", "V-1"], "entity add"),
        (["entity", "update", "Vehicle", "V-1", "--set", "make=Honda"], "entity update"),
        (
            [
                "relationship",
                "add",
                "fits",
                "Part",
                "BP-1",
                "Vehicle",
                "V-1",
            ],
            "relationship add",
        ),
        (
            [
                "relationship",
                "update",
                "fits",
                "Part",
                "BP-1",
                "Vehicle",
                "V-1",
                "--set",
                "source=manual",
            ],
            "relationship update",
        ),
        (
            [
                "state",
                "create-overlay",
                "--transport-ref",
                "file:///tmp/release",
                "--root-dir",
                "/tmp/overlay",
            ],
            "state create-overlay",
        ),
    ],
)
def test_local_mutation_commands_require_server_mode(
    runner: CliRunner,
    tmp_path: Path,
    args: list[str],
    label: str,
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CAR_PARTS_YAML)
    if args[:2] == ["init", "--config"]:
        result = runner.invoke(cli, ["init", "--config", str(config_path)])
    else:
        result = runner.invoke(cli, args)
    assert result.exit_code == 2
    assert f"Local mutation disabled for {label}" in result.output


def test_add_relationship_passes_evidence_fields_to_server_client(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    captured: dict[str, object] = {}

    class StubClient:
        def get_relationship(
            self,
            instance_id,
            *,
            from_type,
            from_id,
            relationship_type,
            to_type,
            to_id,
        ):
            captured["preflight"] = (
                instance_id,
                from_type,
                from_id,
                relationship_type,
                to_type,
                to_id,
            )
            return contracts.GetRelationshipResult(
                found=False,
                from_type=from_type,
                from_id=from_id,
                relationship_type=relationship_type,
                to_type=to_type,
                to_id=to_id,
            )

        def batch_direct_write(self, instance_id, payload, *, dry_run=False):
            captured["instance_id"] = instance_id
            captured["payload"] = payload
            captured["dry_run"] = dry_run
            return contracts.BatchDirectWriteResult(
                dry_run=dry_run,
                valid=True,
                relationships_added=1,
                pending_conflicts=[
                    contracts.DirectWriteGroupInteraction(
                        relationship_type="fits",
                        from_type="Part",
                        from_id="BP-1",
                        to_type="Vehicle",
                        to_id="V-1",
                        group_id="GRP-pending",
                        group_status="pending_review",
                        group_signature="sig-pending",
                        source_workflow_name="wf",
                    )
                ],
                receipt_id="RCP-add",
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "relationship",
            "add",
            "--from-type",
            "Part",
            "--from-id",
            "BP-1",
            "--relationship",
            "fits",
            "--to-type",
            "Vehicle",
            "--to-id",
            "V-1",
            "--props",
            '{"verified": true}',
            "--evidence-ref",
            '{"source":"roadmap_doc","source_record_id":"section-p0"}',
            "--source-evidence",
            '{"source_artifact_id":"SRC-1","chunk_id":"CHK-1"}',
            "--evidence-rationale",
            "Accepted direct source-backed assertion.",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["instance_id"] == "inst_123"
    assert captured["preflight"] == ("inst_123", "Part", "BP-1", "fits", "Vehicle", "V-1")
    payload = captured["payload"]
    assert isinstance(payload, contracts.BatchDirectWritePayload)
    relationship = payload.relationships[0]
    assert relationship.properties == {"verified": True}
    assert relationship.evidence_refs[0].source == "roadmap_doc"
    assert relationship.evidence_refs[0].source_record_id == "section-p0"
    assert relationship.source_evidence[0].source_artifact_id == "SRC-1"
    assert relationship.source_evidence[0].chunk_id == "CHK-1"
    assert relationship.evidence_rationale == "Accepted direct source-backed assertion."
    assert "Add relationship Part:BP-1 -[fits]-> Vehicle:V-1 applied." in result.output
    assert "Notice: 1 pending group conflict(s) detected." in result.output


def test_add_relationship_pending_forwards_to_server_client(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    captured: dict[str, object] = {}

    class StubClient:
        def get_relationship(
            self,
            instance_id,
            *,
            from_type,
            from_id,
            relationship_type,
            to_type,
            to_id,
        ):
            return contracts.GetRelationshipResult(
                found=False,
                from_type=from_type,
                from_id=from_id,
                relationship_type=relationship_type,
                to_type=to_type,
                to_id=to_id,
            )

        def batch_direct_write(self, instance_id, payload, *, dry_run=False):
            captured["instance_id"] = instance_id
            captured["payload"] = payload
            captured["dry_run"] = dry_run
            return contracts.BatchDirectWriteResult(
                dry_run=dry_run,
                valid=True,
                relationships_added=1,
                receipt_id="RCP-pending",
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "relationship",
            "add",
            "--from-type",
            "Part",
            "--from-id",
            "BP-1",
            "--relationship",
            "fits",
            "--to-type",
            "Vehicle",
            "--to-id",
            "V-1",
            "--set-json",
            "verified=true",
            "--pending",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = captured["payload"]
    assert isinstance(payload, contracts.BatchDirectWritePayload)
    assert payload.relationships[0].pending is True
    output = json.loads(result.output)
    assert output["relationships_added"] == 1
    assert output["receipt_id"] == "RCP-pending"


def test_batch_direct_write_passes_payload_file_to_server_client(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class StubClient:
        def batch_direct_write(self, instance_id, payload, *, dry_run=False):
            captured["instance_id"] = instance_id
            captured["payload"] = payload
            captured["dry_run"] = dry_run
            return contracts.BatchDirectWriteResult(
                dry_run=dry_run,
                valid=True,
                entities_added=1,
                updated_group_backed_edges=[
                    contracts.DirectWriteGroupInteraction(
                        relationship_type="fits",
                        from_type="Part",
                        from_id="BP-1",
                        to_type="Vehicle",
                        to_id="V-1",
                        group_id="GRP-resolved",
                        group_status="resolved",
                        group_signature="sig-resolved",
                        source_workflow_name="wf",
                        edge_key=3,
                    )
                ],
                receipt_id=None,
            )

    payload_file = tmp_path / "batch.yaml"
    payload_file.write_text(
        """
entities:
  - entity_type: Vehicle
    entity_id: V-BATCH
    properties:
      vehicle_id: V-BATCH
relationships: []
shared_evidence: {}
"""
    )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "batch-direct-write",
            "--payload-file",
            str(payload_file),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["instance_id"] == "inst_123"
    assert captured["dry_run"] is True
    payload = captured["payload"]
    assert isinstance(payload, contracts.BatchDirectWritePayload)
    assert payload.entities[0].entity_id == "V-BATCH"
    assert "Batch direct write validated." in result.output
    assert "Notice: 1 group-backed edge update(s) detected." in result.output


def test_batch_direct_write_json_includes_group_interaction_fields(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    class StubClient:
        def batch_direct_write(self, instance_id, payload, *, dry_run=False):
            return contracts.BatchDirectWriteResult(
                dry_run=dry_run,
                valid=True,
                entities_added=0,
                relationships_added=1,
                pending_conflicts=[
                    contracts.DirectWriteGroupInteraction(
                        relationship_type="fits",
                        from_type="Part",
                        from_id="BP-1",
                        to_type="Vehicle",
                        to_id="V-1",
                        group_id="GRP-pending",
                        group_status="pending_review",
                        group_signature="sig-pending",
                        source_workflow_name="wf",
                    )
                ],
                updated_group_backed_edges=[],
                receipt_id="RCP-batch",
            )

    payload_file = tmp_path / "batch.yaml"
    payload_file.write_text(
        """
entities: []
relationships:
  - from_type: Part
    from_id: BP-1
    relationship_type: fits
    to_type: Vehicle
    to_id: V-1
shared_evidence: {}
"""
    )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "batch-direct-write",
            "--payload-file",
            str(payload_file),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["pending_conflicts"][0]["group_id"] == "GRP-pending"
    assert payload["updated_group_backed_edges"] == []


@pytest.mark.parametrize("dry_run", [False, True])
def test_batch_direct_write_reads_payload_from_stdin(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    dry_run: bool,
) -> None:
    captured: dict[str, object] = {}

    class StubClient:
        def batch_direct_write(self, instance_id, payload, *, dry_run=False):
            captured["instance_id"] = instance_id
            captured["payload"] = payload
            captured["dry_run"] = dry_run
            return contracts.BatchDirectWriteResult(
                dry_run=dry_run,
                valid=True,
                entities_added=1,
                receipt_id="RCP-stdin" if not dry_run else None,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    args = [
        "--server-url",
        "http://server",
        "--instance-id",
        "inst_123",
        "batch-direct-write",
        "--payload-file",
        "-",
    ]
    if dry_run:
        args.append("--dry-run")

    result = runner.invoke(
        cli,
        args,
        input="""\
entities:
  - entity_type: Vehicle
    entity_id: V-STDIN
    properties:
      vehicle_id: V-STDIN
relationships: []
shared_evidence: {}
""",
    )

    assert result.exit_code == 0, result.output
    assert captured["instance_id"] == "inst_123"
    assert captured["dry_run"] is dry_run
    payload = captured["payload"]
    assert isinstance(payload, contracts.BatchDirectWritePayload)
    assert payload.entities[0].entity_id == "V-STDIN"
    expected = "validated" if dry_run else "applied"
    assert f"Batch direct write {expected}." in result.output


def test_add_entity_shorthand_preserves_string_setters_and_json_values(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    captured: dict[str, object] = {}

    class StubClient:
        def get_entity(self, instance_id, entity_type, entity_id):
            captured["preflight"] = (instance_id, entity_type, entity_id)
            return contracts.GetEntityResult(
                found=False,
                entity_type=entity_type,
                entity_id=entity_id,
            )

        def batch_direct_write(self, instance_id, payload, *, dry_run=False):
            captured["instance_id"] = instance_id
            captured["payload"] = payload
            captured["dry_run"] = dry_run
            return contracts.BatchDirectWriteResult(
                dry_run=dry_run,
                valid=True,
                entities_added=1,
                receipt_id="RCP-add-verb",
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "entity",
            "add",
            "Vehicle",
            "V-NEW",
            "--set",
            "region=NO",
            "--set",
            "status=no",
            "--set",
            "version=1.20",
            "--set",
            "code=0755",
            "--set",
            "literal_null=null",
            "--set",
            "empty=",
            "--set-json",
            "year=2025",
            "--set-json",
            'metadata={"ok":true}',
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["preflight"] == ("inst_123", "Vehicle", "V-NEW")
    payload = captured["payload"]
    assert isinstance(payload, contracts.BatchDirectWritePayload)
    assert payload.entities[0].properties == {
        "region": "NO",
        "status": "no",
        "version": "1.20",
        "code": "0755",
        "literal_null": "null",
        "empty": "",
        "year": 2025,
        "metadata": {"ok": True},
    }
    assert json.loads(result.output)["receipt_id"] == "RCP-add-verb"


def test_update_entity_shorthand_requires_existing_entity(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    class StubClient:
        def get_entity(self, instance_id, entity_type, entity_id):
            return contracts.GetEntityResult(
                found=False,
                entity_type=entity_type,
                entity_id=entity_id,
            )

        def batch_direct_write(self, instance_id, payload, *, dry_run=False):
            raise AssertionError("missing entity update should not write")

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "entity",
            "update",
            "Vehicle",
            "V-MISSING",
            "--set",
            "make=Honda",
        ],
    )

    assert result.exit_code == 1
    assert "Error: DataValidationError:" in result.output
    assert "Entity Vehicle:V-MISSING not found" in result.output


def test_update_entity_shorthand_forwards_batch_payload(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    captured: dict[str, object] = {}

    class StubClient:
        def get_entity(self, instance_id, entity_type, entity_id):
            return contracts.GetEntityResult(
                found=True,
                entity_type=entity_type,
                entity_id=entity_id,
            )

        def batch_direct_write(self, instance_id, payload, *, dry_run=False):
            captured["payload"] = payload
            captured["dry_run"] = dry_run
            return contracts.BatchDirectWriteResult(
                dry_run=dry_run,
                valid=True,
                entities_updated=1,
                receipt_id="RCP-update-verb",
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "entity",
            "update",
            "Vehicle",
            "V-1",
            "--set",
            "make=Honda",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = captured["payload"]
    assert isinstance(payload, contracts.BatchDirectWritePayload)
    assert payload.entities[0].entity_id == "V-1"
    assert payload.entities[0].properties == {"make": "Honda"}
    assert captured["dry_run"] is True
    assert "Update entity Vehicle:V-1 validated." in result.output


def test_add_entity_shorthand_rejects_duplicate_fields(
    runner: CliRunner,
) -> None:
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "entity",
            "add",
            "Vehicle",
            "V-1",
            "--set",
            "make=Honda",
            "--set-json",
            "make=true",
        ],
    )

    assert result.exit_code == 2
    assert "duplicate property assignment for 'make'" in result.output


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--set", "make"], "--set must use FIELD=VALUE"),
        (["--set", "=Honda"], "--set field name must not be blank"),
        (["--set-json", "year=not-json"], "must be valid JSON"),
    ],
)
def test_add_entity_shorthand_rejects_malformed_setters(
    runner: CliRunner,
    args: list[str],
    expected: str,
) -> None:
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "entity",
            "add",
            "Vehicle",
            "V-1",
            *args,
        ],
    )

    assert result.exit_code == 2
    assert expected in result.output


def test_add_relationship_shorthand_forwards_properties_and_evidence(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    captured: dict[str, object] = {}

    class StubClient:
        def get_relationship(
            self,
            instance_id,
            *,
            from_type,
            from_id,
            relationship_type,
            to_type,
            to_id,
        ):
            captured["preflight"] = (
                instance_id,
                from_type,
                from_id,
                relationship_type,
                to_type,
                to_id,
            )
            return contracts.GetRelationshipResult(
                found=False,
                from_type=from_type,
                from_id=from_id,
                relationship_type=relationship_type,
                to_type=to_type,
                to_id=to_id,
            )

        def batch_direct_write(self, instance_id, payload, *, dry_run=False):
            captured["payload"] = payload
            return contracts.BatchDirectWriteResult(
                dry_run=dry_run,
                valid=True,
                relationships_added=1,
                pending_conflicts=[
                    contracts.DirectWriteGroupInteraction(
                        relationship_type="fits",
                        from_type="Part",
                        from_id="BP-1",
                        to_type="Vehicle",
                        to_id="V-1",
                        group_id="GRP-pending",
                    )
                ],
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "relationship",
            "add",
            "fits",
            "Part",
            "BP-1",
            "Vehicle",
            "V-1",
            "--set",
            "source=manual",
            "--set-json",
            "verified=true",
            "--evidence-ref",
            '{"source":"doc","source_record_id":"section"}',
            "--source-evidence",
            '{"source_artifact_id":"SRC-1","chunk_id":"CHK-1"}',
            "--evidence-rationale",
            "Observed in docs.",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["preflight"] == ("inst_123", "Part", "BP-1", "fits", "Vehicle", "V-1")
    payload = captured["payload"]
    assert isinstance(payload, contracts.BatchDirectWritePayload)
    relationship = payload.relationships[0]
    assert relationship.properties == {"source": "manual", "verified": True}
    assert relationship.evidence_refs[0].source_record_id == "section"
    assert relationship.source_evidence[0].chunk_id == "CHK-1"
    assert relationship.evidence_rationale == "Observed in docs."
    assert "Notice: 1 pending group conflict(s) detected." in result.output


def test_add_relationship_shorthand_rejects_existing_relationship(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    class StubClient:
        def get_relationship(
            self,
            instance_id,
            *,
            from_type,
            from_id,
            relationship_type,
            to_type,
            to_id,
        ):
            return contracts.GetRelationshipResult(
                found=True,
                from_type=from_type,
                from_id=from_id,
                relationship_type=relationship_type,
                to_type=to_type,
                to_id=to_id,
                edge_key=1,
            )

        def batch_direct_write(self, instance_id, payload, *, dry_run=False):
            raise AssertionError("existing relationship add should not write")

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "relationship",
            "add",
            "fits",
            "Part",
            "BP-1",
            "Vehicle",
            "V-1",
        ],
    )

    assert result.exit_code == 1
    assert "Relationship already exists" in result.output


def test_update_relationship_shorthand_requires_existing_relationship(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    class StubClient:
        def get_relationship(
            self,
            instance_id,
            *,
            from_type,
            from_id,
            relationship_type,
            to_type,
            to_id,
        ):
            return contracts.GetRelationshipResult(
                found=False,
                from_type=from_type,
                from_id=from_id,
                relationship_type=relationship_type,
                to_type=to_type,
                to_id=to_id,
            )

        def batch_direct_write(self, instance_id, payload, *, dry_run=False):
            raise AssertionError("missing relationship update should not write")

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "relationship",
            "update",
            "fits",
            "Part",
            "BP-1",
            "Vehicle",
            "V-1",
            "--set",
            "source=manual",
        ],
    )

    assert result.exit_code == 1
    assert "Relationship not found" in result.output


def test_update_relationship_shorthand_forwards_evidence_only_update(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    captured: dict[str, object] = {}

    class StubClient:
        def get_relationship(
            self,
            instance_id,
            *,
            from_type,
            from_id,
            relationship_type,
            to_type,
            to_id,
        ):
            return contracts.GetRelationshipResult(
                found=True,
                from_type=from_type,
                from_id=from_id,
                relationship_type=relationship_type,
                to_type=to_type,
                to_id=to_id,
                edge_key=7,
            )

        def batch_direct_write(self, instance_id, payload, *, dry_run=False):
            captured["payload"] = payload
            return contracts.BatchDirectWriteResult(
                dry_run=dry_run,
                valid=True,
                relationships_updated=1,
                receipt_id="RCP-rel-update",
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "relationship",
            "update",
            "fits",
            "Part",
            "BP-1",
            "Vehicle",
            "V-1",
            "--evidence-rationale",
            "Updated supporting rationale.",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["relationships_updated"] == 1
    batch_payload = captured["payload"]
    assert isinstance(batch_payload, contracts.BatchDirectWritePayload)
    assert batch_payload.relationships[0].properties == {}
    assert batch_payload.relationships[0].evidence_rationale == "Updated supporting rationale."


@pytest.mark.parametrize(
    ("raw_ref", "expected_message"),
    [
        ('["not", "object"]', "--evidence-ref must be a JSON object"),
        ('{"source":"doc"}', "--evidence-ref is invalid"),
        (
            '{"source":"","source_record_id":"section-1"}',
            "--evidence-ref is invalid",
        ),
        (
            '{"source":"doc","source_record_id":"section-1","metadata":"bad"}',
            "--evidence-ref is invalid",
        ),
    ],
)
def test_add_relationship_rejects_malformed_evidence_flag(
    runner: CliRunner,
    raw_ref: str,
    expected_message: str,
) -> None:
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "relationship",
            "add",
            "--from-type",
            "Part",
            "--from-id",
            "BP-1",
            "--relationship",
            "fits",
            "--to-type",
            "Vehicle",
            "--to-id",
            "V-1",
            "--evidence-ref",
            raw_ref,
        ],
    )

    assert result.exit_code == 2
    assert expected_message in result.output


@pytest.mark.parametrize(
    ("raw_source_evidence", "expected_message"),
    [
        ('["not", "object"]', "--source-evidence must be a JSON object"),
        ('{"source_artifact_id":"SRC-1"}', "--source-evidence is invalid"),
        ('{"source_artifact_id":"","chunk_id":"CHK-1"}', "--source-evidence is invalid"),
        ('{"source_artifact_id":"SRC-1","chunk_id":""}', "--source-evidence is invalid"),
        (
            '{"source_artifact_id":"SRC-1","heading_path":["Evidence"]}',
            "--source-evidence is invalid",
        ),
    ],
)
def test_add_relationship_rejects_malformed_source_evidence_flag(
    runner: CliRunner,
    raw_source_evidence: str,
    expected_message: str,
) -> None:
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "relationship",
            "add",
            "--from-type",
            "Part",
            "--from-id",
            "BP-1",
            "--relationship",
            "fits",
            "--to-type",
            "Vehicle",
            "--to-id",
            "V-1",
            "--source-evidence",
            raw_source_evidence,
        ],
    )

    assert result.exit_code == 2
    assert expected_message in result.output


def test_server_mode_uses_env_bearer_token_for_client_construction(monkeypatch, runner: CliRunner):
    monkeypatch.setenv("CRUXIBLE_SERVER_BEARER_TOKEN", "local-secret")
    captured: dict[str, object] = {}

    class StubClient:
        def __init__(self, *, base_url=None, socket_path=None, token=None):
            captured["base_url"] = base_url
            captured["socket_path"] = socket_path
            captured["token"] = token

        def stats(self, instance_id):
            captured["instance_id"] = instance_id
            return contracts.StatsResult(
                entity_count=1,
                edge_count=0,
                entity_counts={},
                relationship_counts={},
                head_snapshot_id=None,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common.CruxibleClient", StubClient)
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "stats",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert captured["base_url"] == "http://server"
    assert captured["token"] == "local-secret"
    assert captured["instance_id"] == "inst_123"


def test_server_mode_does_not_use_legacy_server_token_for_client_construction(
    monkeypatch,
    runner: CliRunner,
):
    monkeypatch.setenv("CRUXIBLE_SERVER_TOKEN", "legacy-secret")
    monkeypatch.delenv("CRUXIBLE_SERVER_BEARER_TOKEN", raising=False)
    captured: dict[str, object] = {}

    class StubClient:
        def __init__(self, *, base_url=None, socket_path=None, token=None):
            captured["base_url"] = base_url
            captured["socket_path"] = socket_path
            captured["token"] = token

        def stats(self, instance_id):
            captured["instance_id"] = instance_id
            return contracts.StatsResult(
                entity_count=1,
                edge_count=0,
                entity_counts={},
                relationship_counts={},
                head_snapshot_id=None,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common.CruxibleClient", StubClient)
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "stats",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert captured["base_url"] == "http://server"
    assert captured["token"] is None
    assert captured["instance_id"] == "inst_123"


def test_list_entities_forwards_offset_to_client(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))
    captured: dict[str, object] = {}

    class StubClient:
        def list(self, instance_id, **kwargs):
            captured.update(kwargs)
            return contracts.ListResult(items=[], total=0, limit=5, offset=10)

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_x",
            "list",
            "entities",
            "--type",
            "Vehicle",
            "--limit",
            "5",
            "--offset",
            "10",
        ],
    )

    assert result.exit_code == 0
    assert captured["limit"] == 5
    assert captured["offset"] == 10


def test_list_entities_forwards_fields_to_client(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))
    captured: dict[str, object] = {}

    class StubClient:
        def list(self, instance_id, **kwargs):
            captured.update(kwargs)
            return contracts.ListResult(
                items=[
                    {
                        "entity_type": "Vehicle",
                        "entity_id": "V-1",
                        "properties": {"make": "Honda"},
                    }
                ],
                total=1,
                limit=5,
                offset=0,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_x",
            "list",
            "entities",
            "--type",
            "Vehicle",
            "--field",
            "make",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert captured["fields"] == ["make"]
    payload = json.loads(result.output)
    assert payload["items"][0]["properties"] == {"make": "Honda"}


def test_server_mode_sample_forwards_fields_to_client(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))
    captured: dict[str, object] = {}

    class StubClient:
        def sample(self, instance_id, entity_type, *, limit, fields=None):
            captured.update(
                {
                    "instance_id": instance_id,
                    "entity_type": entity_type,
                    "limit": limit,
                    "fields": fields,
                }
            )
            return contracts.SampleResult(
                items=[
                    {
                        "entity_type": entity_type,
                        "entity_id": "P-1",
                        "properties": {"name": "Pad"},
                    }
                ],
                entity_type=entity_type,
                total=1,
                limit=limit,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_x",
            "sample",
            "--type",
            "Part",
            "--field",
            "name",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert captured == {
        "instance_id": "inst_x",
        "entity_type": "Part",
        "limit": 5,
        "fields": ["name"],
    }
