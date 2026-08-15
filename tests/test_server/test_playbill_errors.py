"""Wire-error laws for the surviving host, auth, and Playbill surface."""

from __future__ import annotations

import pytest

from cruxible_client import errors as client_errors
from cruxible_core.errors import (
    AuthenticationError,
    ConfigError,
    CoreError,
    DataValidationError,
    InstanceNotFoundError,
    PermissionDeniedError,
    RuntimeCredentialNotFoundError,
)
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
