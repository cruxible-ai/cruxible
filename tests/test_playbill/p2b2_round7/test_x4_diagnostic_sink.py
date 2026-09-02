"""Round-7 regression for diagnostic-sink containment."""

from __future__ import annotations

from cruxible_core.playbill.provider_local_runtime import _observe_descendants_best_effort
from cruxible_core.playbill.provider_process_leases import ProviderLocalRuntimeRefused


def test_a_raising_diagnostic_sink_cannot_change_the_observation_outcome() -> None:
    def observe() -> None:
        raise ProviderLocalRuntimeRefused(
            "provider_process_lease_invalid",
            "transient table failure",
        )

    def raising_sink(_failure: ProviderLocalRuntimeRefused) -> None:
        raise RuntimeError("planted diagnostic sink failure")

    _observe_descendants_best_effort(observe, diagnostic_sink=raising_sink)
