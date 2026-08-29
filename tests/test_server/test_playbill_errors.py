"""Wire-error laws for the surviving host, auth, and Playbill surface."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client import errors as client_errors
from cruxible_client.contracts.errors import (
    ProposalEvaluationIntegrityError,
    ProposalIntegrityError,
)
from cruxible_core.errors import (
    AuthenticationError,
    ConfigError,
    CoreError,
    DataValidationError,
    InstanceNotFoundError,
    PermissionDeniedError,
    RuntimeCredentialNotFoundError,
)
from cruxible_core.playbill.authoring.insertions import PublicationClaimNotAccepted
from cruxible_core.playbill.claim_type_migrations import ClaimTypeMigrationIncomplete
from cruxible_core.playbill.search import PlaybillSearchBudgetsV1
from cruxible_core.server.errors import error_to_response
from cruxible_core.server.errors import response_to_error as compat_response_to_error


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_type", "attrs"),
    [
        (
            ConfigError("bad host configuration", errors=["missing setting"]),
            400,
            client_errors.ConfigError,
            {"errors": ["missing setting"]},
        ),
        (
            DataValidationError("bad Playbill request", errors=["invalid coordinate"]),
            400,
            client_errors.DataValidationError,
            {"errors": ["invalid coordinate"]},
        ),
        (
            AuthenticationError("invalid credential"),
            401,
            client_errors.AuthenticationError,
            {},
        ),
        (
            PermissionDeniedError(
                "cruxible_playbill_activate",
                "GOVERNED_WRITE",
                "GRAPH_WRITE",
                ceiling_mode="GOVERNED_WRITE",
            ),
            403,
            client_errors.PermissionDeniedError,
            {
                "tool_name": "cruxible_playbill_activate",
                "current_mode": "GOVERNED_WRITE",
                "required_mode": "GRAPH_WRITE",
                "ceiling_mode": "GOVERNED_WRITE",
            },
        ),
        (
            InstanceNotFoundError("inst_missing"),
            404,
            client_errors.InstanceNotFoundError,
            {"instance_id": "inst_missing"},
        ),
        (
            RuntimeCredentialNotFoundError("rcred_missing"),
            404,
            client_errors.RuntimeCredentialNotFoundError,
            {"credential_id": "rcred_missing"},
        ),
    ],
)
def test_surviving_errors_round_trip_with_status_and_context(
    error: CoreError,
    expected_status: int,
    expected_type: type[client_errors.CoreError],
    attrs: dict[str, object],
) -> None:
    status, body = error_to_response(error)
    restored = client_errors.response_to_error(status, body)

    assert status == expected_status
    assert type(restored) is expected_type
    for name, value in attrs.items():
        assert getattr(restored, name) == value


def test_request_validation_envelope_retains_field_errors() -> None:
    restored = client_errors.response_to_error(
        422,
        client_errors.ErrorResponse(
            error_type="RequestValidationError",
            message="Request validation failed",
            errors=["body.coordinate: Input should be a valid object"],
        ),
    )

    assert type(restored) is client_errors.DataValidationError
    assert restored.errors == ["body.coordinate: Input should be a valid object"]


def test_insertion_protocol_refusal_is_a_typed_bad_request() -> None:
    error = PublicationClaimNotAccepted(
        "playbill.authoring.publication_claim_not_accepted: Claim is not accepted"
    )

    status, body = error_to_response(error)

    assert status == 400
    assert body.error_type == "PublicationClaimNotAccepted"
    assert body.error_code == "playbill.authoring.publication_claim_not_accepted"


def test_daemon_proposal_integrity_failure_is_never_reported_as_a_conflict() -> None:
    status, body = error_to_response(
        ProposalEvaluationIntegrityError("proposal evaluation record failed internal validation")
    )

    assert status == 500
    assert body.error_type == "ProposalEvaluationIntegrityError"


def test_general_proposal_integrity_conflicts_remain_retryable() -> None:
    status, body = error_to_response(ProposalIntegrityError("proposal evaluation raced main"))

    assert status == 409
    assert body.error_type == "ProposalIntegrityError"


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (ProposalIntegrityError("proposal evaluation raced main"), 409),
        (
            ProposalEvaluationIntegrityError(
                "proposal evaluation record failed internal validation"
            ),
            500,
        ),
    ],
)
def test_proposal_integrity_split_reaches_the_http_surface(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch: pytest.MonkeyPatch,
    error: ProposalIntegrityError,
    expected_status: int,
) -> None:
    client, instance_id, _private_key = playbill_http

    def fail_search(*_args: object, **_kwargs: object) -> object:
        raise error

    monkeypatch.setattr("cruxible_core.runtime.playbill_api.playbill_search", fail_search)
    response = client.post(
        f"/api/v1/{instance_id}/playbill/search",
        json={"mode": "list"},
    )

    assert response.status_code == expected_status, response.text
    assert response.json()["error_type"] == type(error).__name__


def test_non_insertion_code_attribute_does_not_widen_the_error_envelope() -> None:
    error = ClaimTypeMigrationIncomplete(
        "playbill.claim_type.migration_incomplete: missing dependent"
    )

    status, body = error_to_response(error)

    assert status == 400
    assert "error_code" not in body.model_dump(mode="json", exclude_none=True)


def test_capability_ceiling_denial_message_survives_round_trip() -> None:
    error = PermissionDeniedError(
        "cruxible_playbill_principal_change",
        "GOVERNED_WRITE",
        "ADMIN",
        ceiling_mode="GOVERNED_WRITE",
    )

    status, body = error_to_response(error)
    restored = client_errors.response_to_error(status, body)

    assert status == 403
    assert "capability ceiling is GOVERNED_WRITE" in str(restored)
    assert "cruxible_playbill_principal_change" in str(restored)


def test_unknown_error_type_falls_back_to_core_error() -> None:
    restored = client_errors.response_to_error(
        500,
        client_errors.ErrorResponse(error_type="UnknownServerFailure", message="opaque failure"),
    )

    assert type(restored) is client_errors.CoreError
    assert str(restored) == "opaque failure"


def test_server_compat_decoder_re_exports_client_decoder() -> None:
    body = client_errors.ErrorResponse(
        error_type="AuthenticationError",
        message="invalid credential",
    )

    assert type(compat_response_to_error(401, body)) is client_errors.AuthenticationError


def test_http_next_maps_raw_source_observation_to_typed_refusal(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http
    response = client.post(
        f"/api/v1/{instance_id}/playbill/next",
        json={
            "tag": "playbill-next-request-v1",
            "evaluation_time": "2026-08-26T16:00:00+00:00",
            "access_profile": {
                "tag": "playbill-coverage-access-profile-v1",
                "profile_id": "test-next",
                "permitted_access_classes": ["instance", "public"],
                "disclose_restricted_existence": True,
            },
            "workspace_observation": {
                "tag": "playbill-next-workspace-observation-v1",
                "source_observations": [
                    {
                        "source_id": "corpus.runbook",
                        "document_id": "runbook",
                        "observed_source_digest": "sha256:" + "1" * 64,
                    }
                ],
            },
        },
    )

    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "playbill.next.workspace_observation_invalid"


def test_http_discover_refuses_an_empty_request_typed_not_as_a_server_error(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    """Only profile=interfaces answers an empty request; the rest refuse typed.

    The discovery law always refused this, but it refused from a pydantic
    validator the route builds server-side, so the refusal reached the caller
    as an opaque 500.
    """
    client, instance_id, _private_key = playbill_http

    for profile in ("subjects", "all"):
        response = client.post(
            f"/api/v1/{instance_id}/playbill/discover",
            json={"profile": profile},
        )

        assert response.status_code == 400, response.text
        body = response.json()
        assert body["error_type"] == "PlaybillFormatError"
        assert "needs a query or an entrypoint" in body["message"]
        assert profile in body["message"]


def test_http_discover_still_answers_an_empty_interfaces_request(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http

    response = client.post(
        f"/api/v1/{instance_id}/playbill/discover",
        json={"profile": "interfaces"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["tag"] == "playbill-interface-inventory-v1"


def test_http_activate_refuses_a_missing_proposal_id_typed(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    """A None proposal_id stringifies into the path; it must not reach the catch-all."""
    client, instance_id, _private_key = playbill_http

    response = client.post(f"/api/v1/{instance_id}/playbill/proposals/None/activate")

    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "playbill.proposal.activation_request_invalid"


def test_a_frozen_model_failing_inside_a_service_stays_a_generic_server_error(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An internal invariant breach is a server fault, not the caller's mistake.

    A blanket ValidationError handler cannot tell the two apart, so it would
    report a broken internal model as a 400 and put that model's name on the
    wire. Request-shaped refusals are raised as typed CoreErrors instead, where
    the request is actually understood.
    """
    client, instance_id, _private_key = playbill_http

    def exploding_search(selected: str, **values: object) -> object:
        PlaybillSearchBudgetsV1(max_rows=0)  # below the model's own floor
        raise AssertionError("unreachable")

    monkeypatch.setattr("cruxible_core.runtime.playbill_api.playbill_search", exploding_search)
    # The default TestClient re-raises server exceptions; this asserts on the
    # body the client would actually receive.
    quiet = TestClient(client.app, raise_server_exceptions=False)
    response = quiet.post(
        f"/api/v1/{instance_id}/playbill/search",
        json={"mode": "list"},
    )

    assert response.status_code == 500, response.text
    body = response.json()
    assert body["error_type"] == "InternalServerError"
    assert body["message"] == "internal server error"
    # The internal model name never reaches the client.
    assert "PlaybillSearchBudgetsV1" not in response.text


def test_the_caller_shaped_refusals_stay_typed_400s(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    """The two surfaces X4 fixed refuse at the boundary, not through a blanket rule."""
    client, instance_id, _private_key = playbill_http

    empty_kinds = client.post(
        f"/api/v1/{instance_id}/playbill/search",
        json={"mode": "list", "kinds": []},
    )
    assert empty_kinds.status_code == 200, empty_kinds.text

    empty_discover = client.post(
        f"/api/v1/{instance_id}/playbill/discover",
        json={"profile": "subjects"},
    )
    assert empty_discover.status_code == 400, empty_discover.text
    assert empty_discover.json()["error_type"] == "PlaybillFormatError"
