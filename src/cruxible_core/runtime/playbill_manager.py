"""Persistent loader for opt-in Playbill substrates attached to governed instances."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import structlog
from pydantic import ValidationError

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import (
    PlaybillBootstrapError,
    PlaybillFormatError,
    PlaybillReseedRequired,
)
from cruxible_client.contracts.temporal import utc_now
from cruxible_client.contracts.types import OperatingProfile, PlaybillTrustRoot, PrincipalRecord
from cruxible_core.errors import InstanceNotFoundError
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.provider_process_leases import ProviderProcessRecoveryResultV1
from cruxible_core.playbill.workspace_advertisement import (
    advertise_workspace_refs,
    workspace_git_object_format,
)
from cruxible_core.playbill.workspace_file import WorkspaceFileReader
from cruxible_core.runtime.provider_runtime import (
    ProviderRecoveryFoldDisposition,
    ProviderRuntimeOperator,
)
from cruxible_core.server.config import get_server_state_root
from cruxible_core.server.registry import GOVERNED_DAEMON_BACKEND, get_registry

_log = structlog.get_logger("cruxible.provider_runtime")


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
        self._provider_runtime_operators: dict[Path, ProviderRuntimeOperator] = {}
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
            self._provider_runtime_operators.clear()

    def provider_runtime_operator(self) -> ProviderRuntimeOperator:
        """Return the daemon-local Provider operator for the active state root."""

        state_root = get_server_state_root()
        with self._lock:
            known = self._provider_runtime_operators.get(state_root)
            if known is None:
                try:
                    known = ProviderRuntimeOperator(state_root)
                except Exception as exc:
                    known = ProviderRuntimeOperator.degraded(
                        state_root,
                        message=f"Provider runtime construction failed: {exc}",
                    )
                self._bind_provider_runtime_recovery_fold(known)
                self._provider_runtime_operators[state_root] = known
            return known

    def workspace_file_reader(self, instance_id: str) -> WorkspaceFileReader:
        """Bind workspace reads to registry attachment plus daemon operational config."""

        managed_root, trust_path, attached_roots = self._paths(instance_id)
        instance = self.get(instance_id)
        operator = self.provider_runtime_operator()
        return WorkspaceFileReader(
            instance_id=instance_id,
            operating_profile=instance.descriptor.operating_profile,
            attached_roots=attached_roots,
            operational_allowed_roots=tuple(
                Path(item) for item in operator.config.workspace_allowed_roots
            ),
            # The state root contains every daemon config, trust, custody, and
            # instance substrate. Explicit roots cover restored layouts too.
            managed_roots=(get_server_state_root(), managed_root, trust_path.parent),
        )

    def cached_provider_runtime_operator(self) -> ProviderRuntimeOperator:
        """Return the already-cached operator without retrying construction."""

        state_root = get_server_state_root()
        with self._lock:
            known = self._provider_runtime_operators.get(state_root)
            if known is None:
                known = ProviderRuntimeOperator.degraded(
                    state_root,
                    message="Provider runtime construction failed before caching",
                )
                self._bind_provider_runtime_recovery_fold(known)
                self._provider_runtime_operators[state_root] = known
            return known

    def _bind_provider_runtime_recovery_fold(
        self,
        operator: ProviderRuntimeOperator,
    ) -> None:
        """Give one operator the manager-owned governed-journal fold."""

        def fold(
            result: ProviderProcessRecoveryResultV1,
        ) -> dict[str, ProviderRecoveryFoldDisposition]:
            return self._fold_provider_recovery(operator, result)

        operator.bind_recovery_fold(fold)

    def recover_provider_runtime(self) -> ProviderProcessRecoveryResultV1:
        """Recover process fences before the daemon accepts requests."""

        operator = self.provider_runtime_operator()
        try:
            result = operator.recover_all_with_bound_fold()
        except Exception as exc:
            operator.mark_unavailable(
                "provider_runtime_recovery_failed",
                f"Provider process recovery failed: {exc}",
                retryable=True,
            )
            return ProviderProcessRecoveryResultV1(
                recovered=(),
                removed=(),
                could_not_clean=(),
            )
        return result

    def _fold_provider_recovery(
        self,
        operator: ProviderRuntimeOperator,
        result: ProviderProcessRecoveryResultV1,
    ) -> dict[str, ProviderRecoveryFoldDisposition]:
        """Fold and acknowledge each recovered invocation independently."""

        invocation_ids = result.completion_invocation_ids
        recovery_failures = {
            item.invocation_id: item.code
            for item in result.could_not_clean
            if item.invocation_id is not None
        }
        if not invocation_ids and not recovery_failures:
            return {}
        from cruxible_core.service.playbill_procedure_runs import (
            ProcedureRunRecoveryRequired,
            service_recover_provider_invocations,
        )

        try:
            records = get_registry().list_instances()
        except Exception as exc:
            operator.mark_unavailable(
                "provider_runtime_recovery_failed",
                f"Provider instance enumeration failed: {exc}",
                retryable=True,
            )
            return {
                invocation_id: "fold_failed"
                for invocation_id in sorted({*invocation_ids, *recovery_failures}, key=str.encode)
            }
        handled_invocation_ids: set[str] = set()
        initialized_fold_failed = False
        for record in records:
            if record.backend != GOVERNED_DAEMON_BACKEND:
                continue
            try:
                instance = self.get(record.instance_id)
            except (PlaybillBootstrapError, PlaybillReseedRequired, InstanceNotFoundError) as exc:
                _log.warning(
                    "provider_recovery_instance_skipped",
                    instance_id=record.instance_id,
                    reason=str(exc),
                )
                continue
            except Exception as exc:
                initialized_fold_failed = True
                operator.mark_unavailable(
                    "provider_runtime_recovery_failed",
                    f"Provider instance load failed for {record.instance_id}: {exc}",
                    retryable=True,
                )
                continue
            try:
                handled_invocation_ids.update(
                    service_recover_provider_invocations(
                        instance,
                        invocation_ids=invocation_ids,
                        recovery_failure_codes=recovery_failures,
                        recorded_at=utc_now(),
                    )
                )
            except ProcedureRunRecoveryRequired as exc:
                initialized_fold_failed = True
                operator.mark_unavailable(
                    "provider_runtime_recovery_failed",
                    str(exc),
                    retryable=True,
                )
            except Exception as exc:
                initialized_fold_failed = True
                operator.mark_unavailable(
                    "provider_runtime_recovery_failed",
                    f"Provider journal recovery failed for {record.instance_id}: {exc}",
                    retryable=True,
                )
        dispositions: dict[str, ProviderRecoveryFoldDisposition] = {}
        for invocation_id in sorted({*invocation_ids, *recovery_failures}, key=str.encode):
            if invocation_id in recovery_failures:
                dispositions[invocation_id] = "fold_failed"
            elif invocation_id in handled_invocation_ids:
                dispositions[invocation_id] = "handled"
            elif initialized_fold_failed:
                dispositions[invocation_id] = "fold_failed"
            else:
                dispositions[invocation_id] = "unclaimed"

        for invocation_id, disposition in tuple(dispositions.items()):
            if disposition == "fold_failed":
                continue
            if disposition == "unclaimed":
                _log.warning(
                    "provider_recovery_unclaimed",
                    invocation_id=invocation_id,
                    terminal=True,
                )
            try:
                operator.acknowledge_recovery({invocation_id: disposition})
            except Exception as exc:
                dispositions[invocation_id] = "fold_failed"
                operator.mark_unavailable(
                    "provider_runtime_recovery_failed",
                    f"Provider recovery acknowledgement failed for {invocation_id}: {exc}",
                    retryable=True,
                )
        return dispositions


_manager = PlaybillInstanceManager()


def get_playbill_manager() -> PlaybillInstanceManager:
    return _manager


__all__ = ["PlaybillInstanceManager", "get_playbill_manager"]
