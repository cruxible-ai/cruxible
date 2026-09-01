"""Daemon-owned operational construction for the local Provider runtime."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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
    DEFAULT_PROVIDER_LEASE_ACQUISITION_TIMEOUT_SECONDS,
    DEFAULT_PROVIDER_LEASE_RECOVERY_TIMEOUT_SECONDS,
    ProviderLocalRuntimeRefused,
    ProviderProcessLeaseStore,
    ProviderProcessRecoveryFailureV1,
    ProviderProcessRecoveryResultV1,
)

PROVIDER_RUNTIME_CONFIG_PATH = Path("daemon/provider-runtime.json")


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
        self.state_root = state_root.resolve()
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.unavailable_reason: str | None = None
        try:
            self.config = self._load_config()
        except ProviderLocalRuntimeRefused as exc:
            self.config = ProviderRuntimeOperationalConfigV1()
            self.mark_unavailable(exc.code, str(exc))
        try:
            process_leases = ProviderProcessLeaseStore(
                self.state_root / "daemon" / "provider-process-leases",
                control_root=self.state_root / "c",
                acquisition_timeout_seconds=self.config.lease_acquisition_timeout_seconds,
                recovery_timeout_seconds=self.config.lease_recovery_timeout_seconds,
            )
            process_leases.paths("sha256:" + "0" * 64)
            self.process_leases: ProviderProcessLeaseStore | None = process_leases
        except ProviderLocalRuntimeRefused as exc:
            self.process_leases = None
            self.mark_unavailable(exc.code, str(exc))
        self.secret_store = FileProviderSecretStore(self.state_root / "daemon" / "provider-secrets")
        self.secret_resolvers = ProviderSecretResolverRegistry(
            (EnvironmentProviderSecretResolver(), self.secret_store)
        )
        self.driver = LocalProviderExecutionDriver()
        self.deployments = {
            item.deployment_digest: self._deployment(item) for item in self.config.deployments
        }

    def mark_unavailable(self, code: str, message: str) -> None:
        """Degrade only the Provider lane and preserve an operator-visible reason."""

        reason = f"{code}: {message}"
        if self.unavailable_reason is None:
            self.unavailable_reason = reason
        elif reason not in self.unavailable_reason:
            self.unavailable_reason = f"{self.unavailable_reason}; {reason}"

    def recover_all(self) -> ProviderProcessRecoveryResultV1:
        """Recover every persisted process fence before serving requests."""

        if self.process_leases is None:
            failure = ProviderProcessRecoveryFailureV1(
                record_name="provider-process-leases",
                invocation_id=None,
                code="provider_unavailable",
                message=self.unavailable_reason or "Provider process leases are unavailable",
            )
            return ProviderProcessRecoveryResultV1(
                recovered=(),
                removed=(),
                could_not_clean=(failure,),
            )
        result = self.process_leases.recover_all()
        for item in result.could_not_clean:
            self.mark_unavailable(item.code, item.message)
        return result

    def invoker_for(
        self,
        instance: PlaybillInstance,
        *,
        accepted_oid: str,
    ) -> ProviderLocalRuntimeInvoker | _UnavailableProviderRuntimeInvoker:
        if self.unavailable_reason is not None:
            return _UnavailableProviderRuntimeInvoker(self.unavailable_reason)
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
        return ProviderLocalRuntimeInvoker(
            deployments_by_digest=self.deployments,
            accepted_providers_by_digest=providers,
            accepted_interfaces_by_digest=interfaces,
            secret_resolvers=self.secret_resolvers,
            process_leases=self.process_leases,
            driver=self.driver,
        )

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


class _UnavailableProviderRuntimeInvoker:
    """Refusing invoker used when startup fences degrade the Provider lane."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

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
]
