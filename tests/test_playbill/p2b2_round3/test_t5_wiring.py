"""Round 3 - K-8, C-2, F-5 single-source, and the R-6 exit-condition shape."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import pytest

import cruxible_core.playbill.provider_local_runtime as runtime_module
import cruxible_core.playbill.provider_process_leases as lease_module
from cruxible_client.contracts.errors import PlaybillExecutionError
from cruxible_core.playbill.procedures.egress import compute_effective_rung
from cruxible_core.playbill.procedures.execution import (
    ProcedureExecutor,
    ProcedureRunAdmissionV3,
    ProcedureRunAdmissionV5,
)
from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator
from tests.test_playbill.test_procedure_execution import (
    _fixture,
    _prepare,
    _state_procedure,
    _StateReader,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_k8_operational_config_reaches_the_daemon_lease_store(short_root: Path) -> None:
    config = short_root / "daemon" / "provider-runtime.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "tag": "cruxible-provider-runtime-operational-config-v1",
                "lease_acquisition_timeout_seconds": 1.5,
                "lease_recovery_timeout_seconds": 2.25,
                "recovery_aggregate_timeout_seconds": 2.75,
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
    assert operator.process_leases.recovery_aggregate_timeout_seconds == 2.75
    assert operator.process_leases.secret_writer_join_timeout_seconds == 3.25
    assert operator.process_leases.stdin_writer_join_timeout_seconds == 4.25
    assert operator.process_leases.descendant_tracker_join_timeout_seconds == 5.25
    assert operator.process_leases.root.is_relative_to(short_root.resolve())
    assert operator.process_leases.control_root == short_root.resolve() / "c"
    assert operator.secret_store.root.is_relative_to(short_root.resolve())


def test_c2_a_line_admission_is_the_only_effect_intent_origin(tmp_path: Path) -> None:
    """An acquisition carrier may be direct; an EFFECT intent may not.

    The V3 carrier now admits an actor origin, because a direct run that reads
    an external source binds a real acquisition plan. What still binds a Line
    alone is the authority to cause an effect: an effective rung is checked
    against Line-only coordinates and refused outright on an actor invocation,
    so no direct run can reach a terminal.

    Driven, not read. A real direct admission out of `prepare_direct_procedure_run`
    and a real rung out of `compute_effective_rung` go through the executor's own
    guard three ways: the actor origin refuses, the same rung relabelled as a Line
    run passes, and one term changed refuses again -- so the test fails if the
    guard goes missing, and it does not pass on a guard that is merely present.
    """

    assert set(get_args(ProcedureRunAdmissionV3.model_fields["invocation_origin"].annotation)) == {
        "actor",
        "line",
    }
    assert issubclass(ProcedureRunAdmissionV5, ProcedureRunAdmissionV3)

    accepted = _state_procedure()
    root = tmp_path / "c2"
    root.mkdir()
    fixture = _fixture(root)
    admission = _prepare(accepted, fixture, _StateReader()).admission
    assert admission.invocation_origin == "actor"
    line_spec_digest = _c2_digest("line-spec")
    sensitivity = _c2_digest("sensitivity")
    mandate = _c2_digest("mandate-coordinate")
    calibration = _c2_digest("calibration-coordinate")
    rung = compute_effective_rung(
        procedure_terminal_capability=accepted.procedure.definition.terminal_capability,
        requested_terminal_rung=1,
        selector_privacies={},
        taint_labels=(),
        mandate_grants={},
        calibration_caps=(),
        evaluation_time=admission.admitted_at,
        procedure_definition_digest=admission.definition_digest,
        line_spec_digest=line_spec_digest,
        sensitivity_policy_digest=sensitivity,
        mandate_coordinate_digest=mandate,
        calibration_coordinate_digest=calibration,
        procedure_mandate_rung=1,
        caller_tier_rung=1,
    )
    executor = SimpleNamespace(effective_rung=rung)
    verify = ProcedureExecutor._verify_effective_rung  # noqa: SLF001

    with pytest.raises(PlaybillExecutionError) as refused:
        verify(executor, admission)
    assert "never a direct actor invocation" in str(refused.value)

    as_line = admission.model_copy(
        update={
            "invocation_origin": "line",
            "line_spec_digest": line_spec_digest,
            "sensitivity_policy_digest": sensitivity,
            "mandate_coordinate_digest": mandate,
            "calibration_coordinate_digest": calibration,
        }
    )
    verify(executor, as_line)

    with pytest.raises(PlaybillExecutionError) as mismatched:
        verify(
            executor,
            as_line.model_copy(update={"mandate_coordinate_digest": _c2_digest("another-mandate")}),
        )
    assert "another admission binding" in str(mismatched.value)


def _c2_digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


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
        store_signature["recovery_aggregate_timeout_seconds"].default
        is lease_module.DEFAULT_PROVIDER_RECOVERY_AGGREGATE_TIMEOUT_SECONDS
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
    whole = (REPOSITORY_ROOT / "tests/test_server/test_provider_runtime_operator.py").read_text(
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
    # Only the classifier registry is patched: no double may stand in for the
    # daemon-owned invoker, the direct preparation, or the served Line service.
    patches = set(re.findall(r'monkeypatch\.setattr\(\s*([\w_.]+),\s*\n?\s*"([\w_]+)"', text))
    assert patches == {("execution_module", "PROVIDER_BUCKET_CLASSIFIER_REGISTRY")}, patches
    assert "prepare_direct_procedure_run" not in text
    # The served Line route's own regression sits past that window; the same
    # anti-double law has to reach it or the one test proving the route is
    # un-doubled would be the one test nothing polices.
    served = whole[whole.index("def test_the_live_line_route_runs_a_real_daemon_owned_provider") :]
    served_patches = set(
        re.findall(r'monkeypatch\.setattr\(\s*([\w_.]+),\s*\n?\s*"([\w_]+)"', served)
    )
    assert served_patches == {("execution_module", "PROVIDER_BUCKET_CLASSIFIER_REGISTRY")}, (
        served_patches
    )


def test_the_control_socket_budget_selects_the_private_namespace_once() -> None:
    """Succeeds the retracted 79-byte state-root budget oracle (U8, 2026-09-02).

    The retracted law budgeted the state root against a fixed 79 bytes and
    refused above it. U8 replaced that with a per-user private namespace keyed
    by the state root and selected once at construction, so the name here says
    what the store now does instead of naming a budget the batch retracted.
    """

    store_source = inspect.getsource(lease_module.ProviderProcessLeaseStore.paths)
    selector = inspect.getsource(lease_module.ProviderProcessLeaseStore._select_control_root)
    assert "> 103" in store_source
    assert len("/c/" + "0" * 16 + ".sock") == 24
    assert "state_root_key" in selector
    assert '"XDG_RUNTIME_DIR"' in selector
    assert '"TMPDIR"' in selector
