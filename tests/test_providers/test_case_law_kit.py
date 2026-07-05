from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from cruxible_core.config.loader import load_config


ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "kits/case-law-monitoring/config.yaml"
PROVIDER_PATH = ROOT / "kits/case-law-monitoring/providers/case_law_monitoring.py"
SEED_PATH = ROOT / "kits/case-law-monitoring/data/seed"


def _load_provider_module() -> Any:
    spec = importlib.util.spec_from_file_location("case_law_monitoring_provider", PROVIDER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


providers = _load_provider_module()


def _seed_json(name: str) -> dict[str, Any]:
    data = json.loads((SEED_PATH / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("CRUXIBLE_KIT_DEV_RESOLVE", "1")
    import cruxible_core.runtime  # noqa: F401

    return load_config(CONFIG_PATH)


ENTITY_OUTPUTS = {
    "opinions": "Opinion",
    "courts": "Court",
    "judges": "Judge",
    "statutes": "Statute",
    "legal_issues": "LegalIssue",
    "clients": "Client",
    "matters": "Matter",
    "arguments": "Argument",
    "filings": "Filing",
    "deadlines": "Deadline",
    "case_outcomes": "CaseOutcome",
}

REL_OUTPUTS = {
    "opinion_decided_by_judge_edges": "opinion_decided_by_judge",
    "opinion_cites_opinion_edges": "opinion_cites_opinion",
    "argument_cites_opinion_edges": "argument_cites_opinion",
    "outcome_resolved_argument_edges": "outcome_resolved_argument",
}


def test_seed_evidence_quotes_are_verbatim_opinion_text() -> None:
    evidence_objects = [
        row["evidence"] for row in _seed_json("holdings.json")["curated_holdings"]
    ]
    evidence_objects.extend(
        row["evidence"]
        for row in _seed_json("act2_update.json")["opinion_cites_opinion_edges"]
    )

    assert len(evidence_objects) == 14
    for evidence in evidence_objects:
        text_path = SEED_PATH / evidence["path"]
        text = text_path.read_text(encoding="utf-8")
        expected_sha = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert evidence["text_sha256"] == expected_sha
        assert evidence["quote"] in text
        assert text[evidence["char_start"] : evidence["char_end"]] == evidence["quote"]
        assert evidence["source_artifact_id"] == f"opinion_text_{evidence['opinion_id']}"
        assert evidence["chunk_id"]
        assert evidence["expected_content_hash"].startswith("sha256:")


def test_analysis_providers_emit_source_evidence_fields() -> None:
    corpus = providers.load_corpus_seed({})
    update = providers.load_corpus_update({})
    opinions = corpus["opinions"] + update["opinions"]

    holdings = providers.extract_holdings_from_opinions({"opinions": opinions})["items"]
    by_holding = {row["holding_id"]: row for row in holdings}
    chevron = by_holding["hold_chevron_deference"]
    assert chevron["evidence"]["quote"] in (SEED_PATH / chevron["evidence"]["path"]).read_text(
        encoding="utf-8"
    )
    assert chevron["evidence_refs"][0]["source"] == "source_artifact"
    assert chevron["evidence_refs"][0]["artifact_id"] == chevron["evidence"]["source_artifact_id"]

    treatments = providers.classify_opinion_treatment({
        "opinions": opinions,
        "citation_edges": update["opinion_cites_opinion_edges"],
    })["items"]
    by_pair = {(row["source_opinion_id"], row["cited_opinion_id"]): row for row in treatments}
    loper_chevron = by_pair[("op_loper_bright", "op_chevron")]
    assert loper_chevron["evidence"]["quote"] == "Chevron is overruled."
    assert loper_chevron["evidence_refs"][0]["metadata"]["text_sha256"] == loper_chevron[
        "evidence"
    ]["text_sha256"]


def test_seed_loaders_emit_declared_auto_property_keys(config: Any) -> None:
    corpus = providers.load_corpus_seed({})
    update = providers.load_corpus_update({})
    docket = providers.load_docket_feed({})
    outcomes = providers.load_case_outcome_feed({})

    assert len(corpus["opinions"]) == 10
    assert len(update["opinions"]) == 1
    assert len(docket["filings"]) == 4
    assert len(outcomes["case_outcomes"]) == 2

    for payload in (corpus, update, docket, outcomes):
        for field_name, entity_type in ENTITY_OUTPUTS.items():
            if payload.get(field_name):
                _assert_rows_have_keys(
                    payload[field_name],
                    config.entity_types[entity_type].properties,
                    label=field_name,
                )
        for field_name, relationship_type in REL_OUTPUTS.items():
            if payload.get(field_name):
                rel = config.get_relationship(relationship_type)
                assert rel is not None
                _assert_rows_have_keys(payload[field_name], rel.properties, label=field_name)


def test_analysis_providers_emit_auto_property_keys_and_loper_overrules_chevron(config: Any) -> None:
    corpus = providers.load_corpus_seed({})
    update = providers.load_corpus_update({})
    opinions = corpus["opinions"] + update["opinions"]

    holding_candidates = providers.extract_holdings_from_opinions({"opinions": opinions})
    _assert_rows_have_keys(
        holding_candidates["items"],
        config.entity_types["Holding"].properties,
        label="holding_candidates",
    )
    _assert_relationship_item_keys(
        config,
        holding_candidates["items"],
        "opinion_has_holding",
        label="holding_candidates",
    )

    statute_links = providers.link_holdings_to_statutes({
        "holdings": holding_candidates["items"],
        "statutes": corpus["statutes"],
        "opinion_holding_edges": [],
    })
    _assert_relationship_item_keys(
        config,
        statute_links["items"],
        "holding_interprets_statute",
        label="statute_links",
    )

    issue_links = providers.map_holdings_to_issues({
        "holdings": holding_candidates["items"],
        "issues": corpus["legal_issues"],
        "statute_edges": statute_links["items"],
        "statute_issue_edges": corpus["statute_governs_issue_edges"],
    })
    assert issue_links["items"], "graph statute_governs_issue edges must drive issue links"
    _assert_relationship_item_keys(
        config,
        issue_links["items"],
        "holding_addresses_issue",
        label="issue_links",
    )

    treatments = providers.classify_opinion_treatment({
        "opinions": opinions,
        "citation_edges": update["opinion_cites_opinion_edges"],
    })
    _assert_relationship_item_keys(
        config,
        treatments["items"],
        "opinion_treats_opinion",
        label="treatments",
    )
    assert any(
        row["source_opinion_id"] == "op_loper_bright"
        and row["cited_opinion_id"] == "op_chevron"
        and row["treatment"] == "overrules"
        for row in treatments["items"]
    )

    argument_impacts = providers.assess_argument_impact({
        "holdings": holding_candidates["items"],
        "arguments": corpus["arguments"],
        "holding_issue_edges": issue_links["items"],
        "argument_issue_edges": corpus["argument_raises_issue_edges"],
    })
    _assert_relationship_item_keys(
        config,
        argument_impacts["items"],
        "holding_supports_argument",
        label="argument_support",
    )
    _assert_relationship_item_keys(
        config,
        argument_impacts["items"],
        "holding_undermines_argument",
        label="argument_risk",
    )

    matter_scope = providers.scope_matters_to_statutes({
        "matters": corpus["matters"],
        "statutes": corpus["statutes"],
    })
    _assert_relationship_item_keys(
        config,
        matter_scope["items"],
        "matter_turns_on_statute",
        label="matter_scope",
    )
    scoped_chevron_matters = {
        row["matter_id"]
        for row in matter_scope["items"]
        if row["statute_id"] == "stat_chevron_doctrine"
    }
    scoped_apa_matters = {
        row["matter_id"]
        for row in matter_scope["items"]
        if row["statute_id"] == "stat_apa_judicial_review"
    }
    all_matter_ids = {row["matter_id"] for row in corpus["matters"]}
    assert scoped_chevron_matters == all_matter_ids
    assert scoped_apa_matters == all_matter_ids

    matter_impacts = providers.assess_matter_impact({
        "opinions": opinions,
        "matters": corpus["matters"],
        "support_edges": [],
        "undermine_edges": argument_impacts["items"],
        "opinion_holding_edges": holding_candidates["items"],
        "argument_matter_edges": corpus["argument_in_matter_edges"],
        "treatment_edges": treatments["items"],
        "matter_statute_edges": matter_scope["items"],
        "opinion_court_edges": corpus["opinion_from_court_edges"]
        + update["opinion_from_court_edges"],
        "matter_jurisdiction_edges": corpus["matter_in_jurisdiction_edges"],
    })
    _assert_relationship_item_keys(
        config,
        matter_impacts["items"],
        "opinion_affects_matter",
        label="matter_impacts",
    )
    impacted_loper_matters = {
        row["matter_id"] for row in matter_impacts["items"] if row["opinion_id"] == "op_loper_bright"
    }
    assert impacted_loper_matters == all_matter_ids

    docket = providers.load_docket_feed({})
    filing_obligations = providers.assess_filing_response_obligations({
        "filings": docket["filings"],
        "matters": corpus["matters"],
        "deadlines": docket["deadlines"],
        "filing_in_matter_edges": docket["filing_in_matter_edges"],
        "matter_deadline_edges": docket["matter_has_deadline_edges"],
    })
    _assert_relationship_item_keys(
        config,
        filing_obligations["items"],
        "filing_requires_response",
        label="filing_obligations",
    )

    review_work = providers.route_review_work({
        "opinions": opinions,
        "matters": corpus["matters"],
        "matter_impact_edges": matter_impacts["items"],
        "treatment_edges": [],
        "argument_citation_edges": corpus["argument_cites_opinion_edges"],
        "argument_matter_edges": corpus["argument_in_matter_edges"],
    })
    _assert_rows_have_keys(
        review_work["items"],
        {
            "work_item_id": None,
            "title": None,
            "work_item_type": None,
            "priority": None,
            "opinion_id": None,
            "matter_id": None,
            "obligation_type": None,
            "rationale": None,
            "verdict": None,
        },
        label="review_work",
    )
    routed_matter_ids = {row["matter_id"] for row in review_work["items"]}
    assert routed_matter_ids == all_matter_ids


def test_sweep_stale_deadlines_uses_passed_reference_date(config: Any) -> None:
    sweep = providers.sweep_stale_deadlines({
        "as_of": "2024-07-15",
        "deadlines": [
            {
                "entity_type": "Deadline",
                "entity_id": "deadline_greengrid_response",
                "properties": {
                    "title": "GreenGrid supplemental response",
                    "due_date": "2024-07-01",
                    "deadline_type": "response",
                    "status": "pending",
                },
            },
            {
                "entity_type": "Deadline",
                "entity_id": "deadline_harbor_supp_brief",
                "properties": {
                    "title": "Harbor supplemental authority brief",
                    "due_date": "2024-07-22",
                    "deadline_type": "filing",
                    "status": "pending",
                },
            },
        ],
        "matters": [
            {
                "entity_type": "Matter",
                "entity_id": "matter_greengrid_epa",
                "properties": {
                    "name": "GreenGrid EPA BACT Permit Appeal",
                    "matter_type": "litigation",
                    "status": "active",
                    "jurisdiction": "D.C. Circuit",
                },
            },
            {
                "entity_type": "Matter",
                "entity_id": "matter_harbor_noaa",
                "properties": {
                    "name": "Harbor Fisheries NOAA Observer Fee Challenge",
                    "matter_type": "litigation",
                    "status": "active",
                    "jurisdiction": "First Circuit",
                },
            },
        ],
        "matter_deadline_edges": [
            {"from_id": "matter_greengrid_epa", "to_id": "deadline_greengrid_response"},
            {"from_id": "matter_harbor_noaa", "to_id": "deadline_harbor_supp_brief"},
        ],
    })

    _assert_rows_have_keys(
        sweep["deadlines"],
        config.entity_types["Deadline"].properties,
        label="swept_deadlines",
    )
    statuses = {row["deadline_id"]: row["status"] for row in sweep["deadlines"]}
    assert statuses["deadline_greengrid_response"] == "missed"
    assert "deadline_harbor_supp_brief" not in statuses


def test_act_one_citation_treatments_are_quiet() -> None:
    corpus = providers.load_corpus_seed({})
    treatments = providers.classify_opinion_treatment({
        "opinions": corpus["opinions"],
        "citation_edges": corpus["opinion_cites_opinion_edges"],
    })

    assert {
        row["treatment"] for row in treatments["items"] if row["treatment"] in providers.NEGATIVE_TREATMENTS
    } == set()


def test_holding_statute_and_issue_verdicts_follow_provenance() -> None:
    corpus = providers.load_corpus_seed({})
    holdings = providers.extract_holdings_from_opinions({
        "opinions": [
            next(row for row in corpus["opinions"] if row["opinion_id"] == "op_chevron"),
            {
                "opinion_id": "op_uncurated",
                "case_name": "Uncurated Agency Deference Case",
                "citation": "1 F.4th 1",
                "docket_number": "24-1",
                "date_filed": "2024-07-01",
                "jurisdiction": "D.C. Circuit",
                "precedential_status": "published",
                "source_url": None,
            },
        ]
    })["items"]

    by_holding = {row["holding_id"]: row for row in holdings}
    assert by_holding["hold_chevron_deference"]["verdict"] == "support"
    fallback_holding = by_holding["hold_op_uncurated"]
    assert fallback_holding["verdict"] == "unsure"
    assert "Fallback holding shell" in fallback_holding["rationale"]

    statute_links = providers.link_holdings_to_statutes({
        "holdings": [
            by_holding["hold_chevron_deference"],
            {
                "holding_id": "hold_keyword",
                "summary": "Chevron deference governs the agency interpretation.",
                "scope": "agency deference",
            },
        ],
        "statutes": corpus["statutes"],
    })["items"]
    by_statute = {(row["holding_id"], row["statute_id"]): row for row in statute_links}
    assert by_statute[("hold_chevron_deference", "stat_chevron_doctrine")]["verdict"] == "support"
    assert by_statute[("hold_keyword", "stat_chevron_doctrine")]["verdict"] == "unsure"
    assert "Keyword scan" in by_statute[("hold_keyword", "stat_chevron_doctrine")]["rationale"]

    issue_links = providers.map_holdings_to_issues({
        "holdings": [
            by_holding["hold_chevron_deference"],
            {
                "holding_id": "hold_keyword_issue",
                "summary": "Agency deference applies to this issue.",
                "scope": "agency deference",
            },
            {
                "holding_id": "hold_graph_join",
                "summary": "A statutory interpretation holding.",
                "scope": "Clean Air Act",
            },
        ],
        "issues": corpus["legal_issues"],
        "statute_edges": [{"holding_id": "hold_graph_join", "statute_id": "stat_clean_air_act_111d"}],
        "statute_issue_edges": corpus["statute_governs_issue_edges"],
    })["items"]
    by_issue = {(row["holding_id"], row["issue_id"]): row for row in issue_links}
    assert by_issue[("hold_chevron_deference", "issue_agency_deference")]["verdict"] == "support"
    assert by_issue[("hold_graph_join", "issue_environmental_agency_authority")]["verdict"] == "support"
    assert by_issue[("hold_keyword_issue", "issue_agency_deference")]["verdict"] == "unsure"


def test_treatment_argument_and_matter_scope_verdicts_follow_provenance() -> None:
    treatments = providers.classify_opinion_treatment({
        "opinions": [
            _opinion_entity("op_new", "New Authority"),
            _opinion_entity("op_old", "Old Authority"),
        ],
        "citation_edges": [
            {
                "source_opinion_id": "op_new",
                "cited_opinion_id": "op_old",
                "citation_context": "The court overrules Old Authority on this point.",
            },
            {
                "source_opinion_id": "op_old",
                "cited_opinion_id": "op_new",
                "citation_context": "",
            },
        ],
    })["items"]
    by_pair = {(row["source_opinion_id"], row["cited_opinion_id"]): row for row in treatments}
    assert by_pair[("op_new", "op_old")]["treatment"] == "overrules"
    assert by_pair[("op_new", "op_old")]["verdict"] == "support"
    assert by_pair[("op_old", "op_new")]["treatment"] == "follows"
    assert by_pair[("op_old", "op_new")]["verdict"] == "unsure"

    argument_impacts = providers.assess_argument_impact({
        "holdings": [
            {
                "holding_id": "hold_loper_overrules_chevron",
                "summary": "Chevron is overruled.",
                "scope": "agency deference",
            },
            {
                "holding_id": "hold_keyword_risk",
                "summary": "Chevron is overruled.",
                "scope": "agency deference",
            },
        ],
        "arguments": [
            {
                "argument_id": "arg_chevron",
                "title": "Chevron deference supports the agency position",
                "description": "The argument rests on Chevron.",
            }
        ],
        "holding_issue_edges": [
            {"holding_id": "hold_loper_overrules_chevron", "issue_id": "issue_agency_deference"},
            {"holding_id": "hold_keyword_risk", "issue_id": "issue_agency_deference"},
        ],
        "argument_issue_edges": [
            {"argument_id": "arg_chevron", "issue_id": "issue_agency_deference"}
        ],
    })["items"]
    by_holding = {row["holding_id"]: row for row in argument_impacts}
    assert by_holding["hold_loper_overrules_chevron"]["risk_verdict"] == "support"
    assert by_holding["hold_keyword_risk"]["risk_verdict"] == "unsure"
    assert by_holding["hold_keyword_risk"]["support_verdict"] == "unsure"

    matter_scope = providers.scope_matters_to_statutes({
        "matters": [_matter_entity("matter_harbor_noaa", "Harbor Fisheries NOAA Observer Fee Challenge")],
        "statutes": [
            {
                "statute_id": "stat_chevron_doctrine",
                "title": "Chevron doctrine",
                "section": "Chevron",
                "jurisdiction": "United States",
                "topic": "agency deference",
            }
        ],
    })["items"]
    assert matter_scope == [
        {
            "matter_id": "matter_harbor_noaa",
            "statute_id": "stat_chevron_doctrine",
            "scope_basis": (
                "Curated hint keywords matched matter 'Harbor Fisheries NOAA Observer Fee Challenge' "
                "to Chevron doctrine."
            ),
            "verdict": "unsure",
        }
    ]


def test_filing_obligation_verdicts_follow_routing_and_response_provenance() -> None:
    rows = providers.assess_filing_response_obligations({
        "filings": [
            {
                "filing_id": "filing_explicit",
                "title": "Notice of supplemental authority",
                "docket_number": "24-100",
                "filing_type": "notice",
                "filed_at": "2024-07-01",
            },
            {
                "filing_id": "filing_keyword",
                "title": "Harbor motion update",
                "docket_number": "24-101",
                "filing_type": None,
                "filed_at": "2024-07-02",
            },
        ],
        "matters": [
            _matter_entity("matter_harbor_noaa", "Harbor Fisheries NOAA Observer Fee Challenge"),
        ],
        "deadlines": [],
        "filing_in_matter_edges": [
            {"filing_id": "filing_explicit", "matter_id": "matter_harbor_noaa"}
        ],
        "matter_deadline_edges": [],
    })["items"]
    by_filing = {row["filing_id"]: row for row in rows}
    assert by_filing["filing_explicit"]["routing_verdict"] == "support"
    assert by_filing["filing_explicit"]["obligation_verdict"] == "support"
    assert "filing_keyword" not in by_filing

    fallback = providers.assess_filing_response_obligations({
        "filings": [
            {
                "filing_id": "filing_keyword",
                "title": "Harbor motion update",
                "docket_number": "24-101",
                "filing_type": None,
                "filed_at": "2024-07-02",
            }
        ],
        "matters": [
            _matter_entity("matter_harbor_noaa", "Harbor Fisheries NOAA Observer Fee Challenge"),
        ],
        "deadlines": [],
        "filing_in_matter_edges": [],
        "matter_deadline_edges": [],
    })["items"]
    assert fallback[0]["routing_verdict"] == "unsure"
    assert fallback[0]["obligation_verdict"] == "unsure"


def test_assess_matter_impact_handles_workflow_relationship_rows(config: Any) -> None:
    rows = providers.assess_matter_impact({
        "opinions": [
            _opinion_entity("op_loper_bright", "Loper Bright Enterprises v. Raimondo"),
            _opinion_entity("op_chevron", "Chevron U.S.A. Inc. v. NRDC"),
        ],
        "matters": [
            _matter_entity("matter_harbor_noaa", "Harbor Fisheries NOAA Observer Fee Challenge"),
        ],
        "support_edges": [
            {
                "relationship_type": "holding_supports_argument",
                "from_id": "hold_chevron_deference",
                "to_id": "arg_harbor_chevron",
                "properties": {"support_strength": "strong"},
            }
        ],
        "undermine_edges": [
            {
                "relationship_type": "holding_undermines_argument",
                "from_id": "hold_loper_overrules_chevron",
                "to_id": "arg_harbor_chevron",
                "properties": {"risk_type": "adverse_authority"},
            }
        ],
        "opinion_holding_edges": [
            {"relationship_type": "opinion_has_holding", "from_id": "op_loper_bright", "to_id": "hold_loper_overrules_chevron"},
            {"relationship_type": "opinion_has_holding", "from_id": "op_chevron", "to_id": "hold_chevron_deference"},
        ],
        "argument_matter_edges": [
            {"relationship_type": "argument_in_matter", "from_id": "arg_harbor_chevron", "to_id": "matter_harbor_noaa"}
        ],
        "treatment_edges": [
            {
                "relationship_type": "opinion_treats_opinion",
                "from_id": "op_loper_bright",
                "to_id": "op_chevron",
                "properties": {"treatment": "overrules"},
            }
        ],
        "matter_statute_edges": [
            {"relationship_type": "matter_turns_on_statute", "from_id": "matter_harbor_noaa", "to_id": "stat_chevron_doctrine"}
        ],
        "opinion_court_edges": [
            {"relationship_type": "opinion_from_court", "from_id": "op_loper_bright", "to_id": "court_scotus"},
            {"relationship_type": "opinion_from_court", "from_id": "op_chevron", "to_id": "court_scotus"},
        ],
        "matter_jurisdiction_edges": [
            {"relationship_type": "matter_in_jurisdiction", "from_id": "matter_harbor_noaa", "to_id": "court_first_cir"}
        ],
    })

    _assert_relationship_item_keys(config, rows["items"], "opinion_affects_matter", label="matter_impacts")
    by_pair = {(row["opinion_id"], row["matter_id"]): row for row in rows["items"]}
    assert by_pair[("op_loper_bright", "matter_harbor_noaa")]["impact_type"] == "adverse_authority"
    assert by_pair[("op_chevron", "matter_harbor_noaa")]["impact_type"] == "monitoring_only"


def test_route_review_work_handles_negative_treatment_graph_rows() -> None:
    rows = providers.route_review_work({
        "opinions": [
            _opinion_entity("op_loper_bright", "Loper Bright Enterprises v. Raimondo"),
            _opinion_entity("op_chevron", "Chevron U.S.A. Inc. v. NRDC"),
        ],
        "matters": [
            _matter_entity("matter_harbor_noaa", "Harbor Fisheries NOAA Observer Fee Challenge"),
        ],
        "matter_impact_edges": [],
        "treatment_edges": [
            {
                "relationship_type": "opinion_treats_opinion",
                "from_id": "op_loper_bright",
                "to_id": "op_chevron",
                "properties": {
                    "treatment": "overrules",
                    "rationale": "Loper overrules Chevron.",
                },
            }
        ],
        "argument_citation_edges": [
            {
                "relationship_type": "argument_cites_opinion",
                "from_id": "arg_harbor_chevron",
                "to_id": "op_chevron",
                "properties": {"citation_role": "primary"},
            }
        ],
        "argument_matter_edges": [
            {"relationship_type": "argument_in_matter", "from_id": "arg_harbor_chevron", "to_id": "matter_harbor_noaa"}
        ],
    })

    assert rows["items"][0]["matter_id"] == "matter_harbor_noaa"
    assert rows["items"][0]["obligation_type"] == "citation_check"


def test_assess_filing_response_obligations_handles_workflow_relationship_rows(config: Any) -> None:
    rows = providers.assess_filing_response_obligations({
        "filings": [
            {
                "entity_type": "Filing",
                "entity_id": "filing_x",
                "properties": {
                    "title": "Generic supplemental authority notice",
                    "docket_number": "24-100",
                    "filing_type": "notice",
                    "filed_at": "2024-07-01",
                },
            }
        ],
        "matters": [
            _matter_entity("matter_harbor_noaa", "Harbor Fisheries NOAA Observer Fee Challenge"),
            _matter_entity("matter_sentinel_fcc", "Sentinel FCC Shot Clock Petition"),
        ],
        "deadlines": [
            {
                "entity_type": "Deadline",
                "entity_id": "deadline_harbor_supp_brief",
                "properties": {
                    "title": "Harbor supplemental authority brief",
                    "due_date": "2024-07-22",
                    "deadline_type": "filing",
                    "status": "pending",
                },
            }
        ],
        "filing_in_matter_edges": [
            {"relationship_type": "filing_in_matter", "from_id": "filing_x", "to_id": "matter_harbor_noaa"}
        ],
        "matter_deadline_edges": [
            {"relationship_type": "matter_has_deadline", "from_id": "matter_harbor_noaa", "to_id": "deadline_harbor_supp_brief"}
        ],
    })

    _assert_relationship_item_keys(config, rows["items"], "filing_requires_response", label="filing_obligations")
    assert rows["items"] == [
        {
            "filing_id": "filing_x",
            "matter_id": "matter_harbor_noaa",
            "response_type": "client_update",
            "deadline_date": "2024-07-22",
            "rationale": (
                "Filing 'Generic supplemental authority notice' is routed to "
                "matter 'Harbor Fisheries NOAA Observer Fee Challenge'."
            ),
            "obligation_verdict": "support",
            "routing_verdict": "support",
        }
    ]


def test_inline_assessors_handle_graph_style_rows() -> None:
    treatment = providers.classify_opinion_treatment({
        "opinions": [
            {
                "entity_type": "Opinion",
                "entity_id": "op_loper_bright",
                "properties": {
                    "case_name": "Loper Bright Enterprises v. Raimondo",
                    "citation": "603 U.S. 369",
                    "docket_number": "22-451",
                    "date_filed": "2024-06-28",
                    "jurisdiction": "United States",
                    "precedential_status": "published",
                    "source_url": None,
                },
            },
            {
                "entity_type": "Opinion",
                "entity_id": "op_chevron",
                "properties": {
                    "case_name": "Chevron U.S.A. Inc. v. NRDC",
                    "citation": "467 U.S. 837",
                    "docket_number": "82-1005",
                    "date_filed": "1984-06-25",
                    "jurisdiction": "United States",
                    "precedential_status": "published",
                    "source_url": None,
                },
            },
        ],
        "citation_edges": [
            {
                "relationship_type": "opinion_cites_opinion",
                "from_type": "Opinion",
                "from_id": "op_loper_bright",
                "to_type": "Opinion",
                "to_id": "op_chevron",
                "edge_key": "op_loper_bright->op_chevron",
                "properties": {
                    "citation_context": "overrules Chevron"
                },
            }
        ],
    })
    assert treatment["items"][0]["treatment"] == "overrules"

    argument_impact = providers.assess_argument_impact({
        "holdings": [
            {
                "entity_type": "Holding",
                "entity_id": "hold_loper_overrules_chevron",
                "properties": {
                    "summary": "Chevron is overruled.",
                    "holding_type": "rule",
                    "scope": "agency deference",
                },
            }
        ],
        "arguments": [
            {
                "entity_type": "Argument",
                "entity_id": "arg_1",
                "properties": {
                    "title": "Chevron deference supports the agency position",
                    "description": "The argument rests on Chevron.",
                    "argument_type": "claim",
                    "position": "petitioner",
                    "status": "filed",
                },
            }
        ],
        "holding_issue_edges": [
            {"from_id": "hold_loper_overrules_chevron", "to_id": "issue_agency_deference"}
        ],
        "argument_issue_edges": [
            {"from_id": "arg_1", "to_id": "issue_agency_deference"}
        ],
    })
    assert argument_impact["items"][0]["risk_verdict"] == "support"


def _opinion_entity(opinion_id: str, case_name: str) -> dict[str, Any]:
    return {
        "entity_type": "Opinion",
        "entity_id": opinion_id,
        "properties": {
            "case_name": case_name,
            "citation": None,
            "docket_number": None,
            "date_filed": "2024-06-28",
            "jurisdiction": "United States",
            "precedential_status": "published",
            "source_url": None,
        },
    }


def _matter_entity(matter_id: str, name: str) -> dict[str, Any]:
    return {
        "entity_type": "Matter",
        "entity_id": matter_id,
        "properties": {
            "name": name,
            "matter_type": "litigation",
            "status": "active",
            "jurisdiction": "First Circuit",
        },
    }


def _assert_relationship_item_keys(
    config: Any,
    rows: list[dict[str, Any]],
    relationship_type: str,
    *,
    label: str,
) -> None:
    rel = config.get_relationship(relationship_type)
    assert rel is not None
    _assert_rows_have_keys(rows, rel.properties, label=label)


def _assert_rows_have_keys(
    rows: list[dict[str, Any]],
    keys: Any,
    *,
    label: str,
) -> None:
    assert rows, f"{label} should not be empty"
    expected = set(keys)
    for row in rows:
        missing = expected - set(row)
        assert not missing, f"{label} row missing keys: {sorted(missing)}"


def test_seed_evidence_chunk_pins_match_recomputed_chunks() -> None:
    """Every seed evidence locator must pin a real chunk of its pinned text.

    Chunk ids are recomputed with the same parser the artifact store uses;
    the pinned chunk must anchor the quote (whitespace-normalized; the chunk
    containing the quote's opening for quotes that span block boundaries)
    and expected_content_hash must equal that chunk's current content hash,
    so any drift between quotes, chunk pins, and opinion texts fails here.
    """
    import re

    from cruxible_core.source_artifacts.markdown import parse_markdown_chunks

    def norm(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()

    def evidence_objects():
        holdings = json.loads((SEED_PATH / "holdings.json").read_text(encoding="utf-8"))
        for row in holdings["curated_holdings"]:
            if row.get("evidence"):
                yield row["holding_id"], row["evidence"]
        act2 = json.loads((SEED_PATH / "act2_update.json").read_text(encoding="utf-8"))
        for row in act2["opinion_cites_opinion_edges"]:
            if row.get("evidence"):
                yield f"{row['source_opinion_id']}->{row['cited_opinion_id']}", row["evidence"]

    checked = 0
    for key, evidence in evidence_objects():
        content = (SEED_PATH / evidence["path"]).read_bytes()
        chunks = {
            chunk.chunk_id: chunk
            for chunk in parse_markdown_chunks(
                source_artifact_id=evidence["source_artifact_id"],
                content=content,
            )
        }
        chunk = chunks.get(evidence["chunk_id"])
        assert chunk is not None, f"{key}: pinned chunk_id not produced by the parser"
        lines = (
            content.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").splitlines()
        )
        body = norm(
            "\n".join(lines[max(chunk.line_start - 1, 0) : max(chunk.line_end, chunk.line_start)])
        )
        quote = norm(evidence["quote"])
        assert quote in body or quote[:30] in body, f"{key}: quote not anchored in pinned chunk"
        assert evidence["expected_content_hash"] == chunk.content_hash, (
            f"{key}: expected_content_hash drifted from pinned chunk content"
        )
        checked += 1
    assert checked >= 14
