"""U1's own typed refusals reach the wire as 4xx with a runnable repair."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cruxible_client.contracts.repairs import (
    DECLARED_HAND_EDIT_CHANGES,
    RUNNABLE_REFUSAL_REPAIRS,
)

_ABSENT = "sha256:" + "c" * 64
_OTHER = "sha256:" + "d" * 64


def _body(digest: str) -> dict[str, object]:
    # No asserted instant: the occurrence's EVALUATION INSTANT is the daemon's.
    return {
        "tag": "playbill-line-run-request-v1",
        "line_identity_digest": digest,
        "occurrence_id": None,
        "evaluation_time": None,
    }


def test_an_unaccepted_line_refuses_typed_over_http_instead_of_a_daemon_fault(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _reviewer_key = playbill_http

    response = client.post(
        f"/api/v1/{instance_id}/playbill/lines/{_ABSENT}/runs",
        json=_body(_ABSENT),
    )

    assert response.status_code == 404, response.text
    payload = response.json()
    assert payload["error_code"] == "line_not_accepted"
    assert payload["repair"] == {
        "hand_edit": {
            "target": "refusal/line_not_accepted",
            "required_change": DECLARED_HAND_EDIT_CHANGES["line_not_accepted"],
        }
    }


def test_a_route_body_identity_mismatch_refuses_typed_over_http(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _reviewer_key = playbill_http

    response = client.post(
        f"/api/v1/{instance_id}/playbill/lines/{_ABSENT}/runs",
        json=_body(_OTHER),
    )

    assert response.status_code == 400, response.text
    payload = response.json()
    assert payload["error_code"] == "line_identity_mismatch"
    assert payload["repair"] == RUNNABLE_REFUSAL_REPAIRS["line_identity_mismatch"].model_dump(
        mode="json"
    )


def test_the_sibling_procedure_run_route_refuses_typed_in_the_same_family(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    """The 500 posture U1 inherited is repaired for the whole surface family."""

    client, instance_id, _reviewer_key = playbill_http

    response = client.post(
        f"/api/v1/{instance_id}/playbill/procedures/no-such-procedure/runs",
        json={
            "tag": "playbill-procedure-run-request-v2",
            "evaluation_time": None,
            "input": {},
        },
    )

    assert response.status_code == 404, response.text
    payload = response.json()
    assert payload["error_type"] == "ProcedureNotFound"
    assert payload["message"] != "internal server error"


def test_an_instant_outside_the_daemon_skew_bound_refuses_typed_over_http(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    """A due proof the caller controls is not a rate: the bound is served."""

    client, instance_id, _reviewer_key = playbill_http

    response = client.post(
        f"/api/v1/{instance_id}/playbill/lines/{_ABSENT}/runs",
        json={
            "tag": "playbill-line-run-request-v1",
            "line_identity_digest": _ABSENT,
            "occurrence_id": None,
            "evaluation_time": "2099-01-01T00:00:00Z",
        },
    )

    assert response.status_code == 400, response.text
    payload = response.json()
    assert payload["error_code"] == "evaluation_instant_skewed"
    assert payload["repair"] == RUNNABLE_REFUSAL_REPAIRS["evaluation_instant_skewed"].model_dump(
        mode="json"
    )
