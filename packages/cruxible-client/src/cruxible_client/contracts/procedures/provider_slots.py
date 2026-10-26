"""The fit shared by slotted Procedure admission, Lines and build moves."""

from cruxible_client.contracts.provider_interfaces import (
    ProviderInterfaceRegistrationAny,
    interface_definition_digest,
)
from cruxible_client.contracts.providers import ProviderV2, provider_interface_pin


def fitting_implementation(
    provider: ProviderV2, interface: ProviderInterfaceRegistrationAny
) -> str | None:
    """One live build must declare the interface and implement it exactly once."""
    if provider.lifecycle.state != "live" or interface.lifecycle.state != "live":
        return None
    declared = provider_interface_pin(provider, interface.interface_id)
    if declared is None or declared.target != interface.identity:
        return None
    matches = tuple(
        row.implementation_digest
        for row in provider.implementations
        if row.interface_id == interface.interface_id
        and row.interface_digest == interface_definition_digest(interface)
    )
    return matches[0] if len(matches) == 1 else None
