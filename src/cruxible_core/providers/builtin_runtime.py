"""In-process execution of core built-in Providers.

A built-in is an ordinary governed Provider artifact (seeded at genesis) whose
implementation is core code: ``workspace.file`` is the one today. Its binding is
a constant -- the deployment, materialization and environment-manifest digests
are compiler-owned (``governance/seed_artifacts/workspace_file.py``) -- and its
invocation is a function call in the daemon's own process, with no child, no
process lease and no materialized environment. The receipt says so:
``fence_scope="in_process"`` and egress ``observer_backend="core.in-process"``.

Dispatch is by the admitted binding: an occurrence whose ``local_execution`` is
fenced ``in_process`` runs here, every other occurrence goes to the local
subprocess runtime unchanged.

Two lane rules, decided for built-ins:

- ``unavailable_reason``. The Provider operator marks its lane unavailable when
  process fences, the lease store, secret custody or deployments fail. None of
  those is on a built-in's path, so a built-in still binds and runs while the
  subprocess lane is degraded; only subprocess occurrences refuse.
- Hosted profile. The shared hosted profile refuses customer code without an
  isolated executor. A built-in is core code, not customer code, so this module
  never calls that gate. The served run boundary (``playbill_procedure_run``)
  still refuses every Procedure run on that profile before admission; lifting
  that for built-in-only Procedures is a separate ruling, not made here.

A crash between a built-in's durable start and its completion leaves no process
to fence; the daemon closes such starts at startup
(``service_recover_provider_invocations(close_in_process_starts=True)``).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cruxible_client.contracts.provider_execution import (
    ProviderEgressObservation,
    ProviderExternalOccurrencePlan,
    VerifiedProviderBinding,
)
from cruxible_client.contracts.provider_interfaces import AcceptedProviderInterfaceRegistration
from cruxible_client.contracts.providers import AcceptedProvider, ProviderV2
from cruxible_core.governance.seed_artifacts.workspace_file import (
    WORKSPACE_FILE_BUILTIN_DEPLOYMENT_DIGEST,
    WORKSPACE_FILE_BUILTIN_ENTRYPOINT,
    WORKSPACE_FILE_BUILTIN_ENVIRONMENT_MANIFEST_DIGEST,
    WORKSPACE_FILE_BUILTIN_IMPLEMENTATION_DIGEST,
    WORKSPACE_FILE_BUILTIN_MATERIALIZATION_DIGEST,
)
from cruxible_core.providers.builtin_workspace_file import WorkspaceFile
from cruxible_core.providers.provider_local_runtime import (
    ProviderDriverOutcomeV1,
    ProviderSpawnDeadline,
)
from cruxible_core.providers.provider_process_leases import ProviderLocalRuntimeRefused
from cruxible_core.providers.provider_runtime_contract import (
    PROVIDER_RUNTIME_PROTOCOL,
    ProviderRuntimeProtocolVersionV1,
    ProviderRuntimeProviderErrorPayloadV1,
    ProviderRuntimeResultEnvelopeV1,
    ProviderRuntimeRunContextV1,
)

BUILTIN_EGRESS_OBSERVER = "core.in-process"


@dataclass(frozen=True)
class BuiltinImplementation:
    """One core-owned implementation and its constant binding identity."""

    implementation_digest: str
    entrypoint: str
    deployment_digest: str
    materialization_digest: str
    environment_manifest_digest: str
    adapter: Callable[[ProviderRuntimeRunContextV1], ProviderRuntimeResultEnvelopeV1]

    def binding(
        self,
        *,
        provider_artifact_digest: str,
        interface_artifact_digest: str,
        interface_id: str,
        interface_digest: str,
    ) -> VerifiedProviderBinding:
        return VerifiedProviderBinding(
            provider_artifact_digest=provider_artifact_digest,
            interface_artifact_digest=interface_artifact_digest,
            interface_id=interface_id,
            interface_digest=interface_digest,
            implementation_digest=self.implementation_digest,
            deployment_digest=self.deployment_digest,
            materialization_digest=self.materialization_digest,
            environment_manifest_digest=self.environment_manifest_digest,
            entrypoint=self.entrypoint,
            declared_endpoints=(),
            fence_scope="in_process",
        )


BUILTIN_IMPLEMENTATIONS: dict[str, BuiltinImplementation] = {
    WORKSPACE_FILE_BUILTIN_IMPLEMENTATION_DIGEST: BuiltinImplementation(
        implementation_digest=WORKSPACE_FILE_BUILTIN_IMPLEMENTATION_DIGEST,
        entrypoint=WORKSPACE_FILE_BUILTIN_ENTRYPOINT,
        deployment_digest=WORKSPACE_FILE_BUILTIN_DEPLOYMENT_DIGEST,
        materialization_digest=WORKSPACE_FILE_BUILTIN_MATERIALIZATION_DIGEST,
        environment_manifest_digest=WORKSPACE_FILE_BUILTIN_ENVIRONMENT_MANIFEST_DIGEST,
        adapter=WorkspaceFile(),
    )
}


def builtin_provider_binding(
    accepted_provider: AcceptedProvider,
    accepted_interface: AcceptedProviderInterfaceRegistration,
    implementation_digest: str,
) -> VerifiedProviderBinding | None:
    """The constant binding for a built-in implementation, or None for any other.

    The accepted Provider must carry the implementation row exactly as the
    built-in defines it (entrypoint, interface and materialization); a row that
    names the built-in digest with anything else refuses rather than running.
    """

    builtin = BUILTIN_IMPLEMENTATIONS.get(implementation_digest)
    if builtin is None:
        return None
    registration = accepted_interface.registration
    provider = accepted_provider.provider
    record = (
        next(
            (
                item
                for item in provider.implementations
                if item.implementation_digest == implementation_digest
            ),
            None,
        )
        if isinstance(provider, ProviderV2)
        else None
    )
    if (
        record is None
        or record.entrypoint != builtin.entrypoint
        or record.interface_id != registration.interface_id
        or record.interface_digest != registration.interface_digest
        or {item.materialization_digest for item in record.materialization_references}
        != {builtin.materialization_digest}
    ):
        raise ProviderLocalRuntimeRefused(
            "acceptance_divergence",
            "the accepted Provider row for a core built-in differs from the built-in",
        )
    return builtin.binding(
        provider_artifact_digest=accepted_provider.artifact_digest,
        interface_artifact_digest=accepted_interface.artifact_digest,
        interface_id=registration.interface_id,
        interface_digest=registration.interface_digest,
    )


@dataclass(frozen=True)
class BoundBuiltinProviderV1:
    binding: VerifiedProviderBinding


class BuiltinProviderInvoker:
    """Bind and run built-in occurrences as function calls in this process."""

    def bind_provider(
        self, *, occurrence: ProviderExternalOccurrencePlan
    ) -> BoundBuiltinProviderV1:
        binding = occurrence.local_execution
        builtin = BUILTIN_IMPLEMENTATIONS.get(binding.implementation_digest)
        if builtin is None or binding != builtin.binding(
            provider_artifact_digest=binding.provider_artifact_digest,
            interface_artifact_digest=binding.interface_artifact_digest,
            interface_id=binding.interface_id,
            interface_digest=binding.interface_digest,
        ):
            raise ProviderLocalRuntimeRefused(
                "acceptance_divergence",
                "the admitted in-process binding is not a core built-in at this build",
            )
        return BoundBuiltinProviderV1(binding=binding)

    def invoke_provider(
        self,
        *,
        occurrence: ProviderExternalOccurrencePlan,
        context: ProviderRuntimeRunContextV1,
        invocation_id: str,
        bound: BoundBuiltinProviderV1,
        deadline: ProviderSpawnDeadline | None = None,
    ) -> ProviderDriverOutcomeV1:
        fresh = self.bind_provider(occurrence=occurrence)
        if fresh != bound:
            raise ProviderLocalRuntimeRefused(
                "environment_divergence",
                "Provider binding changed after the durable invocation start",
            )
        binding = bound.binding
        if (
            context.implementation_digest != binding.implementation_digest
            or context.entrypoint != binding.entrypoint
        ):
            raise ProviderLocalRuntimeRefused(
                "provider_protocol_violation", "run context names another implementation"
            )
        requested = ProviderRuntimeProtocolVersionV1.parse(context.protocol_version)
        if (
            requested.major
            != ProviderRuntimeProtocolVersionV1.parse(PROVIDER_RUNTIME_PROTOCOL).major
        ):
            raise ProviderLocalRuntimeRefused(
                "unsupported_protocol", "run context protocol is unsupported"
            )
        if deadline is not None:
            deadline.require_remaining()
        adapter = BUILTIN_IMPLEMENTATIONS[binding.implementation_digest].adapter
        started = time.monotonic()
        try:
            envelope = adapter(context)
        except Exception as exc:  # the child harness reports a raise the same way
            envelope = ProviderRuntimeResultEnvelopeV1(
                protocol_version=PROVIDER_RUNTIME_PROTOCOL,
                run_id=context.run_id,
                status="error",
                error=ProviderRuntimeProviderErrorPayloadV1(
                    kind=type(exc).__name__, message=str(exc)
                ),
            )
        duration = time.monotonic() - started
        if len(envelope.to_json()) > context.budgets.output_bytes:
            raise ProviderLocalRuntimeRefused(
                "budget_output_size", "built-in Provider exceeded its output budget"
            )
        return ProviderDriverOutcomeV1(
            envelope=envelope,
            stderr="",
            duration_seconds=round(duration, 4),
            egress=ProviderEgressObservation(
                observer_backend=BUILTIN_EGRESS_OBSERVER,
                observer_grade="attribution",
            ),
            verified_binding=binding,
        )


class BuiltinDispatchingInvoker:
    """Route in-process occurrences to core; everything else to ``delegate``."""

    def __init__(self, delegate: Any, builtin: BuiltinProviderInvoker | None = None) -> None:
        self.delegate = delegate
        self.builtin = builtin or BuiltinProviderInvoker()

    def _for(self, occurrence: object) -> Any:
        binding = getattr(occurrence, "local_execution", None)
        in_process = getattr(binding, "fence_scope", None) == "in_process"
        return self.builtin if in_process else self.delegate

    def bind_provider(self, *, occurrence: object) -> object:
        return self._for(occurrence).bind_provider(occurrence=occurrence)

    def invoke_provider(self, **kwargs: Any) -> object:
        return self._for(kwargs.get("occurrence")).invoke_provider(**kwargs)


__all__ = [
    "BUILTIN_EGRESS_OBSERVER",
    "BUILTIN_IMPLEMENTATIONS",
    "BoundBuiltinProviderV1",
    "BuiltinDispatchingInvoker",
    "BuiltinImplementation",
    "BuiltinProviderInvoker",
    "builtin_provider_binding",
]
