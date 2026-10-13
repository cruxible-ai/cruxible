"""Run a package's install-time probes in the existing supervised provider process."""

from typing import Any, Literal

from cruxible_client.contracts.canonical import canonical_digest
from cruxible_client.contracts.errors import ExecutionError
from cruxible_client.contracts.primitives import new_id
from cruxible_core.providers.provider_local_runtime import (
    LocalProviderDeploymentV1,
    _run_child,
)
from cruxible_core.providers.provider_process_leases import ProviderProcessLeaseStore
from cruxible_core.providers.provider_runtime_contract import (
    ProviderRuntimeBudgetsV1,
    ProviderRuntimeRunContextV1,
    parse_provider_runtime_result,
)

_PROBE_WALL_CLOCK_SECONDS = 30


class PackageProbeFailed(ExecutionError):
    """A package probe child refused, failed, or returned no object."""


def run_package_probe(
    deployment: LocalProviderDeploymentV1,
    leases: ProviderProcessLeaseStore,
    *,
    kind: Literal["resource"],
    digest: str,
    value: dict[str, Any],
) -> dict[str, Any]:
    """Run one package probe child: a runtime-resource readiness probe.

    Probes run at installation, outside any Procedure run, within the probe's
    own wall-clock window.
    """
    if deployment.installation_verification is None:
        raise ValueError("package probes require a verified installation")
    identifier = "sha256:" + canonical_digest("provider-package-probe-v1", {"id": new_id("probe")})
    entrypoint = f"cruxible_provider_runtime.package_probe:{kind.title()}Probe"
    budgets = ProviderRuntimeBudgetsV1(
        wall_clock_seconds=float(_PROBE_WALL_CLOCK_SECONDS), output_bytes=1_048_576
    )
    context = ProviderRuntimeRunContextV1(
        protocol_version="1.0",
        run_id=identifier,
        interface_id=f"cruxible.package.{kind}",
        interface_digest=digest,
        implementation_digest=digest,
        entrypoint=entrypoint,
        # The probe protocol's own fixed label; it names the probe kind.
        input_bucket=f"package={kind}",
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
        raise PackageProbeFailed(f"package {kind} probe refused or returned invalid output")
    return result.output


__all__ = ["PackageProbeFailed", "run_package_probe"]
