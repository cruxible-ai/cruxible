"""Compatibility shim for case-law monitoring demo providers."""

from cruxible_core.demo_providers.case_law import (
    assess_opinion_matter_impact,
    assess_position_authority,
    classify_filing_obligation,
    extract_matter_statutes,
    load_case_outcomes,
    load_firm_seed_data,
    load_public_courtlistener_rows,
)

__all__ = [
    "load_public_courtlistener_rows",
    "load_firm_seed_data",
    "load_case_outcomes",
    "extract_matter_statutes",
    "assess_opinion_matter_impact",
    "assess_position_authority",
    "classify_filing_obligation",
]
