"""CLI curation: list is a pure read; block sync and repin record what churn reads."""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli

COORDINATE = contracts.AcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)


def _listed(**values: Any) -> contracts.CurationListResult:
    return contracts.CurationListResult(
        coordinate=COORDINATE,
        generation=3,
        operational_head_digest="sha256:" + "5" * 64,
        detection=contracts.CurationDetection(
            state="current",
            trigger="live",
            detected_through_generation=3,
            detected_at="2026-09-01T12:00:00Z",
        ),
        detector_coverage=[],
        result_digest="sha256:" + "6" * 64,
        **values,
    )


def test_cli_curation_list_is_one_read_that_prints_each_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    item = {
        "item_id": "sha256:" + "a" * 64,
        "pattern_kind": "playbill.curation.qualifier_crystallization.v1",
        "subject": {"kind": "ClaimType", "name": "project.work_item.status"},
        "status": "open",
        "latest_event_digest": "sha256:" + "b" * 64,
    }

    class StubClient:
        def list_curation(self, instance_id: str, **values: Any) -> contracts.CurationListResult:
            calls.append((instance_id, values))
            return _listed(
                items=[item],
                inactive_detectors=[
                    contracts.CurationInactiveDetector(
                        pattern_kind="playbill.curation.dead_vocabulary.v1",
                        reason="consumption_receipts_off",
                    )
                ],
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://curation.example.test",
            "--instance-id",
            "inst",
            "curation",
            "list",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "generation 3: 1 item(s); detection current" in result.output
    assert (
        f"{item['item_id']}  qualifier_crystallization.v1  ClaimType:project.work_item.status  "
        f"latest={item['latest_event_digest']}"
    ) in result.output
    assert "Inactive: playbill.curation.dead_vocabulary.v1 (consumption_receipts_off)" in (
        result.output
    )
    ((instance_id, values),) = calls
    assert instance_id == "inst"
    # A pure read: no clock, no workspace scan travels.
    assert set(values) == {"access_profile", "limit", "cursor"}
    assert values["access_profile"]["profile_id"] == "cli-curation"


def test_curation_observe_is_gone_block_sync_and_repin_record_instead() -> None:
    result = CliRunner().invoke(cli, ["curation", "observe", "--help"])
    assert result.exit_code == 2


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
        (
            [
                "accept-fixed",
                "--generation",
                "4",
                "--attribution-ref",
                "ticket-9",
            ],
            "accept_fixed",
        ),
        (["suppress", "--scope", "lineage", "--until-generation", "9"], "suppress"),
        (["unsuppress"], "unsuppress"),
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
        ) -> contracts.CurationActionResult:
            calls.append((operation, values))
            return contracts.CurationActionResult(
                coordinate=contracts.AcceptedCoordinate(
                    git_oid="1" * 64,
                    semantic_root="sha256:" + "2" * 64,
                    generation_root="sha256:" + "3" * 64,
                    compiler_digest="sha256:" + "4" * 64,
                ),
                generation=3,
                operational_head_digest="sha256:" + "5" * 64,
                item={"item_id": values["item_id"], "status": "resolved"},
            )

        def overrule_curation(
            self, _instance_id: str, **values: object
        ) -> contracts.CurationActionResult:
            return self._action("overrule", values)

        def accept_fixed_curation(
            self, _instance_id: str, **values: object
        ) -> contracts.CurationActionResult:
            return self._action("accept_fixed", values)

        def suppress_curation(
            self, _instance_id: str, **values: object
        ) -> contracts.CurationActionResult:
            return self._action("suppress", values)

        def unsuppress_curation(
            self, _instance_id: str, **values: object
        ) -> contracts.CurationActionResult:
            return self._action("unsuppress", values)

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://curation.example.test",
            "--instance-id",
            "inst",
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


@pytest.mark.parametrize(
    ("command", "phrase"),
    (
        ("overrule", "permanently"),
        ("accept-fixed", "--generation"),
        ("suppress", "lineage"),
        ("unsuppress", "Lift a suppression"),
    ),
)
def test_curation_commands_carry_their_own_help(command: str, phrase: str) -> None:
    result = CliRunner().invoke(cli, ["curation", command, "--help"])
    assert result.exit_code == 0, result.output
    assert phrase in result.output
