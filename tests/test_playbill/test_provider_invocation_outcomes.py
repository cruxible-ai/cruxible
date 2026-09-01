from __future__ import annotations

from typing import get_args

import pytest

from cruxible_core.playbill.provider_outcomes import (
    ABSORBABLE_PROVIDER_REFUSALS,
    PROVIDER_RUNTIME_REFUSAL_CODES,
    map_provider_envelope,
    map_provider_refusal,
    provider_canonical_value,
    provider_refusal_is_absorbable,
)
from cruxible_core.playbill.provider_runtime_contract import (
    ProviderRuntimeProviderErrorPayloadV1,
    ProviderRuntimeRefusalCodeV1,
    ProviderRuntimeRefusalV1,
    ProviderRuntimeResultEnvelopeV1,
)


def test_outcome_map_is_exhaustive_and_absorption_is_exact() -> None:
    assert PROVIDER_RUNTIME_REFUSAL_CODES == frozenset(get_args(ProviderRuntimeRefusalCodeV1))
    actual = {
        code
        for code in PROVIDER_RUNTIME_REFUSAL_CODES
        if provider_refusal_is_absorbable(map_provider_refusal(code, message="x", detail={}))
    }
    assert actual == ABSORBABLE_PROVIDER_REFUSALS


@pytest.mark.parametrize("code", sorted(PROVIDER_RUNTIME_REFUSAL_CODES))
def test_every_runtime_refusal_maps_without_falling_through(code: str) -> None:
    outcome = map_provider_refusal(code, message="refused", detail={"code": code})
    assert outcome.code == code
    assert outcome.outcome_class != "ok"
    assert outcome.attribution != "none"


def test_unknown_refusal_and_error_envelope_are_not_implementation_faults() -> None:
    unknown = map_provider_refusal("future_refusal", message="x", detail={})
    assert (unknown.outcome_class, unknown.attribution, unknown.code) == (
        "internal",
        "executor_mirror_drift",
        "provider_refusal_taxonomy_unknown",
    )
    error = map_provider_envelope(
        ProviderRuntimeResultEnvelopeV1(
            protocol_version="1.0",
            run_id="run-1",
            status="error",
            error=ProviderRuntimeProviderErrorPayloadV1(kind="upstream", message="down"),
        )
    )
    assert (error.outcome_class, error.attribution, error.code) == (
        "operational",
        "provider_runtime",
        "provider_execution_error",
    )


def test_ok_refusal_and_finite_float_translation_preserve_wire_facts() -> None:
    ok = map_provider_envelope(
        ProviderRuntimeResultEnvelopeV1(
            protocol_version="1.0",
            run_id="run-1",
            status="ok",
            output={"score": 0.5},
        )
    )
    assert ok.status == "ok"
    refused = map_provider_envelope(
        ProviderRuntimeResultEnvelopeV1(
            protocol_version="1.0",
            run_id="run-1",
            status="refused",
            refusal=ProviderRuntimeRefusalV1(
                code="invalid_parameter",
                message="bad",
                detail={"score": 0.5},
            ),
        )
    )
    assert refused.detail == {"score": {"$provider_float_v1": "0.5"}}
    assert provider_canonical_value({"score": 0.5}) == refused.detail
