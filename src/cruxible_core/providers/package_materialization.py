"""Use the first-party provider toolchain for exact local wheel preparation.

The toolchain is installed alongside the daemon by its operator, like uv. Its
metadata/resolution modules execute no provider code; executable probes use the
supervised child separately. Client-local paths never enter this module.
"""

import importlib
import platform
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from zipfile import BadZipFile

import httpx
from packaging.markers import default_environment
from packaging.tags import sys_tags

from cruxible_client.contracts.canonical import canonical_digest
from cruxible_client.contracts.providers import (
    ProviderLocalDistributionPinV1,
    ProviderLocalEnvBackendPinV1,
    ProviderV3,
)
from cruxible_core.errors import ConfigError
from cruxible_core.providers.package_registration import PackageRegistrationDocumentV1
from cruxible_core.providers.provider_local_runtime import LocalProviderDeploymentV1
from cruxible_core.runtime.execution_policy import enforce_customer_code_execution_supported


def toolchain(module: str) -> Any:
    try:
        return importlib.import_module(f"cruxible_provider_runtime.{module}")
    except ModuleNotFoundError as exc:
        raise ConfigError(
            "Provider installation requires cruxible-provider-runtime 0.2.0 or later "
            "installed in the daemon environment, plus uv. Local wheels are supported."
        ) from exc


@contextmanager
def package_preparation_errors() -> Iterator[None]:
    """Translate toolchain refusals without leaking build output or credentials."""
    refusal = toolchain("errors").RefusalError
    try:
        yield
    except refusal as exc:
        raise ConfigError(f"Provider preparation refused: {exc.code}") from exc
    except (ValueError, BadZipFile) as exc:
        raise ConfigError(
            f"Provider package metadata or lock is invalid: {str(exc)[:1024]}"
        ) from exc
    except subprocess.SubprocessError as exc:
        raise ConfigError("Provider build or environment preparation failed") from exc
    except httpx.HTTPError as exc:
        raise ConfigError("Provider dependency download failed") from exc
    except OSError as exc:
        raise ConfigError("Provider installation files could not be read or written") from exc


@dataclass(frozen=True)
class PreparedProviderPackage:
    document: PackageRegistrationDocumentV1
    provider: ProviderV3
    deployment: LocalProviderDeploymentV1


class _ArtifactTransport:
    def __init__(self, local_root: Path) -> None:
        self.local_root = local_root.resolve()

    def get(self, url: str) -> Any:
        parsed = urlsplit(url)
        if parsed.scheme == "file":
            path = Path(unquote(parsed.path)).resolve(strict=True)
            if parsed.netloc not in {"", "localhost"} or not path.is_relative_to(self.local_root):
                raise ValueError("package lock points outside transferred wheel custody")
            return toolchain("index").TransportResponse(200, url, path.read_bytes())
        with httpx.stream("GET", url, follow_redirects=False, timeout=60) as response:
            chunks = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 512 * 1024 * 1024:
                    raise ValueError("dependency wheel exceeds 512 MiB installation budget")
                chunks.append(chunk)
            return toolchain("index").TransportResponse(
                response.status_code, str(response.url), b"".join(chunks)
            )


def prepare_provider_package(
    *,
    wheel: Path,
    lock_path: Path,
    dependency_wheels: tuple[Path, ...],
    cache_root: Path,
    extras: tuple[str, ...],
    control_domain: str,
    index_urls: tuple[str, ...],
) -> PreparedProviderPackage:
    enforce_customer_code_execution_supported()
    wheels = toolchain("wheels")
    resolution = toolchain("resolution")
    backend = toolchain("backends")
    pin = wheels.wheel_pin(wheel)
    with wheels.wheel_registration(wheel) as bundle:
        document = PackageRegistrationDocumentV1.model_validate(bundle.export_document())
    interfaces = document.interface_registrations()
    local_pins = [wheels.wheel_pin(path) for path in dependency_wheels]
    local_by_name = {item.name: item for item in local_pins}
    if len(local_by_name) != len(local_pins) or pin.name in local_by_name:
        raise ValueError("transferred dependencies must have unique distribution identities")
    env = resolution.MarkerEnvironment(
        id=f"{sys.platform}-{platform.machine().lower()}-cp{sys.version_info.major}{sys.version_info.minor}",
        markers=default_environment(),
        tags=tuple(str(tag) for tag in sys_tags()),
    )
    lock = resolution.load_uv_lock(lock_path)
    roots = [row for row in lock.packages if row["name"] == pin.name]
    if len(roots) != 1 or str(roots[0]["version"]) != pin.version:
        raise ValueError("provider wheel identity differs from the lock root")
    resolved = resolution.resolve(lock, pin.name, env, extras=extras, local_wheels=local_by_name)
    retained = []
    for item in resolved.distributions:
        transferred = local_by_name.get(item.name)
        if transferred is not None:
            if (item.version, item.artifact_id, item.filename) != (
                transferred.version,
                transferred.artifact_id,
                transferred.filename,
            ):
                raise ValueError("transferred dependency differs from the exact locked wheel")
            item = item.model_copy(update={"url": transferred.url})
        retained.append(item)
    resolved = resolved.model_copy(update={"distributions": tuple(retained)})
    materialization = toolchain("digests").materialization_digest(
        resolved, distribution_sha256=pin.artifact_id
    )
    distribution = ProviderLocalDistributionPinV1(
        name=pin.name,
        version=pin.version,
        filename=pin.filename,
        sha256=pin.artifact_id,
    )
    local_env = ProviderLocalEnvBackendPinV1(
        lock_sha256=lock.lock_sha256,
        materialization_digests={resolved.pin_key(): materialization},
    )
    provider = document.provider_definition(
        distribution=distribution,
        local_env=local_env,
        control_domain=control_domain,
        interfaces=interfaces,
    )
    custody = wheel.parent.resolve()
    fetcher = toolchain("index").ArtifactFetcher(
        toolchain("index").IndexConfig(index_urls=(custody.as_uri(), *index_urls)),
        _ArtifactTransport(custody),
    )
    root_pin = toolchain("artifact").DistributionPin(
        name=pin.name,
        version=pin.version,
        filename=pin.filename,
        sha256=pin.artifact_id,
        index_url=custody.as_uri(),
        url=pin.url,
    )
    builder = backend.UvSyncBuilder(python_executable=sys.executable)
    cache = toolchain("cache").MaterializationCache(cache_root)
    # Never repair an existing environment in place: an admitted run may still
    # name it. Explicit re-verification reports drift rather than deleting it.
    if cache.path_for(materialization).exists():
        environment = cache.verify(materialization)
    else:
        environment = cache.get_or_materialize(
            materialization,
            lambda target: builder.build(
                backend.MaterializationRequest(
                    target=target,
                    resolved=resolved,
                    fetcher=fetcher,
                    lock_path=lock_path,
                    distribution=root_pin,
                )
            ),
        )
    deployment_digest = "sha256:" + canonical_digest(
        "playbill-provider-package-deployment-v1",
        {
            "distribution": pin.artifact_id,
            "materialization": materialization,
            "environment_pin_key": resolved.pin_key(),
        },
    )
    runtime_pin = next(
        (item for item in resolved.distributions if item.name == "cruxible-provider-runtime"), None
    )
    if runtime_pin is None:
        raise ValueError("provider lock must retain cruxible-provider-runtime")
    deployment = LocalProviderDeploymentV1(
        deployment_digest=deployment_digest,
        distribution_path=environment / "artifact" / pin.filename,
        lock_path=environment / "uv.lock",
        environment_path=environment,
        environment_manifest_path=environment / "execution-seal.json",
        environment_pin_key=resolved.pin_key(),
        interpreter_path=builder.interpreter(environment),
        provider_runtime_version=runtime_pin.version,
    )
    return PreparedProviderPackage(document, provider, deployment)
