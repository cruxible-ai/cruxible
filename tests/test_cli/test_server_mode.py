"""CLI server-mode tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
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
    result = runner.invoke(cli, ["query", "--query", "parts_for_vehicle"])
    assert result.exit_code == 2
    assert "Server mode is required" in result.output


def test_agent_mode_implies_require_server(monkeypatch, runner: CliRunner):
    monkeypatch.setenv("CRUXIBLE_AGENT_MODE", "true")
    result = runner.invoke(cli, ["query", "--query", "parts_for_vehicle"])
    assert result.exit_code == 2
    assert "Server mode is required" in result.output


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

    shown = runner.invoke(cli, ["context", "show", "--json"])
    assert shown.exit_code == 0
    assert json.loads(shown.output) == {
        "instance_id": "inst_abc123",
        "server_url": "http://server",
    }


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
    assert "Remembered instance: inst_456" in used.output

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
                agent_mode=True,
                state_dir="/srv/cruxible-state",
                version="0.2.0",
                instance_count=3,
            )

    monkeypatch.setattr("cruxible_core.cli.commands.server._get_client", lambda: StubClient())
    result = runner.invoke(cli, ["--server-url", "http://server", "server", "info", "--json"])

    assert result.exit_code == 0
    assert captured["called"] is True
    payload = json.loads(result.output)
    assert payload["agent_mode"] is True
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
                        example_ids=["V-2024-CIVIC-EX"],
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
    assert list_payload["queries"][0]["name"] == "parts_for_vehicle"
    assert list_payload["queries"][0]["required_params"] == ["vehicle_id"]

    described = runner.invoke(
        cli,
        ["query", "describe", "--query", "parts_for_vehicle", "--json"],
    )
    assert described.exit_code == 0
    describe_payload = json.loads(described.output)
    assert describe_payload["entry_point"] == "Vehicle"
    assert describe_payload["returns"] == "Part"


def test_query_decision_record_flag_beats_env(
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
                results=[],
                receipt_id="RCP-1",
                receipt=None,
                total_results=0,
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
            "--query",
            "parts_for_vehicle",
            "--param",
            "vehicle_id=V-1",
            "--decision-record",
            "DR-flag",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert captured == {
        "instance_id": "inst_123",
        "query_name": "parts_for_vehicle",
        "params": {"vehicle_id": "V-1"},
        "limit": None,
        "decision_record_id": "DR-flag",
    }


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
                records=[
                    {
                        "decision_record_id": "DR-1",
                        "question": "Should we act?",
                        "status": "open",
                    }
                ]
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
    assert json.loads(listed.output)["records"][0]["decision_record_id"] == "DR-1"

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


def test_inspect_ontology_uses_existing_server_read_surfaces(
    monkeypatch,
    runner: CliRunner,
):
    class StubClient:
        def schema(self, instance_id):
            assert instance_id == "inst_123"
            return yaml.safe_load(CAR_PARTS_YAML)

        def stats(self, instance_id):
            assert instance_id == "inst_123"
            return contracts.StatsResult(
                entity_count=4,
                edge_count=4,
                entity_counts={"Vehicle": 2, "Part": 2},
                relationship_counts={"fits": 3, "replaces": 1},
                head_snapshot_id=None,
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


def test_inspect_overview_uses_existing_server_read_surfaces(
    monkeypatch,
    runner: CliRunner,
):
    class StubClient:
        def schema(self, instance_id):
            assert instance_id == "inst_123"
            return yaml.safe_load(CAR_PARTS_YAML)

        def stats(self, instance_id):
            assert instance_id == "inst_123"
            return contracts.StatsResult(
                entity_count=4,
                edge_count=4,
                entity_counts={"Vehicle": 2, "Part": 2},
                relationship_counts={"fits": 3, "replaces": 1},
                head_snapshot_id=None,
            )

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
                        example_ids=["V-2024-CIVIC-EX"],
                    ),
                    contracts.NamedQueryInfoResult(
                        name="vehicles_for_part",
                        entry_point="Part",
                        required_params=["part_number"],
                        returns="Vehicle",
                        description="Find vehicles for a part.",
                        example_ids=["BP-1001"],
                    ),
                ]
            )

        def list_groups(self, instance_id, relationship_type=None, status=None, limit=50):
            assert instance_id == "inst_123"
            assert status == "pending_review"
            return contracts.ListGroupsToolResult(groups=[], total=0)

        def list_resolutions(self, instance_id, relationship_type=None, action=None, limit=50):
            assert instance_id == "inst_123"
            return contracts.ListResolutionsToolResult(resolutions=[], total=0)

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


def test_explain_is_rejected_in_server_mode(monkeypatch, runner: CliRunner):
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: object())
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
    assert result.exit_code == 2
    assert "not available in server mode" in result.output


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


def test_run_apply_shortcuts_preview_file_flow_in_server_mode(
    monkeypatch,
    runner: CliRunner,
    tmp_path: Path,
):
    input_path = tmp_path / "input.yaml"
    input_path.write_text("sku: SKU-123\n")
    captured: dict[str, object] = {}

    class StubClient:
        def workflow_run(self, instance_id, *, workflow_name, input_payload=None):
            captured["run_instance_id"] = instance_id
            captured["run_payload"] = input_payload
            return contracts.WorkflowRunResult(
                workflow=workflow_name,
                output={"preview": True},
                receipt_id="RCP-preview",
                mode="preview",
                canonical=True,
                apply_digest="sha256:abc",
                head_snapshot_id="snap-head",
                apply_previews={},
                trace_ids=["TRC-preview"],
            )

        def workflow_apply(
            self,
            instance_id,
            *,
            workflow_name,
            expected_apply_digest,
            expected_head_snapshot_id,
            input_payload=None,
        ):
            captured["apply_instance_id"] = instance_id
            captured["apply_digest"] = expected_apply_digest
            captured["apply_head_snapshot_id"] = expected_head_snapshot_id
            captured["apply_payload"] = input_payload
            return contracts.WorkflowApplyResult(
                workflow=workflow_name,
                output={"applied": True},
                receipt_id="RCP-apply",
                mode="apply",
                canonical=True,
                apply_digest=expected_apply_digest,
                head_snapshot_id=expected_head_snapshot_id,
                committed_snapshot_id="snap-commit",
                apply_previews={},
                trace_ids=["TRC-apply"],
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
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

    assert result.exit_code == 0
    assert captured["run_instance_id"] == "inst_123"
    assert captured["apply_instance_id"] == "inst_123"
    assert captured["apply_digest"] == "sha256:abc"
    assert captured["apply_head_snapshot_id"] == "snap-head"
    assert captured["apply_payload"] == {"sku": "SKU-123"}
    assert "Workflow wf applied." in result.output
    assert "Committed snapshot: snap-commit" in result.output


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

        def list_snapshots(self, instance_id):
            assert instance_id == "inst_123"
            return contracts.SnapshotListResult(
                snapshots=[
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
                ]
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
      "relationship": "fits",
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


@pytest.mark.parametrize(
    ("args", "label"),
    [
        (["init", "--config", "config.yaml"], "init"),
        (["run", "--workflow", "wf"], "run"),
        (["add-entity", "--type", "Vehicle", "--id", "V-1"], "add-entity"),
        (
            [
                "world",
                "create-overlay",
                "--transport-ref",
                "file:///tmp/release",
                "--root-dir",
                "/tmp/overlay",
            ],
            "world create-overlay",
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


def test_server_mode_uses_env_bearer_token_for_client_construction(monkeypatch, runner: CliRunner):
    monkeypatch.setenv("CRUXIBLE_SERVER_TOKEN", "local-secret")
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
