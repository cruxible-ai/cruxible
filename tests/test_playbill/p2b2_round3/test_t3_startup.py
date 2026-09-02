"""Round 3 - attack daemon startup: create_app -> recover_provider_runtime -> degrade."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import cruxible_core.runtime.playbill_manager as manager_module
import cruxible_core.service.playbill_procedure_runs as run_service
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_core.playbill.provider_process_leases import (
    ProviderLocalRuntimeRefused,
    ProviderProcessLeaseStore,
    _current_boot_id,
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


def _write_record(store: ProviderProcessLeaseStore, invocation_id: str, **fields: object) -> Path:
    record_path, _control = store.paths(invocation_id)
    document = {
        "invocation_id": invocation_id,
        "pid": 99_999_991,
        "process_group_id": 99_999_991,
        "session_id": None,
        "boot_id": None,
        "process_start_time": None,
    }
    document.update(fields)
    record_path.write_bytes(canonical_bytes(document))
    return record_path


# ---------------------------------------------------------------- CONFIRM


def test_a_malformed_operational_config_degrades_only_the_lane(short_root: Path) -> None:
    """CONFIRM (N-1 startup clause)."""

    config = short_root / "daemon" / "provider-runtime.json"
    config.parent.mkdir(parents=True)
    config.write_text("{not json", encoding="utf-8")
    operator = ProviderRuntimeOperator(short_root)
    assert operator.unavailable_reason is not None
    assert "provider_process_lease_invalid" in operator.unavailable_reason
    invoker = operator.invoker_for(SimpleNamespace(), accepted_oid="a" * 40)  # type: ignore[arg-type]
    with pytest.raises(ProviderLocalRuntimeRefused) as caught:
        invoker.invoke_provider()
    assert caught.value.code == "provider_unavailable"
    assert caught.value.details == {
        "reason": {
            "code": operator.unavailable_code,
            "detail": operator.unavailable_reason,
        }
    }


def test_an_unreadable_lease_directory_degrades_only_the_lane(short_root: Path) -> None:
    """CONFIRM."""

    lease_root = short_root / "daemon" / "provider-process-leases"
    lease_root.mkdir(parents=True)
    os.chmod(lease_root, 0o000)
    try:
        operator = ProviderRuntimeOperator(short_root)
        assert operator.process_leases is None
        assert operator.unavailable_reason is not None
        result = operator.recover_all()
        assert result.could_not_clean[0].code == "provider_process_lease_invalid"
    finally:
        os.chmod(lease_root, 0o700)


def test_a_survivor_at_startup_no_longer_stops_create_app(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONFIRM (round-2 N-1 startup DoS is closed for the survivor shape)."""

    state = _server_state(monkeypatch, short_root / "s")
    lease_root = state.resolve() / "daemon" / "provider-process-leases"
    lease_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = ProviderProcessLeaseStore(
        lease_root, control_root=state.resolve() / "c", recovery_timeout_seconds=0.2
    )
    blocked = 99_999_996
    _write_record(
        store,
        "sha256:" + "b" * 64,
        pid=blocked,
        process_group_id=blocked,
        boot_id=_current_boot_id(),
        process_start_time="x",
    )
    from cruxible_core.server.app import create_app

    app = create_app()
    assert app is not None
    operator = get_playbill_manager().provider_runtime_operator()
    assert operator.unavailable_reason is None or "provider" in operator.unavailable_reason
    get_playbill_manager().clear()


# ---------------------------------------------------------------- T-J


def test_a_deployment_path_that_escapes_the_state_root_degrades_the_lane(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ProviderRuntimeOperator.__init__` builds `self.deployments` OUTSIDE the two
    guarded blocks, so a schema-valid config whose relative deployment path resolves
    out of the state root raises straight through `create_app`."""

    state = _server_state(monkeypatch, short_root / "s")
    state.mkdir(parents=True, exist_ok=True)
    escape = state / "link"
    escape.symlink_to(short_root)
    config = state / "daemon" / "provider-runtime.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps(
            {
                "tag": "cruxible-provider-runtime-operational-config-v1",
                "deployments": [
                    {
                        "tag": "cruxible-provider-deployment-config-v1",
                        "deployment_digest": "sha256:" + "0" * 64,
                        "distribution_path": "link/dist.whl",
                        "lock_path": "link/lock",
                        "environment_path": "link/env",
                        "environment_manifest_path": "link/seal.json",
                        "environment_pin_key": "k",
                        "interpreter_path": "link/python",
                        "provider_runtime_version": "1.0.0",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    operator = ProviderRuntimeOperator(state)
    assert operator.unavailable_code == "provider_process_lease_invalid"
    assert operator.unavailable_reason is not None

    from cruxible_core.server.app import create_app

    get_playbill_manager().clear()
    assert create_app() is not None
    get_playbill_manager().clear()


def test_a_non_recovery_error_from_the_recovery_service_degrades_and_continues(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`recover_provider_runtime` catches only `ProcedureRunRecoveryRequired`; any
    other failure of `service_recover_provider_invocations` (journal conflict,
    OSError, ValidationError) escapes `create_app`."""

    state = _server_state(monkeypatch, short_root / "s")
    lease_root = state.resolve() / "daemon" / "provider-process-leases"
    lease_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = ProviderProcessLeaseStore(lease_root, control_root=state.resolve() / "c")
    _write_record(store, "sha256:" + "c" * 64)

    record = SimpleNamespace(
        backend=manager_module.GOVERNED_DAEMON_BACKEND, instance_id="inst_probe"
    )
    monkeypatch.setattr(
        manager_module,
        "get_registry",
        lambda: SimpleNamespace(list_instances=lambda: [record]),
    )
    monkeypatch.setattr(
        manager_module.PlaybillInstanceManager,
        "get",
        lambda self, instance_id: SimpleNamespace(),
    )

    def explode(*_args: object, **_kwargs: object) -> None:
        raise OSError(5, "journal device is gone")

    monkeypatch.setattr(run_service, "service_recover_provider_invocations", explode)

    from cruxible_core.server.app import create_app

    get_playbill_manager().clear()
    assert create_app() is not None
    operator = get_playbill_manager().provider_runtime_operator()
    assert operator.unavailable_code == "provider_runtime_recovery_failed"
    assert operator.unavailable_reason is not None
    get_playbill_manager().clear()


def test_an_unmatched_recovered_start_degrades_and_continues(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONFIRM (the ruled clause): `ProcedureRunRecoveryRequired` degrades the lane
    and the sweep continues over later instances."""

    state = _server_state(monkeypatch, short_root / "s")
    lease_root = state.resolve() / "daemon" / "provider-process-leases"
    lease_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = ProviderProcessLeaseStore(lease_root, control_root=state.resolve() / "c")
    _write_record(store, "sha256:" + "d" * 64)

    seen: list[str] = []
    records = [
        SimpleNamespace(backend=manager_module.GOVERNED_DAEMON_BACKEND, instance_id="a"),
        SimpleNamespace(backend=manager_module.GOVERNED_DAEMON_BACKEND, instance_id="b"),
    ]
    monkeypatch.setattr(
        manager_module,
        "get_registry",
        lambda: SimpleNamespace(list_instances=lambda: records),
    )
    monkeypatch.setattr(
        manager_module.PlaybillInstanceManager,
        "get",
        lambda self, instance_id: SimpleNamespace(instance_id=instance_id),
    )

    def refuse(instance, **_kwargs: object) -> None:
        seen.append(instance.instance_id)
        raise run_service.ProcedureRunRecoveryRequired("no admitted occurrence")

    monkeypatch.setattr(run_service, "service_recover_provider_invocations", refuse)

    from cruxible_core.server.app import create_app

    get_playbill_manager().clear()
    app = create_app()
    assert app is not None
    assert seen == ["a", "b"]
    operator = get_playbill_manager().provider_runtime_operator()
    assert operator.unavailable_reason is not None
    get_playbill_manager().clear()


def test_startup_recovery_reads_the_boot_id_once_per_fold(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cost: `recover_all` recomputes the boot id and the process start time per
    record, each an OS subprocess on the platform ps path."""

    import cruxible_core.playbill.provider_process_leases as lease_module

    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    boot = _current_boot_id()
    for index in range(200):
        _write_record(
            store,
            "sha256:" + f"{index:064x}",
            pid=99_000_000 + index,
            process_group_id=99_000_000 + index,
            boot_id=boot,
            process_start_time="x",
        )
    calls: list[object] = []
    real_run = lease_module.subprocess.run

    def counted(args, *rest, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(args)
        return real_run(args, *rest, **kwargs)

    monkeypatch.setattr(lease_module.subprocess, "run", counted)
    started = time.monotonic()
    result = store.recover_all()
    elapsed = time.monotonic() - started
    assert len(result.removed) == 200
    print(f"OBSERVED: {len(calls)} subprocesses / {elapsed:.2f}s for 200 lease records")
    boot_calls = [item for item in calls if isinstance(item, list) and "sysctl" in item]
    assert len(boot_calls) <= 1
