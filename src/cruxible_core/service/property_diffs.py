"""Shared property diff helpers for service read and mutation models."""

from __future__ import annotations

from typing import Any

from cruxible_core.service.types import PropertyChangeItem, PropertyDeltaResult


def property_delta(
    proposed: dict[str, Any],
    current: dict[str, Any],
) -> PropertyDeltaResult:
    """Return key-level property delta between proposed and current values."""
    proposed_keys = set(proposed)
    current_keys = set(current)
    shared = proposed_keys & current_keys
    return PropertyDeltaResult(
        added=sorted(proposed_keys - current_keys),
        removed=sorted(current_keys - proposed_keys),
        changed=sorted(key for key in shared if proposed[key] != current[key]),
        unchanged=sorted(key for key in shared if proposed[key] == current[key]),
    )


def property_value_changes(
    proposed: dict[str, Any],
    current: dict[str, Any],
    *,
    include_added: bool = True,
    include_removed: bool = False,
) -> list[PropertyChangeItem]:
    """Return value-level property changes using the same delta semantics."""
    delta = property_delta(proposed, current)
    property_names = list(delta.changed)
    if include_added:
        property_names.extend(delta.added)
    if include_removed:
        property_names.extend(delta.removed)

    return [
        PropertyChangeItem(
            property=property_name,
            from_value=current.get(property_name),
            to_value=proposed.get(property_name),
        )
        for property_name in sorted(property_names)
    ]
