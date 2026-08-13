"""Persistent loader for opt-in Playbill substrates attached to governed instances."""

from __future__ import annotations

import os
import threading
from pathlib import Path

from pydantic import ValidationError

from cruxible_core.errors import InstanceNotFoundError
from cruxible_core.playbill.canonical import canonical_bytes
from cruxible_core.playbill.errors import PlaybillBootstrapError, PlaybillFormatError
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.types import OperatingProfile, PlaybillTrustRoot, PrincipalRecord
from cruxible_core.server.registry import GOVERNED_DAEMON_BACKEND, get_registry

_PLAYBILL_DIRECTORY = "playbill-v1"
_TRUST_ROOT_FILE = "playbill-trust-root-v1.json"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class PlaybillInstanceManager:
    """Open Playbill only from a registry-owned root and pinned trust-root file."""

    def __init__(self) -> None:
        self._instances: dict[str, PlaybillInstance] = {}
        self._lock = threading.RLock()

    def _paths(self, instance_id: str) -> tuple[Path, Path, tuple[Path, ...]]:
        record = get_registry().get(instance_id)
        if record is None or record.backend != GOVERNED_DAEMON_BACKEND:
            raise InstanceNotFoundError(instance_id)
        legacy_managed = Path(record.location).resolve(strict=False) / ".cruxible"
        managed_root = legacy_managed / _PLAYBILL_DIRECTORY
        trust_root = legacy_managed / _TRUST_ROOT_FILE
        workspaces = (
            (Path(record.workspace_root).resolve(strict=False),)
            if record.workspace_root is not None
            else ()
        )
        return managed_root, trust_root, workspaces

    def initialize(
        self,
        instance_id: str,
        *,
        client_principals: tuple[PrincipalRecord, ...],
        operating_profile: OperatingProfile = "local",
    ) -> PlaybillInstance:
        managed_root, trust_path, workspaces = self._paths(instance_id)
        with self._lock:
            if managed_root.exists() or trust_path.exists():
                raise PlaybillBootstrapError("Playbill is already initialized for this instance")
            instance = PlaybillInstance.initialize(
                managed_root,
                instance_id=instance_id,
                client_principals=client_principals,
                workspace_roots=workspaces,
                operating_profile=operating_profile,
            )
            trust_path.parent.mkdir(parents=True, exist_ok=True)
            payload = canonical_bytes(instance.trust_root.model_dump(mode="json")) + b"\n"
            descriptor: int | None = None
            try:
                descriptor = os.open(
                    trust_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:  # pragma: no cover - defensive OS contract
                        raise PlaybillBootstrapError("trust-root write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            except OSError as exc:
                raise PlaybillBootstrapError("failed to persist Playbill trust root") from exc
            finally:
                if descriptor is not None:
                    os.close(descriptor)
            os.chmod(trust_path, 0o600)
            _fsync_directory(trust_path.parent)
            self._instances[instance_id] = instance
            return instance

    def get(self, instance_id: str) -> PlaybillInstance:
        with self._lock:
            known = self._instances.get(instance_id)
            if known is not None:
                return known
            managed_root, trust_path, _workspaces = self._paths(instance_id)
            if trust_path.is_symlink() or not trust_path.is_file() or not managed_root.is_dir():
                raise PlaybillBootstrapError("Playbill is not initialized for this instance")
            try:
                raw = trust_path.read_bytes()
                trust = PlaybillTrustRoot.model_validate_json(raw)
            except (OSError, ValidationError, ValueError) as exc:
                raise PlaybillFormatError("persisted Playbill trust root is malformed") from exc
            if canonical_bytes(trust.model_dump(mode="json")) + b"\n" != raw:
                raise PlaybillFormatError("persisted Playbill trust root is not canonical")
            instance = PlaybillInstance.open(managed_root, trust_root=trust)
            self._instances[instance_id] = instance
            return instance

    def register(self, instance_id: str, instance: PlaybillInstance) -> None:
        """Testing/embedded seam; production instances load through pinned storage."""

        with self._lock:
            self._instances[instance_id] = instance

    def clear(self) -> None:
        with self._lock:
            self._instances.clear()


_manager = PlaybillInstanceManager()


def get_playbill_manager() -> PlaybillInstanceManager:
    return _manager


__all__ = ["PlaybillInstanceManager", "get_playbill_manager"]
