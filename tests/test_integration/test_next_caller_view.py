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


def _approval_row() -> PlaybillNextItemV1:
    return _item(
        severity="repair",
        reason="proposal_awaiting_approval",
        subject_identity="PRP-0001",
        related_identities=(_CLAIM,),
        detail={"signer_id": "reviewer"},
        repair=PlaybillNextRepairV1(
            operation="playbill.proposal.approve",
            target="PRP-0001",
            required_change="review_and_approve_the_candidate_with_your_signing_key",
            arguments={"proposal_id": "PRP-0001", "signer_id": "reviewer"},
        ),
    )


def test_an_approval_row_is_shown_only_to_a_caller_who_can_approve() -> None:
    """Approval is a GRAPH_WRITE act, whichever surface asks."""

    row = _approval_row()

    for rung in (0, 1):  # READ_ONLY, GOVERNED_WRITE
        kept, hidden = _view(surface="cli", caller_rung=rung).items([row])
        assert (kept, hidden) == ((), 1)
    kept, hidden = _view(surface="cli", caller_rung=2).items([row])
    assert len(kept) == 1 and hidden == 0


def test_an_mcp_profile_without_the_approval_tool_hides_the_approval_row() -> None:
    row = _approval_row()

    without = _view(surface="mcp", tools=("cruxible_playbill_next",), caller_rung=3)
    assert without.items([row]) == ((), 1)
    with_tool = _view(
        surface="mcp", tools=("cruxible_playbill_next", "cruxible_playbill_approve"), caller_rung=3
    )
    (kept,), hidden = with_tool.items([row])
    assert hidden == 0 and kept.repair.command is not None


def test_a_read_only_caller_counts_the_nested_findings_it_cannot_repair() -> None:
    row = _conflict_with_uncovered_member()

    kept, hidden = _view(surface="cli", caller_rung=0).items([row])

    # Revising the conflict and binding evidence are both governed writes.
    assert (kept, hidden) == ((), 2)


def test_every_repair_operation_names_its_door_in_one_table() -> None:
    from typing import get_args

    from cruxible_core.runtime.permissions import TOOL_PERMISSIONS
    from cruxible_core.service.discovery.next import _REPAIR_TOOLS, NextRepairOperation

    assert set(_REPAIR_TOOLS) == set(get_args(NextRepairOperation))
    assert {tool for tool in _REPAIR_TOOLS.values() if tool is not None} <= set(TOOL_PERMISSIONS)
