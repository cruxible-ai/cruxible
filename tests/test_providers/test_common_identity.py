"""Tests for common identity providers."""

from __future__ import annotations

from cruxible_core.provider.types import ProviderContext
from cruxible_core.providers.common.identifiers import normalize_identifiers
from cruxible_core.providers.common.identity import resolve_entities_by_alias


def _context() -> ProviderContext:
    return ProviderContext(
        workflow_name="wf",
        step_id="step",
        provider_name="provider",
        provider_version="1.0.0",
    )


def test_resolve_entities_by_alias_matches_normalized_names() -> None:
    payload = resolve_entities_by_alias(
        {
            "records": [{"id": "r1", "name": "Apache httpd"}],
            "entities": [
                {"entity_id": "p1", "name": "Apache HTTP Server"},
                {"entity_id": "p2", "name": "Google Chrome"},
            ],
            "record_alias_fields": ["name"],
            "entity_alias_fields": ["name"],
            "threshold": 0.8,
        },
        _context(),
    )

    assert payload["matches"][0]["record_id"] == "r1"
    assert payload["matches"][0]["entity_id"] == "p1"
    assert payload["summary"] == {
        "records": 1,
        "entities": 2,
        "matches": 1,
        "unmatched": 0,
        "ambiguous": 0,
    }


def test_normalize_identifiers_adds_normalized_fields_and_diagnostics() -> None:
    payload = normalize_identifiers(
        {
            "items": [
                {"cve": "cve_2024_12345", "due": "04/27/2026", "sku": "ab 12"},
                {"cve": "not-a-cve", "due": "bad-date", "sku": "xy"},
            ],
            "fields": {"cve": "cve", "due": "date", "sku": "sku"},
        },
        _context(),
    )

    assert payload["items"][0]["cve_normalized"] == "CVE-2024-12345"
    assert payload["items"][0]["due_normalized"] == "2026-04-27"
    assert payload["items"][0]["sku_normalized"] == "AB-12"
    assert [item["code"] for item in payload["diagnostics"]] == [
        "invalid_cve",
        "invalid_date",
    ]
