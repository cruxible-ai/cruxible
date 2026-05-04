"""Placeholder providers for the case-law monitoring kit."""

from __future__ import annotations

from typing import Any, NoReturn

from cruxible_core.provider.types import ProviderContext


def extract_holdings_from_opinions(
    _input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    _raise_not_implemented("extract_holdings_from_opinions")


def link_holdings_to_statutes(
    _input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    _raise_not_implemented("link_holdings_to_statutes")


def map_holdings_to_issues(
    _input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    _raise_not_implemented("map_holdings_to_issues")


def classify_opinion_treatment(
    _input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    _raise_not_implemented("classify_opinion_treatment")


def assess_argument_impact(
    _input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    _raise_not_implemented("assess_argument_impact")


def scope_matters_to_statutes(
    _input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    _raise_not_implemented("scope_matters_to_statutes")


def assess_matter_impact(
    _input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    _raise_not_implemented("assess_matter_impact")


def assess_filing_response_obligations(
    _input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    _raise_not_implemented("assess_filing_response_obligations")


def route_review_items(
    _input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    _raise_not_implemented("route_review_items")


def _raise_not_implemented(provider_name: str) -> NoReturn:
    raise NotImplementedError(
        f"Case-law monitoring kit provider '{provider_name}' is not implemented yet."
    )
