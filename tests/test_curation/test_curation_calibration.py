"""Public audit surfaces stay coherent with centralized calibration."""

from __future__ import annotations

import subprocess
import sys
from inspect import signature
from textwrap import dedent

import click
from annotated_types import Ge, Le

from cruxible_client import CruxibleClient
from cruxible_client.authoring.sdk import Cruxible
from cruxible_client.contracts import AuditFactors
from cruxible_core.cli.main import cli
from cruxible_core.curation.audit import AuditBudgetV1
from cruxible_core.curation.curation_calibration import (
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
from cruxible_core.server.playbill_request_models import AuditRequest


def _audit_cli_option(name: str) -> click.Option:
    audit = cli.commands["audit"]
    return next(
        parameter
        for parameter in audit.params
        if isinstance(parameter, click.Option) and parameter.name == name
    )


def _ge_bound(field_name: str) -> int:
    return next(
        constraint.ge
        for constraint in AuditFactors.model_fields[field_name].metadata
        if isinstance(constraint, Ge)
    )


def _le_bound(field_name: str) -> int:
    return next(
        constraint.le
        for constraint in AuditFactors.model_fields[field_name].metadata
        if isinstance(constraint, Le)
    )


def test_audit_budget_calibration_is_coherent_across_public_surfaces() -> None:
    core = AuditBudgetV1()
    server = AuditRequest(
        evaluation_time="2026-08-27T00:00:00Z",
        access_profile={"profile_id": "test"},
    )
    transport_parameters = signature(CruxibleClient.audit).parameters
    sdk_parameters = signature(Cruxible.audit).parameters
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


def test_calibration_exports_survive_concurrent_import_during_reload() -> None:
    # A fresh interpreter isolates reload and tracing from pytest's imports.
    # Pause after the comprehension's iterator exists, then let an importer
    # attach metadata: iterating the live globals dict fails on its next step.
    code = dedent("""
        import dis
        import importlib
        import sys
        import threading
        from concurrent.futures import ThreadPoolExecutor

        name = 'cruxible_core.curation.curation_calibration'
        calibration = importlib.import_module(name)
        expected = set(calibration.__all__)
        iterating = threading.Event()
        mutated = threading.Event()

        def trace(frame, event, arg):
            if frame.f_code.co_filename != calibration.__file__:
                return None
            frame.f_trace_opcodes = True
            if (event == 'opcode' and not iterating.is_set()
                    and dis.opname[frame.f_code.co_code[frame.f_lasti]] == 'FOR_ITER'):
                iterating.set()
                assert mutated.wait(10), 'concurrent importer did not finish'
            return trace

        def reload_calibration():
            sys.settrace(trace)
            try:
                return importlib.reload(calibration)
            finally:
                sys.settrace(None)

        def import_calibration():
            assert iterating.wait(10), 'reload never reached export iteration'
            module = importlib.import_module(name)
            module._concurrent_import_observer = object()
            mutated.set()
            return module

        with ThreadPoolExecutor(max_workers=2) as workers:
            reloaded = workers.submit(reload_calibration)
            imported = workers.submit(import_calibration)
            assert reloaded.result(timeout=20) is imported.result(timeout=20)

        assert iterating.is_set() and mutated.is_set()
        assert set(calibration.__all__) == expected
        assert all(name.isupper() for name in calibration.__all__)
    """)
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
