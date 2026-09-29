"""R07: the next queue is filtered and rendered for the caller who reads it.

These drive the caller view on synthetic rows, so each law is checked on its
own: nested findings survive rendering, every repair is gated by its own door,
and each surface renders its own invocation.
"""

from __future__ import annotations

from typing import cast

from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.next import (
    PlaybillNextItemV1,
    PlaybillNextRepairV1,
    _CallerView,
    _item,
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
            operation="playbill.authoring.create",
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
            operation="playbill.authoring.bind",
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

    (rendered,), hidden = _view(surface="mcp").items([row])

    assert hidden == 0
    assert [finding.reason for finding in rendered.findings] == ["claim_uncovered"]
    # The finding is rendered for MCP too: no CLI line survives inside it.
    (finding,) = rendered.findings
    assert finding.repair.command is None or not finding.repair.command.startswith("cruxible ")
    assert rendered.repair.command is None or not rendered.repair.command.startswith("cruxible ")


def test_a_cli_row_whose_findings_all_stay_keeps_its_bytes() -> None:
    row = _conflict_with_uncovered_member()

    (kept,), hidden = _view(surface="cli").items([row])

    assert hidden == 0
    assert kept == row
