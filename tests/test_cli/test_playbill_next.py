"""CLI Playbill next is a thin client-observation adapter."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli

COORDINATE = contracts.PlaybillAcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)


@pytest.mark.parametrize(
    ("provided_time", "expected_time"),
    [
        ("2026-08-24T18:00:00Z", "2026-08-24T18:00:00Z"),
        (None, "2026-08-24T18:00:00.123456Z"),
    ],
)
@pytest.mark.parametrize(
    ("duration", "expected_microseconds"),
    [
        (None, 604_800_000_000),
        ("P7D", 604_800_000_000),
        ("PT12H", 43_200_000_000),
        ("P1DT2H30M", 95_400_000_000),
        ("PT0.000001S", 1),
    ],
)
def test_cli_next_observes_locally_then_calls_one_queue_route(
    monkeypatch: pytest.MonkeyPatch,
    provided_time: str | None,
    expected_time: str,
    duration: str | None,
    expected_microseconds: int,
) -> None:
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

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            return datetime(2026, 8, 24, 18, 0, 0, 123456, tzinfo=UTC)

    monkeypatch.setattr("cruxible_core.cli.commands.playbill.datetime", FrozenDatetime)
    arguments = [
        "--server-url",
        "https://next.example.test",
        "--instance-id",
        "inst_next",
        "playbill",
        "next",
    ]
    if provided_time is not None:
        arguments.extend(["--evaluation-time", provided_time])
    if duration is not None:
        arguments.extend(["--expiring-within", duration])
    result = CliRunner().invoke(
        cli,
        arguments,
    )

    assert result.exit_code == 0, result.output
    assert "No repair work" in result.output
    assert "Unobserved: workspace_sources" in result.output
    assert calls[0]["workspace_observation"] == observation
    assert calls[0]["evaluation_time"] == expected_time
    assert calls[0]["expiring_within"] == {"microseconds": expected_microseconds}
    profile = calls[0]["access_profile"]
    assert isinstance(profile, dict)
    assert profile["permitted_access_classes"] == ["instance", "public"]


@pytest.mark.parametrize("duration", ["P", "PT", "P1M", "-P1D", "PT0.0000001S"])
def test_cli_next_refuses_invalid_or_calendar_ambiguous_duration(duration: str) -> None:
    result = CliRunner().invoke(cli, ["playbill", "next", "--expiring-within", duration])

    assert result.exit_code != 0
    assert "ISO-8601" in result.output


def test_cli_next_no_longer_accepts_the_microsecond_flag() -> None:
    result = CliRunner().invoke(cli, ["playbill", "next", "--expiring-within-us", "1"])

    assert result.exit_code != 0
    assert "No such option" in result.output


def test_cli_next_delta_labels_additions_and_removals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed = {
        "item_id": "sha256:" + "a" * 64,
        "severity": "warning",
        "reason": "claim_conflicted",
        "subject_identity": "Claim:removed",
        "repair": {"operation": "playbill.authoring.create"},
    }
    added = {
        "item_id": "sha256:" + "b" * 64,
        "severity": "repair",
        "reason": "claim_uncovered",
        "subject_identity": "Claim:added",
        "repair": {"operation": "playbill.authoring.create"},
    }

    class StubClient:
        def __init__(self) -> None:
            self.calls = 0

        def next_playbill(self, instance_id: str, **values: object) -> contracts.PlaybillNextResult:
            assert instance_id == "inst_next"
            self.calls += 1
            items = [removed, added] if values.get("since_result_digest") else [added]
            return contracts.PlaybillNextResult(
                coordinate=COORDINATE,
                evaluation_time="2026-08-24T18:00:00Z",
                observed_domains=["accepted_state", "workspace_floor", "workspace_sources"],
                unobserved_domains=[],
                items=items,
                result_digest="sha256:" + str(self.calls) * 64,
                delta_since="sha256:" + "0" * 64 if self.calls == 1 else None,
            )

    client = StubClient()
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill.observe_playbill_next_workspace",
        lambda _root: {},
    )
    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill.observe_playbill_next_workspace_with_coverage",
        lambda *_args, **_kwargs: ({}, COORDINATE),
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
            "--delta",
            "sha256:" + "0" * 64,
        ],
    )

    assert result.exit_code == 0, result.output
    assert "removed  warning  claim_conflicted  Claim:removed" in result.output
    assert "added  repair  claim_uncovered  Claim:added" in result.output
    assert client.calls == 2


def test_cli_next_delta_memo_miss_renders_the_full_queue_without_change_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = {
        "item_id": "sha256:" + "a" * 64,
        "severity": "warning",
        "reason": "claim_conflicted",
        "subject_identity": "Claim:current",
        "repair": {"operation": "playbill.authoring.create"},
    }

    class StubClient:
        def next_playbill(self, instance_id: str, **values: object) -> contracts.PlaybillNextResult:
            assert instance_id == "inst_next"
            return contracts.PlaybillNextResult(
                coordinate=COORDINATE,
                evaluation_time="2026-08-24T18:00:00Z",
                observed_domains=["accepted_state", "workspace_floor", "workspace_sources"],
                unobserved_domains=[],
                items=[item],
                result_digest="sha256:" + "1" * 64,
                delta_since=None,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill.observe_playbill_next_workspace",
        lambda _root: {},
    )
    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill.observe_playbill_next_workspace_with_coverage",
        lambda *_args, **_kwargs: ({}, COORDINATE),
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
            "--delta",
            "sha256:" + "0" * 64,
        ],
    )

    assert result.exit_code == 0, result.output
    assert "warning  claim_conflicted  Claim:current" in result.output
    assert "added  warning" not in result.output
    assert "removed  warning" not in result.output
