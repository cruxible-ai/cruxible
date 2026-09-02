"""Round-4: attack T-3/T-4/T-5/T-6 -- operator construction, startup, re-arm, reason."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_core.playbill.provider_process_leases import (
    ProviderLocalRuntimeRefused,
)
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator


def _server_state(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    from cruxible_core.runtime.permissions import reset_permissions
    from cruxible_core.server.registry import get_registry, reset_registry

    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(root))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    reset_permissions()
    reset_registry()
    get_playbill_manager().clear()
    get_registry()
    return root


# ------------------------------------------------------------------ U-2 / U-3


def test_a_file_at_daemon_provider_secrets_degrades_operator_construction(
    short_root: Path,
) -> None:
    """T-3: a Provider-lane-only filesystem shape must degrade, not raise."""

    secrets = short_root / "daemon" / "provider-secrets"
    secrets.parent.mkdir(parents=True)
    secrets.write_text("occupied", encoding="utf-8")
    operator = ProviderRuntimeOperator(short_root)
    assert operator.unavailable_code == "provider_process_lease_invalid"
    assert "secret store" in (operator.unavailable_reason or "")


def test_that_degradation_does_not_kill_create_app(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The create_app last resort re-enters the same failing constructor."""

    secrets = short_root / "daemon" / "provider-secrets"
    secrets.parent.mkdir(parents=True)
    secrets.write_text("occupied", encoding="utf-8")
    _server_state(monkeypatch, short_root)
    from cruxible_core.server.app import create_app

    assert create_app() is not None
    manager = get_playbill_manager()
    first = manager.provider_runtime_operator()
    second = manager.provider_runtime_operator()
    assert first is second
    assert first.unavailable_code == "provider_process_lease_invalid"


def test_a_read_only_state_root_degrades_operator_construction(short_root: Path) -> None:
    """The lease store's own mkdir is outside its typed guard."""

    root = short_root / "ro"
    root.mkdir()
    os.chmod(root, 0o500)
    try:
        operator = ProviderRuntimeOperator(root)
        assert operator.unavailable_code == "provider_process_lease_invalid"
        assert operator.lane_status()[0] == "unavailable"
    finally:
        os.chmod(root, 0o700)


def test_lane_status_uses_the_cached_snapshot_without_filesystem_access(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = ProviderRuntimeOperator(short_root)
    operator.mark_unavailable("provider_process_lease_invalid", "offline")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("lane_status touched the filesystem")

    monkeypatch.setattr(Path, "stat", forbidden)
    assert operator.lane_status() == (
        "unavailable",
        "provider_process_lease_invalid",
        "provider_process_lease_invalid: offline",
    )


# ------------------------------------------------------------------ U-4 re-arm


def _degraded_operator(short_root: Path) -> ProviderRuntimeOperator:
    operator = ProviderRuntimeOperator(short_root)
    assert operator.process_leases is not None
    operator.mark_unavailable("provider_process_group_survived_recovery", "stuck", retryable=True)
    return operator


def test_the_lazy_rearm_never_completes_the_durable_start_it_unblocks(
    short_root: Path,
) -> None:
    """T-5 residue: a re-armed lane deletes the record without folding the journal."""

    operator = ProviderRuntimeOperator(short_root)
    store = operator.process_leases
    assert store is not None
    invocation = "sha256:" + "a" * 64
    record_path, _control = store.paths(invocation)
    record_path.write_bytes(
        canonical_bytes(
            {
                "invocation_id": invocation,
                "pid": 99_999_991,
                "process_group_id": 99_999_991,
                "session_id": None,
                "boot_id": None,
                "process_start_time": None,
            }
        )
    )
    operator.mark_unavailable(
        "provider_process_group_survived_recovery", "previously stuck", retryable=True
    )
    assert operator.unavailable_reason is not None

    # A lazy re-arm on the invocation path.
    operator._begin_invocation()
    operator._end_invocation()

    assert operator.unavailable_reason is None  # lane is back
    assert not record_path.exists()  # the record is GONE
    # ... and nothing ever folded `invocation` into the journal: the operator
    # discards the ProviderProcessRecoveryResultV1 the re-arm produced.
    import inspect

    assert "completion_invocation_ids" not in inspect.getsource(
        ProviderRuntimeOperator._lazy_rearm_locked
    )
    # ... and a later daemon restart can no longer find it either: the record the
    # startup fold needs is gone, so the run stays `incomplete Provider invocation`
    # with no path back to runnable.
    restart = ProviderRuntimeOperator(short_root)
    assert restart.process_leases is not None
    later = restart.process_leases.recover_all()
    assert later.completion_invocation_ids == ()
    assert invocation not in later.completion_invocation_ids


def test_two_concurrent_first_invocations_rearm_exactly_once(short_root: Path) -> None:
    operator = _degraded_operator(short_root)
    calls: list[float] = []
    original = operator.process_leases.recover_all  # type: ignore[union-attr]

    def counted():  # type: ignore[no-untyped-def]
        calls.append(time.monotonic())
        time.sleep(0.05)
        return original()

    operator.process_leases.recover_all = counted  # type: ignore[union-attr,assignment]
    errors: list[BaseException] = []

    def run() -> None:
        try:
            operator._begin_invocation()
            time.sleep(0.02)
            operator._end_invocation()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(calls) == 1
    assert operator._in_flight == 0


def test_in_flight_is_decremented_on_every_delegate_exception(short_root: Path) -> None:
    from cruxible_core.runtime.provider_runtime import _OperatorBoundProviderRuntimeInvoker

    operator = ProviderRuntimeOperator(short_root)

    class Boom:
        def invoke_provider(self, **_kwargs: object) -> object:
            raise RuntimeError("boom")

        def bind_provider(self, *, occurrence: object) -> object:
            raise RuntimeError("boom")

    bound = _OperatorBoundProviderRuntimeInvoker(operator, Boom())
    with pytest.raises(RuntimeError):
        bound.invoke_provider()
    assert operator._in_flight == 0
    # a refused begin never increments
    operator.mark_unavailable("provider_runtime_recovery_failed", "down")
    with pytest.raises(ProviderLocalRuntimeRefused):
        bound.invoke_provider()
    assert operator._in_flight == 0


def test_a_non_retryable_degradation_never_rearms(short_root: Path) -> None:
    """Config/containment failures need a restart; confirm that is deliberate."""

    config = short_root / "daemon" / "provider-runtime.json"
    config.parent.mkdir(parents=True)
    config.write_text("{not json", encoding="utf-8")
    operator = ProviderRuntimeOperator(short_root)
    assert operator.unavailable_reason is not None
    assert operator._rearm_required is False
    config.write_text(json.dumps({"tag": "cruxible-provider-runtime-operational-config-v1"}))
    with pytest.raises(ProviderLocalRuntimeRefused):
        operator._begin_invocation()
    assert operator.unavailable_reason is not None


# ------------------------------------------------------------------ U-5 reason


def test_the_degraded_reason_reaches_the_server_info_result(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _server_state(monkeypatch, short_root)
    from cruxible_core.runtime import host_api

    manager = get_playbill_manager()
    operator = manager.provider_runtime_operator()
    operator.mark_unavailable("provider_process_lease_invalid", "path too long")
    result = host_api.server_info()
    assert result.provider_lane.state == "unavailable"
    assert result.provider_lane.code == "provider_process_lease_invalid"
    assert "path too long" in (result.provider_lane.detail or "")


def test_the_degraded_reason_reaches_a_node_refusal_projection() -> None:
    from cruxible_core.playbill.procedures.execution import _RunRefusal

    refusal = _RunRefusal(
        "provider_unavailable",
        "down",
        node_id="n",
        details={"reason": {"code": "provider_runtime_recovery_failed", "detail": "why"}},
    )
    assert refusal.refusal.details == {
        "reason": {"code": "provider_runtime_recovery_failed", "detail": "why"}
    }


def test_lane_status_blocks_while_a_rearm_recovery_runs(short_root: Path) -> None:
    """Liveness: /server/info and next share the operator lock with recovery."""

    operator = _degraded_operator(short_root)
    original = operator.process_leases.recover_all  # type: ignore[union-attr]
    gate = threading.Event()

    def slow():  # type: ignore[no-untyped-def]
        gate.set()
        time.sleep(0.6)
        return original()

    operator.process_leases.recover_all = slow  # type: ignore[union-attr,assignment]
    thread = threading.Thread(target=operator._begin_invocation)
    thread.start()
    gate.wait(2.0)
    started = time.monotonic()
    operator.lane_status()
    blocked = time.monotonic() - started
    thread.join()
    operator._end_invocation()
    assert blocked > 0.3, blocked
