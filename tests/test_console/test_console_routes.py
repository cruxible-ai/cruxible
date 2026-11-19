"""The console's read allow-list is reads by the daemon's own classification.

The daemon classifies an operation by the permission tier its handler checks
(``runtime.permissions.PERMISSION_REQUIREMENTS``) and refuses anything above
its capability ceiling. These tests send every allow-listed route, in the shape
the console sends it, to a daemon fixed at the READ_ONLY ceiling and record
every permission check the handler makes: each route must pass, and every
check it makes must be a READ_ONLY one. A write added to the table fails here.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from cruxible_core.console.routes import READ_ROUTES, forwardable_path, is_read_route
from cruxible_core.runtime import permissions
from cruxible_core.runtime.permissions import (
    PERMISSION_REQUIREMENTS,
    PermissionMode,
    init_permissions,
    reset_permissions,
)
from cruxible_core.server.app import create_app, iter_api_routes
from tests.test_server.test_playbill_procedure_measurements import (  # noqa: F401
    owned_playbill_http,
)

_ACCESS_PROFILE = {
    "tag": "playbill-coverage-access-profile-v1",
    "profile_id": "console",
    "permitted_access_classes": ["instance", "public"],
}


def _requests(instance_id: str, coordinate: dict[str, Any]) -> dict[tuple[str, str], Any]:
    """One request per allow-listed route, shaped as the console's requests.ts builds it."""

    at = {key: coordinate[key] for key in coordinate}
    params = {
        key: coordinate[key]
        for key in ("git_oid", "semantic_root", "generation_root", "compiler_digest")
    }
    i = instance_id
    return {
        ("GET", "/api/v1/{instance_id}/head"): ("GET", f"/api/v1/{i}/head", {}, None),
        ("GET", "/api/v1/{instance_id}/orient"): (
            "GET",
            f"/api/v1/{i}/orient",
            {**params, "surface": "sdk"},
            None,
        ),
        ("GET", "/api/v1/{instance_id}/kits"): ("GET", f"/api/v1/{i}/kits", {}, None),
        ("GET", "/api/v1/{instance_id}/proposals"): (
            "GET",
            f"/api/v1/{i}/proposals",
            {"limit": 10},
            None,
        ),
        ("GET", "/api/v1/{instance_id}/inbox"): ("GET", f"/api/v1/{i}/inbox", {"limit": 10}, None),
        ("GET", "/api/v1/{instance_id}/inbox/{item_id}"): (
            "GET",
            f"/api/v1/{i}/inbox/missing",
            {},
            None,
        ),
        ("GET", "/api/v1/{instance_id}/procedure-runs/{run_id}"): (
            "GET",
            f"/api/v1/{i}/procedure-runs/missing",
            {},
            None,
        ),
        ("POST", "/api/v1/{instance_id}/get"): (
            "POST",
            f"/api/v1/{i}/get",
            {},
            {"ref": "ClaimType:missing", "detail": "summary", "at": at, "surface": "sdk"},
        ),
        ("POST", "/api/v1/{instance_id}/get-batch"): (
            "POST",
            f"/api/v1/{i}/get-batch",
            {},
            {
                "refs": ["ClaimType:missing"],
                "detail": "summary",
                "at": at,
                "surface": "sdk",
                "subject_suggestions": False,
            },
        ),
        ("POST", "/api/v1/{instance_id}/query"): (
            "POST",
            f"/api/v1/{i}/query",
            {},
            {
                "kind": "Trigger",
                "where": [{"field": "schedule", "eq": "capture_landing"}],
                "select": ["name", "schedule"],
                "limit": 5,
                "at": at,
            },
        ),
        ("POST", "/api/v1/{instance_id}/next"): (
            "POST",
            f"/api/v1/{i}/next",
            {},
            {
                "at": at,
                "evaluation_time": "2026-11-10T00:00:00Z",
                "access_profile": _ACCESS_PROFILE,
                "caller_surface": "cli",
                "limit": 10,
            },
        ),
        ("POST", "/api/v1/{instance_id}/claims/read-batch"): (
            "POST",
            f"/api/v1/{i}/claims/read-batch",
            {},
            {"claim_ids": ["CLM-missing"], "at": at},
        ),
        ("POST", "/api/v1/{instance_id}/since"): (
            "POST",
            f"/api/v1/{i}/since",
            {},
            {
                "generation": 0,
                "at": at,
                "access_profile": {**_ACCESS_PROFILE, "disclose_restricted_existence": True},
                "max_rows": 10,
                "max_bytes": 1_048_576,
            },
        ),
        ("POST", "/api/v1/{instance_id}/captures/read"): (
            "POST",
            f"/api/v1/{i}/captures/read",
            {},
            {"capture_digest": "sha256:" + "0" * 64, "at": at, "max_bytes": 1024},
        ),
        ("POST", "/api/v1/{instance_id}/proposals/{proposal_id}/review"): (
            "POST",
            f"/api/v1/{i}/proposals/missing/review",
            {},
            {"detail": True},
        ),
        ("POST", "/api/v1/{instance_id}/procedures/{name}/readings"): (
            "POST",
            f"/api/v1/{i}/procedures/missing/readings",
            {},
            {"at": at, "limit": 10},
        ),
        ("GET", "/api/v1/{instance_id}/runbooks/{name}/preview"): (
            "GET",
            f"/api/v1/{i}/runbooks/missing/preview",
            {},
            None,
        ),
        ("GET", "/version"): ("GET", "/version", {}, None),
        ("GET", "/api/v1/{instance_id}/host"): ("GET", f"/api/v1/{i}/host", {}, None),
        ("GET", "/api/v1/{instance_id}/whoami"): ("GET", f"/api/v1/{i}/whoami", {}, None),
        ("GET", "/api/v1/{instance_id}/providers"): ("GET", f"/api/v1/{i}/providers", {}, None),
    }


@pytest.fixture
def recorded_checks(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Every ``check_permission`` tool name, from every module that imported it."""

    original = permissions.check_permission
    seen: list[str] = []

    def recording(tool_name: str, *args: Any, **kwargs: Any) -> None:
        seen.append(tool_name)
        original(tool_name, *args, **kwargs)

    for module in list(sys.modules.values()):
        if getattr(module, "check_permission", None) is original:
            monkeypatch.setattr(module, "check_permission", recording)
    yield seen


def test_every_read_route_passes_only_read_only_checks_at_a_read_only_ceiling(
    owned_playbill_http: tuple[TestClient, str, Path],  # noqa: F811
    recorded_checks: list[str],
) -> None:
    client, instance_id, _key = owned_playbill_http
    coordinate = client.get(f"/api/v1/{instance_id}/head").json()["coordinate"]
    # From here the daemon's ceiling is READ_ONLY, as `server start
    # --capability-ceiling read_only` fixes it.
    reset_permissions()
    init_permissions(PermissionMode.READ_ONLY)

    requests = _requests(instance_id, coordinate)
    assert set(requests) == set(READ_ROUTES), "every allow-listed route has a probe"
    problems: list[str] = []
    for route, (method, path, params, body) in requests.items():
        recorded_checks.clear()
        response = client.request(method, path, params=params, json=body)
        label = " ".join(route)
        if response.status_code in (403, 422):
            problems.append(f"{label}: {response.status_code} {response.text[:300]}")
            continue
        if route == ("GET", "/version"):
            # Unauthenticated build identity, like /health; it checks nothing.
            assert response.status_code == 200, response.text
            continue
        if not recorded_checks:
            problems.append(f"{label}: answered {response.status_code} without a permission check")
        for tool in recorded_checks:
            if PERMISSION_REQUIREMENTS[tool] is not PermissionMode.READ_ONLY:
                problems.append(f"{label}: checks {tool} at {PERMISSION_REQUIREMENTS[tool].name}")
    assert problems == [], "\n".join(problems)


def test_every_read_route_is_served_by_the_daemon_with_that_method() -> None:
    served = {
        (method, route.path)
        for route in iter_api_routes(create_app().routes)
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    served |= {("GET", "/version")}
    assert set(READ_ROUTES) <= served, sorted(set(READ_ROUTES) - served)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v1/inst/lines/l/runs"),
        ("POST", "/api/v1/inst/procedures/p/runs"),
        ("POST", "/api/v1/inst/runbooks/r/runs"),
        ("POST", "/api/v1/inst/lines/l/evaluate"),
        ("POST", "/api/v1/inst/proposals/p/approve"),
        ("POST", "/api/v1/inst/set"),
        ("POST", "/api/v1/inst/write"),
        ("GET", "/api/v1/inst/get"),
        ("POST", "/api/v1/inst/head"),
        ("DELETE", "/api/v1/inst/head"),
        ("GET", "/health"),
        ("GET", "/openapi.json"),
        ("GET", "/api/v1/server/info"),
        ("POST", "/api/v1/server/stop"),
    ],
)
def test_writes_and_unlisted_reads_are_not_read_routes(method: str, path: str) -> None:
    assert not is_read_route(method, path)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/inst/inbox/a%2Fack",
        "/api/v1/inst/inbox/a%2fack",
        "/api/v1/inst/inbox/a%5Cack",
        "/api/v1/inst/inbox/..",
        "/api/v1/inst/inbox/%2e%2e",
        "/api/v1/inst/inbox/.",
        "/api/v1//head",
        "/api/v1/inst/head/",
        "/api/v1/inst/inbox/a%00",
        "/api/v1/inst/inbox/a b",
        "api/v1/inst/head",
    ],
)
def test_paths_that_decode_into_other_segments_are_refused(path: str) -> None:
    assert not forwardable_path(path)
    assert not is_read_route("GET", path)


def test_template_parameters_match_one_segment_each() -> None:
    assert is_read_route("GET", "/api/v1/inst_1/inbox/item-7")
    assert is_read_route("get", "/api/v1/inst_1/head")
    assert is_read_route("POST", "/api/v1/inst_1/procedures/security.start/readings")
    assert not is_read_route("GET", "/api/v1/inst_1/inbox/item-7/extra")
