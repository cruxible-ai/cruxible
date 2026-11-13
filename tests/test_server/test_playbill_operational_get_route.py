"""POST /get resolves operational references and refuses wrong ones with a repair."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cruxible_core.governance.keys import GeneratedKeyMaterial
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from tests.core_support._knowledge_loop_support import seed_claims_into
from tests.test_server.test_playbill_procedure_measurements import (  # noqa: F401
    owned_playbill_http,
)


def test_http_get_reads_a_capture_by_handle_and_refuses_unknown_operational_refs(
    owned_playbill_http: tuple[TestClient, str, Path],  # noqa: F811
) -> None:
    client, instance_id, key = owned_playbill_http
    instance = get_playbill_manager().get(instance_id)
    reviewer = instance._recovered.head.principals.require_active("reviewer")  # noqa: SLF001
    seed_claims_into(
        instance,
        GeneratedKeyMaterial(
            principal=reviewer, private_key_path=key, public_key_path=key.with_suffix(".pub")
        ),
    )
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        (digest,) = projection.typed.connection.execute(
            "SELECT capture_digest FROM captures ORDER BY capture_digest LIMIT 1"
        ).fetchone()
    url = f"/api/v1/{instance_id}/get"
    handle = "CAP-" + str(digest).removeprefix("sha256:")[:12]

    answered = client.post(url, json={"ref": handle, "surface": "cli"})

    assert answered.status_code == 200, answered.text
    body = answered.json()
    assert body["kind"] == "capture" and body["ref"] == f"Capture:{digest}"
    assert body["card"]["capture"] == handle and body["card"]["status"] == "available"
    assert body["card"]["next"][-1] == f"cruxible capture read {digest}"

    # The handle reads the Capture's material too, and at takes a generation.
    read = client.post(f"/api/v1/{instance_id}/captures/read", json={"capture_digest": handle})
    assert read.status_code == 200, read.text
    assert read.json()["capture_digest"] == digest and read.json()["status"] == "verified"
    head = instance.accepted_history()[-1]
    pinned = client.post(url, json={"ref": handle, "at": str(head.sequence)})
    assert pinned.status_code == 200, pinned.text
    assert pinned.json()["coordinate"] == {"git_oid": head.oid[:12], "generation": head.sequence}

    for ref, section in (
        ("Line:hourly", "lines"),
        ("Mandate:nothing", "mandates"),
        ("ResolutionContract:none", "predictions"),
        ("CAP-" + "f" * 12, "captures"),
    ):
        missing = client.post(url, json={"ref": ref})
        assert missing.status_code == 404, missing.text
        assert missing.json()["error_code"] == "cruxible.get.ref_not_found"
        repair = missing.json()["repair"]
        assert repair["operation"] == "cruxible.orient"
        assert repair["arguments"] == {"section": section}

    malformed = client.post(url, json={"ref": "Capture:xyz"})
    assert malformed.status_code == 400, malformed.text
    assert malformed.json()["error_code"] == "cruxible.get.ref_malformed"


def test_http_serves_the_runs_section_and_refuses_an_unknown_run(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _key = playbill_http

    listed = client.get(f"/api/v1/{instance_id}/orient", params={"section": "runs"})
    assert listed.status_code == 200, listed.text
    assert listed.json()["runs"] == [] and listed.json()["section"] == "runs"

    missing = client.post(f"/api/v1/{instance_id}/get", json={"ref": "RUN-" + "a" * 12})
    assert missing.status_code == 404, missing.text
    assert missing.json()["error_code"] == "cruxible.get.ref_not_found"
    assert missing.json()["repair"]["arguments"] == {"section": "runs"}


def test_http_orient_serves_every_operational_section_and_counts_them(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _key = playbill_http
    url = f"/api/v1/{instance_id}/orient"

    for section in ("lines", "captures", "capture_contracts", "predictions", "mandates"):
        answered = client.get(url, params={"section": section})
        assert answered.status_code == 200, answered.text
        assert answered.json()["section"] == section
        assert answered.json()[section] == []

    counts = client.get(url).json()["artifacts"]
    assert {"lines", "captures", "capture_contracts", "resolution_contracts", "mandates"} <= set(
        counts
    )
    assert counts["runs"] == counts["running"] == 0
