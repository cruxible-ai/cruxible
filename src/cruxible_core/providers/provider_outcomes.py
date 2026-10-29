"""Single Provider-runtime outcome translation.

Two vocabularies reach core, and they never overlap (provider runtime protocol
2.0):

- the runtime's own reserved codes, mapped exhaustively here, with core's own
  local codes beside them; and
- the codes the bound operation declares: the operation's own vocabulary,
  carried through under its own spelling once its detail is the declared
  Contract. Whether one is absorbable is the operation's ``absorbable``
  declaration, never a list kept here.

An envelope is the provider's word; a refusal core raised is core's. A runtime
code inside an envelope is believed only when the child harness itself produces
it (``PROVIDER_RUNTIME_HARNESS_REFUSAL_CODES``): every other runtime code names a
condition only the executor observes, so a provider asserting one is a protocol
violation. A code the operation does not declare is ``undeclared_refusal``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

from cruxible_client.contracts.canonical import CanonicalValue, normalize_canonical
from cruxible_client.contracts.provider_execution import ProviderInvocationOutcome
from cruxible_client.contracts.provider_operations import ProviderOperation
from cruxible_core.providers.provider_runtime_contract import (
    PROVIDER_RUNTIME_HARNESS_REFUSAL_CODES,
    PROVIDER_RUNTIME_RESERVED_REFUSAL_CODES,
    ProviderRuntimeResultEnvelope,
)

_MAPPING: dict[str, tuple[str, str]] = {
    "acceptance_divergence": ("internal", "governed_binding"),
    "air_gapped_cache_miss": ("operational", "cache"),
    "artifact_hash_mismatch": ("internal", "materialization_integrity"),
    "budget_cost": ("node_refusal", "input_budget"),
    "budget_output_size": ("node_refusal", "input_budget"),
    "budget_wall_clock": ("node_refusal", "input_budget"),
    "cache_integrity": ("internal", "cache_integrity"),
    "cache_permissions": ("operational", "cache"),
    "environment_divergence": ("internal", "materialization_integrity"),
    "image_provenance_mismatch": ("internal", "materialization_integrity"),
    "index_not_pinned": ("internal", "materialization_integrity"),
    "index_redirect": ("internal", "materialization_integrity"),
    "lock_ambiguous_fork": ("internal", "materialization_integrity"),
    "lock_bytes_mismatch": ("internal", "materialization_integrity"),
    "lock_mismatch": ("internal", "materialization_integrity"),
    "lock_missing_hash": ("internal", "materialization_integrity"),
    "manifest_divergence": ("internal", "implementation"),
    "network_disabled": ("operational", "deployment"),
    "no_compatible_artifact": ("operational", "resolver"),
    "non_finite_output": ("internal", "implementation"),
    "provider_protocol_violation": ("internal", "implementation"),
    "secret_bundle_too_large": ("node_refusal", "binding_input"),
    "secret_leak": ("internal", "implementation"),
    "unaccepted_provider": ("internal", "closure_mirror_drift"),
    "undeclared_egress": ("internal", "implementation"),
    # The entrypoint does not serve the operation its accepted build says it does.
    "undeclared_operation": ("internal", "implementation"),
    "undeclared_refusal": ("internal", "implementation"),
    "unknown_extra": ("node_refusal", "governed_binding"),
    "unknown_manifest_field": ("internal", "implementation"),
    "unknown_run_context_field": ("internal", "executor"),
    "unresolvable_source": ("operational", "resolver"),
    "unresolved_secret_ref": ("operational", "custody"),
    "unsupported_backend": ("operational", "local_deployment_binding"),
    "unsupported_protocol": ("operational", "runtime_compatibility"),
}
_LOCAL_MAPPING: dict[str, tuple[str, str]] = {
    "provider_unavailable": ("node_refusal", "executor"),
    # The lane precondition is the operator's environment, not a fence fault.
    "provider_runtime_directory_unavailable": ("operational", "deployment"),
    "secret_epoch_unavailable": ("operational", "custody"),
    "secret_resolver_not_installed": ("operational", "custody"),
    "secret_reference_invalid": ("node_refusal", "binding_input"),
    "provider_process_lease_invalid": ("internal", "executor"),
    "provider_process_lease_missing": ("internal", "executor"),
    "provider_process_lease_echo_failed": ("internal", "executor"),
    "provider_process_lease_echo_mismatch": ("internal", "executor"),
    "provider_process_group_survived_recovery": ("internal", "executor"),
    "provider_in_process_interrupted": ("internal", "executor"),
    "provider_runtime_not_in_materialization": ("internal", "materialization_integrity"),
    "budget_max_provider_calls_exceeded": ("node_refusal", "input_budget"),
}

PROVIDER_RUNTIME_REFUSAL_CODES = PROVIDER_RUNTIME_RESERVED_REFUSAL_CODES
if set(_MAPPING) != PROVIDER_RUNTIME_REFUSAL_CODES:  # pragma: no cover - import-time guard
    raise RuntimeError("Provider outcome mapping does not equal the mirrored runtime vocabulary")
if set(_MAPPING) & set(_LOCAL_MAPPING):  # pragma: no cover - import-time guard
    raise RuntimeError("core's local Provider codes overlap the runtime's reserved codes")

#: The problems with one declared refusal's detail against its Contract, or None.
RefusalDetailCheck = Callable[[str, object], tuple[str, ...] | None]


def provider_canonical_value(value: Any) -> CanonicalValue:
    """Translate the wider finite-number runtime JSON law into Cruxible canonical values."""

    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Provider values must be finite")
        return normalize_canonical({"$provider_float_v1": format(value, ".17g")})
    if isinstance(value, Mapping):
        return normalize_canonical(
            {str(key): provider_canonical_value(item) for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return normalize_canonical([provider_canonical_value(item) for item in value])
    raise ValueError(f"unsupported Provider wire value {type(value).__name__}")


def _refused(
    outcome_class: str, attribution: str, code: str, *, message: str, detail: Any
) -> ProviderInvocationOutcome:
    return ProviderInvocationOutcome(
        status="refused",
        outcome_class=outcome_class,  # type: ignore[arg-type]
        attribution=attribution,  # type: ignore[arg-type]
        code=code,
        message=message,
        detail=provider_canonical_value(detail),
    )


def map_provider_refusal(
    code: str,
    *,
    message: str,
    detail: Any,
) -> ProviderInvocationOutcome:
    """A refusal core itself raised or observed: a runtime code or one of core's own."""

    mapped = (_MAPPING | _LOCAL_MAPPING).get(code)
    if mapped is None:
        return _refused(
            "internal",
            "executor_mirror_drift",
            "provider_refusal_taxonomy_unknown",
            message=message,
            detail=detail,
        )
    return _refused(mapped[0], mapped[1], code, message=message, detail=detail)


def map_operation_refusal(
    operation: ProviderOperation,
    code: str,
    *,
    message: str,
    detail: Any,
    check_detail: RefusalDetailCheck,
) -> ProviderInvocationOutcome:
    """A refusal the provider returned: a harness runtime code or the operation's own.

    A declared code is recognised only once its detail is the declared Contract
    (``check_detail``). A runtime code outside the harness set, or a code the
    operation does not declare, is the provider's protocol violation.
    """

    if code in PROVIDER_RUNTIME_RESERVED_REFUSAL_CODES:
        if code in PROVIDER_RUNTIME_HARNESS_REFUSAL_CODES:
            return map_provider_refusal(code, message=message, detail=detail)
        return _refused(
            "internal",
            "implementation",
            "provider_protocol_violation",
            message=(
                f"the provider envelope asserts the runtime code {code!r}, a condition "
                "only the executor observes"
            ),
            detail={"operation": operation.name, "refused_code": code},
        )
    declared = operation.refusals.get(code)
    if declared is None:
        return _refused(
            "internal",
            "implementation",
            "undeclared_refusal",
            message=f"operation {operation.name!r} declares no refusal {code!r}",
            detail={"operation": operation.name, "refused_code": code},
        )
    problems = check_detail(code, detail)
    if problems is not None:
        return _refused(
            "node_refusal",
            "implementation",
            "contract_output_refused",
            message=(
                f"typed refusal {code!r} detail is not {declared.contract.spelling}: "
                + "; ".join(problems)
            ),
            detail={"operation": operation.name, "refused_code": code},
        )
    return _refused("node_refusal", "operation_declaration", code, message=message, detail=detail)


def map_provider_envelope(
    envelope: ProviderRuntimeResultEnvelope,
    *,
    operation: ProviderOperation,
    check_detail: RefusalDetailCheck,
) -> ProviderInvocationOutcome:
    if envelope.status == "ok":
        return ProviderInvocationOutcome(
            status="ok",
            outcome_class="ok",
            attribution="none",
        )
    if envelope.status == "refused":
        assert envelope.refusal is not None
        return map_operation_refusal(
            operation,
            envelope.refusal.code,
            message=envelope.refusal.message,
            detail=envelope.refusal.detail,
            check_detail=check_detail,
        )
    assert envelope.error is not None
    return ProviderInvocationOutcome(
        status="error",
        outcome_class="operational",
        attribution="provider_runtime",
        code="provider_execution_error",
        message=envelope.error.message,
        detail=provider_canonical_value(
            {"kind": envelope.error.kind, "detail": envelope.error.detail}
        ),
    )


def operation_refusal_is_declared(
    operation: ProviderOperation, outcome: ProviderInvocationOutcome
) -> bool:
    """Whether ``outcome`` is a refusal ``operation`` declares, recognised as such."""

    return (
        outcome.status == "refused"
        and outcome.attribution == "operation_declaration"
        and (outcome.code or "") in operation.refusals
    )


def provider_refusal_is_absorbable(
    operation: ProviderOperation, outcome: ProviderInvocationOutcome
) -> bool:
    """A declared refusal ``operation`` declares absorbable: "this input is unavailable"."""

    return (
        operation_refusal_is_declared(operation, outcome)
        and operation.refusals[outcome.code or ""].absorbable
    )


__all__ = [
    "PROVIDER_RUNTIME_REFUSAL_CODES",
    "RefusalDetailCheck",
    "map_operation_refusal",
    "map_provider_envelope",
    "map_provider_refusal",
    "operation_refusal_is_declared",
    "provider_canonical_value",
    "provider_refusal_is_absorbable",
]
