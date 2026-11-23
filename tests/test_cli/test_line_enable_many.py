"""`cruxible line enable` takes several Lines, or a Runbook's, with one receipt per Line.

One LINE alone keeps the single-Line output and `--json` shape exactly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.line_dispatch import LineEnablement
from cruxible_client.contracts.runbooks import Runbook
from cruxible_client.errors import CoreError
from cruxible_core.cli.main import cli

COMMON = ["--server-url", "https://lines.example.test", "--instance-id", "inst_lines"]
AT = datetime(2026, 11, 23, tzinfo=UTC)
COORDINATE = contracts.AcceptedCoordinate(
    git_oid="1" * 40,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)


class _Refused(CoreError):
    error_code = "cruxible.line.mandate_required"

    def __init__(self, line: str) -> None:
        super().__init__(f"Line {line} can settle and no current ProcedureMandate covers it")
        self.repair_commands = (f"cruxible line grant {line} --settle-observations",)


def _enablement(line: str, outcome: str | None) -> LineEnablement:
    return LineEnablement(
        enablement_id=f"arm-{line}",
        line=f"Line:{line}",
        line_artifact_digest="sha256:" + "a" * 64,
        occurrence_epoch=1,
        state="enabled",
        enabled_at=AT,
        enabled_by={"kind": "local_operator", "label": "operator"},
        evaluated_until=AT,
        outcome=outcome,
        coordinate=COORDINATE,
    )


def _runbook(*steps: dict[str, Any], state: str = "live") -> dict[str, Any]:
    return Runbook.model_validate(
        {
            "identity": {"kind": "Runbook", "name": "kev-daily"},
            "steps": list(steps),
            "lifecycle": {"state": state},
        }
    ).model_dump(mode="json")


def _line_step(name: str) -> dict[str, Any]:
    return {"kind": "line", "line": {"target": {"kind": "Line", "name": name}}}


ACTION_STEP = {"kind": "action", "action": "evidence.sweep"}


class _LinesClient:
    def __init__(
        self,
        *,
        already: tuple[str, ...] = (),
        refuse: tuple[str, ...] = (),
        runbook: dict[str, Any] | None = None,
    ) -> None:
        self.already = already
        self.refuse = refuse
        self.runbook = runbook
        self.enabled: list[tuple[str, bool | None]] = []
        self.reads: list[str] = []

    def enable_line(
        self, instance_id: str, line: str, *, dry_run: bool | None = None, at: str | None = None
    ) -> LineEnablement:
        assert instance_id == "inst_lines"
        self.enabled.append((line, dry_run))
        if line in self.refuse:
            raise _Refused(line)
        if dry_run:
            return _enablement(line, "would_enable")
        return _enablement(line, "already_enabled" if line in self.already else "enabled")

    def get(self, instance_id: str, *, request: GetRequest) -> Any:
        self.reads.append(request.ref)
        if self.runbook is None:
            raise CoreError(f"cruxible.get.not_found: {request.ref} names nothing accepted")
        return type("Found", (), {"proof": {"envelope": self.runbook}})()


def _run(monkeypatch: pytest.MonkeyPatch, client: _LinesClient, *args: str) -> Any:
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    return CliRunner().invoke(cli, [*COMMON, "line", "enable", *args])


@pytest.mark.parametrize("output_json", [False, True])
def test_one_line_keeps_the_single_line_output_and_json(
    monkeypatch: pytest.MonkeyPatch, output_json: bool
) -> None:
    client = _LinesClient()
    result = _run(monkeypatch, client, "kev-sync", *(["--json"] if output_json else []))
    assert result.exit_code == 0, result.output
    assert client.enabled == [("kev-sync", None)]
    if output_json:
        assert json.loads(result.stdout) == _enablement("kev-sync", "enabled").model_dump(
            mode="json"
        )
    else:
        assert result.stdout.splitlines()[0] == "Line:kev-sync: enabled"
        assert "Matched through:" in result.output and "Lines:" not in result.output


def test_several_lines_enable_each_once_with_a_receipt_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _LinesClient(already=("kev-sync",))
    result = _run(monkeypatch, client, "kev-sync", "Line:epss-sync", "kev-sync")
    assert result.exit_code == 0, result.output
    assert client.enabled == [("kev-sync", None), ("epss-sync", None)]
    assert result.stdout.splitlines() == [
        "Line:kev-sync: enabled (already enabled; nothing changed)",
        "Line:epss-sync: enabled",
        "Lines: 1 enabled, 1 unchanged",
    ]


def test_a_refused_line_is_reported_beside_the_others_and_exits_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _LinesClient(refuse=("settle-findings",))
    result = _run(monkeypatch, client, "kev-sync", "settle-findings", "epss-sync")
    assert result.exit_code == 1, result.output
    # Every Line is tried, in the order named.
    assert [line for line, _ in client.enabled] == ["kev-sync", "settle-findings", "epss-sync"]
    lines = result.stdout.splitlines()
    assert lines[0] == "Line:kev-sync: enabled"
    assert lines[1] == (
        "Line:settle-findings: refused (cruxible.line.mandate_required: Line settle-findings "
        "can settle and no current ProcedureMandate covers it)"
    )
    assert lines[2] == "  repair: cruxible line grant settle-findings --settle-observations"
    assert lines[3] == "Line:epss-sync: enabled"
    assert lines[4] == "Lines: 2 enabled, 1 refused"


def test_several_lines_json_lists_each_enablement_or_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _LinesClient(refuse=("settle-findings",))
    result = _run(monkeypatch, client, "kev-sync", "settle-findings", "--json")
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload == {
        "lines": [
            {
                "line": "kev-sync",
                "enablement": _enablement("kev-sync", "enabled").model_dump(mode="json"),
            },
            {
                "line": "settle-findings",
                "refused": {
                    "code": "cruxible.line.mandate_required",
                    "message": "Line settle-findings can settle and no current "
                    "ProcedureMandate covers it",
                    "repairs": ["cruxible line grant settle-findings --settle-observations"],
                },
            },
        ]
    }


def test_runbook_enables_every_line_its_steps_name_after_the_lines_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _LinesClient(
        runbook=_runbook(
            _line_step("kev-sync"), ACTION_STEP, _line_step("epss-sync"), _line_step("kev-sync")
        )
    )
    result = _run(monkeypatch, client, "triage", "Line:epss-sync", "--runbook", "kev-daily")
    assert result.exit_code == 0, result.output
    assert client.reads == ["Runbook:kev-daily"]
    assert [line for line, _ in client.enabled] == ["triage", "epss-sync", "kev-sync"]
    assert result.stdout.splitlines()[-1] == "Lines: 3 enabled"


def test_runbook_alone_uses_the_receipts_even_for_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _LinesClient(runbook=_runbook(_line_step("kev-sync")))
    result = _run(monkeypatch, client, "--runbook", "Runbook:kev-daily", "--dry-run")
    assert result.exit_code == 0, result.output
    assert client.enabled == [("kev-sync", True)]
    assert result.stdout.splitlines()[:2] == [
        "Line:kev-sync: enabled (preview: would enable; nothing changed)",
        "Lines: 1 previewed",
    ]
    assert f"Preview at {COORDINATE.git_oid}; nothing was written." in result.output


@pytest.mark.parametrize(
    ("runbook", "message"),
    [
        (None, "cruxible.get.not_found: Runbook:kev-daily names nothing accepted"),
        (_runbook(ACTION_STEP), "Runbook:kev-daily lists no Lines (only actions)"),
        (_runbook(_line_step("a"), state="retired"), "Runbook:kev-daily is retired"),
    ],
)
def test_runbook_refuses_when_it_names_no_line_to_enable(
    monkeypatch: pytest.MonkeyPatch, runbook: dict[str, Any] | None, message: str
) -> None:
    client = _LinesClient(runbook=runbook)
    result = _run(monkeypatch, client, "kev-sync", "--runbook", "kev-daily")
    assert result.exit_code != 0
    assert message in result.output
    assert client.enabled == []


def test_no_line_and_no_runbook_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _LinesClient()
    result = _run(monkeypatch, client)
    assert result.exit_code == 2
    assert "name a LINE, or --runbook RUNBOOK" in result.output
    assert client.enabled == []
