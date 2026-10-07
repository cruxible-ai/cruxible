"""HTTP orient: one GET, values without nulls, coded refusals."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_core.runtime.permissions import PermissionMode


def test_http_orient_answers_the_map_rendered_for_the_requested_surface(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http

    response = client.get(f"/api/v1/{instance_id}/orient", params={"surface": "mcp"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tag"] == "playbill-orient-v1" and body["instance"] == instance_id
    assert body["kinds"] == [] and body["truncated"] is False
    assert set(body["artifacts"]) == {
        "claim_types",
        "procedures",
        "documents",
        "queries",
        "interfaces",
        # Operational families: counted in the map, paged by their own section.
        "lines",
        "captures",
        "capture_contracts",
        "resolution_contracts",
        "mandates",
        "runs",
        "running",
        "claims",
    }
    assert body["you"]["actor"] is not None
    # Optional parts that do not apply are absent, never null.
    assert "kind_detail" not in body and "next_cursor" not in body
    assert None not in body.values()
    assert all(line.startswith("cruxible_") for line in body["next"])

    pinned = client.get(
        f"/api/v1/{instance_id}/orient",
        params={"at": body["coordinate"]["git_oid"], "section": "documents"},
    )
    assert pinned.status_code == 200, pinned.text
    assert pinned.json()["coordinate"] == body["coordinate"]
    assert pinned.json()["documents"] == []


def test_http_orient_refusals_are_coded(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http
    url = f"/api/v1/{instance_id}/orient"

    missing = client.get(url, params={"kind": "project.nothing"})
    assert missing.status_code == 404, missing.text
    assert missing.json()["error_code"] == "cruxible.orient.kind_not_found"
    assert missing.json()["context"]["kind"] == "project.nothing"
    assert missing.json()["repair"]["operation"] == "cruxible.orient"

    both = client.get(url, params={"kind": "project.nothing", "section": "queries"})
    assert both.status_code == 400, both.text
    assert both.json()["error_code"] == "cruxible.orient.request_invalid"

    stale = client.get(url, params={"section": "queries", "cursor": "not-a-cursor"})
    assert stale.status_code == 400, stale.text
    assert stale.json()["error_code"] == "cruxible.list.cursor_mismatch"

    assert client.get(url, params={"section": "nothing"}).status_code == 422


@pytest.mark.parametrize("surface", ["cli", "sdk", "mcp"])
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (PermissionMode.READ_ONLY, 0),
        (PermissionMode.GOVERNED_WRITE, 1),
        (PermissionMode.GRAPH_WRITE, 2),
    ],
)
def test_orient_attention_matches_next_for_the_effective_caller(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    mode: PermissionMode,
    expected: int,
) -> None:
    _assert_attention_parity(playbill_http, monkeypatch, mode, surface, None, expected)


@pytest.mark.parametrize(
    ("tools", "expected"),
    [((), 0), (("cruxible_prediction_settle",), 1), (("cruxible_proposal_approve",), 1)],
)
def test_orient_attention_matches_next_for_an_mcp_profile_missing_a_tool(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tools: tuple[str, ...],
    expected: int,
) -> None:
    _assert_attention_parity(
        playbill_http, monkeypatch, PermissionMode.GRAPH_WRITE, "mcp", tools, expected
    )


def _assert_attention_parity(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch: pytest.MonkeyPatch,
    mode: PermissionMode,
    surface: str,
    tools: tuple[str, ...] | None,
    expected: int,
) -> None:
    from cruxible_core.runtime import permissions
    from cruxible_core.service.discovery import next as next_module
    from tests.test_integration.test_next_caller_view import _approval_row

    client, instance_id, _key = playbill_http
    prediction = next_module._item(
        severity="repair",
        reason="prediction_settleable",
        subject_identity="PredictionContract:window",
        detail={},
        repair=next_module.PlaybillNextRepairV1(
            operation="cruxible.prediction.settle",
            target="PredictionContract:window",
            required_change="settle_the_window",
            arguments={"prediction_id": "window"},
        ),
    )
    # Feed both row families into the real shared fold. A credential cannot
    # raise this process ceiling, so both routes must use the effective tier.
    monkeypatch.setattr(next_module, "_prediction_items", lambda *a, **kw: (prediction,))
    monkeypatch.setattr(next_module, "_approval_items", lambda *a, **kw: (_approval_row(),))
    monkeypatch.setattr(permissions, "_cached_mode", mode)
    when = "2026-09-29T00:00:00Z"
    params: dict[str, str | list[str]] = {"surface": surface, "evaluation_time": when}
    if tools is not None:
        params["caller_tools"] = list(tools) or [""]
    orient = client.get(f"/api/v1/{instance_id}/orient", params=params)
    assert orient.status_code == 200, orient.text
    queue = client.post(
        f"/api/v1/{instance_id}/next",
        json={
            "evaluation_time": when,
            "access_profile": {
                "profile_id": "orient",
                "permitted_access_classes": ["instance", "public"],
            },
            "caller_surface": surface,
            "caller_tools": tools,
        },
    )
    assert queue.status_code == 200, queue.text
    attention = orient.json()["attention"]
    result = queue.json()
    # Option (b): both rows stay for every caller; only the repairs the
    # effective caller cannot run are withheld, each naming what it requires.
    assert attention["next_items"] == result["total_items"] == 2
    assert attention["top"] == [
        f"{item['severity']} {item['reason']}: {item['subject_identity']}"
        for item in result["items"][:3]
    ]
    runnable = [item for item in result["items"] if item.get("repair") is not None]
    withheld = [item for item in result["items"] if item.get("repair") is None]
    assert len(runnable) == expected
    assert all(item["repair_requires"]["tool"] for item in withheld)
    assert "hidden" not in result["status"]
