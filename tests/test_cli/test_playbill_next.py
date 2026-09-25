"""CLI Playbill next is a thin client-observation adapter."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli

HEALTHY_STATUS = {
    "blocking": False,
    "instance": {"state": "active"},
    "floor": {"state": "current"},
    "ledger_mirror": {"state": "not_configured"},
    "provider_lane": {"state": "available"},
    "procedure_catalog": {"state": "not_observed"},
    "compiler": {"state": "current"},
    "line_dispatch": {"state": "idle"},
    "consumers": {"state": "current"},
}

AUTHOR = {
    "operation": "playbill.authoring.create",
    "target": "Claim:c",
    "required_change": "author_the_claim",
}

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
                unobserved_domains=["workspace_sources", "workspace_projections"],
                status=HEALTHY_STATUS,
                items=[],
                total_items=0,
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
    assert "Unobserved: workspace_sources, workspace_projections" in result.output
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
        "repair": AUTHOR,
    }
    added = {
        "item_id": "sha256:" + "b" * 64,
        "severity": "repair",
        "reason": "claim_uncovered",
        "subject_identity": "Claim:added",
        "repair": AUTHOR,
    }

    class StubClient:
        def __init__(self) -> None:
            self.calls = 0

        def next_playbill(self, instance_id: str, **values: object) -> contracts.PlaybillNextResult:
            assert instance_id == "inst_next"
            self.calls += 1
            assert values.get("since_result_digest") == "sha256:" + "0" * 64
            return contracts.PlaybillNextResult(
                tag="playbill-next-result-v2",
                coordinate=COORDINATE,
                evaluation_time="2026-08-24T18:00:00Z",
                observed_domains=[
                    "accepted_state",
                    "workspace_floor",
                    "workspace_sources",
                    "workspace_projections",
                ],
                unobserved_domains=[],
                status=HEALTHY_STATUS,
                items=[removed, added],
                total_items=2,
                result_digest="sha256:" + str(self.calls) * 64,
                delta_since="sha256:" + "0" * 64,
                attestation_head_digest="sha256:" + "9" * 64,
                removed_item_ids=[removed["item_id"]],
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
    assert client.calls == 1


def test_cli_next_delta_memo_miss_renders_the_full_queue_without_change_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = {
        "item_id": "sha256:" + "a" * 64,
        "severity": "warning",
        "reason": "claim_conflicted",
        "subject_identity": "Claim:current",
        "repair": AUTHOR,
    }

    class StubClient:
        def next_playbill(self, instance_id: str, **values: object) -> contracts.PlaybillNextResult:
            assert instance_id == "inst_next"
            return contracts.PlaybillNextResult(
                coordinate=COORDINATE,
                evaluation_time="2026-08-24T18:00:00Z",
                observed_domains=[
                    "accepted_state",
                    "workspace_floor",
                    "workspace_sources",
                    "workspace_projections",
                ],
                unobserved_domains=[],
                status=HEALTHY_STATUS,
                items=[item],
                total_items=1,
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


def test_cli_next_attention_states_match_the_served_status_model() -> None:
    from cruxible_core.cli.commands.playbill import _NEXT_STATUS_ATTENTION
    from cruxible_core.service.discovery.next import _HEALTH_ATTENTION

    assert _NEXT_STATUS_ATTENTION == {
        facet: set(states) for facet, states in _HEALTH_ATTENTION.items()
    }


def test_cli_next_prints_status_that_needs_attention_above_the_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = {
        **HEALTHY_STATUS,
        "floor": {
            "state": "stale",
            "repair": {
                "operation": "playbill.floor.export",
                "target": "inst_next",
                "required_change": "replace_installed_floor",
                "command": "cruxible playbill floor export --force --json",
            },
        },
    }

    class StubClient:
        def next_playbill(self, instance_id: str, **values: object) -> contracts.PlaybillNextResult:
            return contracts.PlaybillNextResult(
                coordinate=COORDINATE,
                evaluation_time="2026-08-24T18:00:00.000000Z",
                observed_domains=["accepted_state", "workspace_floor"],
                unobserved_domains=["workspace_sources", "workspace_projections"],
                status=status,
                items=[],
                total_items=0,
                result_digest="sha256:" + "5" * 64,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill.observe_playbill_next_workspace",
        lambda _root: {},
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
    assert "Status: floor stale  next=cruxible playbill floor export --force --json" in (
        result.output
    )
    # A healthy facet stays silent.
    assert "ledger mirror" not in result.output


def _rows() -> list[dict[str, object]]:
    return [
        {
            "item_id": "sha256:" + "a" * 64,
            "severity": "repair",
            "reason": "projection_dirty",
            "subject_identity": "Block:docs/runbook.md#b",
            "detail": {"block_id": "b", "rendered": "a long rendered body"},
            "repair": {
                "operation": "playbill.block.sync",
                "target": "docs/runbook.md",
                "required_change": "resync_projection",
                "arguments": {"all": True},
                "command": "cruxible playbill block sync --all",
            },
            "findings": [
                {
                    "severity": "warning",
                    "reason": "projection_backing_stale",
                    "subject_identity": "Block:docs/runbook.md#b",
                    "repair": AUTHOR,
                }
            ],
        },
        {
            "item_id": "sha256:" + "b" * 64,
            "severity": "warning",
            "reason": "claim_conflicted",
            "subject_identity": "Claim:c",
            "detail": {"claims": ["Claim:c", "Claim:d"]},
            "repair": {
                "operation": "hand_edit",
                "target": "Claim:c",
                "required_change": "revise_claims_into_distinct_qualifiers",
            },
        },
    ]


def _stub_pages(
    monkeypatch: pytest.MonkeyPatch, *, total: int, next_cursor: str | None
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    class StubClient:
        def next_playbill(self, instance_id: str, **values: object) -> contracts.PlaybillNextResult:
            calls.append(values)
            return contracts.PlaybillNextResult(
                coordinate=COORDINATE,
                evaluation_time="2026-08-24T18:00:00Z",
                observed_domains=["accepted_state"],
                unobserved_domains=[
                    "workspace_floor",
                    "workspace_sources",
                    "workspace_projections",
                ],
                status={
                    **HEALTHY_STATUS,
                    "ledger_mirror": {
                        "state": "behind",
                        "repair": {
                            "operation": "hand_edit",
                            "target": "ledger mirror",
                            "required_change": "push_the_ledger_mirror",
                        },
                    },
                },
                items=_rows(),
                total_items=total,
                next_cursor=next_cursor,
                result_digest="sha256:" + "5" * 64,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill.observe_playbill_next_workspace",
        lambda _root: {},
    )
    return calls


def _invoke_next(*arguments: str) -> str:
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
            *arguments,
        ],
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_cli_next_brief_prints_one_line_per_row_with_its_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_pages(monkeypatch, total=2, next_cursor=None)

    output = _invoke_next("--brief")

    assert output.splitlines() == [
        "Status: ledger mirror behind  next=push_the_ledger_mirror",
        "repair  projection_dirty  Block:docs/runbook.md#b  "
        "next=cruxible playbill block sync --all",
        "warning  claim_conflicted  Claim:c",
    ]


def test_cli_next_default_names_each_rows_repair_and_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_pages(monkeypatch, total=2, next_cursor=None)

    output = _invoke_next()

    assert output.splitlines() == [
        "Status: ledger mirror behind  next=push_the_ledger_mirror",
        "repair  projection_dirty  Block:docs/runbook.md#b  next=playbill.block.sync",
        "  repair: cruxible playbill block sync --all",
        "  also: warning  projection_backing_stale  Block:docs/runbook.md#b",
        "warning  claim_conflicted  Claim:c  next=hand_edit",
        "  repair: hand edit Claim:c: revise_claims_into_distinct_qualifiers",
        "Unobserved: workspace_floor, workspace_sources, workspace_projections",
    ]
    assert "long rendered body" not in output


@pytest.mark.parametrize("brief", [False, True])
def test_cli_next_pages_with_limit_and_cursor(monkeypatch: pytest.MonkeyPatch, brief: bool) -> None:
    calls = _stub_pages(monkeypatch, total=5, next_cursor="page-two")

    output = _invoke_next("--limit", "2", "--cursor", "page-one", *(("--brief",) if brief else ()))

    assert (calls[0]["limit"], calls[0]["cursor"]) == (2, "page-one")
    assert output.splitlines()[-1] == "Showing 2 of 5 rows. Next: --cursor page-two"


def test_cli_next_defaults_to_the_shared_page_size_and_bounds_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_pages(monkeypatch, total=2, next_cursor=None)

    _invoke_next()
    refused = CliRunner().invoke(
        cli, ["playbill", "next", "--limit", str(contracts.PLAYBILL_NEXT_MAX_LIMIT + 1)]
    )

    assert (calls[0]["limit"], calls[0]["cursor"]) == (contracts.PLAYBILL_NEXT_DEFAULT_LIMIT, None)
    assert refused.exit_code != 0
    assert "--limit" in refused.output
