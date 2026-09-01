"""Operational Provider child leases guarded by a child-owned echo endpoint."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import signal
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import PlaybillExecutionError


@dataclass(frozen=True)
class ProviderProcessLeaseV1:
    invocation_id: str
    pid: int
    process_group_id: int
    control_path: Path
    record_path: Path


class ProviderProcessLeaseStore:
    """A rebuildable 0700 process fence; its records are never governed state."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        # macOS caps AF_UNIX paths at roughly 104 bytes. `/tmp` is intentionally
        # kept un-resolved here so the kernel sees the short spelling.
        self.control_root = Path("/tmp") / f"cruxible-provider-{os.getuid()}"
        self.control_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.control_root, 0o700)

    def paths(self, invocation_id: str) -> tuple[Path, Path]:
        stem = hashlib.sha256(
            (str(self.root.resolve()) + "\x00" + invocation_id).encode("utf-8")
        ).hexdigest()[:32]
        return self.root / f"{stem}.json", self.control_root / f"{stem}.sock"

    def require(
        self,
        invocation_id: str,
        *,
        timeout_seconds: float = 5.0,
    ) -> ProviderProcessLeaseV1:
        record_path, control_path = self.paths(invocation_id)
        deadline = time.monotonic() + timeout_seconds
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
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise PlaybillExecutionError("provider_process_lease_invalid") from exc
        raise PlaybillExecutionError("provider_process_lease_missing")

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
            raise PlaybillExecutionError("provider_process_lease_echo_failed") from exc
        if echoed != lease.invocation_id:
            raise PlaybillExecutionError("provider_process_lease_echo_mismatch")

    def release(self, lease: ProviderProcessLeaseV1) -> None:
        for path in (lease.record_path, lease.control_path):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    def recover_all(self) -> tuple[str, ...]:
        recovered: list[str] = []
        for record_path in sorted(self.root.glob("*.json"), key=lambda item: item.name.encode()):
            try:
                document = json.loads(record_path.read_bytes())
                invocation_id = str(document["invocation_id"])
                lease = self.require(invocation_id, timeout_seconds=0.1)
                os.killpg(lease.process_group_id, signal.SIGKILL)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        waited, _status = os.waitpid(lease.pid, os.WNOHANG)
                        if waited == lease.pid:
                            break
                    except ChildProcessError:
                        pass
                    try:
                        os.killpg(lease.process_group_id, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.01)
                else:
                    raise PlaybillExecutionError("provider_process_group_survived_recovery")
                recovered.append(invocation_id)
                self.release(lease)
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise PlaybillExecutionError("provider_process_lease_invalid") from exc
        return tuple(recovered)


__all__ = ["ProviderProcessLeaseStore", "ProviderProcessLeaseV1"]
