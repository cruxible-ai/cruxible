"""Installation authority and request failures stay ahead of executable work."""

import contextlib

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
    response = http.post(f"/api/v1/{instance_id}/providers/install", json={"package": "example"})
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
    response = http.post(f"/api/v1/{instance_id}/providers/install", json=payload)
    assert response.status_code == 422, response.text


def test_empty_catalog_does_not_require_toolchain(playbill_http, monkeypatch):
    from cruxible_core.service.procedures import provider_installation

    monkeypatch.setattr(provider_installation, "toolchain", lambda *a: pytest.fail("toolchain"))
    http, instance_id, _ = playbill_http
    response = http.get(f"/api/v1/{instance_id}/providers")
    assert response.status_code == 200
    assert response.json()["packages"] == []
    assert "built wheels can still be transferred" in response.json()["detail"]


def test_an_install_preview_resolves_the_package_and_writes_nothing(
    playbill_http, monkeypatch, tmp_path
):
    """R12: preparing a package is itself a write, so the preview stops before it."""

    from cruxible_core.providers.package_index import IndexRelease
    from cruxible_core.runtime.playbill_manager import get_playbill_manager
    from cruxible_core.service.procedures import provider_installation as service
    from tests.support.store_snapshot import assert_writes_nothing

    http, instance_id, _ = playbill_http
    get_playbill_manager().consumer_runner.close()
    release = IndexRelease(
        name="example-provider",
        version="1.0.0",
        filename="example_provider-1.0.0-py3-none-any.whl",
        url="https://index.example/example_provider-1.0.0-py3-none-any.whl",
        sha256="0" * 64,
        index_url="https://index.example/simple",
    )
    monkeypatch.setattr(service, "find_release", lambda *args, **kwargs: release)
    # The provider toolchain is not installed in this test environment; the
    # preview refuses without it exactly as an install does.
    monkeypatch.setattr(service, "package_preparation_errors", contextlib.nullcontext)
    monkeypatch.setattr(service, "_source_files", lambda *a: pytest.fail("preview fetched"))
    url = f"/api/v1/{instance_id}/providers/install"

    response = assert_writes_nothing(
        [tmp_path],
        lambda: http.post(url, json={"package": "example-provider", "dry_run": True}),
        warm=lambda: http.get(f"/api/v1/{instance_id}/head"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["installed"], body["registered"]) == (
        "would_install",
        False,
        False,
    )
    assert "would fetch and build example-provider" in body["detail"]
    # F-007, the v1 exception (r12-scope-1001): the preview is labelled as
    # validation only, names what it did not run, and carries its coordinate.
    assert body["preview_scope"] == "validation_only"
    assert body["not_run"] == ["package_preparation", "deployment_readiness", "registration"]
    head = http.get(f"/api/v1/{instance_id}/head").json()
    assert body["coordinate"]["git_oid"] == head["coordinate"]["git_oid"]
