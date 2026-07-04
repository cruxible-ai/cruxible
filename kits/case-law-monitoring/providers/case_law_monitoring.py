"""Case-law monitoring kit providers.

Placeholders pending implementation: each raises NotImplementedError with the
contract it must satisfy. Implementations must be deterministic heuristics
over curated metadata (LLM-outside); fetch_courtlistener_cluster is the one
network provider and must fall back to the bundled act-two fixture offline.
"""

from typing import Any, NoReturn


def load_corpus_seed(payload: dict[str, Any]) -> dict[str, Any]:
    _raise_not_implemented("load_corpus_seed")


def fetch_courtlistener_cluster(payload: dict[str, Any]) -> dict[str, Any]:
    _raise_not_implemented("fetch_courtlistener_cluster")


def load_docket_feed(payload: dict[str, Any]) -> dict[str, Any]:
    _raise_not_implemented("load_docket_feed")


def load_case_outcome_feed(payload: dict[str, Any]) -> dict[str, Any]:
    _raise_not_implemented("load_case_outcome_feed")


def sweep_stale_deadlines(payload: dict[str, Any]) -> dict[str, Any]:
    _raise_not_implemented("sweep_stale_deadlines")


def extract_holdings_from_opinions(payload: dict[str, Any]) -> dict[str, Any]:
    _raise_not_implemented("extract_holdings_from_opinions")


def link_holdings_to_statutes(payload: dict[str, Any]) -> dict[str, Any]:
    _raise_not_implemented("link_holdings_to_statutes")


def map_holdings_to_issues(payload: dict[str, Any]) -> dict[str, Any]:
    _raise_not_implemented("map_holdings_to_issues")


def classify_opinion_treatment(payload: dict[str, Any]) -> dict[str, Any]:
    _raise_not_implemented("classify_opinion_treatment")


def assess_argument_impact(payload: dict[str, Any]) -> dict[str, Any]:
    _raise_not_implemented("assess_argument_impact")


def scope_matters_to_statutes(payload: dict[str, Any]) -> dict[str, Any]:
    _raise_not_implemented("scope_matters_to_statutes")


def assess_matter_impact(payload: dict[str, Any]) -> dict[str, Any]:
    _raise_not_implemented("assess_matter_impact")


def assess_filing_response_obligations(payload: dict[str, Any]) -> dict[str, Any]:
    _raise_not_implemented("assess_filing_response_obligations")


def route_review_work(payload: dict[str, Any]) -> dict[str, Any]:
    _raise_not_implemented("route_review_work")


def _raise_not_implemented(provider_name: str) -> NoReturn:
    raise NotImplementedError(
        f"Provider '{provider_name}' is a placeholder. Implement it or wire "
        "your own data source before running this workflow."
    )
