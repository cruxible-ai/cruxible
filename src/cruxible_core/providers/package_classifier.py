"""Invoke package classifiers in the existing supervised provider process."""

from typing import Any, Literal

from cruxible_client.contracts.canonical import CanonicalValue, canonical_digest
from cruxible_client.contracts.primitives import new_id
from cruxible_client.contracts.provider_interfaces import ProviderInterfaceRegistration
from cruxible_core.providers.provider_classifiers import ProviderClassifierInstallationRefused
from cruxible_core.providers.provider_local_runtime import (
    LocalProviderDeploymentV1,
    ProviderSpawnDeadline,
    _run_child,
)
from cruxible_core.providers.provider_process_leases import ProviderProcessLeaseStore
from cruxible_core.providers.provider_runtime_contract import (
    ProviderRuntimeBudgetsV1,
    ProviderRuntimeRunContextV1,
    parse_provider_runtime_result,
)

_PROBE_WALL_CLOCK_SECONDS = 30


class PackageBucketClassifier:
    def __init__(
        self,
        registration: ProviderInterfaceRegistration,
        deployment: LocalProviderDeploymentV1,
        leases: ProviderProcessLeaseStore,
    ) -> None:
        if deployment.installation_verification is None:
            raise ProviderClassifierInstallationRefused(
                "classifier_not_installed", "package environment is not verified"
            )
        self.registration = registration
        self.deployment = deployment
        self.leases = leases
        self.classifier_identity = registration.classifier_identity
        self.classifier_version = registration.classifier_version
        self.classifier_digest = registration.classifier_digest

    def classify(
        self, canonical_input: CanonicalValue, *, deadline: ProviderSpawnDeadline | None
    ) -> str:
        output = run_package_probe(
            self.deployment,
            self.leases,
            deadline=deadline,
            kind="classifier",
            digest=self.classifier_digest,
            value={
                "entrypoint": self.registration.classifier_code.entrypoint,
                "vocabulary": self.registration.vocabulary.model_dump(mode="json"),
                "value": canonical_input,
            },
        )
        bucket = output.get("bucket")
        if not isinstance(bucket, str):
            raise ProviderClassifierInstallationRefused(
                "classifier_digest_mismatch", "classifier returned no bucket"
            )
        self.registration.vocabulary.validate_bucket(bucket)
        return bucket


def run_package_probe(
    deployment: LocalProviderDeploymentV1,
    leases: ProviderProcessLeaseStore,
    *,
    kind: Literal["classifier", "resource"],
    digest: str,
    value: dict[str, Any],
    deadline: ProviderSpawnDeadline | None,
) -> dict[str, Any]:
    """Run one package probe child.

    ``deadline`` is a Procedure run's, when the probe classifies a run's input:
    the probe's own window is clipped to the run's time left, and the spawn
    refuses with ``budget_wall_clock`` once it has passed. Installation probes,
    outside any run, pass None and keep the probe window.
    """
    if deployment.installation_verification is None:
        raise ValueError("package probes require a verified installation")
    identifier = "sha256:" + canonical_digest("provider-package-probe-v1", {"id": new_id("probe")})
    entrypoint = f"cruxible_provider_runtime.package_probe:{kind.title()}Probe"
    window = float(_PROBE_WALL_CLOCK_SECONDS)
    if deadline is not None:
        window = min(window, deadline.require_remaining())
    budgets = ProviderRuntimeBudgetsV1(wall_clock_seconds=window, output_bytes=1_048_576)
    context = ProviderRuntimeRunContextV1(
        protocol_version="1.0",
        run_id=identifier,
        interface_id=f"cruxible.package.{kind}",
        interface_digest=digest,
        implementation_digest=digest,
        entrypoint=entrypoint,
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
        deadline=deadline,
    )
    result = parse_provider_runtime_result(outcome.stdout)
    if result.run_id != identifier or result.status != "ok" or not isinstance(result.output, dict):
        raise ProviderClassifierInstallationRefused(
            "classifier_digest_mismatch", f"package {kind} probe refused or returned invalid output"
        )
    return result.output
