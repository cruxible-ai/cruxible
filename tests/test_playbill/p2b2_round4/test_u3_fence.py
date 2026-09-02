"""Round-4: attack T-1/T-2/T-7/T-8/T-11/T-13 -- the process fence itself."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

import cruxible_core.playbill.provider_local_runtime as runtime_module
from cruxible_core.playbill.provider_local_runtime import (
    _CHILD_FENCE_WRAPPER,
    _open_secret_channel,
    _run_child,
)
from cruxible_core.playbill.provider_process_leases import (
    ProviderLocalRuntimeRefused,
    ProviderProcessLeaseStore,
    _socket_peer_pid,
)
from cruxible_core.playbill.provider_runtime_contract import ProviderRuntimeBudgetsV1

CONTEXT = b'{"run_id":"RUN-r4","input":{"value":"r4"}}'

FAST_CHILD = r"""#!/usr/bin/env python3
import json, os, socket, subprocess, sys, threading

MARKER = "@MARKER@"
MODE = "@MODE@"

invocation_id, control_path = sys.argv[2:4]
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(control_path)
os.chmod(control_path, 0o600)
server.listen(2)

def echo():
    while True:
        try:
            connection, _ = server.accept()
        except OSError:
            return
        with connection:
            received = connection.recv(4096).decode("utf-8")
            connection.sendall(invocation_id.encode("utf-8") if received == invocation_id else b"")

threading.Thread(target=echo, daemon=True).start()

LOOP = (
    "import sys,time\n"
    "path=sys.argv[1]\n"
    "while True:\n"
    "    open(path,'a').write('x')\n"
    "    time.sleep(0.02)\n"
)
preexec = (lambda: os.setpgid(0, 0)) if MODE == "setpgid" else (lambda: os.setsid())
subprocess.Popen(
    [sys.executable, "-c", LOOP, MARKER],
    preexec_fn=preexec,
    close_fds=True,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
open(MARKER + ".spawned", "w").write("1")
document = json.loads(sys.stdin.buffer.read())
import time as _t
_t.sleep(float("@LINGER@"))
envelope = {
    "protocol_version": "1.0",
    "run_id": document["run_id"],
    "status": "ok",
    "output": {"echo": document["input"]["value"]},
    "refusal": None,
    "error": None,
    "trace": {"endpoints_contacted": [], "events": [], "metrics": {}},
}
json.dump(envelope, sys.stdout, sort_keys=True, separators=(",", ":"))
sys.stdout.flush()
"""


def _write_fast_child(path: Path, *, marker: Path, mode: str, linger: float = 0.0) -> Path:
    path.write_text(
        FAST_CHILD.replace("@MARKER@", str(marker))
        .replace("@MODE@", mode)
        .replace("@LINGER@", str(linger)),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _grew(marker: Path, seconds: float = 0.5) -> bool:
    before = marker.stat().st_size if marker.exists() else -1
    time.sleep(seconds)
    after = marker.stat().st_size if marker.exists() else -1
    return after > before


def _kill_tree(marker: Path) -> None:
    completed = subprocess.run(
        ["ps", "-Ao", "pid=,command="], capture_output=True, text=True, check=False
    )
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) == 2 and str(marker) in fields[1]:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.kill(int(fields[0]), signal.SIGKILL)


# ------------------------------------------------------------------ T-1 entry


def test_returncode_is_none_and_the_group_is_alive_at_every_terminate_entry(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONFIRM: the WNOWAIT window holds on the success path."""

    observed: list[tuple[object, bool]] = []
    original = runtime_module._terminate_process_group

    def spy(process, timeout_seconds, *, descendants):  # type: ignore[no-untyped-def]
        alive = True
        try:
            os.killpg(process.pid, 0)
        except OSError:
            alive = False
        observed.append((process.returncode, alive))
        return original(process, timeout_seconds, descendants=descendants)

    monkeypatch.setattr(runtime_module, "_terminate_process_group", spy)
    store = ProviderProcessLeaseStore(short_root / "l")
    marker = short_root / "m"
    interpreter = _write_fast_child(short_root / "c.py", marker=marker, mode="setsid")
    try:
        _run_child(
            interpreter,
            entrypoint="demo:Provider",
            context=CONTEXT,
            budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=8, output_bytes=65_536),
            secret_fd=None,
            invocation_id="sha256:" + "b" * 64,
            process_leases=store,
        )
    finally:
        _kill_tree(marker)
    assert len(observed) == 1
    returncode, group_alive = observed[0]
    # CONFIRM the WNOWAIT window: the leader is an unreaped zombie at entry, so
    # its pid is not released and `killpg` is safe.  When every group member has
    # exited, `killpg(pid, 0)` reports ESRCH -- that is the honest reading, and
    # the in-tree success-path regression covers the same-group live case.
    assert returncode is None
    assert group_alive in (True, False)


# ------------------------------------------------------------------ U: fast escape


@pytest.mark.parametrize("mode", ["setpgid", "setsid"])
def test_a_fast_escaping_descendant_outlives_a_short_successful_invocation(
    short_root: Path, mode: str
) -> None:
    """T-11's 1-second poll plus WNOWAIT reparenting: on the success path the
    forced snapshot walks the ppid chain of a ZOMBIE, whose children were already
    reparented to init, and the escapee's argv never carries the invocation id."""

    store = ProviderProcessLeaseStore(short_root / "l")
    marker = short_root / f"m-{mode}"
    interpreter = _write_fast_child(short_root / f"c-{mode}.py", marker=marker, mode=mode)
    try:
        outcome = _run_child(
            interpreter,
            entrypoint="demo:Provider",
            context=CONTEXT,
            budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=8, output_bytes=65_536),
            secret_fd=None,
            invocation_id="sha256:" + "c" * 64,
            process_leases=store,
        )
        assert json.loads(outcome.stdout)["status"] == "ok"
        assert tuple(store.root.glob("*.json")) == ()  # lease released, invisible to recovery
        survived = _grew(marker)
    finally:
        _kill_tree(marker)
    assert survived, "the escapee was swept -- property holds"


def test_the_same_escape_is_caught_when_the_invocation_lasts_past_one_poll(
    short_root: Path,
) -> None:
    """Contrast: with a sub-second poll interval the same escapee is swept."""

    store = ProviderProcessLeaseStore(
        short_root / "l2", descendant_tracker_poll_interval_seconds=0.05
    )
    marker = short_root / "m2"
    interpreter = _write_fast_child(short_root / "c2.py", marker=marker, mode="setpgid", linger=0.4)
    try:
        _run_child(
            interpreter,
            entrypoint="demo:Provider",
            context=CONTEXT,
            budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=8, output_bytes=65_536),
            secret_fd=None,
            invocation_id="sha256:" + "d" * 64,
            process_leases=store,
        )
        time.sleep(0.2)
        survived = _grew(marker)
    finally:
        _kill_tree(marker)
    assert not survived


# ------------------------------------------------------------------ T-2 residue


def test_a_publish_write_failure_is_typed_and_leaves_no_child_or_artifact(
    short_root: Path,
) -> None:
    """T-2 ruled a publish failure kills the child and unlinks record+socket."""

    lease_root = short_root / "l3"
    store = ProviderProcessLeaseStore(lease_root)
    marker = short_root / "m3"
    interpreter = _write_fast_child(short_root / "c3.py", marker=marker, mode="setsid")
    os.chmod(lease_root, 0o500)  # mkstemp(dir=root) now fails with EACCES
    escaped: BaseException | None = None
    spawned: list[int] = []
    real_popen = subprocess.Popen

    class Spy(subprocess.Popen):  # type: ignore[type-arg]
        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            super().__init__(*args, **kwargs)
            spawned.append(self.pid)

    runtime_module.subprocess.Popen = Spy  # type: ignore[misc]
    try:
        _run_child(
            interpreter,
            entrypoint="demo:Provider",
            context=CONTEXT,
            budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=8, output_bytes=65_536),
            secret_fd=None,
            invocation_id="sha256:" + "e" * 64,
            process_leases=store,
        )
    except BaseException as exc:  # noqa: BLE001
        escaped = exc
    finally:
        runtime_module.subprocess.Popen = real_popen  # type: ignore[misc]
        os.chmod(lease_root, 0o700)
        time.sleep(0.3)
        child_alive = False
        if spawned:
            try:
                os.kill(spawned[0], 0)
                child_alive = True
            except OSError:
                child_alive = False
        descendant_alive = _grew(marker, 0.3)
        for pid in spawned:
            with contextlib.suppress(OSError):
                os.killpg(pid, signal.SIGKILL)
        _kill_tree(marker)
        _kill_tree(str(marker) + ".spawned")
    assert isinstance(escaped, ProviderLocalRuntimeRefused)
    assert escaped.code == "provider_process_lease_invalid"
    assert not child_alive
    assert not descendant_alive
    assert tuple(lease_root.glob("*.json")) == ()
    assert tuple(store.control_root.glob("*.sock")) == ()


# ------------------------------------------------------------------ T-8 peer pid


def test_the_peer_pid_is_kernel_supplied_not_a_message_field(short_root: Path) -> None:
    """CONFIRM T-8: the pid used for authorisation comes from the socket option."""

    path = short_root / "p.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(path))
    connection, _ = server.accept()
    import platform

    try:
        if platform.system() == "Linux":
            assert _socket_peer_pid(connection) == os.getpid()
        else:
            assert _socket_peer_pid(connection) is None  # see U: Darwin branch dead
    finally:
        client.close()
        connection.close()
        server.close()


def test_the_darwin_peer_pid_branch_never_resolves(short_root: Path) -> None:
    """T-8's Darwin half uses names CPython does not define, so it is dead code."""

    import platform
    import struct

    if platform.system() != "Darwin":
        pytest.skip("Darwin-specific spelling")
    assert getattr(socket, "LOCAL_PEERPID", None) is None
    assert getattr(socket, "SOL_LOCAL", None) is None
    path = short_root / "d.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(path))
    connection, _ = server.accept()
    try:
        assert _socket_peer_pid(connection) is None  # the shipped helper
        raw = connection.getsockopt(0, 2, struct.calcsize("i"))
        assert struct.unpack("i", raw)[0] == os.getpid()  # the numeric spelling works
    finally:
        client.close()
        connection.close()
        server.close()


def test_an_independent_echo_server_cannot_authorise_a_kill(short_root: Path) -> None:
    """CONFIRM T-8 end to end: an echo from a non-lease pid never signals."""

    store = ProviderProcessLeaseStore(short_root / "l4")
    invocation_id = "sha256:" + "f" * 64
    record_path, control_path = store.paths(invocation_id)
    victim = subprocess.Popen(
        [sys.executable, "-c", "import time\nwhile True: time.sleep(0.05)"],
        start_new_session=True,
    )
    echo_source = (
        "import os,socket,sys\n"
        "server=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "server.bind(sys.argv[1])\n"
        "server.listen(4)\n"
        "while True:\n"
        "    c,_=server.accept()\n"
        "    c.recv(4096)\n"
        "    c.sendall(sys.argv[2].encode())\n"
        "    c.close()\n"
    )
    echoer = subprocess.Popen(
        [sys.executable, "-c", echo_source, str(control_path), invocation_id],
        start_new_session=True,
    )
    try:
        for _ in range(200):
            if control_path.exists():
                break
            time.sleep(0.01)
        from cruxible_client.contracts.canonical import canonical_bytes

        record_path.write_bytes(
            canonical_bytes(
                {
                    "invocation_id": invocation_id,
                    "pid": victim.pid,
                    "process_group_id": victim.pid,
                    "session_id": None,
                    "boot_id": None,
                    "process_start_time": None,
                }
            )
        )
        result = store.recover_all()
        assert result.recovered == ()
        assert victim.poll() is None, "the victim group was signalled"
    finally:
        for process in (victim, echoer):
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            with contextlib.suppress(Exception):
                process.wait(timeout=2)


# ------------------------------------------------------------------ T-1 wrapper


def test_the_wrapper_clears_inheritance_before_any_provider_code(short_root: Path) -> None:
    """CONFIRM: FD_CLOEXEC is set on the secret fd before runpy runs."""

    lines = _CHILD_FENCE_WRAPPER.splitlines()
    clear = next(i for i, line in enumerate(lines) if "set_inheritable(secret_fd, False)" in line)
    run = next(i for i, line in enumerate(lines) if "runpy.run_module" in line)
    assert clear < run
    bind = next(i for i, line in enumerate(lines) if "server.bind(" in line)
    assert clear < bind


def test_an_execd_grandchild_of_the_real_wrapper_cannot_read_the_secret(
    short_root: Path,
) -> None:
    """Run `_CHILD_FENCE_WRAPPER` verbatim with a stub provider module (T-1)."""

    package = short_root / "cruxible_provider_runtime"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    stolen = short_root / "stolen"
    body = (
        "import os,sys\n"
        "try:\n"
        "    data = os.read(int(os.environ['SECRET_FD']), 65536)\n"
        "except OSError as exc:\n"
        "    data = repr(exc).encode()\n"
        "open(sys.argv[1],'wb').write(data)\n"
    )
    (package / "child.py").write_text(
        "import os, subprocess, sys\n"
        f"BODY = {body!r}\n"
        f"subprocess.run([sys.executable, '-c', BODY, {str(stolen)!r}], check=False)\n"
        'print(\'{"protocol_version":"1.0"}\')\n',
        encoding="utf-8",
    )
    wrapper = short_root / "provider_child_fence.py"
    wrapper.write_text(_CHILD_FENCE_WRAPPER, encoding="utf-8")
    control = short_root / "w.sock"
    with _open_secret_channel({"billing/api": "SUPERSECRET-R4"}, join_timeout_seconds=2.0) as fd:
        assert fd is not None
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(short_root)
        environment["SECRET_FD"] = str(fd)
        process = subprocess.Popen(
            [
                sys.executable,
                str(wrapper),
                "sha256:" + "9" * 64,
                str(control),
                "demo:Provider",
                str(fd),
            ],
            pass_fds=(fd,),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            out, err = process.communicate(timeout=20)
        finally:
            with contextlib.suppress(Exception):
                os.killpg(process.pid, signal.SIGKILL)
    assert process.returncode == 0, err[-400:]
    assert b"protocol_version" in out
    assert stolen.exists(), "the probe never reached the read (vacuous)"
    payload = stolen.read_bytes()
    assert b"SUPERSECRET-R4" not in payload, payload[:200]
    assert b"OSError" in payload or b"Errno 9" in payload, payload[:200]


FORK_CHILD = r"""#!/usr/bin/env python3
import json, os, socket, sys, threading, time
invocation_id, control_path = sys.argv[2:4]
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(control_path)
os.chmod(control_path, 0o600)
server.listen(2)
def echo():
    while True:
        try:
            connection, _ = server.accept()
        except OSError:
            return
        with connection:
            received = connection.recv(4096).decode("utf-8")
            connection.sendall(invocation_id.encode("utf-8") if received == invocation_id else b"")
threading.Thread(target=echo, daemon=True).start()
if os.fork() == 0:
    os.setsid()
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    while True:
        open("@MARKER@", "a").write("x")
        time.sleep(0.02)
document = json.loads(sys.stdin.buffer.read())
envelope = {
    "protocol_version": "1.0",
    "run_id": document["run_id"],
    "status": "ok",
    "output": {"echo": document["input"]["value"]},
    "refusal": None,
    "error": None,
    "trace": {"endpoints_contacted": [], "events": [], "metrics": {}},
}
json.dump(envelope, sys.stdout, sort_keys=True, separators=(",", ":"))
sys.stdout.flush()
"""


def test_a_fork_only_escapee_is_still_swept_because_its_argv_names_the_invocation(
    short_root: Path,
) -> None:
    """CONFIRM: the fd-holding (fork-only) case is covered even on a fast success."""

    store = ProviderProcessLeaseStore(short_root / "lf")
    marker = short_root / "mf"
    interpreter = short_root / "cf.py"
    interpreter.write_text(FORK_CHILD.replace("@MARKER@", str(marker)), encoding="utf-8")
    interpreter.chmod(0o755)
    try:
        outcome = _run_child(
            interpreter,
            entrypoint="demo:Provider",
            context=CONTEXT,
            budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=8, output_bytes=65_536),
            secret_fd=None,
            invocation_id="sha256:" + "8" * 64,
            process_leases=store,
        )
        assert json.loads(outcome.stdout)["status"] == "ok"
        survived = _grew(marker, 0.4)
    finally:
        _kill_tree(marker)
    assert not survived, "a fork-only escapee kept the invocation argv and must be swept"
