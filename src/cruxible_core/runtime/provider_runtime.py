"""Daemon-owned operational construction for the local Provider runtime."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from cruxible_client.contracts import ProviderLaneUnavailableCodeV1
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistrationV1,
    parse_provider_interface,
    provider_interface_digest,
)
from cruxible_client.contracts.providers import (
    AcceptedProviderV1,
    parse_provider,
    provider_digest,
)
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.provider_local_runtime import (
    EnvironmentProviderSecretResolver,
    FileProviderSecretStore,
    LocalProviderDeploymentV1,
    LocalProviderExecutionDriver,
    ProviderLocalRuntimeInvoker,
    ProviderSecretResolverRegistry,
)
from cruxible_core.playbill.provider_process_leases import (
    DEFAULT_PROVIDER_DESCENDANT_TRACKER_JOIN_TIMEOUT_SECONDS,
    DEFAULT_PROVIDER_DESCENDANT_TRACKER_POLL_INTERVAL_SECONDS,
    DEFAULT_PROVIDER_LEASE_ACQUISITION_TIMEOUT_SECONDS,
    DEFAULT_PROVIDER_LEASE_RECOVERY_TIMEOUT_SECONDS,
    DEFAULT_PROVIDER_PROCESS_GROUP_TERMINATION_TIMEOUT_SECONDS,
    DEFAULT_PROVIDER_REARM_BACKOFF_SECONDS,
    DEFAULT_PROVIDER_RECOVERY_AGGREGATE_TIMEOUT_SECONDS,
    DEFAULT_PROVIDER_SECRET_WRITER_JOIN_TIMEOUT_SECONDS,
    DEFAULT_PROVIDER_STDIN_WRITER_JOIN_TIMEOUT_SECONDS,
    ProviderLocalRuntimeRefused,
    ProviderProcessFenceCodeV1,
    ProviderProcessLeaseStore,
    ProviderProcessRecoveryFailureV1,
    ProviderProcessRecoveryResultV1,
)

PROVIDER_RUNTIME_CONFIG_PATH = Path("daemon/provider-runtime.json")
ProviderRecoveryFoldDisposition = Literal["handled", "unclaimed", "fold_failed"]
_ConstructionStage = Literal[
    "state root",
    "operational config",
    "process lease store",
    "secret store",
    "deployment",
]
_FILESYSTEM_CONSTRUCTION_STAGES: tuple[_ConstructionStage, ...] = (
    "operational config",
    "process lease store",
    "secret store",
    "deployment",
)


class _StrictOperationalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderDeploymentConfigV1(_StrictOperationalModel):
    tag: Literal["cruxible-provider-deployment-config-v1"] = (
        "cruxible-provider-deployment-config-v1"
    )
    deployment_digest: str
    distribution_path: str
    lock_path: str
    environment_path: str
    environment_manifest_path: str
    environment_pin_key: str
    interpreter_path: str
    provider_runtime_version: str

    @field_validator(
        "distribution_path",
        "lock_path",
        "environment_path",
        "environment_manifest_path",
        "interpreter_path",
    )
    @classmethod
    def _relative_path(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or value in {"", ".", ".."} or ".." in path.parts:
            raise ValueError("Provider deployment paths must be state-root-relative")
        return value


class ProviderRuntimeOperationalConfigV1(_StrictOperationalModel):
    tag: Literal["cruxible-provider-runtime-operational-config-v1"] = (
        "cruxible-provider-runtime-operational-config-v1"
    )
    lease_acquisition_timeout_seconds: float = Field(
        default=DEFAULT_PROVIDER_LEASE_ACQUISITION_TIMEOUT_SECONDS,
        gt=0,
    )
    lease_recovery_timeout_seconds: float = Field(
        default=DEFAULT_PROVIDER_LEASE_RECOVERY_TIMEOUT_SECONDS,
        gt=0,
    )
    recovery_aggregate_timeout_seconds: float = Field(
        default=DEFAULT_PROVIDER_RECOVERY_AGGREGATE_TIMEOUT_SECONDS,
        gt=0,
        description=(
            "VALIDITY WINDOW: maximum elapsed duration of one process-fence recovery scan."
        ),
    )
    rearm_backoff_seconds: float = Field(
        default=DEFAULT_PROVIDER_REARM_BACKOFF_SECONDS,
        gt=0,
        description="VALIDITY WINDOW: minimum interval between lazy recovery attempts.",
    )
    secret_writer_join_timeout_seconds: float = Field(
        default=DEFAULT_PROVIDER_SECRET_WRITER_JOIN_TIMEOUT_SECONDS,
        gt=0,
    )
    stdin_writer_join_timeout_seconds: float = Field(
        default=DEFAULT_PROVIDER_STDIN_WRITER_JOIN_TIMEOUT_SECONDS,
        gt=0,
    )
    descendant_tracker_join_timeout_seconds: float = Field(
        default=DEFAULT_PROVIDER_DESCENDANT_TRACKER_JOIN_TIMEOUT_SECONDS,
        gt=0,
    )
    descendant_tracker_poll_interval_seconds: float = Field(
        default=DEFAULT_PROVIDER_DESCENDANT_TRACKER_POLL_INTERVAL_SECONDS,
        gt=0,
    )
    process_group_termination_timeout_seconds: float = Field(
        default=DEFAULT_PROVIDER_PROCESS_GROUP_TERMINATION_TIMEOUT_SECONDS,
        gt=0,
    )
    deployments: tuple[ProviderDeploymentConfigV1, ...] = ()

    @field_validator("deployments")
    @classmethod
    def _deployments(
        cls, value: tuple[ProviderDeploymentConfigV1, ...]
    ) -> tuple[ProviderDeploymentConfigV1, ...]:
        expected = tuple(sorted(value, key=lambda item: item.deployment_digest.encode("ascii")))
        digests = tuple(item.deployment_digest for item in value)
        if value != expected or len(digests) != len(set(digests)):
            raise ValueError("Provider deployments must be digest-sorted and unique")
        return value


class ProviderRuntimeOperator:
    """One daemon process's local custody, leases, and installed deployments."""

    def __init__(self, state_root: Path) -> None:
        self._initialize_degraded(state_root)
        if not self._initialize_state_root():
            self._pending_construction_stages.update(_FILESYSTEM_CONSTRUCTION_STAGES)
            return
        self._initialize_filesystem_components()

    @classmethod
    def degraded(cls, state_root: Path, *, message: str) -> ProviderRuntimeOperator:
        """Build a non-filesystem fallback when manager construction itself fails."""

        operator = cls.__new__(cls)
        operator._initialize_degraded(state_root)
        operator.mark_unavailable("provider_runtime_recovery_failed", message, retryable=True)
        return operator

    def _initialize_degraded(self, state_root: Path) -> None:
        """Initialize every field needed by refusal and status paths without filesystem I/O."""

        self.state_root = state_root
        self._lock = threading.RLock()
        self._in_flight = 0
        self._rearm_required = False
        self._next_rearm_after = 0.0
        self._recovery_fold: (
            Callable[
                [ProviderProcessRecoveryResultV1],
                Mapping[str, ProviderRecoveryFoldDisposition],
            ]
            | None
        ) = None
        self._construction_failures: dict[str, tuple[ProviderLaneUnavailableCodeV1, str]] = {}
        self._recovery_failures: dict[ProviderLaneUnavailableCodeV1, str] = {}
        self._pending_construction_stages: set[_ConstructionStage] = set()
        self._latest_failure: tuple[Literal["construction", "recovery"], str] | None = None
        self._unavailable_failure_count = 0
        self.unavailable_code: ProviderLaneUnavailableCodeV1 | None = None
        self.unavailable_reason: str | None = None
        self._lane_status_snapshot: tuple[
            Literal["available", "unavailable"],
            ProviderLaneUnavailableCodeV1 | None,
            str | None,
        ] = ("available", None, None)
        self.config = ProviderRuntimeOperationalConfigV1()
        self.process_leases: ProviderProcessLeaseStore | None = None
        self.secret_store: FileProviderSecretStore | None = None
        self.secret_resolvers = ProviderSecretResolverRegistry(
            (EnvironmentProviderSecretResolver(),)
        )
        self.driver = LocalProviderExecutionDriver()
        self.deployments: dict[str, LocalProviderDeploymentV1] = {}

    def _initialize_state_root(self) -> bool:
        try:
            self.state_root = self.state_root.resolve()
            self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        except (OSError, ProviderLocalRuntimeRefused) as exc:
            self._mark_construction_failure("state root", exc)
            return False
        self._clear_construction_failure("state root")
        return True

    def _initialize_filesystem_components(
        self,
        stages: set[_ConstructionStage] | None = None,
    ) -> None:
        selected = set(_FILESYSTEM_CONSTRUCTION_STAGES) if stages is None else set(stages)
        if "operational config" in selected:
            try:
                self.config = self._load_config()
            except (OSError, ProviderLocalRuntimeRefused) as exc:
                self._mark_construction_failure("operational config", exc)
            else:
                self._clear_construction_failure("operational config")
        if "process lease store" in selected:
            try:
                process_leases = ProviderProcessLeaseStore(
                    self.state_root / "daemon" / "provider-process-leases",
                    control_root=self.state_root / "c",
                    diagnostic_sink=lambda code, message: self.mark_unavailable(
                        code, message, retryable=True
                    ),
                    acquisition_timeout_seconds=self.config.lease_acquisition_timeout_seconds,
                    recovery_timeout_seconds=self.config.lease_recovery_timeout_seconds,
                    recovery_aggregate_timeout_seconds=(
                        self.config.recovery_aggregate_timeout_seconds
                    ),
                    secret_writer_join_timeout_seconds=(
                        self.config.secret_writer_join_timeout_seconds
                    ),
                    stdin_writer_join_timeout_seconds=(
                        self.config.stdin_writer_join_timeout_seconds
                    ),
                    descendant_tracker_join_timeout_seconds=(
                        self.config.descendant_tracker_join_timeout_seconds
                    ),
                    descendant_tracker_poll_interval_seconds=(
                        self.config.descendant_tracker_poll_interval_seconds
                    ),
                    process_group_termination_timeout_seconds=(
                        self.config.process_group_termination_timeout_seconds
                    ),
                )
                process_leases.paths("sha256:" + "0" * 64)
                self.process_leases = process_leases
            except (OSError, ProviderLocalRuntimeRefused) as exc:
                self._mark_construction_failure("process lease store", exc)
            else:
                self._clear_construction_failure("process lease store")
        if "secret store" in selected:
            try:
                secret_store = FileProviderSecretStore(
                    self.state_root / "daemon" / "provider-secrets"
                )
                self.secret_store = secret_store
                self.secret_resolvers = ProviderSecretResolverRegistry(
                    (EnvironmentProviderSecretResolver(), secret_store)
                )
            except (OSError, ProviderLocalRuntimeRefused) as exc:
                self._mark_construction_failure("secret store", exc)
            else:
                self._clear_construction_failure("secret store")
        if "deployment" in selected:
            try:
                self.deployments = {
                    item.deployment_digest: self._deployment(item)
                    for item in self.config.deployments
                }
            except (OSError, ProviderLocalRuntimeRefused) as exc:
                self._mark_construction_failure("deployment", exc)
            else:
                self._clear_construction_failure("deployment")

    def _reinitialize_failed_construction_stages_locked(self) -> None:
        stages = {
            cast(_ConstructionStage, stage)
            for stage in self._construction_failures
            if stage in {"state root", *_FILESYSTEM_CONSTRUCTION_STAGES}
        }
        stages.update(self._pending_construction_stages)
        if "state root" in stages:
            if not self._initialize_state_root():
                return
            stages.remove("state root")
        filesystem_stages = stages & set(_FILESYSTEM_CONSTRUCTION_STAGES)
        if filesystem_stages:
            self._initialize_filesystem_components(filesystem_stages)

    def _mark_construction_failure(
        self,
        component: str,
        exc: OSError | ProviderLocalRuntimeRefused,
    ) -> None:
        code = (
            cast(ProviderProcessFenceCodeV1, exc.code)
            if isinstance(exc, ProviderLocalRuntimeRefused)
            else "provider_process_lease_invalid"
        )
        message = f"Provider {component} is unavailable: {exc}"
        with self._lock:
            self._construction_failures[component] = (code, message)
            self._pending_construction_stages.discard(cast(_ConstructionStage, component))
            self._latest_failure = ("construction", component)
            self._unavailable_failure_count += 1
            self._rearm_required = True
            self._refresh_lane_status_locked()

    def _clear_construction_failure(self, component: _ConstructionStage) -> None:
        with self._lock:
            self._construction_failures.pop(component, None)
            self._pending_construction_stages.discard(component)
            self._refresh_lane_status_locked()

    def mark_unavailable(
        self,
        code: ProviderLaneUnavailableCodeV1 | ProviderProcessFenceCodeV1,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        """Degrade only the Provider lane and preserve an operator-visible reason."""

        with self._lock:
            typed_code = code
            if retryable:
                self._recovery_failures[typed_code] = message
                self._latest_failure = ("recovery", typed_code)
            else:
                key = f"manual:{typed_code}"
                self._construction_failures[key] = (typed_code, message)
                self._latest_failure = ("construction", key)
            self._unavailable_failure_count += 1
            self._rearm_required = self._rearm_required or retryable
            self._refresh_lane_status_locked()

    def _refresh_lane_status_locked(self) -> None:
        failures = [*self._construction_failures.values()]
        failures.extend((code, message) for code, message in self._recovery_failures.items())
        if not failures:
            if self._rearm_required and self.unavailable_reason is not None:
                return
            self.unavailable_code = None
            self.unavailable_reason = None
            self._lane_status_snapshot = ("available", None, None)
            return
        latest: tuple[ProviderLaneUnavailableCodeV1, str] | None = None
        if self._latest_failure is not None:
            category, key = self._latest_failure
            if category == "construction":
                latest = self._construction_failures.get(key)
            else:
                typed_key = cast(ProviderLaneUnavailableCodeV1, key)
                message = self._recovery_failures.get(typed_key)
                if message is not None:
                    latest = (typed_key, message)
        if latest is None:
            latest = failures[-1]
        code, message = latest
        codes = ",".join(sorted({item[0] for item in failures}, key=str.encode))
        self.unavailable_code = code
        self.unavailable_reason = (
            f"latest={code}: {message}; codes=[{codes}]; count={self._unavailable_failure_count}"
        )
        self._lane_status_snapshot = ("unavailable", code, self.unavailable_reason)

    def recover_all(self) -> ProviderProcessRecoveryResultV1:
        """Recover every persisted process fence before serving requests."""

        with self._lock:
            if self._in_flight:
                raise ProviderLocalRuntimeRefused(
                    "provider_process_lease_invalid",
                    "Provider process recovery cannot run while an invocation is in flight",
                )
            return self._recover_locked()

    def _recover_locked(self) -> ProviderProcessRecoveryResultV1:
        if self.process_leases is None:
            failure = ProviderProcessRecoveryFailureV1(
                record_name="provider-process-leases",
                invocation_id=None,
                code="provider_process_lease_invalid",
                message=self.unavailable_reason or "Provider process leases are unavailable",
            )
            return ProviderProcessRecoveryResultV1(
                recovered=(),
                removed=(),
                could_not_clean=(failure,),
            )
        result = self.process_leases.recover_all(defer_completion_release=True)
        for item in result.could_not_clean:
            self.mark_unavailable(item.code, item.message, retryable=True)
        return result

    def _lazy_rearm_locked(self) -> None:
        if not self._rearm_required or self._in_flight:
            return
        if time.monotonic() < self._next_rearm_after:
            return
        self._reinitialize_failed_construction_stages_locked()
        result = self._recover_locked()
        if result.could_not_clean:
            self._schedule_next_rearm_locked()
            return
        if result.completion_invocation_ids:
            if self._recovery_fold is None:
                self.mark_unavailable(
                    "provider_runtime_recovery_failed",
                    "Provider recovery result has no manager-owned journal fold",
                    retryable=True,
                )
                self._schedule_next_rearm_locked()
                return
            dispositions = self._recovery_fold(result)
            if "fold_failed" in dispositions.values():
                self._schedule_next_rearm_locked()
                return
        self._mark_available_locked()
        if self._rearm_required:
            self._schedule_next_rearm_locked()

    def _mark_available_locked(self) -> None:
        self._recovery_failures.clear()
        self._rearm_required = bool(
            self._construction_failures or self._pending_construction_stages
        )
        self._next_rearm_after = 0.0
        if not self._rearm_required:
            self._unavailable_failure_count = 0
            self._latest_failure = None
        self._refresh_lane_status_locked()

    def _schedule_next_rearm_locked(self) -> None:
        self._rearm_required = True
        self._next_rearm_after = time.monotonic() + self.config.rearm_backoff_seconds

    def bind_recovery_fold(
        self,
        fold: Callable[
            [ProviderProcessRecoveryResultV1],
            Mapping[str, ProviderRecoveryFoldDisposition],
        ],
    ) -> None:
        """Bind the manager-owned governed-journal fold used by lazy re-arm."""

        with self._lock:
            self._recovery_fold = fold

    def acknowledge_recovery(self, invocation_ids: tuple[str, ...]) -> None:
        """Release completion-bearing fences after the manager fold commits."""

        with self._lock:
            if self.process_leases is not None:
                self.process_leases.acknowledge_recovery(invocation_ids)

    def lane_status(
        self,
    ) -> tuple[
        Literal["available", "unavailable"], ProviderLaneUnavailableCodeV1 | None, str | None
    ]:
        return self._lane_status_snapshot

    def invoker_for(
        self,
        instance: PlaybillInstance,
        *,
        accepted_oid: str,
    ) -> Any:
        with self._lock:
            self._lazy_rearm_locked()
            if self.unavailable_reason is not None:
                return _UnavailableProviderRuntimeInvoker(
                    code=self.unavailable_code or "provider_runtime_recovery_failed",
                    detail=self.unavailable_reason,
                )
            assert self.process_leases is not None
        tree = instance.tree_at(accepted_oid)
        providers: dict[str, AcceptedProviderV1] = {}
        interfaces: dict[str, AcceptedProviderInterfaceRegistrationV1] = {}
        for path, content in tree.items():
            if path.startswith("providers/") and path.endswith(".json"):
                provider = parse_provider(content, path=path)
                if provider.lifecycle.state != "live":
                    continue
                digest = provider_digest(provider).tagged
                providers[digest] = AcceptedProviderV1(
                    path=path,
                    provider=provider,
                    artifact_digest=digest,
                )
            elif path.startswith("provider-interfaces/") and path.endswith(".json"):
                registration = parse_provider_interface(content, path=path)
                if registration.lifecycle.state != "live":
                    continue
                digest = provider_interface_digest(registration).tagged
                interfaces[digest] = AcceptedProviderInterfaceRegistrationV1(
                    path=path,
                    registration=registration,
                    artifact_digest=digest,
                )
        invoker = ProviderLocalRuntimeInvoker(
            deployments_by_digest=self.deployments,
            accepted_providers_by_digest=providers,
            accepted_interfaces_by_digest=interfaces,
            secret_resolvers=self.secret_resolvers,
            process_leases=self.process_leases,
            driver=self.driver,
        )
        return _OperatorBoundProviderRuntimeInvoker(self, invoker)

    def _begin_invocation(self) -> None:
        with self._lock:
            self._lazy_rearm_locked()
            if self.unavailable_reason is not None:
                raise ProviderLocalRuntimeRefused(
                    "provider_unavailable",
                    "Provider runtime is unavailable until operator recovery",
                    details={
                        "reason": {
                            "code": self.unavailable_code or "provider_runtime_recovery_failed",
                            "detail": self.unavailable_reason,
                        }
                    },
                )
            self._in_flight += 1

    def _end_invocation(self) -> None:
        with self._lock:
            self._in_flight -= 1

    def _load_config(self) -> ProviderRuntimeOperationalConfigV1:
        path = self.state_root / PROVIDER_RUNTIME_CONFIG_PATH
        if not path.exists():
            return ProviderRuntimeOperationalConfigV1()
        if path.is_symlink() or not path.is_file():
            raise ProviderLocalRuntimeRefused(
                "provider_process_lease_invalid", "Provider runtime config is not a regular file"
            )
        try:
            return ProviderRuntimeOperationalConfigV1.model_validate(json.loads(path.read_bytes()))
        except (OSError, ValueError, ValidationError) as exc:
            raise ProviderLocalRuntimeRefused(
                "provider_process_lease_invalid", "Provider runtime config is malformed"
            ) from exc

    def _deployment(self, item: ProviderDeploymentConfigV1) -> LocalProviderDeploymentV1:
        def resolve(value: str) -> Path:
            path = (self.state_root / value).resolve(strict=False)
            if not path.is_relative_to(self.state_root):  # defensive after model validation
                raise ProviderLocalRuntimeRefused(
                    "provider_process_lease_invalid", "Provider deployment path escapes state root"
                )
            return path

        return LocalProviderDeploymentV1(
            deployment_digest=item.deployment_digest,
            distribution_path=resolve(item.distribution_path),
            lock_path=resolve(item.lock_path),
            environment_path=resolve(item.environment_path),
            environment_manifest_path=resolve(item.environment_manifest_path),
            environment_pin_key=item.environment_pin_key,
            interpreter_path=resolve(item.interpreter_path),
            provider_runtime_version=item.provider_runtime_version,
        )


class _OperatorBoundProviderRuntimeInvoker:
    def __init__(self, operator: ProviderRuntimeOperator, delegate: Any) -> None:
        self.operator = operator
        self.delegate = delegate

    def bind_provider(self, *, occurrence: object) -> object:
        return self.delegate.bind_provider(occurrence=occurrence)

    def invoke_provider(self, **kwargs: object) -> object:
        self.operator._begin_invocation()
        try:
            return self.delegate.invoke_provider(**kwargs)
        finally:
            self.operator._end_invocation()


class _UnavailableProviderRuntimeInvoker:
    """Refusing invoker used when startup fences degrade the Provider lane."""

    def __init__(self, *, code: ProviderLaneUnavailableCodeV1, detail: str) -> None:
        self.code = code
        self.detail = detail

    @property
    def reason(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}

    def bind_provider(self, *, occurrence: object) -> object:
        raise ProviderLocalRuntimeRefused(
            "provider_unavailable",
            "Provider runtime is unavailable until operator recovery",
            details={"reason": self.reason},
        )

    def invoke_provider(self, **_kwargs: object) -> object:
        raise ProviderLocalRuntimeRefused(
            "provider_unavailable",
            "Provider runtime is unavailable until operator recovery",
            details={"reason": self.reason},
        )


__all__ = [
    "PROVIDER_RUNTIME_CONFIG_PATH",
    "ProviderDeploymentConfigV1",
    "ProviderRuntimeOperationalConfigV1",
    "ProviderRuntimeOperator",
    "ProviderRecoveryFoldDisposition",
]
