"""Round-7 regressions for the required pre-execution process-table proof."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import cruxible_core.playbill.provider_local_runtime as runtime_module
import cruxible_core.service.playbill_procedure_runs as procedure_run_service
from cruxible_client.contracts.errors import PlaybillExecutionError
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.exhaust import parse_journal_payload
from cruxible_core.playbill.procedures.execution import ProcedureExecutor
from cruxible_core.playbill.provider_classifiers import ProviderBucketClassifierRegistry
from cruxible_core.playbill.provider_local_runtime import _run_child
from cruxible_core.playbill.provider_process_leases import ProviderLocalRuntimeRefused
from cruxible_core.playbill.provider_runtime_contract import ProviderRuntimeBudgetsV1
from cruxible_core.runtime import playbill_manager as manager_module
from cruxible_core.runtime.playbill_manager import PlaybillInstanceManager
from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator
from tests.test_playbill._p2b1_support import install_demo_classifier
from tests.test_playbill.test_provider_invocation_journal import (
    _accepted_one_provider,
    _Authority,
    _Contracts,
    _CrashingInvoker,
    _prepared_v5,
)
from tests.test_playbill.test_provider_local_driver import _fake_interpreter


def _invoke(
    operator: ProviderRuntimeOperator,
    interpreter: Path,
    invocation_id: str,
) -> object:
    store = operator.process_leases
    assert store is not None
    return _run_child(
        interpreter,
        entrypoint="demo:Provider",
        context=b'{"run_id":"RUN-r7","input":{"value":"ok"}}',
        budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=2, output_bytes=65_536),
        secret_fd=None,
        invocation_id=invocation_id,
        process_leases=store,
    )


def _unreadable_process_table(*_args: object, **_kwargs: object) -> tuple[object, ...]:
    raise ProviderLocalRuntimeRefused(
        "provider_process_lease_invalid",
        "the operating-system process table is unavailable",
    )


def test_an_unreadable_process_table_refuses_every_invocation_with_repair(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = ProviderRuntimeOperator(short_root)
    store = operator.process_leases
    assert store is not None
    store.acquisition_timeout_seconds = 0.5
    store.descendant_tracker_poll_interval_seconds = 0.01
    interpreter = _fake_interpreter(short_root / "provider.py")
    monkeypatch.setattr(runtime_module, "snapshot_provider_descendants", _unreadable_process_table)

    for digit in ("1", "2"):
        invocation_id = "sha256:" + digit * 64
        with pytest.raises(ProviderLocalRuntimeRefused) as refusal:
            _invoke(operator, interpreter, invocation_id)
        assert refusal.value.code == "provider_process_lease_invalid"
        assert "install ps" in str(refusal.value)
        assert "fix procfs permissions" in str(refusal.value)
        record_path, _control_path = store.paths(invocation_id)
        record = json.loads(record_path.read_bytes())
        assert record["invocation_id"] == invocation_id
        with pytest.raises(ProcessLookupError):
            os.kill(int(record["pid"]), 0)

    assert len(tuple(store.root.glob("*.json"))) == 2


def test_one_successful_process_table_read_keeps_a_transient_failure_diagnostic_only(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = ProviderRuntimeOperator(short_root)
    store = operator.process_leases
    assert store is not None
    store.acquisition_timeout_seconds = 1.0
    store.descendant_tracker_poll_interval_seconds = 30.0
    interpreter = _fake_interpreter(short_root / "provider.py")
    real_snapshot = runtime_module.snapshot_provider_descendants
    calls = 0

    def fail_once(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            return _unreadable_process_table(*args, **kwargs)
        return real_snapshot(*args, **kwargs)

    monkeypatch.setattr(runtime_module, "snapshot_provider_descendants", fail_once)
    invocation_id = "sha256:" + "3" * 64

    outcome = _invoke(operator, interpreter, invocation_id)

    assert json.loads(outcome.stdout)["status"] == "ok"  # type: ignore[attr-defined]
    assert calls >= 2
    assert store.diagnostics
    record_path, control_path = store.paths(invocation_id)
    assert not record_path.exists()
    assert not control_path.exists()
    assert operator.lane_status() == ("available", None, None)


def test_a_retained_pre_execution_refusal_folds_on_the_next_manager_recovery(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted = _accepted_one_provider()
    journal_root = short_root / "journal"
    journal_root.mkdir()
    prepared, fixture = _prepared_v5(accepted, journal_root)
    classifiers = ProviderBucketClassifierRegistry()
    install_demo_classifier(classifiers)
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=_CrashingInvoker(),
        provider_classifier_registry=classifiers,
    )
    with pytest.raises(PlaybillExecutionError, match="provider_completion_not_durable"):
        executor.execute(prepared, accepted)
    records = fixture.journal.all_records(
        prepared.admission.journal_stream,
        prepared.admission.journal_partition_id,
    )
    started_record = next(
        item for item in records if item.record.event_kind == "provider_invocation_started"
    )
    started = parse_journal_payload(
        fixture.bodies.read(
            started_record.record.payload_digest,
            access=BodyAccessContext(principal_id="test", can_read_body=True),
        )
    )
    invocation_id = str(started["invocation_id"])  # type: ignore[index]

    operator = ProviderRuntimeOperator(short_root / "runtime")
    store = operator.process_leases
    assert store is not None
    store.acquisition_timeout_seconds = 0.5
    store.descendant_tracker_poll_interval_seconds = 0.01
    real_snapshot = runtime_module.snapshot_provider_descendants
    monkeypatch.setattr(runtime_module, "snapshot_provider_descendants", _unreadable_process_table)
    with pytest.raises(ProviderLocalRuntimeRefused, match="install ps"):
        _invoke(operator, _fake_interpreter(short_root / "provider.py"), invocation_id)
    record_path, _control_path = store.paths(invocation_id)
    assert record_path.exists()
    monkeypatch.setattr(runtime_module, "snapshot_provider_descendants", real_snapshot)

    class _Instance:
        def body_store(self):  # type: ignore[no-untyped-def]
            return fixture.bodies

    manager = PlaybillInstanceManager()
    monkeypatch.setattr(manager, "provider_runtime_operator", lambda: operator)
    monkeypatch.setattr(manager, "get", lambda _instance_id: _Instance())
    monkeypatch.setattr(
        manager_module,
        "get_registry",
        lambda: SimpleNamespace(
            list_instances=lambda: (
                SimpleNamespace(backend="governed_daemon", instance_id="inst_r7"),
            )
        ),
    )
    monkeypatch.setattr(
        procedure_run_service,
        "_journal_for_write",
        lambda _instance: (fixture.journal, short_root),
    )
    monkeypatch.setattr(
        procedure_run_service,
        "_journal",
        lambda _instance: (fixture.journal, short_root),
    )
    monkeypatch.setattr(
        procedure_run_service,
        "_stream",
        lambda _instance: prepared.admission.journal_stream,
    )
    operator.bind_recovery_fold(lambda result: manager._fold_provider_recovery(operator, result))

    recovery = manager.recover_provider_runtime()

    assert recovery.completion_invocation_ids == (invocation_id,)
    assert not record_path.exists()
    folded = procedure_run_service.service_get_playbill_procedure_run(
        _Instance(),  # type: ignore[arg-type]
        run_id=prepared.admission.run_id,
    )
    assert folded.status == "operational_failed"
    assert folded.terminal is not None
    assert folded.terminal.code == "provider_completion_not_durable"
    assert folded.terminal.retryable is True  # type: ignore[union-attr]
    assert (
        folded.receipt is not None
        and folded.receipt.terminal is not None
        and folded.receipt.terminal.retryable is True  # type: ignore[union-attr]
    )
    assert (
        tuple(
            item.record.event_kind
            for item in fixture.journal.all_records(
                prepared.admission.journal_stream,
                prepared.admission.journal_partition_id,
            )
        ).count("provider_invocation_completed")
        == 1
    )
