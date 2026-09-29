"""Capture reads share the daemon's instance and body-access boundary."""

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_core.runtime.permissions import reset_permissions


def test_capture_read_permission_and_request_validation(
    playbill_http: tuple[TestClient, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    http, instance_id, _key = playbill_http
    route = f"/api/v1/{instance_id}/playbill/captures/read"
    request = {"capture_digest": "sha256:" + "f" * 64}
    available = http.post(route, json=request)
    assert available.status_code == 200, available.text
    assert available.json()["status"] == "unavailable"
    assert http.post(route, json={"capture_digest": "/etc/passwd"}).status_code == 422
    assert http.post(route, json={**request, "max_bytes": -1}).status_code == 422
    stored = http.post(
        f"/api/v1/{instance_id}/playbill/bodies",
        json={"content_base64": base64.b64encode(b"not a capture").decode("ascii")},
    )
    assert stored.status_code == 200, stored.text
    refused = http.post(route, json={"capture_digest": stored.json()["digest"]})
    assert refused.status_code == 404, refused.text
    assert refused.json()["error_code"] == "playbill.capture.not_a_capture"
    monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
    reset_permissions()
    try:
        denied = http.post(route, json=request)
        assert denied.status_code == 403, denied.text
        assert "envelope" not in denied.json()
    finally:
        monkeypatch.delenv("CRUXIBLE_MODE")
        reset_permissions()
