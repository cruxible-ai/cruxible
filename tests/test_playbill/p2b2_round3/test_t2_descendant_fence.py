"""Round 3 - attack the descendant tracker/sweep and the kill/reap ordering."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import cruxible_core.playbill.provider_local_runtime as runtime_module
import cruxible_core.playbill.provider_process_leases as lease_module
from cruxible_core.playbill.provider_local_runtime import (
    _DescendantTracker,
    _open_secret_channel,
    _run_child,
)
from cruxible_core.playbill.provider_process_leases import (
    ProviderDescendantProcessV1,
    ProviderLocalRuntimeRefused,
    ProviderProcessLeaseStore,
    descendant_is_live,
    descendant_processes,
    _process_start_time,
)
from cruxible_core.playbill.provider_runtime_contract import ProviderRuntimeBudgetsV1

sys.path.insert(0, str(Path(__file__).parent))
from _child import write_child  # noqa: E402

CONTEXT = b'{"run_id":"RUN-r3","input":{"value":"r3"}}'


def _grew(marker: Path, seconds: float = 0.5) -> bool:
    before = marker.stat().st_size if marker.exists() else -1
    time.sleep(seconds)
    after = marker.stat().st_size if marker.exists() else -1
    return after > before


def _kill_tree(marker: Path) -> None:
    """Reap whatever the probe left running (identified by its marker argv)."""

    completed = subprocess.run(
        ["ps", "-Ao", "pid=,command="], capture_output=True, text=True, check=False
    )
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) == 2 and str(marker) in fields[1]:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.kill(int(fields[0]), signal.SIGKILL)


# ---------------------------------------------------------------- T-H


def test_the_success_path_kills_the_group_so_a_plain_child_cannot_survive(
    short_root: Path,
) -> None:
    """`_terminate_process_group(successful=True)` skips `killpg` entirely, and the
    descendant sweep only tracks processes that LEFT the session, so an ordinary
    same-session descendant that exec'd away survives every successful invocation
    and the lease is released, hiding it from `recover_all` forever."""

    store = ProviderProcessLeaseStore(short_root / "l")
    marker = short_root / "survivor"
    interpreter = write_child(short_root / "child.py", mode="ok", marker=marker)
    try:
        outcome = _run_child(
            interpreter,
            entrypoint="demo:Provider",
            context=CONTEXT,
            budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=8, output_bytes=65_536),
            secret_fd=None,
            invocation_id="sha256:" + "a" * 64,
            process_leases=store,
        )
        assert json.loads(outcome.stdout)["status"] == "ok"
        assert tuple(store.root.glob("*.json")) == ()
        assert not _grew(marker), "the descendant must be reaped by the fence"
    finally:
        _kill_tree(marker)


def test_a_same_session_setpgid_descendant_is_swept_on_the_refusal_path(
    short_root: Path,
) -> None:
    """`descendant_processes` skips every row whose session equals the child's, so a
    grandchild that only calls `setpgid(0,0)` is outside the recorded process group
    AND outside the sweep."""

    store = ProviderProcessLeaseStore(short_root / "l")
    marker = short_root / "setpgid-survivor"
    interpreter = write_child(short_root / "child.py", mode="setpgid", marker=marker)
    try:
        with pytest.raises(ProviderLocalRuntimeRefused) as caught:
            _run_child(
                interpreter,
                entrypoint="demo:Provider",
                context=CONTEXT,
                budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=1, output_bytes=65_536),
                secret_fd=None,
                invocation_id="sha256:" + "b" * 64,
                process_leases=store,
            )
        assert caught.value.code == "budget_wall_clock"
        assert not _grew(marker), "the setpgid grandchild must be swept"
    finally:
        _kill_tree(marker)


def test_the_sweep_skips_only_processes_in_the_original_session_and_group() -> None:

    source = lease_module.inspect_source = None  # placeholder to keep flake quiet
    import inspect

    text = inspect.getsource(lease_module._descendant_processes_from_rows)
    assert "row.session_id == root.session_id" in text
    assert "row.process_group_id == root.process_group_id" in text
    assert "continue" in text
    terminate = inspect.getsource(runtime_module._terminate_process_group)
    assert "if process.returncode is None:" in terminate
    assert source is None


# ---------------------------------------------------------------- secret fd


def test_a_descendant_cannot_read_the_secret_bundle_after_the_parent_close(
    short_root: Path,
) -> None:
    """The secret pipe is inherited by grandchildren; the daemon closes only its own
    end, and the success path leaves the descendant alive to read the bundle."""

    store = ProviderProcessLeaseStore(short_root / "l")
    leak = short_root / "leaked"
    interpreter = write_child(short_root / "child.py", mode="leak", marker=leak)
    secret = "SUPERSECRET-CUSTODY-MATERIAL"
    try:
        with _open_secret_channel({"billing/api": secret}, join_timeout_seconds=5.0) as fd:
            assert fd is not None
            context = json.dumps(
                {
                    "run_id": "RUN-leak",
                    "input": {"value": "leak"},
                    "secret_channel": {"kind": "fd", "fd": fd, "refs": []},
                }
            ).encode("utf-8")
            outcome = _run_child(
                interpreter,
                entrypoint="demo:Provider",
                context=context,
                budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=8, output_bytes=65_536),
                secret_fd=fd,
                invocation_id="sha256:" + "c" * 64,
                process_leases=store,
            )
            assert json.loads(outcome.stdout)["status"] == "ok"
        # Parent-side descriptor is now closed and the lease is released.
        assert tuple(store.root.glob("*.json")) == ()
        time.sleep(0.4)
        leaked = leak.read_text(encoding="utf-8", errors="replace") if leak.exists() else ""
        assert secret not in leaked
    finally:
        _kill_tree(leak)


def test_exactly_one_owner_closes_the_secret_read_descriptor() -> None:
    """CONFIRM (N-2): `_run_child` no longer closes the channel descriptor."""

    import inspect

    text = inspect.getsource(runtime_module._run_child)
    assert "os.close(secret_fd)" not in text
    channel = inspect.getsource(runtime_module._open_secret_channel)
    assert channel.count("os.close(read_fd)") == 1


# ---------------------------------------------------------------- identity


def test_descendant_identity_defeats_pid_reuse() -> None:
    """CONFIRM: the sweep never signals a pid whose start token moved."""

    victim = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(3)"])
    identity = ProviderDescendantProcessV1(
        pid=victim.pid, process_start_time=_process_start_time(victim.pid)
    )
    stale = ProviderDescendantProcessV1(pid=victim.pid, process_start_time="Thu Jan 1 00:00:00 1970")
    try:
        assert descendant_is_live(identity) is True
        assert descendant_is_live(stale) is False
        lease_module.kill_descendants((stale,))
        time.sleep(0.2)
        assert victim.poll() is None, "a stale identity must never be signalled"
        lease_module.kill_descendants((identity,))
        victim.wait(timeout=5)
        assert victim.returncode == -signal.SIGKILL
    finally:
        with contextlib.suppress(Exception):
            victim.kill()
            victim.wait(timeout=5)


def test_a_descendant_present_before_the_tracker_is_still_observed(short_root: Path) -> None:
    """`_DescendantTracker`'s baseline is taken AFTER the child is spawned, so
    anything already carrying the invocation token - including a survivor of the
    previous attempt of the same deterministic invocation id - is excluded from
    every later sweep."""

    marker = short_root / "baselined"
    token = "sha256:" + "d" * 64
    body = (
        "import sys,time\n"
        "path=sys.argv[1]\n"
        "while True:\n"
        "    open(path,'a').write('x')\n"
        "    time.sleep(0.02)\n"
    )
    early = subprocess.Popen(
        [sys.executable, "-c", body, str(marker), token],
        start_new_session=True,
    )
    try:
        for _ in range(300):
            if marker.exists():
                break
            time.sleep(0.01)
        tracker = _DescendantTracker(
            os.getpid(), invocation_id=token, poll_interval_seconds=1.0
        )
        try:
            observed = {item.pid for item in tracker.snapshot()}
        finally:
            tracker.close(timeout_seconds=1.0)
        assert early.pid in observed
        assert early.pid in {
            item.pid for item in lease_module.processes_naming_invocation(token)
        }
    finally:
        with contextlib.suppress(Exception):
            os.killpg(early.pid, signal.SIGKILL)
            early.wait(timeout=5)


def test_the_tracker_uses_one_snapshot_per_second_and_one_ps_per_snapshot(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cost: the tracker thread polls `ps -Ao` twice every 10 ms for the whole
    invocation, per in-flight Provider call."""

    calls: list[str] = []
    real_run = lease_module.subprocess.run

    def counted(args, *rest, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(" ".join(args) if isinstance(args, list) else str(args))
        return real_run(args, *rest, **kwargs)

    monkeypatch.setattr(lease_module.subprocess, "run", counted)
    tracker = _DescendantTracker(
        os.getpid(), invocation_id="sha256:" + "e" * 64, poll_interval_seconds=1.0
    )
    time.sleep(1.0)
    tracker.close(timeout_seconds=1.0)
    ps_calls = [item for item in calls if item.startswith("ps ")]
    assert 1 <= len(ps_calls) <= 3, ps_calls


def test_the_success_path_still_has_a_safe_window_to_kill_the_group(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fix is available: `_collect_child_output` observes exit with
    `waitid(..., WNOWAIT)`, so at the moment `_terminate_process_group` is entered
    the child is still an unreaped zombie - its pid is NOT released and its group
    still exists, so a `killpg` there would be safe."""

    store = ProviderProcessLeaseStore(short_root / "l")
    marker = short_root / "window"
    interpreter = write_child(short_root / "child.py", mode="ok", marker=marker)
    observed: list[tuple[object, bool]] = []
    real = runtime_module._terminate_process_group

    def traced(process, timeout_seconds, **kwargs):  # type: ignore[no-untyped-def]
        group_alive = True
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            group_alive = False
        except PermissionError:
            pass
        observed.append((process.returncode, group_alive))
        return real(process, timeout_seconds, **kwargs)

    monkeypatch.setattr(runtime_module, "_terminate_process_group", traced)
    try:
        _run_child(
            interpreter,
            entrypoint="demo:Provider",
            context=CONTEXT,
            budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=8, output_bytes=65_536),
            secret_fd=None,
            invocation_id="sha256:" + "f" * 64,
            process_leases=store,
        )
    finally:
        monkeypatch.undo()
        _kill_tree(marker)
    assert observed == [(None, True)], observed
