"""Runbook run and preview over HTTP and the SDK client (D-R36)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.runbooks import (
    Runbook,
    RunbookPreview,
    RunbookRunResult,
    render_runbook,
    runbook_path,
)
from cruxible_client.contracts.triggers import ActionTarget
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from tests.test_server.test_playbill_line_run_refusals import _accept_members


def _served_runbook(instance_id: str, reviewer_key_path: Path) -> None:
    runbook = Runbook(
        identity=ArtifactIdentity(kind="Runbook", name="test.sweep"),
        steps=(ActionTarget(action="evidence.sweep"), ActionTarget(action="floor.refresh")),
    )
    _accept_members(
        get_playbill_manager().get(instance_id),
        reviewer_key_path,
        {runbook_path("test.sweep"): render_runbook(runbook)},
        timestamp="2026-09-03T09:00:00.000000Z",
    )


def test_a_runbook_runs_and_previews_over_http(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, reviewer_key_path = playbill_http
    _served_runbook(instance_id, reviewer_key_path)

    preview = client.get(f"/api/v1/{instance_id}/runbooks/test.sweep/preview")
    assert preview.status_code == 200, preview.text
    parsed = RunbookPreview.model_validate(preview.json())
    assert [step.action_effect for step in parsed.steps] == ["findings", "workspace_output"]

    run = client.post(f"/api/v1/{instance_id}/runbooks/test.sweep/runs")
    assert run.status_code == 200, run.text
    result = RunbookRunResult.model_validate(run.json())
    assert (result.status, [step.status for step in result.steps]) == (
        "completed",
        ["fired", "fired"],
    )


def test_an_unknown_runbook_is_a_typed_404(playbill_http: tuple[TestClient, str, Path]) -> None:
    client, instance_id, _reviewer_key_path = playbill_http
    for response in (
        client.get(f"/api/v1/{instance_id}/runbooks/test.none/preview"),
        client.post(f"/api/v1/{instance_id}/runbooks/test.none/runs"),
    ):
        assert response.status_code == 404, response.text
        assert response.json()["error_type"] == "RunbookNotFoundError"
