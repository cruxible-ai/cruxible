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
    calls: list[object] = []

    class StubClient:
        def list_playbill_curation(
            self, instance_id: str, *, workspace_observation: object
        ) -> contracts.PlaybillCurationListResult:
            calls.append((instance_id, workspace_observation))
            return contracts.PlaybillCurationListResult(
                coordinate=contracts.PlaybillAcceptedCoordinate(
                    git_oid="1" * 64,
                    semantic_root="sha256:" + "2" * 64,
                    generation_root="sha256:" + "3" * 64,
                    compiler_digest="sha256:" + "4" * 64,
                ),
                generation=3,
                operational_head_digest="sha256:" + "5" * 64,
                items=[],
                observation_coverage={
                    "tag": "playbill-curation-observation-coverage-v1",
                    "source_count": 0,
                    "observed_block_count": 0,
                    "omitted_source_count": 0,
                    "omissions": [],
                },
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
    assert calls == [("inst", observation)]
