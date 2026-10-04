"""Option (b): a next row whose repair the caller cannot run stays, its repair withheld.

The default MCP tool profile advertises no Line, settle or authoring tool, so
before this ruling every operational row -- a stopped Line arm, a settleable
prediction -- vanished from an agent's queue and from orient's attention. Now
the row stays for every caller; only the repair is withheld, and the row says
what running it requires.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from cruxible_core.mcp.curation import PROFILE_DEFAULT, ToolCuration, advertised_tool_names
from cruxible_core.runtime.permissions import TOOL_PERMISSIONS, PermissionMode
from cruxible_core.service.discovery.next import NextRequest, service_playbill_next
from cruxible_core.service.discovery.orient import service_playbill_orient
from cruxible_core.service.procedures.line_dispatch import service_stop_line_arm
from tests.test_procedures.test_line_arming import _armed_world
from tests.test_procedures.test_line_dispatch import _active_segment
from tests.test_procedures.test_procedure_run_surface import _actor

_PROFILE = {"profile_id": "test", "permitted_access_classes": ["instance", "public"]}


def _default_profile_tools() -> tuple[str, ...]:
    return tuple(
        sorted(
            advertised_tool_names(
                mode=PermissionMode.ADMIN,
                registered_tools=set(TOOL_PERMISSIONS),
                curation=ToolCuration(profile=PROFILE_DEFAULT),
            )
        )
    )


def _stopped_arm_world(tmp_path: Path):  # type: ignore[no-untyped-def]
    instance, line, _procedure, start = _armed_world(tmp_path)
    service_stop_line_arm(
        instance,
        _active_segment(instance),
        reason="credential_revoked",
        detail="The arming credential was revoked.",
        actor=_actor(instance),
        now=start + timedelta(seconds=1),
    )
    return instance, line, start + timedelta(seconds=2)


def test_the_default_mcp_profile_keeps_a_stopped_arm_row_and_names_what_it_needs(
    tmp_path: Path,
) -> None:
    instance, line, when = _stopped_arm_world(tmp_path)
    tools = _default_profile_tools()
    assert "cruxible_playbill_line_arm" not in tools

    request = NextRequest(
        evaluation_time=when,
        access_profile=_PROFILE,
        caller_surface="mcp",
        caller_tools=tools,
    )
    result = service_playbill_next(instance, request=request, caller_rung=3)

    (row,) = [item for item in result.items if item.reason == "consumer_stalled"]
    assert row.subject_identity == line.identity.qualified
    assert row.detail["state"] == "stopped"
    assert row.detail["stop_reason"] == "credential_revoked"
    assert row.repair is None
    assert row.repair_requires is not None
    assert row.repair_requires.model_dump(mode="json", exclude={"tag"}) == {
        "operation": "playbill.line.arm",
        "tool": "cruxible_playbill_line_arm",
        "tier": "governed_write",
        "profile": "full",
        "because": ["profile"],
    }
    assert "hidden" not in result.status.model_dump(mode="json")

    # The full profile runs it: same row, repair rendered as a tool call.
    full = service_playbill_next(
        instance,
        request=request.model_copy(update={"caller_tools": tuple(sorted(TOOL_PERMISSIONS))}),
        caller_rung=3,
    )
    (runnable,) = [item for item in full.items if item.reason == "consumer_stalled"]
    assert runnable.repair is not None and runnable.repair_requires is None
    assert runnable.repair.command == f'cruxible_playbill_line_arm(line="{line.identity.name}")'


def test_a_read_only_cli_caller_keeps_the_row_with_the_tier_it_needs(tmp_path: Path) -> None:
    instance, _line, when = _stopped_arm_world(tmp_path)

    result = service_playbill_next(
        instance,
        request=NextRequest(evaluation_time=when, access_profile=_PROFILE),
        caller_rung=0,
    )

    (row,) = [item for item in result.items if item.reason == "consumer_stalled"]
    assert row.repair is None
    assert row.repair_requires is not None
    assert row.repair_requires.because == ("tier",)
    assert row.repair_requires.tier == "governed_write"
    assert "profile" not in row.repair_requires.model_dump(mode="json")


def test_orient_attention_counts_the_kept_operational_rows(tmp_path: Path) -> None:
    instance, line, when = _stopped_arm_world(tmp_path)

    answer = service_playbill_orient(
        instance,
        evaluation_time=when,
        surface="mcp",
        caller_rung=3,
        caller_tools=_default_profile_tools(),
    )

    assert answer.attention is not None
    assert answer.attention.next_items >= 1
    assert f"repair consumer_stalled: {line.identity.qualified}" in answer.attention.top
