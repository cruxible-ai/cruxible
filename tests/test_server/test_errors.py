"""Tests for HTTP error serialization."""

from __future__ import annotations

import pytest

from cruxible_client import errors as client_errors
from cruxible_core.errors import (
    ConfigError,
    ConstraintViolationError,
    CoreError,
    DataValidationError,
    EntityNotFoundError,
    EntityTypeNotFoundError,
    GroupNotFoundError,
    IngestionError,
    InstanceNotFoundError,
    MutationError,
    OutcomeNotFoundError,
    PermissionDeniedError,
    QueryExecutionError,
    QueryNotFoundError,
    ReceiptNotFoundError,
    RelationshipAmbiguityError,
    RelationshipNotFoundError,
)
from cruxible_core.server.errors import (
    error_to_response,
)
from cruxible_core.server.errors import (
    response_to_error as compat_response_to_error,
)


@pytest.mark.parametrize(
    ("error", "expected_type", "attrs"),
    [
        (
            ConfigError("bad config", errors=["missing relationship"]),
            client_errors.ConfigError,
            {"errors": ["missing relationship"]},
        ),
        (
            DataValidationError("bad data", errors=["wrong type"]),
            client_errors.DataValidationError,
            {"errors": ["wrong type"]},
        ),
        (
            ConstraintViolationError("constraint failed", violations=["mismatch"]),
            client_errors.ConstraintViolationError,
            {"violations": ["mismatch"]},
        ),
        (
            PermissionDeniedError("cruxible_query", "READ_ONLY", "ADMIN"),
            client_errors.PermissionDeniedError,
            {
                "tool_name": "cruxible_query",
                "current_mode": "READ_ONLY",
                "required_mode": "ADMIN",
            },
        ),
        (
            EntityTypeNotFoundError("Vehicle", known_entity_types=["Part", "Vehicle"]),
            client_errors.EntityTypeNotFoundError,
            {"entity_type": "Vehicle", "known_entity_types": ["Part", "Vehicle"]},
        ),
        (
            RelationshipNotFoundError("fits"),
            client_errors.RelationshipNotFoundError,
            {"relationship_name": "fits"},
        ),
        (
            QueryNotFoundError("parts_for_vehicle"),
            client_errors.QueryNotFoundError,
            {"query_name": "parts_for_vehicle"},
        ),
        (
            EntityNotFoundError("Vehicle", "V-1"),
            client_errors.EntityNotFoundError,
            {"entity_type": "Vehicle", "entity_id": "V-1"},
        ),
        (
            RelationshipAmbiguityError("Part", "P-1", "Vehicle", "V-1", "fits"),
            client_errors.RelationshipAmbiguityError,
            {
                "from_type": "Part",
                "from_id": "P-1",
                "to_type": "Vehicle",
                "to_id": "V-1",
                "relationship_type": "fits",
            },
        ),
        (
            ReceiptNotFoundError("RCPT-1"),
            client_errors.ReceiptNotFoundError,
            {"receipt_id": "RCPT-1"},
        ),
        (
            OutcomeNotFoundError("RCPT-2"),
            client_errors.OutcomeNotFoundError,
            {"receipt_id": "RCPT-2"},
        ),
        (
            InstanceNotFoundError("inst_123"),
            client_errors.InstanceNotFoundError,
            {"instance_id": "inst_123"},
        ),
        (GroupNotFoundError("GRP-1"), client_errors.GroupNotFoundError, {"group_id": "GRP-1"}),
        (QueryExecutionError("query failed"), client_errors.QueryExecutionError, {}),
        (IngestionError("ingest failed"), client_errors.IngestionError, {}),
        (MutationError("mutation failed"), client_errors.MutationError, {}),
    ],
)
def test_error_round_trip_preserves_subclass_and_context(
    error: CoreError,
    expected_type: type[client_errors.CoreError],
    attrs: dict[str, object],
):
    error.mutation_receipt_id = "RCPT-xyz"

    status, body = error_to_response(error)
    restored = client_errors.response_to_error(status, body)

    assert type(restored) is expected_type
    assert restored.mutation_receipt_id == "RCPT-xyz"
    for key, value in attrs.items():
        assert getattr(restored, key) == value


def test_request_validation_envelope_decodes_with_field_errors():
    restored = client_errors.response_to_error(
        422,
        client_errors.ErrorResponse(
            error_type="RequestValidationError",
            message="Request validation failed",
            errors=["query.offset: Input should be a valid integer"],
        ),
    )

    assert type(restored) is client_errors.DataValidationError
    assert restored.errors == ["query.offset: Input should be a valid integer"]
    assert "query.offset" in str(restored)


def test_unknown_error_type_falls_back_to_core_error():
    restored = client_errors.response_to_error(
        500,
        client_errors.ErrorResponse(
            error_type="UnknownCustomError",
            message="boom",
            context={"extra": "ignored"},
        ),
    )

    assert type(restored) is client_errors.CoreError
    assert str(restored) == "boom"


def test_server_errors_compat_decoder_re_exports_client_decoder():
    restored = compat_response_to_error(
        404,
        client_errors.ErrorResponse(
            error_type="InstanceNotFoundError",
            message="ignored",
            context={"instance_id": "inst_123"},
        ),
    )

    assert type(restored) is client_errors.InstanceNotFoundError
    assert restored.instance_id == "inst_123"
