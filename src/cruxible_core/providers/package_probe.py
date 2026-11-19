"""Run a package's install-time probes in the existing supervised provider process."""

from typing import Any

from cruxible_client.contracts.canonical import canonical_digest
from cruxible_client.contracts.errors import ExecutionError
from cruxible_client.contracts.primitives import new_id
from cruxible_core.providers.provider_local_runtime import (
    LocalProviderDeploymentV1,
    _run_child,
)
from cruxible_core.providers.provider_process_leases import ProviderProcessLeaseStore
from cruxible_core.providers.provider_runtime_contract import (
    PROVIDER_RUNTIME_PROTOCOL,
    ProviderRuntimeBudgets,
    ProviderRuntimeRunContext,
    parse_provider_runtime_result,
)

_PROBE_WALL_CLOCK_SECONDS = 30
#: The runtime's own resource-probe operation and the entrypoint that serves it
#: (``cruxible_provider_runtime.package_probe``).
RESOURCE_PROBE_OPERATION = "cruxible.package.resource"
RESOURCE_PROBE_ENTRYPOINT = "cruxible_provider_runtime.package_probe:ResourceProbe"
#: A probe binds no Contracts; its run context names this fixed signature digest.
RESOURCE_PROBE_SIGNATURE_DIGEST = "sha256:" + canonical_digest(
    "cruxible-package-probe-signature-v1", {"operation": RESOURCE_PROBE_OPERATION}
)


class PackageProbeFailed(ExecutionError):
    """A package probe child refused, failed, or returned no object."""


def run_package_probe(
    deployment: LocalProviderDeploymentV1,
    leases: ProviderProcessLeaseStore,
    *,
    build_digest: str,
    implementation_digest: str,
    value: dict[str, Any],
) -> dict[str, Any]:
    """Run one package probe child: a runtime-resource readiness probe.

    Probes run at installation, outside any Procedure run, within the probe's
    own wall-clock window. The child runs the runtime's ``ResourceProbe`` under
    its own operation, in the environment of the build and implementation the
    requirement belongs to.
    """
    if deployment.installation_verification is None:
        raise ValueError("package probes require a verified installation")
    identifier = "sha256:" + canonical_digest("provider-package-probe-v1", {"id": new_id("probe")})
    entrypoint = RESOURCE_PROBE_ENTRYPOINT
    budgets = ProviderRuntimeBudgets(
        wall_clock_seconds=float(_PROBE_WALL_CLOCK_SECONDS), output_bytes=1_048_576
    )
    context = ProviderRuntimeRunContext(
        protocol_version=PROVIDER_RUNTIME_PROTOCOL,
        run_id=identifier,
        operation=RESOURCE_PROBE_OPERATION,
        operation_signature_digest=RESOURCE_PROBE_SIGNATURE_DIGEST,
        build_digest=build_digest,
        implementation_digest=implementation_digest,
        entrypoint=entrypoint,
        coordinates={},
        input=value,
        budgets=budgets,
    )
    outcome = _run_child(
        deployment.interpreter_path,
        entrypoint=entrypoint,
        context=context.to_json(),
        budgets=budgets,
        secret_fd=None,
        invocation_id=identifier,
        process_leases=leases,
    )
    result = parse_provider_runtime_result(outcome.stdout)
    if result.run_id != identifier or result.status != "ok" or not isinstance(result.output, dict):
        raise PackageProbeFailed("package resource probe refused or returned invalid output")
    return result.output


__all__ = [
    "RESOURCE_PROBE_ENTRYPOINT",
    "RESOURCE_PROBE_OPERATION",
    "RESOURCE_PROBE_SIGNATURE_DIGEST",
    "PackageProbeFailed",
    "run_package_probe",
]
