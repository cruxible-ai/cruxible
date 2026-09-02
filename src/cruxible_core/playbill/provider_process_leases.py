"""Operational Provider child leases guarded by a child-owned echo endpoint."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import signal
import socket
import stat
import struct
import subprocess
import tempfile
import time
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import PlaybillExecutionError

DEFAULT_PROVIDER_LEASE_ACQUISITION_TIMEOUT_SECONDS = 5.0
DEFAULT_PROVIDER_LEASE_RECOVERY_TIMEOUT_SECONDS = 5.0
DEFAULT_PROVIDER_SECRET_WRITER_JOIN_TIMEOUT_SECONDS = 5.0
DEFAULT_PROVIDER_STDIN_WRITER_JOIN_TIMEOUT_SECONDS = 5.0
DEFAULT_PROVIDER_DESCENDANT_TRACKER_JOIN_TIMEOUT_SECONDS = 5.0
DEFAULT_PROVIDER_DESCENDANT_TRACKER_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_PROVIDER_PROCESS_GROUP_TERMINATION_TIMEOUT_SECONDS = 5.0
_BOOT_ID_UNSET = object()

ProviderProcessFenceCodeV1 = Literal[
    "provider_process_lease_invalid",
    "provider_process_lease_missing",
    "provider_process_lease_echo_failed",
    "provider_process_lease_echo_mismatch",
    "provider_process_group_survived_recovery",
]


class ProviderLocalRuntimeRefused(PlaybillExecutionError):
    """Typed daemon-local refusal translated into a Provider completion."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        self.code = code
        self.details = {} if details is None else details
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ProviderProcessLeaseV1:
    """Process fence whose acquisition and recovery deadlines read VALIDITY WINDOW."""

    invocation_id: str
    pid: int
    process_group_id: int
    session_id: int | None
    boot_id: str | None
    process_start_time: str | None
    control_path: Path
    record_path: Path


@dataclass(frozen=True)
class ProviderProcessLeaseRemovalV1:
    """One discarded fence record that did not authorize a recovery signal."""

    record_name: str
    invocation_id: str | None
    reason: Literal["dead_orphan", "malformed"]


@dataclass(frozen=True)
class ProviderProcessRecoveryFailureV1:
    """One isolated fence cleanup failure retained for operator repair."""

    record_name: str
    invocation_id: str | None
    code: ProviderProcessFenceCodeV1
    message: str


@dataclass(frozen=True)
class ProviderProcessRecoveryResultV1:
    """Typed per-record recovery result; operational state, never governed state."""

    recovered: tuple[str, ...]
    removed: tuple[ProviderProcessLeaseRemovalV1, ...]
    could_not_clean: tuple[ProviderProcessRecoveryFailureV1, ...]

    @property
    def completion_invocation_ids(self) -> tuple[str, ...]:
        """Exact invocation ids whose durable starts need recovery completion."""

        return tuple(
            sorted(
                {
                    *self.recovered,
                    *(
                        item.invocation_id
                        for item in self.removed
                        if item.invocation_id is not None
                    ),
                },
                key=str.encode,
            )
        )


@dataclass(frozen=True)
class ProviderDescendantProcessV1:
    """One observed descendant identity, protected against later pid reuse."""

    pid: int
    process_start_time: str


@dataclass(frozen=True)
class _ProviderProcessRow:
    pid: int
    parent_pid: int
    process_group_id: int
    session_id: int
    command: str


def _current_boot_id() -> str:
    """Return an OS-owned boot identity used only as a local recovery fence."""

    linux_boot_id = Path("/proc/sys/kernel/random/boot_id")
    try:
        value = linux_boot_id.read_text(encoding="ascii").strip()
    except OSError:
        value = ""
    if value:
        return value
    if platform.system() == "Darwin":
        try:
            completed = subprocess.run(
                ["sysctl", "-n", "kern.boottime"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise ProviderLocalRuntimeRefused(
                "provider_process_lease_invalid",
                "the operating-system boot identity is unavailable",
            ) from exc
        value = completed.stdout.strip()
        if completed.returncode == 0 and value:
            return value
    raise ProviderLocalRuntimeRefused(
        "provider_process_lease_invalid",
        "the operating-system boot identity is unavailable",
    )


def _process_start_time(pid: int) -> str:
    """Return the OS-reported start token for one live pid.

    Linux procfs carries a jiffy token. The portable ``ps lstart`` fallback is
    whole-second precise, so same-second PID reuse remains a documented local
    fence limit until a host adapter supplies a finer token.
    """

    stat_path = Path(f"/proc/{pid}/stat")
    try:
        raw = stat_path.read_text(encoding="ascii")
    except OSError:
        raw = ""
    if raw:
        close = raw.rfind(")")
        fields = raw[close + 2 :].split() if close >= 0 else []
        if len(fields) > 19:
            return fields[19]
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ProviderLocalRuntimeRefused(
            "provider_process_lease_invalid",
            "the operating-system process start time is unavailable",
        ) from exc
    value = completed.stdout.strip()
    if completed.returncode == 0 and value:
        return " ".join(value.split())
    raise ProviderLocalRuntimeRefused(
        "provider_process_lease_invalid",
        "the operating-system process start time is unavailable",
    )


def _process_rows() -> tuple[_ProviderProcessRow, ...]:
    try:
        completed = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,pgid=,sess=,command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ProviderLocalRuntimeRefused(
            "provider_process_lease_invalid",
            "the operating-system process table is unavailable",
        ) from exc
    if completed.returncode != 0:
        raise ProviderLocalRuntimeRefused(
            "provider_process_lease_invalid",
            "the operating-system process table is unavailable",
        )
    rows: list[_ProviderProcessRow] = []
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=4)
        if len(fields) < 4:
            continue
        try:
            process_id, parent_id, group_id, session_id = (int(item) for item in fields[:4])
        except ValueError:
            continue
        rows.append(
            _ProviderProcessRow(
                pid=process_id,
                parent_pid=parent_id,
                process_group_id=group_id,
                session_id=session_id,
                command="" if len(fields) == 4 else fields[4],
            )
        )
    return tuple(rows)


def _descendant_processes_from_rows(
    pid: int,
    rows: tuple[_ProviderProcessRow, ...],
) -> tuple[ProviderDescendantProcessV1, ...]:
    root = next((row for row in rows if row.pid == pid), None)
    if root is None:
        return ()
    children: dict[int, list[_ProviderProcessRow]] = {}
    for row in rows:
        children.setdefault(row.parent_pid, []).append(row)
    pending = [pid]
    found: list[ProviderDescendantProcessV1] = []
    while pending:
        parent = pending.pop()
        for row in children.get(parent, []):
            pending.append(row.pid)
            if row.session_id == root.session_id and row.process_group_id == root.process_group_id:
                continue
            try:
                start_time = _process_start_time(row.pid)
            except ProviderLocalRuntimeRefused:
                continue
            found.append(
                ProviderDescendantProcessV1(
                    pid=row.pid,
                    process_start_time=start_time,
                )
            )
    return tuple(sorted(found, key=lambda item: item.pid))


def descendant_processes(pid: int) -> tuple[ProviderDescendantProcessV1, ...]:
    """Snapshot descendants outside the root's exact session-and-group pair."""

    return _descendant_processes_from_rows(pid, _process_rows())


def _processes_naming_invocation_from_rows(
    invocation_id: str,
    rows: tuple[_ProviderProcessRow, ...],
) -> tuple[ProviderDescendantProcessV1, ...]:
    found: list[ProviderDescendantProcessV1] = []
    for row in rows:
        if invocation_id not in row.command:
            continue
        try:
            start_time = _process_start_time(row.pid)
        except ProviderLocalRuntimeRefused:
            continue
        found.append(
            ProviderDescendantProcessV1(
                pid=row.pid,
                process_start_time=start_time,
            )
        )
    return tuple(sorted(found, key=lambda item: item.pid))


def processes_naming_invocation(invocation_id: str) -> tuple[ProviderDescendantProcessV1, ...]:
    """Find processes retaining the daemon-injected invocation argv token."""

    return _processes_naming_invocation_from_rows(invocation_id, _process_rows())


def snapshot_provider_descendants(
    pid: int,
    *,
    invocation_id: str,
) -> tuple[ProviderDescendantProcessV1, ...]:
    """Observe descendants and token-holders from one process-table snapshot."""

    rows = _process_rows()
    found = {
        (item.pid, item.process_start_time): item
        for item in (
            *_descendant_processes_from_rows(pid, rows),
            *_processes_naming_invocation_from_rows(invocation_id, rows),
        )
        if item.pid != pid
    }
    return tuple(sorted(found.values(), key=lambda item: item.pid))


def descendant_is_live(identity: ProviderDescendantProcessV1) -> bool:
    """Return true only while this exact descendant identity remains live."""

    try:
        return _process_start_time(identity.pid) == identity.process_start_time
    except ProviderLocalRuntimeRefused:
        return False


def kill_descendants(identities: tuple[ProviderDescendantProcessV1, ...]) -> None:
    """Best-effort SIGKILL of still-matching escaped descendants."""

    for identity in identities:
        if not descendant_is_live(identity):
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(identity.pid, signal.SIGKILL)


def _socket_peer_pid(client: socket.socket) -> int | None:
    """Return the connected Unix peer pid when the host exposes one."""

    so_peercred = getattr(socket, "SO_PEERCRED", None)
    if so_peercred is not None:
        try:
            raw = client.getsockopt(socket.SOL_SOCKET, so_peercred, struct.calcsize("3i"))
            return int(struct.unpack("3i", raw)[0])
        except (OSError, struct.error):
            return None
    local_peerpid = getattr(socket, "LOCAL_PEERPID", None)
    sol_local = getattr(socket, "SOL_LOCAL", None)
    if local_peerpid is not None and sol_local is not None:
        try:
            raw = client.getsockopt(sol_local, local_peerpid, struct.calcsize("i"))
            return int(struct.unpack("i", raw)[0])
        except (OSError, struct.error):
            return None
    return None


class ProviderProcessLeaseStore:
    """A rebuildable 0700 process fence; its records are never governed state."""

    def __init__(
        self,
        root: Path,
        *,
        control_root: Path | None = None,
        acquisition_timeout_seconds: float = DEFAULT_PROVIDER_LEASE_ACQUISITION_TIMEOUT_SECONDS,
        recovery_timeout_seconds: float = DEFAULT_PROVIDER_LEASE_RECOVERY_TIMEOUT_SECONDS,
        secret_writer_join_timeout_seconds: float = (
            DEFAULT_PROVIDER_SECRET_WRITER_JOIN_TIMEOUT_SECONDS
        ),
        stdin_writer_join_timeout_seconds: float = (
            DEFAULT_PROVIDER_STDIN_WRITER_JOIN_TIMEOUT_SECONDS
        ),
        descendant_tracker_join_timeout_seconds: float = (
            DEFAULT_PROVIDER_DESCENDANT_TRACKER_JOIN_TIMEOUT_SECONDS
        ),
        descendant_tracker_poll_interval_seconds: float = (
            DEFAULT_PROVIDER_DESCENDANT_TRACKER_POLL_INTERVAL_SECONDS
        ),
        process_group_termination_timeout_seconds: float = (
            DEFAULT_PROVIDER_PROCESS_GROUP_TERMINATION_TIMEOUT_SECONDS
        ),
    ) -> None:
        self.root = root
        self.acquisition_timeout_seconds = acquisition_timeout_seconds
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self.secret_writer_join_timeout_seconds = secret_writer_join_timeout_seconds
        self.stdin_writer_join_timeout_seconds = stdin_writer_join_timeout_seconds
        self.descendant_tracker_join_timeout_seconds = descendant_tracker_join_timeout_seconds
        self.descendant_tracker_poll_interval_seconds = descendant_tracker_poll_interval_seconds
        self.process_group_termination_timeout_seconds = process_group_termination_timeout_seconds
        self._ensure_private_directory(root)
        # Control sockets are operational state and must share the daemon's
        # protected state-root boundary.  The short component also preserves
        # headroom under the platform AF_UNIX path limit.
        self.control_root = root / "c" if control_root is None else control_root
        self._ensure_private_directory(self.control_root)
        self._control_finalizer = weakref.finalize(
            self,
            self._remove_empty_control_root,
            self.control_root,
        )

    @staticmethod
    def _remove_empty_control_root(path: Path) -> None:
        with contextlib.suppress(FileNotFoundError, OSError):
            path.rmdir()

    def close(self) -> None:
        """Release this store's empty operational control namespace."""

        self._control_finalizer()

    @staticmethod
    def _ensure_private_directory(path: Path) -> None:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            path.mkdir(parents=True, mode=0o700)
            mode = path.lstat().st_mode
        except OSError as exc:
            raise ProviderLocalRuntimeRefused(
                "provider_process_lease_invalid", "process-lease directory is unavailable"
            ) from exc
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise ProviderLocalRuntimeRefused(
                "provider_process_lease_invalid",
                "process-lease directory must be a real directory, not a link",
            )
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ProviderLocalRuntimeRefused(
                "provider_process_lease_invalid", "process-lease directory cannot be secured"
            ) from exc
        try:
            os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)

    def paths(self, invocation_id: str) -> tuple[Path, Path]:
        stem = hashlib.sha256(
            (str(self.root.resolve()) + "\x00" + invocation_id).encode("utf-8")
        ).hexdigest()[:32]
        control_path = self.control_root / f"{stem[:16]}.sock"
        # Darwin permits 103 pathname bytes (excluding the trailing NUL). Use
        # that conservative ceiling on every platform so deployments replay.
        if len(os.fsencode(control_path)) > 103:
            raise ProviderLocalRuntimeRefused(
                "provider_process_lease_invalid",
                "Provider control socket path is too long; use a shorter CRUXIBLE_STATE_ROOT",
            )
        return self.root / f"{stem}.json", control_path

    def prepare_control_path(self, invocation_id: str) -> Path:
        """Remove only a stale socket before a deterministic invocation retry."""

        _record_path, control_path = self.paths(invocation_id)
        try:
            mode = control_path.lstat().st_mode
        except FileNotFoundError:
            return control_path
        except OSError as exc:
            raise ProviderLocalRuntimeRefused(
                "provider_process_lease_invalid", "process control path is unavailable"
            ) from exc
        if not stat.S_ISSOCK(mode):
            raise ProviderLocalRuntimeRefused(
                "provider_process_lease_invalid",
                "process control path is occupied by a non-socket",
            )
        try:
            control_path.unlink()
        except OSError as exc:
            raise ProviderLocalRuntimeRefused(
                "provider_process_lease_invalid", "stale process control socket cannot be removed"
            ) from exc
        return control_path

    def publish(
        self,
        invocation_id: str,
        *,
        pid: int,
        process_group_id: int,
    ) -> Path:
        """Atomically publish the exact spawned process identity for later echo proof."""

        if pid <= 0 or process_group_id <= 0:
            raise ProviderLocalRuntimeRefused(
                "provider_process_lease_invalid", "process identity is invalid"
            )
        boot_id = _current_boot_id()
        process_start_time = _process_start_time(pid)
        try:
            session_id = os.getsid(pid)
        except OSError as exc:
            raise ProviderLocalRuntimeRefused(
                "provider_process_lease_invalid",
                "the operating-system process session is unavailable",
            ) from exc
        record_path, _control_path = self.paths(invocation_id)
        descriptor: int | None = None
        temporary: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=record_path.name + ".tmp-",
                dir=self.root,
            )
            temporary = Path(temporary_name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(
                    canonical_bytes(
                        {
                            "invocation_id": invocation_id,
                            "pid": pid,
                            "process_group_id": process_group_id,
                            "session_id": session_id,
                            "boot_id": boot_id,
                            "process_start_time": process_start_time,
                        }
                    )
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, record_path)
        except OSError as exc:
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            if temporary is not None:
                with contextlib.suppress(FileNotFoundError, OSError):
                    temporary.unlink()
            raise ProviderLocalRuntimeRefused(
                "provider_process_lease_invalid",
                "process-lease record could not be published",
            ) from exc
        except BaseException:
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            if temporary is not None:
                with contextlib.suppress(FileNotFoundError, OSError):
                    temporary.unlink()
            raise
        return record_path

    def require(
        self,
        invocation_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> ProviderProcessLeaseV1:
        """Wait within a VALIDITY WINDOW for the child-owned lease echo."""

        record_path, control_path = self.paths(invocation_id)
        timeout = self.acquisition_timeout_seconds if timeout_seconds is None else timeout_seconds
        deadline = time.monotonic() + timeout
        last_echo_error: ProviderLocalRuntimeRefused | None = None
        while time.monotonic() < deadline:
            try:
                raw = record_path.read_bytes()
                document = json.loads(raw)
                if canonical_bytes(document) != raw:
                    raise ValueError("lease is not canonical")
                lease = ProviderProcessLeaseV1(
                    invocation_id=str(document["invocation_id"]),
                    pid=int(document["pid"]),
                    process_group_id=int(document["process_group_id"]),
                    session_id=(
                        None if document.get("session_id") is None else int(document["session_id"])
                    ),
                    boot_id=(None if document.get("boot_id") is None else str(document["boot_id"])),
                    process_start_time=(
                        None
                        if document.get("process_start_time") is None
                        else str(document["process_start_time"])
                    ),
                    control_path=control_path,
                    record_path=record_path,
                )
                if lease.invocation_id != invocation_id:
                    raise ValueError("lease names another invocation")
                self.require_echo(lease)
                return lease
            except FileNotFoundError:
                time.sleep(0.01)
            except ProviderLocalRuntimeRefused as exc:
                if exc.code != "provider_process_lease_echo_failed":
                    raise
                last_echo_error = exc
                time.sleep(0.01)
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise ProviderLocalRuntimeRefused(
                    "provider_process_lease_invalid", "process-lease record is invalid"
                ) from exc
        if last_echo_error is not None:
            raise last_echo_error
        raise ProviderLocalRuntimeRefused(
            "provider_process_lease_missing", "child did not publish a process lease"
        )

    def require_echo(self, lease: ProviderProcessLeaseV1) -> int | None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self.acquisition_timeout_seconds)
                client.connect(str(lease.control_path))
                peer_pid = _socket_peer_pid(client)
                client.sendall(lease.invocation_id.encode("utf-8"))
                client.shutdown(socket.SHUT_WR)
                echoed = client.recv(4096).decode("utf-8")
        except OSError as exc:
            raise ProviderLocalRuntimeRefused(
                "provider_process_lease_echo_failed", "child lease echo is unavailable"
            ) from exc
        if echoed != lease.invocation_id:
            raise ProviderLocalRuntimeRefused(
                "provider_process_lease_echo_mismatch", "child lease echo names another invocation"
            )
        return peer_pid

    def release(self, lease: ProviderProcessLeaseV1) -> None:
        for path in (lease.control_path, lease.record_path):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    def recover_all(self) -> ProviderProcessRecoveryResultV1:
        """Recover each fenced child independently under exact identity proof."""

        recovered: list[str] = []
        removed: list[ProviderProcessLeaseRemovalV1] = []
        could_not_clean: list[ProviderProcessRecoveryFailureV1] = []
        boot_identity_failure: ProviderLocalRuntimeRefused | None = None
        try:
            current_boot_id = _current_boot_id()
        except ProviderLocalRuntimeRefused as exc:
            current_boot_id = None
            boot_identity_failure = exc
        for record_path in sorted(self.root.glob("*.json"), key=lambda item: item.name.encode()):
            invocation_id: str | None = None
            control_path = self.control_root / f"{record_path.stem[:16]}.sock"
            try:
                raw = record_path.read_bytes()
                document = json.loads(raw)
                if canonical_bytes(document) != raw:
                    raise ValueError("lease is not canonical")
                invocation_id = str(document["invocation_id"])
                lease = ProviderProcessLeaseV1(
                    invocation_id=invocation_id,
                    pid=int(document["pid"]),
                    process_group_id=int(document["process_group_id"]),
                    session_id=(
                        None if document.get("session_id") is None else int(document["session_id"])
                    ),
                    boot_id=(None if document.get("boot_id") is None else str(document["boot_id"])),
                    process_start_time=(
                        None
                        if document.get("process_start_time") is None
                        else str(document["process_start_time"])
                    ),
                    control_path=self.paths(invocation_id)[1],
                    record_path=record_path,
                )
                if lease.pid <= 0 or lease.process_group_id <= 0:
                    raise ValueError("lease process identity is invalid")
                authorized_to_signal = False
                try:
                    peer_pid = self.require_echo(lease)
                    authorized_to_signal = peer_pid == lease.pid
                except ProviderLocalRuntimeRefused as exc:
                    if exc.code not in {
                        "provider_process_lease_echo_failed",
                        "provider_process_lease_echo_mismatch",
                    }:
                        raise
                if not authorized_to_signal:
                    if boot_identity_failure is not None:
                        raise boot_identity_failure
                    authorized_to_signal = self._live_identity_matches(
                        lease,
                        current_boot_id=current_boot_id,
                    )
                if authorized_to_signal:
                    self._kill_and_verify(lease)
                    recovered.append(invocation_id)
                else:
                    removed.append(
                        ProviderProcessLeaseRemovalV1(
                            record_name=record_path.name,
                            invocation_id=invocation_id,
                            reason="dead_orphan",
                        )
                    )
                self.release(lease)
            except ProviderLocalRuntimeRefused as exc:
                could_not_clean.append(
                    ProviderProcessRecoveryFailureV1(
                        record_name=record_path.name,
                        invocation_id=invocation_id,
                        code=cast(ProviderProcessFenceCodeV1, exc.code),
                        message=str(exc),
                    )
                )
            except OSError as exc:
                could_not_clean.append(
                    ProviderProcessRecoveryFailureV1(
                        record_name=record_path.name,
                        invocation_id=invocation_id,
                        code="provider_process_lease_invalid",
                        message=(
                            f"provider_process_lease_invalid: process-fence recovery failed: {exc}"
                        ),
                    )
                )
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                # A malformed record cannot prove a process identity. Remove it
                # without starving later valid records.
                for path in (record_path, control_path):
                    with contextlib.suppress(FileNotFoundError, OSError):
                        path.unlink()
                removed.append(
                    ProviderProcessLeaseRemovalV1(
                        record_name=record_path.name,
                        invocation_id=None,
                        reason="malformed",
                    )
                )
        return ProviderProcessRecoveryResultV1(
            recovered=tuple(sorted(recovered, key=str.encode)),
            removed=tuple(removed),
            could_not_clean=tuple(could_not_clean),
        )

    @staticmethod
    def _live_identity_matches(
        lease: ProviderProcessLeaseV1,
        *,
        current_boot_id: str | None | object = _BOOT_ID_UNSET,
    ) -> bool:
        """Authorize recovery signalling only for the published OS identity."""

        if lease.boot_id is None or lease.process_start_time is None or lease.session_id is None:
            return False
        if current_boot_id is _BOOT_ID_UNSET:
            try:
                current_boot_id = _current_boot_id()
            except ProviderLocalRuntimeRefused:
                return False
        if current_boot_id is None:
            return False
        try:
            return (
                current_boot_id == lease.boot_id
                and _process_start_time(lease.pid) == lease.process_start_time
                and os.getpgid(lease.pid) == lease.process_group_id
                and os.getsid(lease.pid) == lease.session_id
            )
        except (OSError, ProviderLocalRuntimeRefused):
            return False

    def _kill_and_verify(self, lease: ProviderProcessLeaseV1) -> None:
        descendants = descendant_processes(lease.pid)
        group_alive = True
        try:
            os.killpg(lease.process_group_id, 0)
        except ProcessLookupError:
            group_alive = False
        except PermissionError:
            # Lack of permission is not proof of death; SIGKILL will either
            # succeed or leave the record fenced for another recovery attempt.
            pass
        if group_alive:
            try:
                os.killpg(lease.process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                group_alive = False
            except OSError as exc:
                raise ProviderLocalRuntimeRefused(
                    "provider_process_group_survived_recovery",
                    "process group could not be terminated during recovery",
                ) from exc
        kill_descendants(descendants)
        deadline = time.monotonic() + self.recovery_timeout_seconds
        while time.monotonic() < deadline:
            try:
                os.waitpid(lease.pid, os.WNOHANG)
            except ChildProcessError:
                pass
            if group_alive:
                try:
                    os.killpg(lease.process_group_id, 0)
                except ProcessLookupError:
                    group_alive = False
                except PermissionError:
                    pass
            if not group_alive and not any(descendant_is_live(item) for item in descendants):
                return
            time.sleep(0.01)
        raise ProviderLocalRuntimeRefused(
            "provider_process_group_survived_recovery",
            "process group survived the configured recovery deadline",
        )


__all__ = [
    "DEFAULT_PROVIDER_LEASE_ACQUISITION_TIMEOUT_SECONDS",
    "DEFAULT_PROVIDER_LEASE_RECOVERY_TIMEOUT_SECONDS",
    "DEFAULT_PROVIDER_SECRET_WRITER_JOIN_TIMEOUT_SECONDS",
    "DEFAULT_PROVIDER_STDIN_WRITER_JOIN_TIMEOUT_SECONDS",
    "DEFAULT_PROVIDER_DESCENDANT_TRACKER_JOIN_TIMEOUT_SECONDS",
    "DEFAULT_PROVIDER_DESCENDANT_TRACKER_POLL_INTERVAL_SECONDS",
    "DEFAULT_PROVIDER_PROCESS_GROUP_TERMINATION_TIMEOUT_SECONDS",
    "ProviderDescendantProcessV1",
    "ProviderLocalRuntimeRefused",
    "ProviderProcessLeaseRemovalV1",
    "ProviderProcessLeaseStore",
    "ProviderProcessLeaseV1",
    "ProviderProcessRecoveryFailureV1",
    "ProviderProcessRecoveryResultV1",
    "ProviderProcessFenceCodeV1",
    "descendant_is_live",
    "descendant_processes",
    "kill_descendants",
    "processes_naming_invocation",
    "snapshot_provider_descendants",
]
