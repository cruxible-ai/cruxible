"""Read service for instance-local boundary telemetry."""

from __future__ import annotations

from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.telemetry.types import BoundaryTelemetrySummary


def service_telemetry_summary(instance: InstanceProtocol) -> BoundaryTelemetrySummary:
    """Return aggregate counters for traffic crossing core-owned surfaces."""
    return instance.get_boundary_telemetry_summary()
