"""Public audit surfaces stay coherent with centralized calibration."""

from __future__ import annotations

from inspect import signature

import click
from annotated_types import Ge, Le

from cruxible_client import CruxibleClient
from cruxible_client.authoring.sdk import Playbill
from cruxible_client.contracts import PlaybillAuditFactors
from cruxible_core.cli.main import cli
from cruxible_core.playbill.audit import AuditBudgetV1
from cruxible_core.playbill.curation_calibration import (
    AUDIT_BUDGET_DEFAULT_MAX_BYTES,
    AUDIT_BUDGET_DEFAULT_MAX_ROWS,
    AUDIT_BUDGET_MAX_MAX_BYTES,
    AUDIT_BUDGET_MAX_MAX_ROWS,
    AUDIT_BUDGET_MIN_MAX_BYTES,
    AUDIT_BUDGET_MIN_MAX_ROWS,
    AUDIT_STAKE_BASE,
    AUDIT_STALENESS_BASE,
    AUDIT_WEAKNESS_BASE,
    AUDIT_WEAKNESS_SIGNAL_COUNT,
    AUDIT_WEAKNESS_SIGNAL_WEIGHT,
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


def _ge_bound(field_name: str) -> int:
    return next(
        constraint.ge
        for constraint in PlaybillAuditFactors.model_fields[field_name].metadata
        if isinstance(constraint, Ge)
    )


def _le_bound(field_name: str) -> int:
    return next(
        constraint.le
        for constraint in PlaybillAuditFactors.model_fields[field_name].metadata
        if isinstance(constraint, Le)
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


def test_client_audit_factor_bounds_mirror_core_calibration() -> None:
    assert _ge_bound("stake") == AUDIT_STAKE_BASE
    assert _ge_bound("weakness") == AUDIT_WEAKNESS_BASE
    assert _le_bound("weakness") == (
        AUDIT_WEAKNESS_BASE + AUDIT_WEAKNESS_SIGNAL_COUNT * AUDIT_WEAKNESS_SIGNAL_WEIGHT
    )
    assert _ge_bound("staleness") == AUDIT_STALENESS_BASE
