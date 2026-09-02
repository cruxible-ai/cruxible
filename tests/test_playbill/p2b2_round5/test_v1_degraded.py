"""Round-5: attack U-2 -- degraded operator construction and its status surfaces."""

from __future__ import annotations

import builtins
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import cruxible_core.playbill.provider_local_runtime as runtime_module
import cruxible_core.playbill.provider_process_leases as lease_module
from cruxible_core.playbill.provider_process_leases import ProviderLocalRuntimeRefused
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator

STAGES = ("state_root", "config", "lease_dirs", "secret_store", "deployments")


def _server_state(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    from cruxible_core.runtime.permissions import reset_permissions
    from cruxible_core.server.credentials import reset_runtime_credential_store
    from cruxible_core.server.registry import get_registry, reset_registry

    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(root))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    reset_permissions()
    reset_runtime_credential_store()
    reset_registry()
    get_playbill_manager().clear()
    get_registry()


def _config_with_one_deployment(root: Path) -> None:
    (root / "daemon").mkdir(parents=True, exist_ok=True)
    (root / "daemon" / "provider-runtime.json").write_text(
        '{"tag":"cruxible-provider-runtime-operational-config-v1",'
        '"deployments":[{"tag":"cruxible-provider-deployment-config-v1",'
        '"deployment_digest":"sha256:' + "a" * 64 + '",'
        '"distribution_path":"d/dist.whl","lock_path":"d/lock",'
        '"environment_path":"d/env","environment_manifest_path":"d/seal.json",'
        '"environment_pin_key":"k","interpreter_path":"d/env/bin/python",'
        '"provider_runtime_version":"1.0.0"}]}',
        encoding="utf-8",
    )


# ------------------------------------------------------- every stage, OSError


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("kind", ["oserror", "refused"])
def test_every_construction_stage_degrades_and_never_raises(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, stage: str, kind: str
) -> None:
    """No filesystem stage may raise out of ProviderRuntimeOperator.__init__."""

    root = short_root / "s"
    root.mkdir()
    _config_with_one_deployment(root)
    boom: BaseException = (
        OSError(5, "planted")
        if kind == "oserror"
        else ProviderLocalRuntimeRefused("provider_process_lease_missing", "planted")
    )

    if stage == "state_root":
        real_mkdir = Path.mkdir

        def mkdir(self: Path, *args: object, **kwargs: object) -> None:
            if self == root.resolve():
                raise boom
            return real_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "mkdir", mkdir)
    elif stage == "config":
        monkeypatch.setattr(
            ProviderRuntimeOperator,
            "_load_config",
            lambda self: (_ for _ in ()).throw(boom),
        )
    elif stage == "lease_dirs":
        monkeypatch.setattr(
            lease_module.ProviderProcessLeaseStore,
            "_ensure_private_directory",
            staticmethod(lambda path: (_ for _ in ()).throw(boom)),
        )
    elif stage == "secret_store":
        real_init = runtime_module.FileProviderSecretStore.__init__

        def init(self: object, *args: object, **kwargs: object) -> None:
            raise boom

        monkeypatch.setattr(runtime_module.FileProviderSecretStore, "__init__", init)
        assert real_init is not None
    else:
        monkeypatch.setattr(
            ProviderRuntimeOperator,
            "_deployment",
            lambda self, item: (_ for _ in ()).throw(boom),
        )

    operator = ProviderRuntimeOperator(root)
    state, code, detail = operator.lane_status()
    assert state == "unavailable", (stage, kind)
    assert code in {
        "provider_process_lease_invalid",
        "provider_process_lease_missing",
    }
    assert detail is not None and "planted" in detail
    # And the refusing invoker is what the lane hands out.
    invoker = operator.invoker_for(SimpleNamespace(), accepted_oid="oid")  # type: ignore[arg-type]
    with pytest.raises(ProviderLocalRuntimeRefused) as excinfo:
        invoker.invoke_provider()
    assert excinfo.value.code == "provider_unavailable"


def test_lane_status_of_a_cached_degraded_operator_touches_no_filesystem(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero filesystem syscalls while reading lane status from a degraded operator."""

    _server_state(monkeypatch, short_root)
    secrets = short_root / "daemon" / "provider-secrets"
    secrets.parent.mkdir(parents=True, exist_ok=True)
    secrets.write_text("occupied", encoding="utf-8")
    manager = get_playbill_manager()
    operator = manager.provider_runtime_operator()
    assert operator.lane_status()[0] == "unavailable"

    touched: list[str] = []
    originals = {
        name: getattr(os, name) for name in ("stat", "lstat", "open", "listdir", "scandir")
    }
    builtin_open = builtins.open

    def make(name: str, real: object):  # type: ignore[no-untyped-def]
        def wrapper(*a: object, **k: object):  # type: ignore[no-untyped-def]
            touched.append(name)
            return real(*a, **k)  # type: ignore[operator]

        return wrapper

    for name, real in originals.items():
        setattr(os, name, make(name, real))
    builtins.open = make("builtins.open", builtin_open)  # type: ignore[assignment]
    try:
        statuses = [operator.lane_status(), operator.lane_status()]
    finally:
        for name, real in originals.items():
            setattr(os, name, real)
        builtins.open = builtin_open  # type: ignore[assignment]
    assert touched == []
    assert all(item[0] == "unavailable" for item in statuses)
    assert manager.cached_provider_runtime_operator() is operator


def test_server_info_and_playbill_next_never_500_on_a_degraded_lane(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = short_root / "daemon" / "provider-secrets"
    secrets.parent.mkdir(parents=True, exist_ok=True)
    secrets.write_text("occupied", encoding="utf-8")
    _server_state(monkeypatch, short_root)
    from cruxible_core.runtime import host_api

    info = host_api.server_info()
    assert info.provider_lane.state == "unavailable"
    assert info.provider_lane.code == "provider_process_lease_invalid"
    assert info.provider_lane.detail is not None


def test_a_degraded_operator_rearms_after_repairing_the_filesystem(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V-3: the cached operator repairs its failed construction stage."""

    _server_state(monkeypatch, short_root)
    secrets = short_root / "daemon" / "provider-secrets"
    secrets.parent.mkdir(parents=True, exist_ok=True)
    secrets.write_text("occupied", encoding="utf-8")
    manager = get_playbill_manager()
    operator = manager.provider_runtime_operator()
    assert operator.secret_store is None
    assert operator.lane_status()[0] == "unavailable"
    # Operator repairs the host exactly as the docs' typed cause describes.
    secrets.unlink()
    operator._begin_invocation()
    operator._end_invocation()
    assert operator.lane_status() == ("available", None, None)
    assert operator.secret_store is not None
    assert manager.provider_runtime_operator() is operator


def test_a_retryable_record_never_clears_an_unrepaired_construction_failure(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V-2: a clean recovery clears only the retryable failure set."""

    from cruxible_client.contracts.canonical import canonical_bytes

    _server_state(monkeypatch, short_root)
    secrets = short_root / "daemon" / "provider-secrets"
    secrets.parent.mkdir(parents=True, exist_ok=True)
    secrets.write_text("occupied", encoding="utf-8")
    manager = get_playbill_manager()
    operator = manager.provider_runtime_operator()
    assert operator.secret_store is None
    assert operator.lane_status()[0] == "unavailable"
    store = operator.process_leases
    assert store is not None
    # A second, retryable degradation: one lease record whose group is stuck.
    invocation = "sha256:" + "b" * 64
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
        "provider_process_group_survived_recovery", "transiently stuck", retryable=True
    )
    registry_record = SimpleNamespace(backend="governed_daemon", instance_id="inst_v")
    monkeypatch.setattr(
        "cruxible_core.runtime.playbill_manager.get_registry",
        lambda: SimpleNamespace(list_instances=lambda: (registry_record,)),
    )
    monkeypatch.setattr(manager, "get", lambda _instance_id: SimpleNamespace())
    monkeypatch.setattr(
        "cruxible_core.service.playbill_procedure_runs.service_recover_provider_invocations",
        lambda _instance, **kwargs: tuple(kwargs["invocation_ids"]),
    )
    with pytest.raises(ProviderLocalRuntimeRefused):
        operator._begin_invocation()
    assert operator.lane_status()[0] == "unavailable"
    assert operator.secret_store is None
    assert list(operator.secret_resolvers._resolvers) == ["environment"]  # type: ignore[attr-defined]
