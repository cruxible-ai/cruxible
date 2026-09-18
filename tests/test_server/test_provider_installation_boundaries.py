"""Installation authority and request failures stay ahead of executable work."""

import pytest

from cruxible_core.runtime.permissions import reset_permissions


def test_install_requires_admin_before_preparation(playbill_http, monkeypatch):
    from cruxible_core.runtime import playbill_api

    http, instance_id, _ = playbill_http
    monkeypatch.setenv("CRUXIBLE_MODE", "governed_write")
    reset_permissions()
    monkeypatch.setattr(
        playbill_api, "service_install_provider", lambda *a, **kw: pytest.fail("prepared code")
    )
    response = http.post(
        f"/api/v1/{instance_id}/playbill/providers/install", json={"package": "example"}
    )
    assert response.status_code == 403, response.text
    reset_permissions()


@pytest.mark.parametrize(
    "payload",
    [
        {"package": "../local"},
        {
            "wheel": {"filename": "../x.whl", "digest": "sha256:" + "a" * 64},
            "lock_digest": "sha256:" + "b" * 64,
        },
        {"package": "example", "lock_digest": "sha256:" + "b" * 64},
        {"package": "example", "environment_path": "/not-a-daemon-path"},
    ],
)
def test_install_rejects_paths_and_mixed_sources(playbill_http, payload):
    http, instance_id, _ = playbill_http
    response = http.post(f"/api/v1/{instance_id}/playbill/providers/install", json=payload)
    assert response.status_code == 422, response.text


def test_empty_catalog_does_not_require_toolchain(playbill_http, monkeypatch):
    from cruxible_core.service.procedures import provider_installation

    monkeypatch.setattr(provider_installation, "toolchain", lambda *a: pytest.fail("toolchain"))
    http, instance_id, _ = playbill_http
    response = http.get(f"/api/v1/{instance_id}/playbill/providers")
    assert response.status_code == 200
    assert response.json()["packages"] == []
    assert "built wheels can still be transferred" in response.json()["detail"]
