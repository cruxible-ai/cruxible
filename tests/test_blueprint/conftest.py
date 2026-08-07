"""Shared fixtures for the blueprint-format tests."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
KEV_BLUEPRINT_PATH = FIXTURES / "kev-triage.blueprint.yaml"

_MINIMAL_DOCUMENT: dict[str, Any] = {
    "blueprint": {
        "id": "widget-triage",
        "version": "1.0.0",
        "publisher": "acme",
        "description": "Score widgets that need attention.",
        "provenance": {"origin": "curated", "evidence": ["receipt:RCP-0001"]},
    },
    "contracts": {
        "acme.widget-triage.ScopeInput": {"fields": {"subject_id": {"type": "string"}}},
        "acme.widget-triage.QueryEnvelope": {
            "fields": {"results": {"type": "json"}},
            "allow_extra": True,
        },
        "acme.widget-triage.ScoreInput": {"fields": {"rows": {"type": "json"}}},
        "acme.widget-triage.ScoreResult": {"fields": {"scores": {"type": "json"}}},
    },
    "dependencies": {
        "reference_states": [{"state_ref": "widget-reference@2026.30"}],
        "entity_types": ["Widget"],
        "relationship_types": ["widget_owned_by"],
        "enums": [{"name": "criticality", "ordered": "low_to_high"}],
        "kits": [{"kit_id": "widget-triage", "min_version": "0.2.8"}],
    },
    "query_slots": {
        "subject_rows": {
            "description": "Widgets in scope.",
            "install_as": "acme__widget_triage__subject_rows",
            "param_contract": "acme.widget-triage.ScopeInput",
            "result_contract": "acme.widget-triage.QueryEnvelope",
            "default": {
                "mode": "collection",
                "returns": "Widget",
                "result_shape": "entity",
            },
        }
    },
    "slots": {
        "scorer": {
            "description": "Score the widgets in scope.",
            "contract_in": "acme.widget-triage.ScoreInput",
            "contract_out": "acme.widget-triage.ScoreResult",
            "billing": ["platform", "byok"],
            "capabilities": ["deterministic"],
            "outcome_metric": {
                "outcome_profile": "widget_resolution",
                "metric": "precision_recall",
            },
        }
    },
    "procedures": [
        {
            "name": "widget_score",
            "description": "Score every widget in scope.",
            "contract_in": "acme.widget-triage.ScopeInput",
            "contract_out": "acme.widget-triage.ScoreResult",
            "declared_tier": "governed_write",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 1},
            "steps": [
                {
                    "id": "rows",
                    "query": "subject_rows",
                    "params": {"subject_id": "$input.subject_id"},
                    "as": "rows",
                },
                {
                    "id": "score",
                    "provider": "scorer",
                    "input": {"rows": "$steps.rows.results"},
                    "as": "score",
                },
            ],
            "returns": "score",
        }
    ],
    "install_checks": ["contracts_load", "all_required_slots_bindable"],
}


def minimal_document() -> dict[str, Any]:
    """Return a fresh, valid blueprint document to mutate per test."""
    return deepcopy(_MINIMAL_DOCUMENT)


@pytest.fixture
def document() -> dict[str, Any]:
    return minimal_document()
