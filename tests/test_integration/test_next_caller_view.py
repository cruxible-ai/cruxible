"""R07: the next queue is filtered and rendered for the caller who reads it.

These drive the caller view on synthetic rows, so each law is checked on its
own: nested findings survive rendering, every repair is gated by its own door,
and each surface renders its own invocation.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.next import (
    PlaybillNextItemV1,
    PlaybillNextRepairV1,
    _caller_queue,
    _CallerView,
    _item,
    _row_of,
    _with_findings,
)

_SUBJECT = "Subject:svc-a"
_CLAIM = "Claim:CLM-0001"


def _conflict_with_uncovered_member() -> PlaybillNextItemV1:
    conflict = _item(
        severity="blocking",
        reason="claim_conflicted",
        subject_identity=_SUBJECT,
        related_identities=(_CLAIM,),
        detail={"contender_count": 2, "predicate": "owner", "qualifier": None},
        repair=PlaybillNextRepairV1(
            operation="cruxible.authoring.example",
            target=_SUBJECT,
            required_change="revise_claims_into_distinct_qualifiers",
            arguments={"claim_ids": [_CLAIM]},
        ),
    )
    uncovered = _item(
        severity="repair",
        reason="claim_uncovered",
        subject_identity=_CLAIM,
        related_identities=(_SUBJECT,),
        detail={"currency": "current", "predicate": "owner", "verdict": "uncovered"},
        repair=PlaybillNextRepairV1(
            operation="cruxible.authoring.bind",
            target=_CLAIM,
            required_change="add_admissible_evidence",
            arguments={"claim_id": "CLM-0001"},
        ),
    )
    return _with_findings(conflict, [uncovered])


def _view(
    *,
    surface: str | None = None,
    tools: tuple[str, ...] | None = None,
    caller_rung: int | None = None,
) -> _CallerView:
    # No row here is a Line dispatch, so the view never reads the instance.
    return _CallerView(
        cast(PlaybillInstance, None),
        surface=surface,  # type: ignore[arg-type]
        tools=tools,
        caller_rung=caller_rung,
    )


def test_mcp_rendering_keeps_a_rows_nested_findings() -> None:
    row = _conflict_with_uncovered_member()

    (rendered,), _held = _caller_queue([row], _view(surface="mcp"), None)

    assert [finding.reason for finding in rendered.findings] == ["claim_uncovered"]
    # The finding is rendered for MCP too: no CLI line survives inside it.
    (finding,) = rendered.findings
    assert finding.repair is not None and rendered.repair is not None
    assert finding.repair.command is None or not finding.repair.command.startswith("cruxible ")
    assert rendered.repair.command is None or not rendered.repair.command.startswith("cruxible ")


def test_a_cli_row_whose_findings_all_stay_keeps_its_bytes() -> None:
    row = _conflict_with_uncovered_member()

    (kept,) = _view(surface="cli").render([row])

    assert kept == row


def _approval_row() -> PlaybillNextItemV1:
    return _item(
        severity="repair",
        reason="proposal_awaiting_approval",
        subject_identity="PRP-0001",
        related_identities=(_CLAIM,),
        detail={"signer_id": "reviewer"},
        repair=PlaybillNextRepairV1(
            operation="cruxible.proposal.approve",
            target="PRP-0001",
            required_change="review_and_approve_the_candidate_with_your_signing_key",
            arguments={"proposal_id": "PRP-0001", "signer_id": "reviewer"},
        ),
    )


def test_an_approval_row_withholds_its_repair_from_a_caller_who_cannot_approve() -> None:
    """Approval is a GRAPH_WRITE act, whichever surface asks; the row stays for everyone."""

    row = _approval_row()

    for rung in (0, 1):  # READ_ONLY, GOVERNED_WRITE
        (kept,) = _view(surface="cli", caller_rung=rung).render([row])
        assert kept.reason == row.reason and kept.subject_identity == row.subject_identity
        assert kept.repair is None
        assert kept.repair_requires is not None
        assert kept.repair_requires.model_dump(mode="json", exclude={"tag"}) == {
            "operation": "cruxible.proposal.approve",
            "tool": "cruxible_approve",
            "tier": "graph_write",
            "because": ["tier"],
        }
    (kept,) = _view(surface="cli", caller_rung=2).render([row])
    assert kept == row


def test_an_mcp_profile_without_the_approval_tool_keeps_the_row_and_names_the_profile() -> None:
    row = _approval_row()

    without = _view(surface="mcp", tools=("cruxible_next",), caller_rung=3)
    (kept,), _held = _caller_queue([row], without, None)
    assert kept.repair is None and kept.repair_requires is not None
    assert kept.repair_requires.because == ("profile",)
    assert kept.repair_requires.profile == "full"
    assert kept.repair_requires.tier == "graph_write"
    with_tool = _view(surface="mcp", tools=("cruxible_next", "cruxible_approve"), caller_rung=3)
    (kept,), _held = _caller_queue([row], with_tool, None)
    assert kept.repair is not None and kept.repair.command is not None
    assert kept.repair_requires is None


def test_a_read_only_caller_keeps_the_row_and_its_nested_findings_withheld() -> None:
    row = _conflict_with_uncovered_member()

    (kept,) = _view(surface="cli", caller_rung=0).render([row])

    # Revising the conflict and binding evidence are both governed writes: the
    # row and its finding stay, each repair withheld and named by its tier.
    assert kept.reason == "claim_conflicted" and kept.repair is None
    assert kept.repair_requires is not None and kept.repair_requires.tier == "governed_write"
    (finding,) = kept.findings
    assert finding.reason == "claim_uncovered" and finding.repair is None
    assert finding.repair_requires is not None
    assert finding.repair_requires.tool == "cruxible_authoring_bind"


def test_every_repair_operation_names_its_door_in_one_table() -> None:
    from typing import get_args

    from cruxible_core.runtime.permissions import TOOL_PERMISSIONS
    from cruxible_core.service.discovery.next import _REPAIR_TOOLS, NextRepairOperation

    assert set(_REPAIR_TOOLS) == set(get_args(NextRepairOperation))
    assert {tool for tool in _REPAIR_TOOLS.values() if tool is not None} <= set(TOOL_PERMISSIONS)


def _line_arm_row() -> PlaybillNextItemV1:
    return _item(
        severity="repair",
        reason="consumer_stalled",
        subject_identity="Line:hourly",
        detail={},
        repair=PlaybillNextRepairV1(
            operation="cruxible.line.arm",
            target="Line:hourly",
            required_change="arm_the_line",
            arguments={"line": "hourly"},
        ),
    )


def test_sdk_rows_render_sdk_calls_not_cli_commands() -> None:
    rows = [_line_arm_row(), _approval_row()]

    kept, _held = _caller_queue(rows, _view(surface="sdk"), None)

    commands = {
        item.repair.operation: item.repair.command for item in kept if item.repair is not None
    }
    assert commands["cruxible.line.arm"] == 'cx.arm_line("hourly")'
    assert commands["cruxible.proposal.approve"] == (
        'cx.proposal("PRP-0001").approve(reviewed=cx.proposal("PRP-0001").review())'
    )
    for command in commands.values():
        assert command is None or not command.startswith("cruxible ")


def _sdk_next(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, status: dict[str, object]
) -> tuple[object, list[dict[str, object]]]:
    """Run `Cruxible.next` against a stub queue route; return the page and the calls."""

    from cruxible_client import contracts
    from cruxible_client.authoring import sdk as sdk_module
    from tests.test_cli.test_playbill_next import COORDINATE

    calls: list[dict[str, object]] = []

    class StubClient:
        def next(self, instance_id: str, **values: object) -> contracts.NextResult:
            calls.append(values)
            return contracts.NextResult(
                coordinate=COORDINATE,
                evaluation_time="2026-08-24T18:00:00.000000Z",
                observed_domains=["accepted_state"],
                unobserved_domains=[],
                status=status,
                items=[],
                total_items=0,
                result_digest="sha256:" + "5" * 64,
            )

    playbill = sdk_module.Cruxible.__new__(sdk_module.Cruxible)
    playbill._client = StubClient()  # type: ignore[assignment]
    playbill._instance_id = "inst_sdk_next"
    playbill._workspace = tmp_path
    playbill._access_profile = sdk_module.AccessProfile(
        profile_id="default", permitted_access_classes=(), disclose_restricted_existence=False
    )
    monkeypatch.setattr(playbill, "_read_at", lambda: None)
    monkeypatch.setattr(playbill, "_evaluation_time", lambda: "2026-08-24T18:00:00.000000Z")
    monkeypatch.setattr(playbill, "_observe_read", lambda *_a, **_k: None)
    monkeypatch.setattr(sdk_module, "observe_next_workspace", lambda _w: None)
    monkeypatch.setattr(
        sdk_module,
        "observe_next_workspace_with_coverage",
        lambda *_a, **_k: (None, None),
    )

    return playbill.next(expiring_within=sdk_module.Duration(value=1)), calls


def test_the_sdk_next_names_its_surface(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from tests.test_cli.test_playbill_next import HEALTHY_STATUS

    _page, calls = _sdk_next(monkeypatch, tmp_path, status=HEALTHY_STATUS)

    assert [call["caller_surface"] for call in calls] == ["sdk"]


def test_the_sdk_page_carries_the_status_and_no_hidden_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No row is left out for a caller, so neither the page nor its status counts any."""

    from tests.test_cli.test_playbill_next import HEALTHY_STATUS

    page, _calls = _sdk_next(monkeypatch, tmp_path, status=dict(HEALTHY_STATUS))

    assert tuple(page) == ()  # type: ignore[call-overload]
    assert not hasattr(page, "hidden")
    assert "hidden" not in type(page.status).model_fields  # type: ignore[attr-defined]
    assert page.status.line_dispatch.state == "idle"  # type: ignore[attr-defined]


class _StubHolds:
    """Holds that park exactly the named reasons, standing in for `_Holds`."""

    def __init__(self, *reasons: str) -> None:
        self.reasons = frozenset(reasons)

    def __bool__(self) -> bool:
        return True

    def covers(self, row: object) -> bool:
        return getattr(row, "reason", None) in self.reasons


def _unreviewed_capture_row(subject: str) -> PlaybillNextItemV1:
    return _item(
        severity="warning",
        reason="claim_new_evidence_unreviewed",
        subject_identity=subject,
        detail={"claim_id": "CLM-0001", "capture_digest": "sha256:" + "c" * 64},
        repair=PlaybillNextRepairV1(
            operation="cruxible.authoring.example",
            target=subject,
            required_change="adjudicate_unreviewed_evidence",
            arguments={"claim_id": "CLM-0001", "capture_digest": "sha256:" + "c" * 64},
        ),
    )


def _supporting_capture_row(subject: str) -> PlaybillNextItemV1:
    return _item(
        severity="warning",
        reason="claim_new_evidence_supporting",
        subject_identity=subject,
        detail={"claim_id": "CLM-0001", "capture_digest": "sha256:" + "d" * 64},
        repair=PlaybillNextRepairV1(
            operation="cruxible.authoring.example",
            target=subject,
            required_change="cite_supporting_evidence",
            arguments={"claim_id": "CLM-0001", "capture_digest": "sha256:" + "d" * 64},
        ),
    )


def _repair(item: PlaybillNextItemV1) -> PlaybillNextRepairV1:
    assert item.repair is not None
    return item.repair


def _commands(items: tuple[PlaybillNextItemV1, ...]) -> list[str | None]:
    return [
        repair.command
        for item in items
        for repair in (item.repair, *(finding.repair for finding in item.findings))
        if repair is not None
    ]


def _assert_rendered_for(surface: str, items: tuple[PlaybillNextItemV1, ...]) -> None:
    commands = _commands(items)
    assert any(command is not None for command in commands), commands
    for command in commands:
        assert command is None or not command.startswith("cruxible "), (surface, command)


@pytest.mark.parametrize("surface", ["mcp", "sdk"])
def test_a_finding_promoted_past_a_hold_is_rendered_for_the_caller(surface: str) -> None:

    conflict = _conflict_with_uncovered_member()
    carrier = _with_findings(
        conflict.model_copy(update={"findings": ()}),
        [*map(_row_of, conflict.findings), _unreviewed_capture_row(_CLAIM)],
    )
    holds = _StubHolds("claim_conflicted")

    items, held = _caller_queue([carrier], _view(surface=surface), holds)  # type: ignore[arg-type]

    assert held == 1
    assert {item.reason for item in items} == {
        "claim_uncovered",
        "claim_new_evidence_unreviewed",
    }
    _assert_rendered_for(surface, items)


@pytest.mark.parametrize("surface", ["mcp", "sdk"])
def test_supporting_evidence_folded_into_a_conflict_is_rendered_for_the_caller(
    surface: str,
) -> None:

    rows = [_conflict_with_uncovered_member(), _supporting_capture_row(_CLAIM)]

    items, held = _caller_queue(rows, _view(surface=surface), None)

    (conflict,) = items
    assert held == 0
    assert [finding.reason for finding in conflict.findings] == [
        "claim_uncovered",
        "claim_new_evidence_supporting",
    ]
    _assert_rendered_for(surface, items)


_SDK_REPAIRS: tuple[tuple[str, dict[str, object]], ...] = (
    ("cruxible.line.arm", {"line": "hourly"}),
    ("cruxible.line.dispatch", {"line": "hourly", "limit": 3}),
    ("cruxible.settle", {"prediction_id": "RSC-0001"}),
    ("cruxible.authoring.example", {"example": "procedure-mandate"}),
    ("cruxible.proposal.approve", {"proposal_id": "PRP-0001", "signer_id": "reviewer"}),
    ("cruxible.claim.retire", {"claim_id": "CLM-0001"}),
    ("cruxible.block.repin", {"source_id": "SRC-1", "block_id": "b1", "claim_id": "CLM-0001"}),
    ("cruxible.block.sync", {"all": True}),
)


def test_every_sdk_rendered_repair_is_python() -> None:
    import ast

    from cruxible_core.service.discovery.next import _repair_command

    commands = [
        _repair_command(operation, arguments=arguments, surface="sdk")  # type: ignore[arg-type]
        for operation, arguments in _SDK_REPAIRS
    ]
    commands.append(
        _item(
            severity="warning",
            reason="claim_new_evidence_unreviewed",
            subject_identity=_CLAIM,
            detail={},
            repair=_repair(_unreviewed_capture_row(_CLAIM)).model_copy(update={"command": None}),
            surface="sdk",
        ).repair.command  # type: ignore[union-attr]
    )

    assert all(command is not None for command in commands), commands
    for command in commands:
        ast.parse(str(command), mode="eval")


def test_the_sdk_block_sync_repair_runs_as_written() -> None:
    from types import SimpleNamespace

    from cruxible_core.service.discovery.next import _repair_command

    calls: list[dict[str, object]] = []
    playbill = SimpleNamespace(
        block=SimpleNamespace(sync=lambda *paths, **options: calls.append(options))
    )

    command = _repair_command("cruxible.block.sync", arguments={"all": True}, surface="sdk")
    eval(str(command), {"__builtins__": {}}, {"cx": playbill})  # noqa: S307

    assert calls == [{"all": True}]


def test_the_sdk_reads_a_row_whose_repair_is_withheld(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cruxible_client import contracts

    withheld = _view(surface="sdk", caller_rung=0).render([_line_arm_row()])
    wire = contracts.NextItem.model_validate(withheld[0].model_dump(mode="json"))

    assert wire.repair is None
    assert wire.repair_requires is not None
    assert wire.repair_requires.tool == "cruxible_line_arm"
    assert wire.repair_requires.because == ["tier"]
