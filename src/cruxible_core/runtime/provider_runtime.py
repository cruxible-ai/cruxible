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
    ProviderLocalRuntimeRefused,
    ProviderProcessLeaseStore,
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
    lease_acquisition_timeout_seconds: float = Field(default=5.0, gt=0)
    lease_recovery_timeout_seconds: float = Field(default=5.0, gt=0)
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
        self.config = self._load_config()
        self.process_leases = ProviderProcessLeaseStore(
            self.state_root / "daemon" / "provider-process-leases",
            acquisition_timeout_seconds=self.config.lease_acquisition_timeout_seconds,
            recovery_timeout_seconds=self.config.lease_recovery_timeout_seconds,
        )
        self.secret_store = FileProviderSecretStore(self.state_root / "daemon" / "provider-secrets")
        self.secret_resolvers = ProviderSecretResolverRegistry(
            (EnvironmentProviderSecretResolver(), self.secret_store)
        )
        self.driver = LocalProviderExecutionDriver()
        self.deployments = {
            item.deployment_digest: self._deployment(item) for item in self.config.deployments
        }

    def recover_all(self) -> tuple[str, ...]:
        """Recover every persisted process fence before serving requests."""

        return self.process_leases.recover_all()

    def invoker_for(
        self,
        instance: PlaybillInstance,
        *,
        accepted_oid: str,
    ) -> ProviderLocalRuntimeInvoker:
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


__all__ = [
    "PROVIDER_RUNTIME_CONFIG_PATH",
    "ProviderDeploymentConfigV1",
    "ProviderRuntimeOperationalConfigV1",
    "ProviderRuntimeOperator",
]
