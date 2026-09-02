"""Round-4: startup cost, liveness at scale, and the T-12/T-13 fixes."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import cruxible_core.playbill.provider_process_leases as lease_module
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_core.playbill.provider_process_leases import (
    ProviderProcessLeaseStore,
)


def _record(store: ProviderProcessLeaseStore, invocation_id: str, **fields: object) -> Path:
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


def test_t12_two_hundred_records_read_the_boot_id_once(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    for index in range(200):
        _record(store, "sha256:" + f"{index:064x}")
    boot_calls: list[int] = []
    original = lease_module._current_boot_id

    def counted() -> str:
        boot_calls.append(1)
        return original()

    monkeypatch.setattr(lease_module, "_current_boot_id", counted)
    started = time.monotonic()
    result = store.recover_all()
    elapsed = time.monotonic() - started
    assert len(result.removed) == 200
    assert len(boot_calls) == 1
    assert elapsed < 10.0, elapsed
    assert tuple(store.root.glob("*.json")) == ()


def test_two_hundred_records_do_not_block_create_app_beyond_a_bounded_time(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.runtime.permissions import reset_permissions
    from cruxible_core.runtime.playbill_manager import get_playbill_manager
    from cruxible_core.server.registry import get_registry, reset_registry

    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(short_root))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    reset_permissions()
    reset_registry()
    get_playbill_manager().clear()
    get_registry()
    store = ProviderProcessLeaseStore(
        short_root / "daemon" / "provider-process-leases", control_root=short_root / "c"
    )
    for index in range(200):
        _record(store, "sha256:" + f"{index:064x}")
    from cruxible_core.server.app import create_app

    started = time.monotonic()
    app = create_app()
    elapsed = time.monotonic() - started
    assert app is not None
    assert elapsed < 20.0, elapsed
    lane = get_playbill_manager().provider_runtime_operator().lane_status()
    assert lane == ("available", None, None)


def test_one_stuck_record_is_bounded_by_its_configured_deadline(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProviderProcessLeaseStore(
        short_root / "l2",
        control_root=short_root / "c",
        recovery_timeout_seconds=0.5,
    )
    live = subprocess.Popen(
        [sys.executable, "-c", "import time\nwhile True: time.sleep(0.05)"],
        start_new_session=True,
    )
    real_killpg = os.killpg
    try:
        _record(
            store,
            "sha256:" + "1" * 64,
            pid=live.pid,
            process_group_id=live.pid,
            session_id=os.getsid(live.pid),
            boot_id=lease_module._current_boot_id(),
            process_start_time=lease_module._process_start_time(live.pid),
        )
        monkeypatch.setattr(lease_module.os, "killpg", lambda *args: None)  # never dies
        started = time.monotonic()
        result = store.recover_all()
        elapsed = time.monotonic() - started
        assert len(result.could_not_clean) == 1
        assert result.could_not_clean[0].code == "provider_process_group_survived_recovery"
        assert result.could_not_clean[0].invocation_id == "sha256:" + "1" * 64
        assert 0.5 <= elapsed < 3.0, elapsed
    finally:
        with contextlib.suppress(OSError):
            real_killpg(os.getpgid(live.pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            live.wait(timeout=2)


def test_the_recovery_loop_reports_records_beyond_its_aggregate_budget(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The config-carried aggregate deadline preserves unattempted records."""

    store = ProviderProcessLeaseStore(
        short_root / "l3",
        control_root=short_root / "c",
        recovery_timeout_seconds=0.4,
        recovery_aggregate_timeout_seconds=0.65,
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", "import time\nwhile True: time.sleep(0.05)"],
            start_new_session=True,
        )
        for _ in range(4)
    ]
    real_killpg = os.killpg
    try:
        boot = lease_module._current_boot_id()
        for index, process in enumerate(processes):
            _record(
                store,
                "sha256:" + f"{index:064x}",
                pid=process.pid,
                process_group_id=process.pid,
                session_id=os.getsid(process.pid),
                boot_id=boot,
                process_start_time=lease_module._process_start_time(process.pid),
            )
        monkeypatch.setattr(lease_module.os, "killpg", lambda *args: None)
        started = time.monotonic()
        result = store.recover_all()
        elapsed = time.monotonic() - started
        assert len(result.could_not_clean) == 4
        assert [item.attempt_status for item in result.could_not_clean].count("not_attempted") >= 2
        assert elapsed < 1.3, elapsed
    finally:
        for process in processes:
            with contextlib.suppress(OSError):
                real_killpg(os.getpgid(process.pid), signal.SIGKILL)
            with contextlib.suppress(Exception):
                process.wait(timeout=2)


def test_t13_a_prior_attempt_survivor_is_still_observed(short_root: Path) -> None:
    """The tracker keeps no baseline exclusion set."""

    from cruxible_core.playbill.provider_local_runtime import _DescendantTracker

    invocation_id = "sha256:" + "7" * 64
    survivor = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys,time\nwhile True: time.sleep(0.05)",
            invocation_id,
        ],
        start_new_session=True,
    )
    try:
        time.sleep(0.3)
        tracker = _DescendantTracker(
            os.getpid(), invocation_id=invocation_id, poll_interval_seconds=10.0
        )
        try:
            observed = {item.pid for item in tracker.snapshot()}
        finally:
            tracker.close(timeout_seconds=1.0)
        assert survivor.pid in observed
    finally:
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(survivor.pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            survivor.wait(timeout=2)


def test_a_record_that_vanishes_mid_scan_is_already_handled(short_root: Path) -> None:
    """A FileNotFoundError between glob and read is a harmless concurrent recovery."""

    store = ProviderProcessLeaseStore(short_root / "l4", control_root=short_root / "c")
    path = _record(store, "sha256:" + "2" * 64)
    original = Path.read_bytes

    def vanish(self: Path) -> bytes:
        if self == path:
            raise FileNotFoundError(2, "No such file", str(self))
        return original(self)

    Path.read_bytes = vanish  # type: ignore[method-assign]
    try:
        result = store.recover_all()
    finally:
        Path.read_bytes = original  # type: ignore[method-assign]
    assert result.removed == ()
    assert result.could_not_clean == ()


def test_release_failure_is_only_could_not_clean(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProviderProcessLeaseStore(short_root / "release", control_root=short_root / "c")
    invocation_id = "sha256:" + "3" * 64
    _record(store, invocation_id)

    def refuse_release(_lease: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(store, "release", refuse_release)
    result = store.recover_all()
    assert result.recovered == ()
    assert result.removed == ()
    assert len(result.could_not_clean) == 1
    assert result.could_not_clean[0].invocation_id == invocation_id


def test_two_hundred_identity_bearing_records_still_serve_within_a_bounded_time(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The honest profile: every record forces one `ps` start-token lookup."""

    store = ProviderProcessLeaseStore(short_root / "l5", control_root=short_root / "c")
    boot = lease_module._current_boot_id()
    for index in range(200):
        _record(
            store,
            "sha256:" + f"{index:064x}",
            pid=99_000_000 + index,
            process_group_id=99_000_000 + index,
            session_id=1,
            boot_id=boot,
            process_start_time="not-the-real-token",
        )
    boot_calls: list[int] = []
    original_boot = lease_module._current_boot_id
    start_calls: list[int] = []
    original_start = lease_module._process_start_time

    def counted_boot() -> str:
        boot_calls.append(1)
        return original_boot()

    def counted_start(pid: int) -> str:
        start_calls.append(1)
        return original_start(pid)

    monkeypatch.setattr(lease_module, "_current_boot_id", counted_boot)
    monkeypatch.setattr(lease_module, "_process_start_time", counted_start)
    started = time.monotonic()
    result = store.recover_all()
    elapsed = time.monotonic() - started
    assert len(result.removed) == 200
    assert len(boot_calls) == 1
    assert elapsed < 20.0, (elapsed, len(start_calls))
    print(f"\n200 identity-bearing records: {elapsed:.2f}s, ps lookups={len(start_calls)}")
