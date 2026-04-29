"""Placeholder providers for the supply-chain blast-radius kit."""

from __future__ import annotations

from typing import Any, NoReturn

from cruxible_core.provider.types import ProviderContext


def load_seed_data(
    _input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    _raise_not_implemented("load_seed_data")


def assess_incident_supplier_scope(
    _input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    _raise_not_implemented("assess_incident_supplier_scope")


def assess_incident_component_cascade(
    _input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    _raise_not_implemented("assess_incident_component_cascade")


def assess_incident_assembly_cascade(
    _input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    _raise_not_implemented("assess_incident_assembly_cascade")


def assess_incident_product_cascade(
    _input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    _raise_not_implemented("assess_incident_product_cascade")


def _raise_not_implemented(provider_name: str) -> NoReturn:
    raise NotImplementedError(
        f"Supply-chain blast-radius kit provider '{provider_name}' is not implemented yet."
    )
