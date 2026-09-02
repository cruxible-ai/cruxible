"""Round 3 - K-8, C-2, F-5 single-source, and the R-6 exit-condition shape."""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from typing import get_args

import cruxible_core.playbill.provider_local_runtime as runtime_module
import cruxible_core.playbill.provider_process_leases as lease_module
from cruxible_core.playbill.procedures.execution import (
    ProcedureRunAdmissionV3,
    ProcedureRunAdmissionV5,
)
from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator

WORKTREE = Path("/Users/robertmalone/Git/p2-worktrees/p2b2")


def test_k8_operational_config_reaches_the_daemon_lease_store(short_root: Path) -> None:
    config = short_root / "daemon" / "provider-runtime.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "tag": "cruxible-provider-runtime-operational-config-v1",
                "lease_acquisition_timeout_seconds": 1.5,
                "lease_recovery_timeout_seconds": 2.25,
                "secret_writer_join_timeout_seconds": 3.25,
                "stdin_writer_join_timeout_seconds": 4.25,
                "descendant_tracker_join_timeout_seconds": 5.25,
                "deployments": [],
            }
        ),
        encoding="utf-8",
    )
    operator = ProviderRuntimeOperator(short_root)
    assert operator.process_leases is not None
    assert operator.process_leases.acquisition_timeout_seconds == 1.5
    assert operator.process_leases.recovery_timeout_seconds == 2.25
    assert operator.process_leases.secret_writer_join_timeout_seconds == 3.25
    assert operator.process_leases.stdin_writer_join_timeout_seconds == 4.25
    assert operator.process_leases.descendant_tracker_join_timeout_seconds == 5.25
    assert operator.process_leases.root.is_relative_to(short_root.resolve())
    assert operator.process_leases.control_root == short_root.resolve() / "c"
    assert operator.secret_store.root.is_relative_to(short_root.resolve())


def test_c2_a_line_admission_is_the_only_effect_intent_origin() -> None:
    assert get_args(ProcedureRunAdmissionV3.model_fields["invocation_origin"].annotation) == (
        "line",
    )
    assert issubclass(ProcedureRunAdmissionV5, ProcedureRunAdmissionV3)


def test_f5_no_bare_timeout_literal_remains_on_the_fence_path() -> None:
    store_signature = inspect.signature(lease_module.ProviderProcessLeaseStore.__init__).parameters
    assert (
        store_signature["acquisition_timeout_seconds"].default
        is lease_module.DEFAULT_PROVIDER_LEASE_ACQUISITION_TIMEOUT_SECONDS
    )
    assert (
        store_signature["recovery_timeout_seconds"].default
        is lease_module.DEFAULT_PROVIDER_LEASE_RECOVERY_TIMEOUT_SECONDS
    )
    assert (
        store_signature["secret_writer_join_timeout_seconds"].default
        is lease_module.DEFAULT_PROVIDER_SECRET_WRITER_JOIN_TIMEOUT_SECONDS
    )
    assert (
        store_signature["stdin_writer_join_timeout_seconds"].default
        is lease_module.DEFAULT_PROVIDER_STDIN_WRITER_JOIN_TIMEOUT_SECONDS
    )
    assert (
        store_signature["descendant_tracker_join_timeout_seconds"].default
        is lease_module.DEFAULT_PROVIDER_DESCENDANT_TRACKER_JOIN_TIMEOUT_SECONDS
    )
    echo = inspect.getsource(lease_module.ProviderProcessLeaseStore.require_echo)
    assert "settimeout(self.acquisition_timeout_seconds)" in echo
    channel = inspect.getsource(runtime_module._open_secret_channel)
    assert "writer.join(timeout=join_timeout_seconds)" in channel
    collect = inspect.getsource(runtime_module._collect_child_output)
    assert "writer.join(timeout=writer_join_timeout_seconds)" in collect
    child = inspect.getsource(runtime_module._run_child)
    assert "command = [" in child
    assert child.count("command = [") == 1, "the dead first command assignment is gone"


def test_r6_regression_shape() -> None:
    whole = (WORKTREE / "tests/test_server/test_provider_runtime_operator.py").read_text(
        encoding="utf-8"
    )
    start = whole.index("def test_daemon_operator_rebinds_and_runs_a_real_local_subprocess(")
    end = whole.index("def test_malformed_runtime_config_degrades_only_the_provider_lane")
    text = whole[start:end]
    # Real Line closure through the service entry point.
    assert "service_prepare_playbill_line_admission(" in text
    assert "_complete_line_for_admission(" in text
    assert "evaluate_line_spec_law(" in whole
    assert 'assert law.verdict == "accepted", law.diagnostics' in whole
    # Executed through the executor with the daemon-owned operator's invoker.
    assert "service_execute_direct_procedure(" in text
    assert "invoker = operator.invoker_for(" in text
    assert "provider_runtime_invoker=invoker," in text
    # Real subprocess (the fake interpreter is an executable python shim).
    assert "_fake_interpreter" in text
    # Only the classifier registry is patched; direct preparation is not.
    patches = set(re.findall(r'monkeypatch\.setattr\(\s*([\w_.]+),\s*\n?\s*"([\w_]+)"', text))
    assert patches == {("execution_module", "PROVIDER_BUCKET_CLASSIFIER_REGISTRY")}, patches
    assert "prepare_direct_procedure_run" not in text


def test_the_control_socket_budget_leaves_79_bytes_for_a_state_root() -> None:
    """Operability: `<state_root>/c/<16 hex>.sock` costs 24 bytes, and the ceiling is
    103, so any state root longer than 79 bytes silently degrades the Provider lane."""

    store_source = inspect.getsource(lease_module.ProviderProcessLeaseStore.paths)
    assert "> 103" in store_source
    assert len("/c/" + "0" * 16 + ".sock") == 24
