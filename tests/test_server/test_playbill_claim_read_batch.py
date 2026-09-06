"""The bounded read surface reaches the shared service through HTTP."""

from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.runtime.playbill_manager import get_playbill_manager


def test_http_bound_empty_selection_and_missing_backing(playbill_http):
    client, instance_id, _ = playbill_http
    instance = get_playbill_manager().get(instance_id)
    coordinate = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())
    body = {"at": coordinate.model_dump(mode="json"), "subject_paths": ["subjects/work/item.json"]}
    response = client.post(f"/api/v1/{instance_id}/playbill/claims/read-batch", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["claims"] == [] and not response.json()["truncated"]
    assert response.json()["coordinate"] == body["at"]
    too_many = client.post(
        f"/api/v1/{instance_id}/playbill/claims/read-batch", json={**body, "limit": 257}
    )
    assert too_many.status_code == 422
    missing = client.post(
        f"/api/v1/{instance_id}/playbill/claims/backings",
        json={
            "at": body["at"],
            "claim_ids": ["CLM-" + "0" * 32],
        },
    )
    assert missing.status_code >= 400, missing.text
    assert missing.json()["error_type"] == "ClaimNotFoundError"
