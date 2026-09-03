"""HTTP delivery for the read-only G9 audit patrol."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def _payload() -> dict[str, object]:
    return {
        "tag": "playbill-audit-request-v1",
        "evaluation_time": "2026-08-26T18:00:00+00:00",
        "access_profile": {
            "tag": "playbill-coverage-access-profile-v1",
            "profile_id": "http-audit",
            "permitted_access_classes": ["instance", "public"],
            "disclose_restricted_existence": True,
        },
        "scope": {
            "tag": "playbill-audit-scope-v1",
            "claim_type_identities": [],
            "subject_kinds": [],
        },
        "budget": {
            "tag": "playbill-audit-budget-v1",
            "max_rows": 10,
            "max_bytes": 4096,
        },
    }


def test_http_audit_is_read_tier_and_returns_completed_coverage(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http
    response = client.post(f"/api/v1/{instance_id}/playbill/audit", json=_payload())

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["tag"] == "playbill-audit-result-v1"
    assert payload["rows"] == []
    assert payload["coverage"]["access_permitted"]
    # This host takes init's explicit seed opt-out, so genesis is the only generation.
    assert payload["audited_through_generation"] == 0
    assert payload["operational_input_head_digest"].startswith("sha256:")


def test_http_audit_maps_invalid_profile_and_budget_to_typed_refusals(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http
    invalid_profile = _payload()
    invalid_profile["access_profile"] = {
        "tag": "playbill-coverage-access-profile-v1",
        "profile_id": "not valid",
        "permitted_access_classes": ["instance"],
        "disclose_restricted_existence": True,
    }
    profile_response = client.post(f"/api/v1/{instance_id}/playbill/audit", json=invalid_profile)
    invalid_budget = _payload()
    invalid_budget["budget"] = {
        "tag": "playbill-audit-budget-v1",
        "max_rows": 0,
        "max_bytes": 4096,
    }
    budget_response = client.post(f"/api/v1/{instance_id}/playbill/audit", json=invalid_budget)

    assert profile_response.status_code == 400, profile_response.text
    assert profile_response.json()["error_code"] == "playbill.audit.access_profile_invalid"
    assert budget_response.status_code == 400, budget_response.text
    assert budget_response.json()["error_code"] == "playbill.audit.budget_invalid"
