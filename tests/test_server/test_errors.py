"""Tests for HTTP error serialization."""

from __future__ import annotations

import pytest

from cruxible_client import errors as client_errors
from cruxible_core.errors import (
    CitationHandleResolutionError,
    ConfigError,
    ConstraintViolationError,
    CoreError,
    CustomerCodeExecutionUnsupportedError,
    DataValidationError,
    EntityNotFoundError,
    EntityTypeNotFoundError,
    GroupNotFoundError,
    IngestionError,
    InstanceNotFoundError,
    MutationError,
    OutcomeNotFoundError,
    PendingEdgeWriteRefusedError,
    PermissionDeniedError,
    ProcedureNotFoundError,
    ProcedureWithdrawalRefusedError,
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
            CitationHandleResolutionError(
                "cite1_stale",
                "stale",
                detail="the token targets superseded revision 'source@1'",
            ),
            client_errors.CitationHandleResolutionError,
            {
                "handle": "cite1_stale",
                "failure_kind": "stale",
                "detail": "the token targets superseded revision 'source@1'",
            },
        ),
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
            PermissionDeniedError(
                "cruxible_apply_workflow",
                "GOVERNED_WRITE",
                "GRAPH_WRITE",
                ceiling_mode="GOVERNED_WRITE",
            ),
            client_errors.PermissionDeniedError,
            {
                "tool_name": "cruxible_apply_workflow",
                "current_mode": "GOVERNED_WRITE",
                "required_mode": "GRAPH_WRITE",
                "ceiling_mode": "GOVERNED_WRITE",
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
        (
            ProcedureNotFoundError("PRC-1"),
            client_errors.ProcedureNotFoundError,
            {"procedure_id": "PRC-1"},
        ),
        (QueryExecutionError("query failed"), client_errors.QueryExecutionError, {}),
        (
            CustomerCodeExecutionUnsupportedError(),
            client_errors.CustomerCodeExecutionUnsupportedError,
            {},
        ),
        (IngestionError("ingest failed"), client_errors.IngestionError, {}),
        (MutationError("mutation failed"), client_errors.MutationError, {}),
        (
            # wi-pending-edge-clobber: the refusal a client sees must survive
            # the wire with its edge coordinates AND its two-exit message —
            # that message is the whole remediation, so a lossy round-trip
            # would strand the caller.
            PendingEdgeWriteRefusedError("fits", "Part", "BP-1", "Vehicle", "V-1"),
            client_errors.PendingEdgeWriteRefusedError,
            {
                "relationship_type": "fits",
                "from_type": "Part",
                "from_id": "BP-1",
                "to_type": "Vehicle",
                "to_id": "V-1",
            },
        ),
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
    if isinstance(error, ProcedureNotFoundError):
        assert status == 404
    if isinstance(error, CustomerCodeExecutionUnsupportedError):
        assert status == 403
        assert body.error_code == "customer_code_execution_unsupported"
    if isinstance(error, CitationHandleResolutionError):
        assert status == 409
        assert body.error_code == "citation_handle_resolution_failed"
    for key, value in attrs.items():
        assert getattr(restored, key) == value


def test_procedure_withdrawal_refusal_is_a_403_naming_the_author_rule() -> None:
    """The refusal must cross the wire with the RULE, not just a tier number.

    "You are neither the author nor a reviewer" is the whole remediation, so a
    round trip that kept only ``required_mode`` would strand the caller.
    """
    error = ProcedureWithdrawalRefusedError(
        "PRC-1",
        current_mode="GOVERNED_WRITE",
        required_mode="GRAPH_WRITE",
        message=(
            "procedure 'PRC-1' may be withdrawn only by its proposing author "
            "(actor 'author' in org 'org-1') at their own tier, or by a reviewer "
            "holding GRAPH_WRITE; actor 'bystander' in org 'org-1' is neither "
            "(current mode GOVERNED_WRITE)"
        ),
    )
    status, body = error_to_response(error)

    assert status == 403
    assert body.error_code == "procedure_withdrawal_refused"
    assert body.context == {
        "procedure_id": "PRC-1",
        "current_mode": "GOVERNED_WRITE",
        "required_mode": "GRAPH_WRITE",
    }

    restored = client_errors.response_to_error(status, body)
    assert type(restored) is client_errors.ProcedureWithdrawalRefusedError
    assert str(restored) == str(error)
    assert restored.procedure_id == "PRC-1"
    assert restored.required_mode == "GRAPH_WRITE"


def test_pending_edge_refusal_is_a_409_conflict_with_both_exits_intact() -> None:
    """The refusal is a STATE conflict (409), not a tier/policy 403, and its
    message is the remediation: withdraw/re-propose, or resolve first."""
    error = PendingEdgeWriteRefusedError("fits", "Part", "BP-1", "Vehicle", "V-1")
    status, body = error_to_response(error)

    assert status == 409
    assert body.error_code == "pending_edge_write_refused"

    restored = client_errors.response_to_error(status, body)
    assert isinstance(restored, client_errors.PendingEdgeWriteRefusedError)
    assert "pending=true" in str(restored)
    assert "feedback approve/reject, or group resolve" in str(restored)


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


def test_reconstructed_client_error_shares_core_error_identity() -> None:
    restored = client_errors.response_to_error(
        404,
        client_errors.ErrorResponse(
            error_type="QueryNotFoundError",
            message="ignored",
            context={"query_name": "missing_query"},
        ),
    )

    assert isinstance(restored, CoreError)


def test_capability_ceiling_denial_round_trip_preserves_loud_message() -> None:
    error = PermissionDeniedError(
        "cruxible_apply_workflow",
        "GOVERNED_WRITE",
        "GRAPH_WRITE",
        ceiling_mode="GOVERNED_WRITE",
    )

    status, body = error_to_response(error)
    restored = client_errors.response_to_error(status, body)

    assert status == 403
    assert type(restored) is client_errors.PermissionDeniedError
    assert str(restored) == str(error)
    assert "cruxible_apply_workflow" in str(restored)
    assert "GRAPH_WRITE" in str(restored)
    assert "capability ceiling is GOVERNED_WRITE" in str(restored)


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
