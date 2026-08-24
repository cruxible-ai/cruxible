"""CLI Playbill next is a thin client-observation adapter."""

from __future__ import annotations

from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli

COORDINATE = contracts.PlaybillAcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)


def test_cli_next_observes_locally_then_calls_one_queue_route(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[dict[str, object]] = []
    observation = {
        "tag": "playbill-next-workspace-observation-v1",
        "floor_status": "missing",
        "installed_coordinate": None,
        "drift_observations": None,
    }

    class StubClient:
        def next_playbill(self, instance_id: str, **values: object) -> contracts.PlaybillNextResult:
            assert instance_id == "inst_next"
            calls.append(values)
            return contracts.PlaybillNextResult(
                coordinate=COORDINATE,
                evaluation_time="2026-08-24T18:00:00.000000Z",
                observed_domains=["accepted_state", "workspace_floor"],
                unobserved_domains=["workspace_sources"],
                items=[],
                result_digest="sha256:" + "5" * 64,
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
            "https://next.example.test",
            "--instance-id",
            "inst_next",
            "playbill",
            "next",
            "--evaluation-time",
            "2026-08-24T18:00:00Z",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "No repair work" in result.output
    assert "Unobserved: workspace_sources" in result.output
    assert calls[0]["workspace_observation"] == observation
    profile = calls[0]["access_profile"]
    assert isinstance(profile, dict)
    assert profile["permitted_access_classes"] == ["instance", "public"]
