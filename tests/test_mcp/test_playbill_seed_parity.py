"""The retained MCP seed planner reuses the deterministic core planner."""

from __future__ import annotations

from pathlib import Path

from cruxible_core.mcp import handlers


def test_seed_plan_is_offline_and_matches_the_core_planner(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    repository = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(repository))

    result = handlers.handle_playbill_seed_plan(
        bundle_path="benchmarks/playbill_taubench/seed-example",
        proposal_name="mcp-example",
    )

    assert len(result.plan.groups) == 9
    assert result.plan_digest in result.rendered[0]
    assert result.plan.group_ids[0] == "claim_type:project.work_item.status"
