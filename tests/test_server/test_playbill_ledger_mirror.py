"""The served doors onto the ledger mirror: bind it, read it back, orient on it."""

from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from cruxible_core.playbill.keys import generate_client_principal_key
from cruxible_core.server.registry import get_registry


def _bare(path: Path) -> Path:
    subprocess.run(["git", "init", "--bare", "-q", "--object-format=sha1", str(path)], check=True)
    return path


def test_clone_url_refuses_typed_before_a_mirror_is_bound(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _reviewer = playbill_http

    response = client.get(f"/api/v1/{instance_id}/playbill/ledger/mirror")

    assert response.status_code == 400, response.text
    assert "playbill.ledger.mirror_unset" in response.text
    assert "set-mirror" in response.text


def test_setting_a_mirror_publishes_and_reads_back(
    tmp_path: Path,
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _reviewer = playbill_http
    remote = _bare(tmp_path / "mirror.git")

    bound = client.post(
        f"/api/v1/{instance_id}/playbill/ledger/mirror",
        json={"url": str(remote)},
    )

    assert bound.status_code == 200, bound.text
    assert bound.json()["status"] == "current"
    assert bound.json()["mirror_url"] == str(remote)
    read_back = client.get(f"/api/v1/{instance_id}/playbill/ledger/mirror")
    assert read_back.status_code == 200, read_back.text
    assert read_back.json()["mirror_url"] == str(remote)


def test_a_credential_bearing_url_never_reaches_the_descriptor(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _reviewer = playbill_http

    response = client.post(
        f"/api/v1/{instance_id}/playbill/ledger/mirror",
        json={"url": "https://x-access-token:secret@forge.invalid/ledger.git"},
    )

    assert response.status_code == 400, response.text
    assert "playbill.ledger.mirror_url_invalid" in response.text
    assert client.get(f"/api/v1/{instance_id}/playbill/ledger/mirror").status_code == 400


def test_orientation_carries_the_mirror_url_without_a_second_round_trip(
    tmp_path: Path,
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _reviewer = playbill_http
    remote = _bare(tmp_path / "mirror.git")
    assert (
        client.post(
            f"/api/v1/{instance_id}/playbill/ledger/mirror",
            json={"url": str(remote)},
        ).status_code
        == 200
    )

    response = client.post(
        f"/api/v1/{instance_id}/playbill/search",
        json={"mode": "orient"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["orientation"]["mirror_url"] == str(remote)


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
        f"/api/v1/{second.record.instance_id}/playbill/init",
        json={
            "principals": [owner.principal.model_dump(mode="json")],
            "seed": False,
            "mirror_url": str(remote),
        },
    )

    assert initialized.status_code == 200, initialized.text
    read_back = client.get(f"/api/v1/{second.record.instance_id}/playbill/ledger/mirror")
    assert read_back.status_code == 200, read_back.text
    assert read_back.json() == {
        "tag": "playbill-ledger-mirror-v1",
        "instance_id": second.record.instance_id,
        "mirror_url": str(remote),
        "status": "current",
        "attempted_at": read_back.json()["attempted_at"],
        "published_main_oid": read_back.json()["published_main_oid"],
        "requested_sequence": 1,
        "attempted_sequence": 1,
        "published_sequence": 1,
        "wait_sequence": None,
        "published_refs": {"refs/heads/main": read_back.json()["published_main_oid"]},
        "detail": None,
    }


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
        f"/api/v1/{third.record.instance_id}/playbill/init",
        json={
            "principals": [owner.principal.model_dump(mode="json")],
            "seed": False,
            "mirror_url": "ext::sh -c 'curl evil'",
        },
    )

    assert refused.status_code == 400, refused.text
    assert "playbill.ledger.mirror_url_invalid" in refused.text
    assert not (managed / "instance.json").exists()
