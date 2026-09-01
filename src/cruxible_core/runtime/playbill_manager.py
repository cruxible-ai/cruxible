"""Persistent loader for opt-in Playbill substrates attached to governed instances."""

from __future__ import annotations

import os
import threading
from pathlib import Path

from pydantic import ValidationError

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import (
    PlaybillBootstrapError,
    PlaybillFormatError,
    PlaybillReseedRequired,
)
from cruxible_client.contracts.types import OperatingProfile, PlaybillTrustRoot, PrincipalRecord
from cruxible_core.errors import InstanceNotFoundError
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.workspace_advertisement import (
    advertise_workspace_refs,
    workspace_git_object_format,
)
from cruxible_core.server.registry import GOVERNED_DAEMON_BACKEND, get_registry


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
        registry = get_registry()
        record = registry.get(instance_id)
        if record is None or record.backend != GOVERNED_DAEMON_BACKEND:
            raise InstanceNotFoundError(instance_id)
        managed_root = Path(record.location).resolve(strict=False)
        legacy_root = managed_root / ".cruxible"
        if (legacy_root / "playbill-v1").exists() or (
            legacy_root / "playbill-trust-root-v1.json"
        ).exists():
            raise PlaybillReseedRequired()
        trust_root = registry.state_root / "trust" / f"{instance_id}.json"
        if managed_root.exists() != trust_root.exists():
            raise PlaybillReseedRequired()
        workspaces = (
            (Path(record.workspace_root).resolve(strict=False),)
            if record.workspace_root is not None
            else ()
        )
        return managed_root, trust_root, workspaces

    @staticmethod
    def _bind_workspace(
        instance: PlaybillInstance,
        workspaces: tuple[Path, ...],
    ) -> None:
        workspace = workspaces[0] if workspaces else None
        instance.bind_workspace_advertiser(
            lambda: advertise_workspace_refs(
                workspace_root=workspace,
                ledger_path=instance.root / instance.descriptor.storage.ledger,
                ledger_object_format=instance.descriptor.git_object_format,
            )
        )

    def initialize(
        self,
        instance_id: str,
        *,
        client_principals: tuple[PrincipalRecord, ...],
        operating_profile: OperatingProfile = "local",
        require_independent_approval: bool = False,
    ) -> PlaybillInstance:
        managed_root, trust_path, workspaces = self._paths(instance_id)
        with self._lock:
            if managed_root.exists() or trust_path.exists():
                instance = self.get(instance_id)
                actual_clients = tuple(
                    sorted(
                        (
                            principal
                            for principal in instance.trust_root.principals
                            if principal.kind != "daemon"
                        ),
                        key=lambda item: item.principal_id.encode("utf-8"),
                    )
                )
                requested_clients = tuple(
                    sorted(
                        client_principals,
                        key=lambda item: item.principal_id.encode("utf-8"),
                    )
                )
                expected_policy = (
                    "independent_approval_required"
                    if require_independent_approval
                    else "self_approval_allowed"
                )
                if (
                    actual_clients != requested_clients
                    or instance.descriptor.operating_profile != operating_profile
                    or instance.inspect().approval_policy_mode != expected_policy
                ):
                    raise PlaybillBootstrapError(
                        "Playbill is already initialized with a different principal set, "
                        "operating profile, or bootstrap approval policy"
                    )
                return instance
            try:
                object_format = (
                    workspace_git_object_format(workspaces[0]) if workspaces else "sha256"
                )
            except ValueError as exc:
                raise PlaybillBootstrapError(
                    "attached workspace must be one exact local Git worktree"
                ) from exc
            instance = PlaybillInstance.initialize(
                managed_root,
                instance_id=instance_id,
                client_principals=client_principals,
                workspace_roots=workspaces,
                git_object_format=object_format,
                operating_profile=operating_profile,
                require_independent_approval=require_independent_approval,
            )
            trust_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(trust_path.parent, 0o700)
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
            self._bind_workspace(instance, workspaces)
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
            self._bind_workspace(instance, _workspaces)
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
