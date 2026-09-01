"""Single exhaustive Provider-runtime outcome translation."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, get_args

from cruxible_client.contracts.canonical import CanonicalValue, normalize_canonical
from cruxible_client.contracts.provider_execution import ProviderInvocationOutcomeV1
from cruxible_core.playbill.provider_runtime_contract import (
    ProviderRuntimeRefusalCodeV1,
    ProviderRuntimeResultEnvelopeV1,
)

_MAPPING: dict[str, tuple[str, str]] = {
    "unknown_manifest_field": ("internal", "implementation"),
    "manifest_divergence": ("internal", "implementation"),
    "acceptance_divergence": ("internal", "governed_binding"),
    "unaccepted_provider": ("internal", "closure_mirror_drift"),
    "undeclared_interface": ("internal", "closure_mirror_drift"),
    "ambiguous_implementation": ("internal", "closure_mirror_drift"),
    "unknown_interface": ("internal", "closure_mirror_drift"),
    "interface_digest_mismatch": ("internal", "implementation"),
    "bucket_fixture_missing": ("internal", "interface_registration"),
    "invalid_bucket_vocabulary": ("internal", "interface_registration"),
    "unsupported_protocol": ("operational", "runtime_compatibility"),
    "unknown_run_context_field": ("internal", "executor"),
    "provider_protocol_violation": ("internal", "implementation"),
    "unsupported_backend": ("operational", "local_deployment_binding"),
    "lock_mismatch": ("internal", "materialization_integrity"),
    "lock_bytes_mismatch": ("internal", "materialization_integrity"),
    "lock_missing_hash": ("internal", "materialization_integrity"),
    "lock_ambiguous_fork": ("internal", "materialization_integrity"),
    "no_compatible_artifact": ("operational", "resolver"),
    "unresolvable_source": ("operational", "resolver"),
    "unknown_extra": ("node_refusal", "governed_binding"),
    "index_not_pinned": ("internal", "materialization_integrity"),
    "index_redirect": ("internal", "materialization_integrity"),
    "artifact_hash_mismatch": ("internal", "materialization_integrity"),
    "air_gapped_cache_miss": ("operational", "cache"),
    "network_disabled": ("operational", "deployment"),
    "cache_permissions": ("operational", "cache"),
    "cache_integrity": ("internal", "cache_integrity"),
    "environment_divergence": ("internal", "materialization_integrity"),
    "unclaimed_bucket": ("node_refusal", "input"),
    "unclassified_input": ("node_refusal", "input"),
    "budget_wall_clock": ("node_refusal", "input_budget"),
    "budget_output_size": ("node_refusal", "input_budget"),
    "budget_cost": ("node_refusal", "input_budget"),
    "undeclared_egress": ("internal", "implementation"),
    "secret_leak": ("internal", "implementation"),
    "provider_declined": ("node_refusal", "input"),
    "unresolved_secret_ref": ("operational", "custody"),
    "secret_bundle_too_large": ("node_refusal", "binding_input"),
    "non_finite_output": ("internal", "implementation"),
    "insufficient_series_length": ("node_refusal", "input"),
    "non_finite_input": ("node_refusal", "input"),
    "non_finite_result": ("internal", "implementation"),
    "degenerate_scale": ("node_refusal", "input"),
    "mismatched_lengths": ("node_refusal", "input"),
    "unknown_method": ("node_refusal", "input"),
    "unknown_test_name": ("node_refusal", "input"),
    "declared_family_mismatch": ("node_refusal", "input"),
    "unsupported_aggregation": ("node_refusal", "input"),
    "unknown_column": ("node_refusal", "input"),
    "malformed_model_ref": ("node_refusal", "input"),
    "undeclared_match_parameters": ("node_refusal", "input"),
    "invalid_parameter": ("node_refusal", "input"),
    "cross_origin_credentialed_redirect": ("node_refusal", "input_upstream_response"),
    "unsupported_redirect_scheme": ("node_refusal", "input_upstream_response"),
    "redirect_limit": ("node_refusal", "input_upstream_response"),
    "image_provenance_mismatch": ("internal", "materialization_integrity"),
}
_LOCAL_MAPPING: dict[str, tuple[str, str]] = {
    "provider_unavailable": ("node_refusal", "executor"),
    "secret_epoch_unavailable": ("operational", "custody"),
    "secret_resolver_not_installed": ("operational", "custody"),
    "secret_reference_invalid": ("node_refusal", "binding_input"),
    "provider_process_lease_invalid": ("internal", "executor"),
    "provider_process_lease_missing": ("internal", "executor"),
    "provider_process_lease_echo_failed": ("internal", "executor"),
    "provider_process_lease_echo_mismatch": ("internal", "executor"),
    "provider_process_group_survived_recovery": ("internal", "executor"),
    "provider_runtime_not_in_materialization": ("internal", "materialization_integrity"),
    "budget_max_provider_calls_exceeded": ("node_refusal", "input_budget"),
}

PROVIDER_RUNTIME_REFUSAL_CODES = frozenset(get_args(ProviderRuntimeRefusalCodeV1))
if set(_MAPPING) != PROVIDER_RUNTIME_REFUSAL_CODES:  # pragma: no cover - import-time guard
    raise RuntimeError("Provider outcome mapping does not equal the mirrored runtime vocabulary")

ABSORBABLE_PROVIDER_REFUSALS = frozenset(
    {
        "unclaimed_bucket",
        "unclassified_input",
        "provider_declined",
        "insufficient_series_length",
        "non_finite_input",
        "degenerate_scale",
        "mismatched_lengths",
        "unknown_method",
        "unknown_test_name",
        "declared_family_mismatch",
        "unsupported_aggregation",
        "unknown_column",
        "malformed_model_ref",
        "undeclared_match_parameters",
        "invalid_parameter",
    }
)
if any(_MAPPING[code] != ("node_refusal", "input") for code in ABSORBABLE_PROVIDER_REFUSALS):
    raise RuntimeError("absorbable Provider refusals must be input-attributed node refusals")


def provider_canonical_value(value: Any) -> CanonicalValue:
    """Translate the wider finite-number runtime JSON law into Playbill canonical values."""

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


def map_provider_refusal(
    code: str,
    *,
    message: str,
    detail: Any,
) -> ProviderInvocationOutcomeV1:
    outcome_class, attribution = (_MAPPING | _LOCAL_MAPPING).get(
        code,
        ("internal", "executor_mirror_drift"),
    )
    normalized_code = (
        code if code in _MAPPING or code in _LOCAL_MAPPING else "provider_refusal_taxonomy_unknown"
    )
    return ProviderInvocationOutcomeV1(
        status="refused",
        outcome_class=outcome_class,  # type: ignore[arg-type]
        attribution=attribution,  # type: ignore[arg-type]
        code=normalized_code,
        message=message,
        detail=provider_canonical_value(detail),
    )


def map_provider_envelope(envelope: ProviderRuntimeResultEnvelopeV1) -> ProviderInvocationOutcomeV1:
    if envelope.status == "ok":
        return ProviderInvocationOutcomeV1(
            status="ok",
            outcome_class="ok",
            attribution="none",
        )
    if envelope.status == "refused":
        assert envelope.refusal is not None
        return map_provider_refusal(
            envelope.refusal.code,
            message=envelope.refusal.message,
            detail=envelope.refusal.detail,
        )
    assert envelope.error is not None
    return ProviderInvocationOutcomeV1(
        status="error",
        outcome_class="operational",
        attribution="provider_runtime",
        code="provider_execution_error",
        message=envelope.error.message,
        detail=provider_canonical_value(
            {"kind": envelope.error.kind, "detail": envelope.error.detail}
        ),
    )


def provider_refusal_is_absorbable(outcome: ProviderInvocationOutcomeV1) -> bool:
    return (
        outcome.status == "refused"
        and outcome.outcome_class == "node_refusal"
        and outcome.attribution == "input"
        and outcome.code in ABSORBABLE_PROVIDER_REFUSALS
    )


__all__ = [
    "ABSORBABLE_PROVIDER_REFUSALS",
    "PROVIDER_RUNTIME_REFUSAL_CODES",
    "map_provider_envelope",
    "map_provider_refusal",
    "provider_canonical_value",
    "provider_refusal_is_absorbable",
]
