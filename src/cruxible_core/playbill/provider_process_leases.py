"""Operational Provider child leases guarded by a child-owned echo endpoint."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import signal
import socket
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import PlaybillExecutionError


class ProviderLocalRuntimeRefused(PlaybillExecutionError):
    """Typed daemon-local refusal translated into a Provider completion."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ProviderProcessLeaseV1:
    """Process fence whose acquisition and recovery deadlines read VALIDITY WINDOW."""

    invocation_id: str
    pid: int
    process_group_id: int
    control_path: Path
    record_path: Path


class ProviderProcessLeaseStore:
    """A rebuildable 0700 process fence; its records are never governed state."""

    def __init__(
        self,
        root: Path,
        *,
        acquisition_timeout_seconds: float = 5.0,
        recovery_timeout_seconds: float = 5.0,
    ) -> None:
        self.root = root
        self.acquisition_timeout_seconds = acquisition_timeout_seconds
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self._ensure_private_directory(root)
        # Control sockets are operational state and must share the daemon's
        # protected state-root boundary.  The short component also preserves
        # headroom under the platform AF_UNIX path limit.
        self.control_root = root / "c"
        self._ensure_private_directory(self.control_root)
        # Darwin's AF_UNIX limit can be shorter than a legitimate state-root
        # path.  A daemon-created, unguessable 0700 alias keeps the socket inode
        # in ``control_root`` while presenting the kernel a short pathname.
        alias_parent = Path(tempfile.mkdtemp(prefix="cruxible-pf-", dir="/tmp"))
        os.chmod(alias_parent, 0o700)
        alias = alias_parent / "c"
        alias.symlink_to(self.control_root.resolve(), target_is_directory=True)
        self._control_alias_parent = alias_parent
        self._control_alias = alias

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
        return self.root / f"{stem}.json", self._control_alias / f"{stem[:16]}.sock"

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
        record_path, _control_path = self.paths(invocation_id)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=record_path.name + ".tmp-",
            dir=self.root,
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(
                    canonical_bytes(
                        {
                            "invocation_id": invocation_id,
                            "pid": pid,
                            "process_group_id": process_group_id,
                        }
                    )
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, record_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
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

    @staticmethod
    def require_echo(lease: ProviderProcessLeaseV1) -> None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(1.0)
                client.connect(str(lease.control_path))
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

    def release(self, lease: ProviderProcessLeaseV1) -> None:
        for path in (lease.record_path, lease.control_path):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    def recover_all(self) -> tuple[str, ...]:
        """Kill fenced children within a VALIDITY WINDOW recovery deadline."""

        recovered: list[str] = []
        for record_path in sorted(self.root.glob("*.json"), key=lambda item: item.name.encode()):
            invocation_id = record_path.stem
            control_path = self._control_alias / f"{record_path.stem[:16]}.sock"
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
                    control_path=self.paths(invocation_id)[1],
                    record_path=record_path,
                )
                if lease.pid <= 0 or lease.process_group_id <= 0:
                    raise ValueError("lease process identity is invalid")
                # Echo is useful evidence for a live child, but its absence is
                # the ordinary crash/reboot state recovery exists to clean.
                try:
                    self.require_echo(lease)
                except ProviderLocalRuntimeRefused:
                    pass
                self._kill_and_verify(lease)
                recovered.append(invocation_id)
                self.release(lease)
            except ProviderLocalRuntimeRefused:
                # A survivor is the only per-record condition that cannot be
                # safely discarded: keep its fence for the next recovery pass.
                raise
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                # A malformed record cannot prove a process identity. Remove it
                # without starving later valid records.
                for path in (record_path, control_path):
                    with contextlib.suppress(FileNotFoundError, OSError):
                        path.unlink()
                recovered.append(invocation_id)
        return tuple(recovered)

    def _kill_and_verify(self, lease: ProviderProcessLeaseV1) -> None:
        try:
            os.killpg(lease.process_group_id, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            # Lack of permission is not proof of death; SIGKILL will either
            # succeed or leave the record fenced for another recovery attempt.
            pass
        try:
            os.killpg(lease.process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError as exc:
            raise ProviderLocalRuntimeRefused(
                "provider_process_group_survived_recovery",
                "process group could not be terminated during recovery",
            ) from exc
        deadline = time.monotonic() + self.recovery_timeout_seconds
        while time.monotonic() < deadline:
            try:
                waited, _status = os.waitpid(lease.pid, os.WNOHANG)
                if waited == lease.pid:
                    return
            except ChildProcessError:
                pass
            try:
                os.killpg(lease.process_group_id, 0)
            except ProcessLookupError:
                return
            except PermissionError:
                pass
            time.sleep(0.01)
        raise ProviderLocalRuntimeRefused(
            "provider_process_group_survived_recovery",
            "process group survived the configured recovery deadline",
        )


__all__ = [
    "ProviderLocalRuntimeRefused",
    "ProviderProcessLeaseStore",
    "ProviderProcessLeaseV1",
]
