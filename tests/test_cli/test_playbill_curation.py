"""CLI curation list performs one explicit local scan."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli


def test_cli_curation_list_scans_then_calls_one_route(monkeypatch: pytest.MonkeyPatch) -> None:
    observation = {
        "tag": "playbill-next-workspace-observation-v1",
        "source_observations": [],
    }
    calls: list[tuple[str, str, object, object]] = []

    class StubClient:
        def list_playbill_curation(
            self,
            instance_id: str,
            *,
            evaluation_time: str,
            access_profile: object,
            workspace_observation: object,
        ) -> contracts.PlaybillCurationListResult:
            calls.append((instance_id, evaluation_time, access_profile, workspace_observation))

            return contracts.PlaybillCurationListResult(
                coordinate=contracts.PlaybillAcceptedCoordinate(
                    git_oid="1" * 64,
                    semantic_root="sha256:" + "2" * 64,
                    generation_root="sha256:" + "3" * 64,
                    compiler_digest="sha256:" + "4" * 64,
                ),
                generation=3,
                evaluation_time=evaluation_time,
                operational_head_digest="sha256:" + "5" * 64,
                items=[],
                detector_coverage=[],
                observation_coverage={
                    "tag": "playbill-curation-observation-coverage-v1",
                    "source_count": 0,
                    "observed_block_count": 0,
                    "omitted_source_count": 0,
                    "omissions": [],
                },
                result_digest="sha256:" + "6" * 64,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill.observe_playbill_next_workspace",
        lambda _root: observation,
    )
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://curation.example.test",
            "--instance-id",
            "inst",
            "playbill",
            "curation",
            "list",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "generation 3: 0 item(s)" in result.output
    assert len(calls) == 1
    assert calls[0][0] == "inst"
    assert isinstance(calls[0][2], dict)
    assert calls[0][2]["profile_id"] == "cli-curation"
    assert calls[0][3] == observation


@pytest.mark.parametrize(
    ("command", "expected_operation"),
    (
        (["overrule"], "overrule"),
        (
            [
                "accept-fixed",
                "--proposal-id",
                "sha256:" + "3" * 64,
                "--changeset-digest",
                "sha256:" + "4" * 64,
            ],
            "accept_fixed",
        ),
        (["suppress", "--scope", "pattern", "--until-generation", "9"], "suppress"),
    ),
)
def test_cli_curation_lifecycle_commands_delegate_once(
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    expected_operation: str,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class StubClient:
        def _action(
            self, operation: str, values: dict[str, object]
        ) -> contracts.PlaybillCurationActionResult:
            calls.append((operation, values))
            return contracts.PlaybillCurationActionResult(
                coordinate=contracts.PlaybillAcceptedCoordinate(
                    git_oid="1" * 64,
                    semantic_root="sha256:" + "2" * 64,
                    generation_root="sha256:" + "3" * 64,
                    compiler_digest="sha256:" + "4" * 64,
                ),
                generation=3,
                operational_head_digest="sha256:" + "5" * 64,
                item={"item_id": values["item_id"], "status": "resolved"},
            )

        def overrule_playbill_curation(
            self, _instance_id: str, **values: object
        ) -> contracts.PlaybillCurationActionResult:
            return self._action("overrule", values)

        def accept_fixed_playbill_curation(
            self, _instance_id: str, **values: object
        ) -> contracts.PlaybillCurationActionResult:
            return self._action("accept_fixed", values)

        def suppress_playbill_curation(
            self, _instance_id: str, **values: object
        ) -> contracts.PlaybillCurationActionResult:
            return self._action("suppress", values)

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://curation.example.test",
            "--instance-id",
            "inst",
            "playbill",
            "curation",
            *command,
            "sha256:" + "1" * 64,
            "--expected-latest-event-digest",
            "sha256:" + "2" * 64,
            "--reason",
            "operator-reviewed mechanical facts",
        ],
    )

    assert result.exit_code == 0, result.output
    assert [name for name, _values in calls] == [expected_operation]
