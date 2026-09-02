"""Round-6 regression for diagnostics-only process-table observations."""

from __future__ import annotations

from pathlib import Path

from cruxible_core.playbill.provider_process_leases import ProviderLocalRuntimeRefused
from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator


def test_transient_observation_diagnostics_never_degrade_or_serialize_the_lane(
    short_root: Path,
) -> None:
    operator = ProviderRuntimeOperator(short_root)
    store = operator.process_leases
    assert store is not None

    store.record_diagnostic(
        ProviderLocalRuntimeRefused(
            "provider_process_lease_invalid",
            "transient process-table hiccup",
        )
    )

    assert store.diagnostics[-1][0] == "provider_process_lease_invalid"
    assert operator._observation_diagnostic_count == 1
    assert operator._last_observation_diagnostic == (
        "provider_process_lease_invalid",
        "provider_process_lease_invalid: transient process-table hiccup",
    )
    state, code, detail = operator.lane_status()
    assert (state, code) == ("available", None)
    assert detail is not None and "count=1" in detail
    assert "transient process-table hiccup" in detail

    operator._begin_invocation()
    operator._begin_invocation()
    assert operator._in_flight == 2
    operator._end_invocation()
    operator._end_invocation()
    assert operator.lane_status() == (state, code, detail)
