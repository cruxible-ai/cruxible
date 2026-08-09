"""CLI coverage for the governed procedure command family."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
import yaml
from click.testing import CliRunner

from cruxible_core.cli.main import cli
from cruxible_core.procedure.types import ProcedureRun
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.service import service_lock, service_propose_procedure
from tests.test_procedures.conftest import CONFIG_YAML, actor, provider_definition


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def procedure_cli_instance(tmp_path: Path) -> tuple[CruxibleInstance, str]:
    root = tmp_path / "procedure-cli"
    root.mkdir()
    (root / "config.yaml").write_text(CONFIG_YAML)
    instance = CruxibleInstance.init(root, "config.yaml")
    service_lock(instance)
    proposed = service_propose_procedure(
        instance,
        provider_definition("cli_action"),
        actor_context=actor("cli-proposer"),
    )
    procedure_id = proposed.procedure.procedure_id
    with instance.write_transaction() as uow:
        uow.procedures.save_run(
            ProcedureRun(
                procedure_id=procedure_id,
                definition_digest=proposed.procedure.definition_digest,
            )
        )
    return instance, procedure_id


def test_procedure_and_workflow_help_contain_contrast_sentence(runner: CliRunner) -> None:
    procedure_help = runner.invoke(cli, ["procedure", "--help"])
    workflow_help = runner.invoke(cli, ["run", "--help"])

    assert procedure_help.exit_code == 0
    assert workflow_help.exit_code == 0
    sentence = "Workflows are designed; procedures are learned."
    assert sentence in procedure_help.output
    assert sentence in workflow_help.output


def test_procedure_read_commands_use_envelopes_and_surface_started_tombstone(
    runner: CliRunner,
    procedure_cli_instance: tuple[CruxibleInstance, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, procedure_id = procedure_cli_instance
    monkeypatch.chdir(instance.get_root_path())

    listed = runner.invoke(cli, ["procedure", "list", "--status", "pending", "--json"])
    listed_text = runner.invoke(cli, ["procedure", "list", "--status", "pending"])
    shown = runner.invoke(cli, ["procedure", "show", procedure_id, "--json"])
    shown_text = runner.invoke(cli, ["procedure", "show", procedure_id])
    runs_json = runner.invoke(cli, ["procedure", "runs", procedure_id, "--json"])
    runs_text = runner.invoke(cli, ["procedure", "runs", procedure_id])

    assert listed.exit_code == 0, listed.output
    list_payload = json.loads(listed.output)
    assert set(list_payload) == {
        "items",
        "total",
        "limit",
        "offset",
        "truncated",
        "read_revision",
    }
    assert [item["procedure_id"] for item in list_payload["items"]] == [procedure_id]
    expected_track_record = {
        "runs": 1,
        "succeeded": 0,
        "failed": 0,
        "refused": 0,
        "budget_exceeded": 0,
        "in_flight": 1,
        "last_succeeded_at": None,
        "top_refusal_reason": None,
        "linked_outcomes": None,
    }
    assert list_payload["items"][0]["track_record"] == expected_track_record
    assert listed_text.exit_code == 0, listed_text.output
    assert (
        "Track record: runs=1, succeeded=0, failed=0, refused=0, "
        "budget_exceeded=0, in_flight=1" in listed_text.output
    )

    assert shown.exit_code == 0, shown.output
    shown_payload = json.loads(shown.output)
    assert shown_payload["procedure"]["procedure_id"] == procedure_id
    assert shown_payload["procedure"]["track_record"] == expected_track_record
    assert shown_payload["contract_in_schema"] == {
        "fields": [{"name": "value", "type": "int", "required": True}],
        "allow_extra": False,
        "input_example": {"value": 1},
    }

    assert shown_text.exit_code == 0, shown_text.output
    assert "top_refusal_reason=null, linked_outcomes=null" in shown_text.output
    assert "Input schema:" in shown_text.output
    assert "value (int, required)" in shown_text.output
    assert "Input example:" in shown_text.output
    assert '{"value": 1}' in shown_text.output

    assert runs_json.exit_code == 0, runs_json.output
    run_payload = json.loads(runs_json.output)
    assert set(run_payload) == {
        "items",
        "total",
        "limit",
        "offset",
        "truncated",
        "read_revision",
    }
    assert run_payload["items"][0]["status"] == "started"
    assert run_payload["items"][0]["verdict"] is None

    assert runs_text.exit_code == 0, runs_text.output
    assert "verdict=null (started/unfinalized tombstone)" in runs_text.output


def test_procedure_propose_loads_yaml_and_forwards_governance_fields(
    runner: CliRunner,
    procedure_cli_instance: tuple[CruxibleInstance, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    instance, supersedes_id = procedure_cli_instance
    definition = provider_definition("cli_action")
    definition_path = tmp_path / "procedure.yaml"

    definition_path.write_text(
        yaml.safe_dump(
            definition.model_dump(mode="json", by_alias=True, exclude_none=True),
            sort_keys=False,
        )
    )
    captured: dict[str, object] = {}

    class StubClient:
        def propose_procedure(
            self,
            instance_id: str,
            *,
            definition: dict[str, object],
            supersedes_procedure_id: str | None,
            evidence_refs: list[object],
        ) -> dict[str, object]:
            captured.update(
                {
                    "instance_id": instance_id,
                    "definition": definition,
                    "supersedes": supersedes_procedure_id,
                    "evidence_refs": evidence_refs,
                }
            )
            store = instance.get_procedure_store()
            try:
                procedure = store.get_procedure(supersedes_id)
            finally:
                store.close()
            assert procedure is not None
            return {
                "action": "propose",
                "procedure": procedure.model_dump(
                    mode="json",
                    by_alias=True,
                    exclude_none=True,
                ),
                "receipt_id": "RCP-procedure",
                "warnings": ["budget does not match provider-call count"],
            }

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_procedure",
            "procedure",
            "propose",
            str(definition_path),
            "--supersedes",
            supersedes_id,
            "--evidence-ref",
            '{"source":"receipt","source_record_id":"RCP-source"}',
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["instance_id"] == "inst_procedure"
    assert captured["supersedes"] == supersedes_id
    assert cast(dict[str, object], captured["definition"])["name"] == "cli_action"
    evidence_refs = cast(list[object], captured["evidence_refs"])
    assert len(evidence_refs) == 1
    assert "Receipt: RCP-procedure" in result.output
    # The stub answers with the deprecated string list only, as a daemon that
    # predates typed warnings does. The warning still prints, and the absent
    # code is visibly absent rather than silently dropped.
    assert "Warning [?]: budget does not match provider-call count" in result.output


def test_procedure_propose_prints_the_typed_warning_code_when_the_daemon_sends_one(
    runner: CliRunner,
    procedure_cli_instance: tuple[CruxibleInstance, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The typed channel wins over the string list when both are present."""
    instance, supersedes_id = procedure_cli_instance
    definition_path = tmp_path / "procedure.yaml"
    definition_path.write_text(
        yaml.safe_dump(
            provider_definition("cli_action").model_dump(
                mode="json", by_alias=True, exclude_none=True
            ),
            sort_keys=False,
        )
    )

    class StubClient:
        def propose_procedure(
            self,
            instance_id: str,
            *,
            definition: dict[str, object],
            supersedes_procedure_id: str | None,
            evidence_refs: list[object],
        ) -> dict[str, object]:
            store = instance.get_procedure_store()
            try:
                procedure = store.get_procedure(supersedes_id)
            finally:
                store.close()
            assert procedure is not None
            return {
                "action": "propose",
                "procedure": procedure.model_dump(mode="json", by_alias=True, exclude_none=True),
                "receipt_id": "RCP-procedure",
                "warnings": ["only on some paths"],
                "typed_warnings": [
                    {
                        "code": "contract_field_path_conditional",
                        "message": "only on some paths",
                        "node_ids": ["escalate"],
                    }
                ],
            }

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_procedure",
            "procedure",
            "propose",
            str(definition_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Warning [contract_field_path_conditional]: only on some paths" in result.output
    # Not twice: the two channels carry the same findings, so printing both
    # would double every warning during the dual-emit window.
    assert result.output.count("only on some paths") == 1


def test_procedure_withdraw_forwards_version_and_optional_reason(
    runner: CliRunner,
    procedure_cli_instance: tuple[CruxibleInstance, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, procedure_id = procedure_cli_instance
    captured: dict[str, object] = {}

    class StubClient:
        def withdraw_procedure(
            self,
            instance_id: str,
            target_id: str,
            *,
            expected_version: int,
            reason: str | None,
        ) -> dict[str, object]:
            captured.update(
                {
                    "instance_id": instance_id,
                    "procedure_id": target_id,
                    "expected_version": expected_version,
                    "reason": reason,
                }
            )
            store = instance.get_procedure_store()
            try:
                procedure = store.get_procedure(target_id)
            finally:
                store.close()
            assert procedure is not None
            payload = procedure.model_dump(mode="json", by_alias=True, exclude_none=True)
            payload["status"] = "withdrawn"
            payload["version"] = expected_version + 1
            return {
                "action": "withdraw",
                "procedure": payload,
                "receipt_id": "RCP-withdraw",
            }

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_procedure",
            "procedure",
            "withdraw",
            procedure_id,
            "--expected-version",
            "1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured == {
        "instance_id": "inst_procedure",
        "procedure_id": procedure_id,
        "expected_version": 1,
        "reason": None,
    }
    assert f"Procedure {procedure_id} withdrawn." in result.output
    assert "Version: 2" in result.output
    assert "Receipt: RCP-withdraw" in result.output


def test_procedure_record_reading_forwards_explicit_grade_and_arm(
    runner: CliRunner,
    procedure_cli_instance: tuple[CruxibleInstance, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, procedure_id = procedure_cli_instance
    captured: dict[str, object] = {}

    class StubClient:
        def record_procedure_reading(
            self,
            instance_id: str,
            target_id: str,
            **kwargs: object,
        ) -> dict[str, object]:
            captured.update({"instance_id": instance_id, "procedure_id": target_id, **kwargs})
            return {
                "reading_id": "PRD-cli-reading",
                "grade": kwargs["grade"],
                "receipt_id": "RCP-cli-reading",
            }

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_procedure",
            "procedure",
            "record-reading",
            procedure_id,
            "--subject-grain",
            "arm",
            "--grade",
            "contract",
            "--verdict",
            "satisfied",
            "--observed-at",
            "2026-07-22T12:00:00Z",
            "--node-id",
            "tail",
            "--from-node-id",
            "gate",
            "--arm-label",
            "on_true",
            "--measurement-name",
            "true_arm",
            "--contract-id",
            "RCT-arm",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["instance_id"] == "inst_procedure"
    assert captured["procedure_id"] == procedure_id
    assert captured["grade"] == "contract"
    assert captured["arm_label"] == "on_true"
    assert "Procedure reading PRD-cli-reading recorded as contract grade." in result.output
