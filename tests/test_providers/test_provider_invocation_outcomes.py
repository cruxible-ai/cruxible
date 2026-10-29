from __future__ import annotations

import pytest

from cruxible_client.contracts.provider_operations import ProviderOperation
from cruxible_core.providers.provider_outcomes import (
    PROVIDER_RUNTIME_REFUSAL_CODES,
    map_operation_refusal,
    map_provider_envelope,
    map_provider_refusal,
    operation_refusal_is_declared,
    provider_canonical_value,
    provider_refusal_is_absorbable,
)
from cruxible_core.providers.provider_runtime_contract import (
    PROVIDER_RUNTIME_HARNESS_REFUSAL_CODES,
    PROVIDER_RUNTIME_RESERVED_REFUSAL_CODES,
    ProviderRuntimeProviderErrorPayload,
    ProviderRuntimeRefusal,
    ProviderRuntimeResultEnvelope,
)

_PIN = "sha256:" + "1" * 64
OPERATION = ProviderOperation.model_validate(
    {
        "name": "demo.score",
        "in": {"name": "demo.in", "version": 1, "digest": _PIN},
        "out": {"name": "demo.out", "version": 1, "digest": _PIN},
        "effect": "pure",
        "refusals": {
            "too_long": {
                "contract": {"name": "demo.too_long", "version": 1, "digest": _PIN},
                "absorbable": True,
            },
            "engine_unavailable": {
                "contract": {"name": "demo.engine_unavailable", "version": 1, "digest": _PIN},
            },
        },
        "entrypoint": "demo.score:Score",
    }
)


def _valid(code: str, detail: object) -> tuple[str, ...] | None:
    return None


def _refused(code: str, detail: object | None = None) -> ProviderRuntimeResultEnvelope:
    return ProviderRuntimeResultEnvelope(
        protocol_version="2.0",
        run_id="run-1",
        status="refused",
        refusal=ProviderRuntimeRefusal(code=code, message="refused", detail=detail or {}),
    )


def test_the_outcome_map_is_exactly_the_runtime_reserved_vocabulary() -> None:
    assert PROVIDER_RUNTIME_REFUSAL_CODES == PROVIDER_RUNTIME_RESERVED_REFUSAL_CODES
    assert {"undeclared_operation", "undeclared_refusal"} <= PROVIDER_RUNTIME_REFUSAL_CODES
    # No shared decline vocabulary: provider-domain codes belong to operations.
    for code in ("provider_declined", "invalid_parameter", "unclaimed_bucket"):
        assert code not in PROVIDER_RUNTIME_REFUSAL_CODES


@pytest.mark.parametrize("code", sorted(PROVIDER_RUNTIME_REFUSAL_CODES))
def test_every_runtime_refusal_maps_without_falling_through(code: str) -> None:
    outcome = map_provider_refusal(code, message="refused", detail={"code": code})
    assert outcome.code == code
    assert outcome.outcome_class != "ok"
    assert outcome.attribution not in {"none", "operation_declaration"}


def test_a_declared_operation_refusal_keeps_its_own_code_and_absorbability() -> None:
    outcome = map_provider_envelope(
        _refused("too_long", {"limit": 3}), operation=OPERATION, check_detail=_valid
    )
    assert (outcome.outcome_class, outcome.attribution, outcome.code) == (
        "node_refusal",
        "operation_declaration",
        "too_long",
    )
    assert operation_refusal_is_declared(OPERATION, outcome)
    assert provider_refusal_is_absorbable(OPERATION, outcome)
    other = map_provider_envelope(
        _refused("engine_unavailable"), operation=OPERATION, check_detail=_valid
    )
    assert operation_refusal_is_declared(OPERATION, other)
    assert not provider_refusal_is_absorbable(OPERATION, other)


def test_a_declared_refusal_is_recognised_only_after_its_detail_contract() -> None:
    def invalid(code: str, detail: object) -> tuple[str, ...] | None:
        assert (code, detail) == ("too_long", {"limit": "three"})
        return ("limit: wrong_type",)

    outcome = map_provider_envelope(
        _refused("too_long", {"limit": "three"}), operation=OPERATION, check_detail=invalid
    )
    assert (outcome.outcome_class, outcome.attribution, outcome.code) == (
        "node_refusal",
        "implementation",
        "contract_output_refused",
    )
    assert "demo.too_long@1" in (outcome.message or "")
    assert not operation_refusal_is_declared(OPERATION, outcome)
    assert not provider_refusal_is_absorbable(OPERATION, outcome)


def test_an_undeclared_code_is_undeclared_refusal_never_renamed_or_believed() -> None:
    def never(code: str, detail: object) -> tuple[str, ...] | None:
        raise AssertionError("an undeclared code has no Contract to check")

    outcome = map_provider_envelope(
        _refused("future_refusal"), operation=OPERATION, check_detail=never
    )
    assert (outcome.outcome_class, outcome.attribution, outcome.code) == (
        "internal",
        "implementation",
        "undeclared_refusal",
    )
    assert outcome.detail == {"operation": "demo.score", "refused_code": "future_refusal"}


@pytest.mark.parametrize(
    "code", sorted(PROVIDER_RUNTIME_RESERVED_REFUSAL_CODES - PROVIDER_RUNTIME_HARNESS_REFUSAL_CODES)
)
def test_an_envelope_asserting_an_executor_runtime_code_is_a_protocol_violation(code: str) -> None:
    outcome = map_provider_envelope(_refused(code), operation=OPERATION, check_detail=_valid)
    assert (outcome.outcome_class, outcome.attribution, outcome.code) == (
        "internal",
        "implementation",
        "provider_protocol_violation",
    )
    assert outcome.detail == {"operation": "demo.score", "refused_code": code}


@pytest.mark.parametrize("code", sorted(PROVIDER_RUNTIME_HARNESS_REFUSAL_CODES))
def test_an_envelope_carrying_a_harness_code_maps_as_the_harness_observation(code: str) -> None:
    outcome = map_operation_refusal(
        OPERATION, code, message="harness", detail={}, check_detail=_valid
    )
    assert outcome == map_provider_refusal(code, message="harness", detail={})


def test_unknown_core_refusal_and_error_envelope_are_not_implementation_faults() -> None:
    unknown = map_provider_refusal("future_refusal", message="x", detail={})
    assert (unknown.outcome_class, unknown.attribution, unknown.code) == (
        "internal",
        "executor_mirror_drift",
        "provider_refusal_taxonomy_unknown",
    )
    error = map_provider_envelope(
        ProviderRuntimeResultEnvelope(
            protocol_version="2.0",
            run_id="run-1",
            status="error",
            error=ProviderRuntimeProviderErrorPayload(kind="upstream", message="down"),
        ),
        operation=OPERATION,
        check_detail=_valid,
    )
    assert (error.outcome_class, error.attribution, error.code) == (
        "operational",
        "provider_runtime",
        "provider_execution_error",
    )


@pytest.mark.parametrize(
    "code",
    [
        "provider_process_lease_invalid",
        "provider_process_lease_missing",
        "provider_process_lease_echo_failed",
        "provider_process_lease_echo_mismatch",
        "provider_process_group_survived_recovery",
    ],
)
def test_every_process_fence_refusal_has_a_typed_local_outcome(code: str) -> None:
    outcome = map_provider_refusal(code, message="fence refused", detail={})
    assert outcome.code == code
    assert outcome.outcome_class == "internal"
    assert outcome.attribution == "executor"


def test_ok_refusal_and_finite_float_translation_preserve_wire_facts() -> None:
    ok = map_provider_envelope(
        ProviderRuntimeResultEnvelope(
            protocol_version="2.0",
            run_id="run-1",
            status="ok",
            output={"score": 0.5},
        ),
        operation=OPERATION,
        check_detail=_valid,
    )
    assert ok.status == "ok"
    refused = map_provider_envelope(
        _refused("too_long", {"score": 0.5}), operation=OPERATION, check_detail=_valid
    )
    assert refused.detail == {"score": {"$provider_float_v1": "0.5"}}
    assert provider_canonical_value({"score": 0.5}) == refused.detail
