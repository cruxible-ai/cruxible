"""Public audit surfaces stay coherent with centralized calibration."""

from __future__ import annotations

from inspect import signature

import click

from cruxible_client import CruxibleClient
from cruxible_client.authoring.sdk import Playbill
from cruxible_core.cli.main import cli
from cruxible_core.playbill.audit import AuditBudgetV1
from cruxible_core.playbill.curation_calibration import (
    AUDIT_BUDGET_DEFAULT_MAX_BYTES,
    AUDIT_BUDGET_DEFAULT_MAX_ROWS,
    AUDIT_BUDGET_MAX_MAX_BYTES,
    AUDIT_BUDGET_MAX_MAX_ROWS,
    AUDIT_BUDGET_MIN_MAX_BYTES,
    AUDIT_BUDGET_MIN_MAX_ROWS,
)
from cruxible_core.server.playbill_request_models import PlaybillAuditRequest


def _audit_cli_option(name: str) -> click.Option:
    playbill = cli.commands["playbill"]
    assert isinstance(playbill, click.Group)
    audit = playbill.commands["audit"]
    return next(
        parameter
        for parameter in audit.params
        if isinstance(parameter, click.Option) and parameter.name == name
    )


def test_audit_budget_calibration_is_coherent_across_public_surfaces() -> None:
    core = AuditBudgetV1()
    server = PlaybillAuditRequest(
        evaluation_time="2026-08-27T00:00:00Z",
        access_profile={"profile_id": "test"},
    )
    transport_parameters = signature(CruxibleClient.audit_playbill).parameters
    sdk_parameters = signature(Playbill.audit).parameters
    rows_option = _audit_cli_option("max_rows")
    bytes_option = _audit_cli_option("max_bytes")

    assert core.max_rows == server.budget["max_rows"] == AUDIT_BUDGET_DEFAULT_MAX_ROWS
    assert core.max_bytes == server.budget["max_bytes"] == AUDIT_BUDGET_DEFAULT_MAX_BYTES
    assert transport_parameters["max_rows"].default == AUDIT_BUDGET_DEFAULT_MAX_ROWS
    assert transport_parameters["max_bytes"].default == AUDIT_BUDGET_DEFAULT_MAX_BYTES
    assert sdk_parameters["max_rows"].default == AUDIT_BUDGET_DEFAULT_MAX_ROWS
    assert sdk_parameters["max_bytes"].default == AUDIT_BUDGET_DEFAULT_MAX_BYTES
    assert rows_option.default == AUDIT_BUDGET_DEFAULT_MAX_ROWS
    assert bytes_option.default == AUDIT_BUDGET_DEFAULT_MAX_BYTES
    assert isinstance(rows_option.type, click.IntRange)
    assert (rows_option.type.min, rows_option.type.max) == (
        AUDIT_BUDGET_MIN_MAX_ROWS,
        AUDIT_BUDGET_MAX_MAX_ROWS,
    )
    assert isinstance(bytes_option.type, click.IntRange)
    assert (bytes_option.type.min, bytes_option.type.max) == (
        AUDIT_BUDGET_MIN_MAX_BYTES,
        AUDIT_BUDGET_MAX_MAX_BYTES,
    )
