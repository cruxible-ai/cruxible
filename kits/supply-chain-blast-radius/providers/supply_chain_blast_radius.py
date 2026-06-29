"""Placeholder providers for the supply-chain blast-radius kit."""

from __future__ import annotations

from typing import Any, NoReturn

from cruxible_core.provider.types import ProviderContext


def load_seed_data(
    _input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    _raise_not_implemented("load_seed_data")


def load_incident_feed(
    _input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    _raise_not_implemented("load_incident_feed")


def assess_incident_supplier_scope(
    _input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    _raise_not_implemented("assess_incident_supplier_scope")


def fetch_inventory_positions(
    _input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    _raise_not_implemented("fetch_inventory_positions")


def assess_buffer_coverage(
    _input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    _raise_not_implemented("assess_buffer_coverage")


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


def assess_incident_product_exposure(
    _input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    _raise_not_implemented("assess_incident_product_exposure")


def _raise_not_implemented(provider_name: str) -> NoReturn:
    raise NotImplementedError(
        f"Supply-chain blast-radius kit provider '{provider_name}' is a scaffold placeholder; "
        "implement it or supply seed data before running this kit's workflows."
    )
