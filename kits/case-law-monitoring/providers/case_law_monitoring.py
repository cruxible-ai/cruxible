"""Case-law monitoring kit providers.

The corpus bundle is curated and pinned under ``data/seed``. The providers
return plain JSON rows because the surrounding workflows own graph writes,
proposal construction, and signal governance.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

from cruxible_core.provider.payloads import load_artifact_json, source_artifact_evidence_ref
from cruxible_core.provider.types import ProviderContext


CORPUS_FIELDS = (
    "opinions",
    "courts",
    "judges",
    "statutes",
    "legal_issues",
    "clients",
    "matters",
    "arguments",
    "opinion_texts",
    "opinion_from_court_edges",
    "opinion_decided_by_judge_edges",
    "opinion_cites_opinion_edges",
    "matter_for_client_edges",
    "matter_in_jurisdiction_edges",
    "argument_in_matter_edges",
    "argument_raises_issue_edges",
    "argument_cites_opinion_edges",
    "statute_governs_issue_edges",
)

NEGATIVE_TREATMENTS = {"distinguishes", "criticizes", "limits", "overrules", "abrogates"}


def _curated_holdings(context: ProviderContext | None) -> dict[str, dict[str, Any]]:
    """Curated corpus holdings from the digest-pinned seed bundle, keyed by opinion.

    Holdings are reference DATA about the corpus, not provider logic: a user
    running this kit on their own corpus ships their own holdings.json (or
    none — uncurated opinions land as unsure and stop at review), and genuinely
    new holdings arrive through the HoldingCandidates contract path.
    """
    rows = _load_seed_json("holdings.json", context).get("curated_holdings", [])
    return {str(row["opinion_id"]): dict(row) for row in rows if row.get("opinion_id")}


def _holdings_by_id(context: ProviderContext | None) -> dict[str, dict[str, Any]]:
    return {
        str(holding["holding_id"]): dict(holding)
        for holding in _curated_holdings(context).values()
        if holding.get("holding_id")
    }


def _treatment_evidence_by_pair(context: ProviderContext | None) -> dict[tuple[str, str], dict[str, Any]]:
    rows = _load_seed_json("act2_update.json", context).get("opinion_cites_opinion_edges", [])
    evidence_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        source_id = _first_non_empty(row.get("source_opinion_id"))
        cited_id = _first_non_empty(row.get("cited_opinion_id"))
        evidence = _evidence_object(row.get("evidence"))
        if source_id and cited_id and evidence is not None:
            evidence_by_pair[(source_id, cited_id)] = evidence
    return evidence_by_pair


def _evidence_object(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    required = {"quote", "opinion_id", "char_start", "char_end", "text_sha256"}
    if not required <= set(value):
        return None
    return dict(value)


def _evidence_refs(evidence: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if evidence is None:
        return []
    source_artifact_id = _first_non_empty(evidence.get("source_artifact_id"))
    chunk_id = _first_non_empty(evidence.get("chunk_id"))
    if not source_artifact_id or not chunk_id:
        return []
    opinion_id = _first_non_empty(evidence.get("opinion_id"))
    return [
        source_artifact_evidence_ref(
            source_artifact_id,
            chunk_id,
            quote=evidence.get("quote"),
            char_start=evidence.get("char_start"),
            char_end=evidence.get("char_end"),
            content_hash=evidence.get("expected_content_hash"),
            label=f"{opinion_id} opinion text" if opinion_id else "opinion text",
            opinion_id=opinion_id,
            text_sha256=evidence.get("text_sha256"),
            source_path=evidence.get("path"),
        )
    ]


def _attach_evidence_fields(
    row: dict[str, Any],
    evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    evidence_payload = dict(evidence) if evidence is not None else None
    row["evidence"] = evidence_payload
    row["evidence_refs"] = _evidence_refs(evidence_payload)
    return row



def load_corpus_seed(payload: dict[str, Any], context: ProviderContext | None = None) -> dict[str, Any]:
    _require_mapping(payload, "load_corpus_seed")
    return _corpus_payload(_load_seed_json("act1_seed.json", context))


def load_corpus_update(payload: dict[str, Any], context: ProviderContext | None = None) -> dict[str, Any]:
    """Load the bundled act-two corpus update fixture.

    Acquisition is deliberately not a provider concern: fetch fresh opinions
    with ``scripts/fetch_courtlistener.py``, review the JSON, register the
    opinion texts as source artifacts, and apply the rows through
    ``sync_corpus_update`` (same contract as this output).
    """
    _require_mapping(payload, "load_corpus_update")
    return _corpus_payload(_load_seed_json("act2_update.json", context))


def load_docket_feed(payload: dict[str, Any], context: ProviderContext | None = None) -> dict[str, Any]:
    _require_mapping(payload, "load_docket_feed")
    data = _load_seed_json("docket_feed.json", context)
    return {
        "filings": _sorted_rows(data.get("filings", []), "filing_id"),
        "deadlines": _sorted_rows(data.get("deadlines", []), "deadline_id"),
        "filing_in_matter_edges": _sorted_rows(data.get("filing_in_matter_edges", []), "filing_id"),
        "matter_has_deadline_edges": _sorted_rows(
            data.get("matter_has_deadline_edges", []),
            "matter_id",
            "deadline_id",
        ),
    }


def load_case_outcome_feed(payload: dict[str, Any], context: ProviderContext | None = None) -> dict[str, Any]:
    _require_mapping(payload, "load_case_outcome_feed")
    data = _load_seed_json("case_outcomes.json", context)
    return {
        "case_outcomes": _sorted_rows(data.get("case_outcomes", []), "outcome_id"),
        "outcome_of_matter_edges": _sorted_rows(data.get("outcome_of_matter_edges", []), "outcome_id"),
        "outcome_resolved_argument_edges": _sorted_rows(
            data.get("outcome_resolved_argument_edges", []),
            "outcome_id",
            "argument_id",
        ),
    }


def sweep_stale_deadlines(payload: dict[str, Any], context: ProviderContext | None = None) -> dict[str, Any]:
    _require_mapping(payload, "sweep_stale_deadlines")
    del context
    as_of = _parse_date(
        _first_non_empty(
            payload.get("as_of"),
            payload.get("reference_date"),
            _mapping_value(payload.get("params"), "as_of"),
            _mapping_value(payload.get("params"), "reference_date"),
        )
    )
    deadlines = _rows(payload, "deadlines")
    matters = _rows(payload, "matters")
    matter_deadline_edges = _rows(payload, "matter_deadline_edges")

    matter_status = {
        _entity_id(row, "matter_id"): _first_non_empty(_properties(row).get("status"))
        for row in matters
        if _entity_id(row, "matter_id")
    }
    matters_by_deadline: dict[str, list[str]] = defaultdict(list)
    for edge in matter_deadline_edges:
        matter_id = _edge_from_id(edge, "matter_id")
        deadline_id = _edge_to_id(edge, "deadline_id")
        if matter_id and deadline_id:
            matters_by_deadline[deadline_id].append(matter_id)

    updates: list[dict[str, Any]] = []
    for row in deadlines:
        props = _properties(row)
        deadline_id = _entity_id(row, "deadline_id")
        if not deadline_id:
            continue
        status = _first_non_empty(props.get("status"))
        if status not in {"pending", "extended"}:
            continue
        due_date = _parse_date(props.get("due_date"))
        linked_matters = matters_by_deadline.get(deadline_id, [])
        if any(matter_status.get(matter_id) == "closed" for matter_id in linked_matters):
            next_status = "closed"
        elif as_of is not None and due_date is not None and due_date < as_of:
            next_status = "missed"
        else:
            continue
        updates.append({
            "deadline_id": deadline_id,
            "title": props.get("title"),
            "due_date": props.get("due_date"),
            "deadline_type": props.get("deadline_type"),
            "status": next_status,
        })

    return {"deadlines": _sorted_rows(updates, "deadline_id")}


def extract_holdings_from_opinions(
    payload: dict[str, Any],
    context: ProviderContext | None = None,
) -> dict[str, Any]:
    _require_mapping(payload, "extract_holdings_from_opinions")
    curated_holdings = _curated_holdings(context)
    items: list[dict[str, Any]] = []
    for opinion in _rows(payload, "opinions"):
        opinion_id = _entity_id(opinion, "opinion_id")
        if not opinion_id:
            continue
        is_curated = opinion_id in curated_holdings
        curated = dict(curated_holdings.get(opinion_id, {}))
        props = _properties(opinion)
        if not curated:
            case_name = _first_non_empty(props.get("case_name"), opinion_id) or opinion_id
            curated = {
                "holding_id": f"hold_{_slugify(opinion_id)}",
                "summary": f"{case_name} contains a monitored administrative-law holding.",
                "holding_type": "rule",
                "scope": props.get("jurisdiction"),
                "locator": props.get("citation"),
                "statute_ids": [],
                "issue_ids": [],
                "orientation": "unknown",
            }
        rationale = (
            f"Curated public-law seed metadata identifies this holding in "
            f"{props.get('case_name') or opinion_id}."
            if is_curated
            else (
                f"Fallback holding shell for {props.get('case_name') or opinion_id}; "
                "no curated holding row was present."
            )
        )
        evidence = _evidence_object(curated.get("evidence")) if is_curated else None
        items.append(_attach_evidence_fields({
            "opinion_id": opinion_id,
            "holding_id": curated["holding_id"],
            "summary": curated["summary"],
            "holding_type": curated.get("holding_type"),
            "scope": curated.get("scope"),
            "locator": curated.get("locator"),
            "rationale": rationale,
            "verdict": "support" if is_curated else "unsure",
            "statute_ids": list(curated.get("statute_ids", [])),
            "issue_ids": list(curated.get("issue_ids", [])),
            "orientation": curated.get("orientation"),
        }, evidence))
    return {"items": _sorted_rows(items, "opinion_id", "holding_id")}


def link_holdings_to_statutes(
    payload: dict[str, Any],
    context: ProviderContext | None = None,
) -> dict[str, Any]:
    _require_mapping(payload, "link_holdings_to_statutes")
    holding_index = _holdings_by_id(context)
    statutes = {_entity_id(row, "statute_id"): _properties(row) for row in _rows(payload, "statutes")}
    items: list[dict[str, Any]] = []
    for holding in _rows(payload, "holdings"):
        holding_id = _entity_id(holding, "holding_id")
        if not holding_id:
            continue
        props = _properties(holding)
        curated = holding_index.get(holding_id, {})
        statute_paths: dict[str, str] = {
            sid: "curated"
            for sid in curated.get("statute_ids", [])
            if sid in statutes
        }
        if not statute_paths:
            statute_paths = {
                statute_id: "keyword"
                for statute_id in _keyword_statute_matches(props, statutes)
            }
        for statute_id, path in sorted(statute_paths.items()):
            statute = statutes[statute_id]
            if path == "curated":
                rationale = (
                    f"Curated holding metadata lists {statute.get('title') or statute_id} "
                    f"for {holding_id}."
                )
                verdict = "support"
            else:
                rationale = (
                    f"Keyword scan matched holding text to {statute.get('title') or statute_id} "
                    f"within {props.get('scope') or 'the monitored issue scope'}."
                )
                verdict = "unsure"
            items.append({
                "holding_id": holding_id,
                "statute_id": statute_id,
                "interpretation_type": _interpretation_type(props, curated),
                "rationale": rationale,
                "verdict": verdict,
            })
    return {"items": _dedupe_sorted(items, "holding_id", "statute_id")}


def map_holdings_to_issues(payload: dict[str, Any], context: ProviderContext | None = None) -> dict[str, Any]:
    _require_mapping(payload, "map_holdings_to_issues")
    holding_index = _holdings_by_id(context)
    issues = {_entity_id(row, "issue_id"): _properties(row) for row in _rows(payload, "issues")}
    statute_edges = _rows(payload, "statute_edges")
    # statute -> issues from the graph's own statute_governs_issue edges.
    issues_by_statute: dict[str, set[str]] = defaultdict(set)
    for edge in _rows(payload, "statute_issue_edges"):
        statute_id = _edge_from_id(edge, "statute_id")
        issue_id = _edge_to_id(edge, "issue_id")
        if statute_id and issue_id and issue_id in issues:
            issues_by_statute[statute_id].add(issue_id)
    issue_ids_by_holding: dict[str, set[str]] = defaultdict(set)
    for edge in statute_edges:
        holding_id = _edge_from_id(edge, "holding_id")
        statute_id = _edge_to_id(edge, "statute_id")
        if holding_id:
            issue_ids_by_holding[holding_id].update(issues_by_statute.get(statute_id or "", set()))

    items: list[dict[str, Any]] = []
    for holding in _rows(payload, "holdings"):
        holding_id = _entity_id(holding, "holding_id")
        if not holding_id:
            continue
        props = _properties(holding)
        curated = holding_index.get(holding_id, {})
        issue_paths: dict[str, str] = {
            issue_id: "curated"
            for issue_id in curated.get("issue_ids", [])
            if issue_id in issues
        }
        for issue_id in sorted(issue_ids_by_holding.get(holding_id, set())):
            issue_paths.setdefault(issue_id, "graph_join")
        if not issue_paths:
            issue_paths = {
                issue_id: "keyword"
                for issue_id in _keyword_issue_matches(props, issues)
            }
        for issue_id, path in sorted(issue_paths.items()):
            issue = issues[issue_id]
            if path == "curated":
                rationale = (
                    f"Curated holding metadata lists {issue.get('name') or issue_id} "
                    f"for {holding_id}."
                )
                verdict = "support"
                issue_fit = "direct"
            elif path == "graph_join":
                rationale = (
                    f"Accepted statute-governs-issue data links this holding to "
                    f"{issue.get('name') or issue_id}."
                )
                verdict = "support"
                issue_fit = "analogous"
            else:
                rationale = (
                    f"Keyword scan matched holding text to {issue.get('name') or issue_id} "
                    f"through {props.get('scope') or 'its summarized rule'}."
                )
                verdict = "unsure"
                issue_fit = "analogous"
            items.append({
                "holding_id": holding_id,
                "issue_id": issue_id,
                "issue_fit": issue_fit,
                "rationale": rationale,
                "verdict": verdict,
            })
    return {"items": _dedupe_sorted(items, "holding_id", "issue_id")}


def classify_opinion_treatment(
    payload: dict[str, Any],
    context: ProviderContext | None = None,
) -> dict[str, Any]:
    _require_mapping(payload, "classify_opinion_treatment")
    fixture_evidence = _treatment_evidence_by_pair(context)
    opinions = {_entity_id(row, "opinion_id"): _properties(row) for row in _rows(payload, "opinions")}
    items: list[dict[str, Any]] = []
    for edge in _rows(payload, "citation_edges"):
        source_id = _edge_from_id(edge, "source_opinion_id")
        cited_id = _edge_to_id(edge, "cited_opinion_id")
        if not source_id or not cited_id:
            continue
        props = _properties(edge)
        context_text = _first_non_empty(props.get("citation_context")) or ""
        treatment, verdict, signal_basis = _classify_treatment(context_text)
        source_name = opinions.get(source_id, {}).get("case_name") or source_id
        cited_name = opinions.get(cited_id, {}).get("case_name") or cited_id
        evidence = _evidence_object(props.get("evidence")) or fixture_evidence.get(
            (source_id, cited_id)
        )
        items.append(_attach_evidence_fields({
            "source_opinion_id": source_id,
            "cited_opinion_id": cited_id,
            "treatment": treatment,
            "rationale": (
                f"{source_name} citation context {signal_basis} {treatment} treatment "
                f"of {cited_name}: {context_text or 'no recognizable treatment signal'}"
            ),
            "verdict": verdict,
        }, evidence))
    return {"items": _dedupe_sorted(items, "source_opinion_id", "cited_opinion_id")}


def assess_argument_impact(payload: dict[str, Any], context: ProviderContext | None = None) -> dict[str, Any]:
    _require_mapping(payload, "assess_argument_impact")
    holding_index = _holdings_by_id(context)
    holdings = {_entity_id(row, "holding_id"): _properties(row) for row in _rows(payload, "holdings")}
    arguments = {_entity_id(row, "argument_id"): _properties(row) for row in _rows(payload, "arguments")}
    holding_ids_by_issue: dict[str, set[str]] = defaultdict(set)
    argument_ids_by_issue: dict[str, set[str]] = defaultdict(set)
    for edge in _rows(payload, "holding_issue_edges"):
        holding_id = _edge_from_id(edge, "holding_id")
        issue_id = _edge_to_id(edge, "issue_id")
        if holding_id and issue_id:
            holding_ids_by_issue[issue_id].add(holding_id)
    for edge in _rows(payload, "argument_issue_edges"):
        argument_id = _edge_from_id(edge, "argument_id")
        issue_id = _edge_to_id(edge, "issue_id")
        if argument_id and issue_id:
            argument_ids_by_issue[issue_id].add(argument_id)

    rows: list[dict[str, Any]] = []
    for issue_id in sorted(set(holding_ids_by_issue) & set(argument_ids_by_issue)):
        for holding_id in sorted(holding_ids_by_issue[issue_id]):
            for argument_id in sorted(argument_ids_by_issue[issue_id]):
                holding = holdings.get(holding_id, {})
                argument = arguments.get(argument_id, {})
                orientation = holding_index.get(holding_id, {}).get("orientation")
                text = _normalize_text(
                    holding.get("summary"),
                    holding.get("scope"),
                    argument.get("title"),
                    argument.get("description"),
                )
                has_chevron_overrule_text = "overrule" in text and "chevron" in text
                if orientation == "overrules_deference" or has_chevron_overrule_text:
                    support_strength = None
                    risk_type = "adverse_authority"
                    if orientation == "overrules_deference":
                        support_verdict = "contradict"
                        risk_verdict = "support"
                        rationale = (
                            "Curated holding metadata says the holding removes Chevron "
                            "deference that this argument's issue path relies on."
                        )
                    else:
                        support_verdict = "unsure"
                        risk_verdict = "unsure"
                        rationale = (
                            "Keyword scan found overrule/Chevron language on a shared "
                            "issue path; attorney review must confirm argument risk."
                        )
                elif orientation == "limits_deference":
                    support_verdict = "unsure"
                    risk_verdict = "support"
                    support_strength = "weak"
                    risk_type = "distinction_required"
                    rationale = (
                        "The holding preserves some deference but narrows when the "
                        "argument can invoke it."
                    )
                else:
                    support_verdict = "support"
                    risk_verdict = "contradict"
                    support_strength = "strong" if "chevron" in text else "moderate"
                    risk_type = None
                    rationale = "The holding and argument share a tracked legal issue."
                rows.append({
                    "holding_id": holding_id,
                    "argument_id": argument_id,
                    "support_strength": support_strength,
                    "risk_type": risk_type,
                    "rationale": rationale,
                    "support_verdict": support_verdict,
                    "risk_verdict": risk_verdict,
                    "issue_id": issue_id,
                })
    return {"items": _dedupe_sorted(rows, "holding_id", "argument_id")}


def scope_matters_to_statutes(payload: dict[str, Any], context: ProviderContext | None = None) -> dict[str, Any]:
    _require_mapping(payload, "scope_matters_to_statutes")
    match_hints = _load_match_hints(context)
    matters = {_entity_id(row, "matter_id"): _properties(row) for row in _rows(payload, "matters")}
    statutes = {_entity_id(row, "statute_id"): _properties(row) for row in _rows(payload, "statutes")}
    rows: list[dict[str, Any]] = []
    for matter_id, matter in sorted(matters.items()):
        matter_text = _normalize_text(matter.get("name"), matter.get("jurisdiction"), matter.get("matter_type"))
        matter_tokens = set(matter_text.split())
        for statute_id, statute in sorted(statutes.items()):
            statute_text = _normalize_text(statute.get("title"), statute.get("section"), statute.get("topic"))
            hint_tokens = match_hints.get(statute_id or "", set())
            if _matter_statute_match(matter_tokens, statute_text, hint_tokens):
                basis = "Curated hint keywords" if hint_tokens & matter_tokens else "Token overlap"
                rows.append({
                    "matter_id": matter_id,
                    "statute_id": statute_id,
                    "scope_basis": (
                        f"{basis} matched matter '{matter.get('name') or matter_id}' "
                        f"to {statute.get('title') or statute_id}."
                    ),
                    "verdict": "unsure",
                })
    return {"items": _dedupe_sorted(rows, "matter_id", "statute_id")}


def assess_matter_impact(payload: dict[str, Any], context: ProviderContext | None = None) -> dict[str, Any]:
    _require_mapping(payload, "assess_matter_impact")
    del context
    opinions = {_entity_id(row, "opinion_id"): _properties(row) for row in _rows(payload, "opinions")}
    matters = {_entity_id(row, "matter_id"): _properties(row) for row in _rows(payload, "matters")}
    matter_statutes: dict[str, set[str]] = defaultdict(set)
    for edge in _rows(payload, "matter_statute_edges"):
        matter_id = _edge_from_id(edge, "matter_id")
        statute_id = _edge_to_id(edge, "statute_id")
        if matter_id and statute_id:
            matter_statutes[matter_id].add(statute_id)

    opinion_courts: dict[str, set[str]] = defaultdict(set)
    for edge in _rows(payload, "opinion_court_edges"):
        opinion_id = _edge_from_id(edge, "opinion_id")
        court_id = _edge_to_id(edge, "court_id")
        if opinion_id and court_id:
            opinion_courts[opinion_id].add(court_id)
    matter_courts: dict[str, set[str]] = defaultdict(set)
    for edge in _rows(payload, "matter_jurisdiction_edges"):
        matter_id = _edge_from_id(edge, "matter_id")
        court_id = _edge_to_id(edge, "court_id")
        if matter_id and court_id:
            matter_courts[matter_id].add(court_id)

    opinions_by_holding: dict[str, set[str]] = defaultdict(set)
    for edge in _rows(payload, "opinion_holding_edges"):
        opinion_id = _edge_from_id(edge, "opinion_id")
        holding_id = _edge_to_id(edge, "holding_id")
        if opinion_id and holding_id:
            opinions_by_holding[holding_id].add(opinion_id)

    matters_by_argument: dict[str, set[str]] = defaultdict(set)
    for edge in _rows(payload, "argument_matter_edges"):
        argument_id = _edge_from_id(edge, "argument_id")
        matter_id = _edge_to_id(edge, "matter_id")
        if argument_id and matter_id:
            matters_by_argument[argument_id].add(matter_id)

    rows_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for edge in _rows(payload, "treatment_edges"):
        opinion_id = _edge_from_id(edge, "source_opinion_id")
        cited_id = _edge_to_id(edge, "cited_opinion_id")
        treatment = _properties(edge).get("treatment")
        if not opinion_id or treatment not in NEGATIVE_TREATMENTS:
            continue
        for matter_id, statutes in matter_statutes.items():
            if cited_id == "op_chevron" and "stat_chevron_doctrine" not in statutes:
                continue
            _store_matter_impact(
                rows_by_pair,
                opinion_id,
                matter_id,
                "critical" if treatment in {"overrules", "abrogates"} else "high",
                "adverse_authority",
                f"Negative treatment ({treatment}) reaches authority in the matter's scope.",
                opinions,
                matters,
                opinion_courts,
                matter_courts,
            )

    for edge in _rows(payload, "support_edges"):
        holding_id = _edge_from_id(edge, "holding_id")
        argument_id = _edge_to_id(edge, "argument_id")
        if not holding_id or not argument_id:
            continue
        for opinion_id in sorted(opinions_by_holding.get(holding_id, set())):
            for matter_id in sorted(matters_by_argument.get(argument_id, set())):
                _store_matter_impact(
                    rows_by_pair,
                    opinion_id,
                    matter_id,
                    "low",
                    "monitoring_only",
                    "A holding supports an argument linked to this matter.",
                    opinions,
                    matters,
                    opinion_courts,
                    matter_courts,
                )

    for edge in _rows(payload, "undermine_edges"):
        holding_id = _edge_from_id(edge, "holding_id")
        argument_id = _edge_to_id(edge, "argument_id")
        joined_opinion_ids = opinions_by_holding.get(holding_id or "", set())
        joined_matter_ids = matters_by_argument.get(argument_id or "", set())
        if not joined_opinion_ids:
            direct_opinion_id = _first_non_empty(edge.get("opinion_id"))
            if direct_opinion_id:
                joined_opinion_ids = {direct_opinion_id}
        if not joined_matter_ids:
            direct_matter_id = _first_non_empty(edge.get("matter_id"))
            if direct_matter_id:
                joined_matter_ids = {direct_matter_id}
        for opinion_id in sorted(joined_opinion_ids):
            for matter_id in sorted(joined_matter_ids):
                _store_matter_impact(
                    rows_by_pair,
                    opinion_id,
                    matter_id,
                    "high",
                    "argument_update",
                    "A holding undermines an argument linked to this matter.",
                    opinions,
                    matters,
                    opinion_courts,
                    matter_courts,
                )

    return {"items": _sorted_rows(rows_by_pair.values(), "opinion_id", "matter_id")}


def assess_filing_response_obligations(
    payload: dict[str, Any],
    context: ProviderContext | None = None,
) -> dict[str, Any]:
    _require_mapping(payload, "assess_filing_response_obligations")
    del context
    filings = _rows(payload, "filings")
    matters = {_entity_id(row, "matter_id"): _properties(row) for row in _rows(payload, "matters")}
    deadlines = [_properties_with_id(row, "deadline_id") for row in _rows(payload, "deadlines")]
    deadlines_by_id = {row["deadline_id"]: row for row in deadlines if row.get("deadline_id")}
    filing_matter_edges = _rows(payload, "filing_in_matter_edges")
    matter_deadline_edges = _rows(payload, "matter_deadline_edges")
    matters_by_filing: dict[str, set[str]] = defaultdict(set)
    for edge in filing_matter_edges:
        filing_id = _edge_from_id(edge, "filing_id")
        matter_id = _edge_to_id(edge, "matter_id")
        if filing_id and matter_id:
            matters_by_filing[filing_id].add(matter_id)
    deadlines_by_matter: dict[str, set[str]] = defaultdict(set)
    for edge in matter_deadline_edges:
        matter_id = _edge_from_id(edge, "matter_id")
        deadline_id = _edge_to_id(edge, "deadline_id")
        if matter_id and deadline_id:
            deadlines_by_matter[matter_id].add(deadline_id)
    rows: list[dict[str, Any]] = []
    for filing in filings:
        filing_id = _entity_id(filing, "filing_id")
        filing_props = _properties(filing)
        if not filing_id:
            continue
        if filing_matter_edges:
            matched_matter_ids = sorted(
                matter_id for matter_id in matters_by_filing.get(filing_id, set()) if matter_id in matters
            )
            routing_verdict = "support"
        else:
            matched_matter_ids = _matched_matter_ids(filing, filing_props, matters)
            routing_verdict = "unsure"
        for matter_id in matched_matter_ids:
            response_type, obligation_verdict = _response_type_and_verdict(filing_props)
            deadline_date = _matched_deadline_date(
                matter_id,
                filing_props,
                deadlines,
                deadlines_by_matter=deadlines_by_matter if matter_deadline_edges else None,
                deadlines_by_id=deadlines_by_id,
            )
            rows.append({
                "filing_id": filing_id,
                "matter_id": matter_id,
                "response_type": response_type,
                "deadline_date": deadline_date,
                "rationale": (
                    f"Filing '{filing_props.get('title') or filing_id}' is routed to "
                    f"matter '{matters[matter_id].get('name') or matter_id}'."
                ),
                "obligation_verdict": obligation_verdict,
                "routing_verdict": routing_verdict,
            })
    return {"items": _dedupe_sorted(rows, "filing_id", "matter_id")}


def route_review_work(payload: dict[str, Any], context: ProviderContext | None = None) -> dict[str, Any]:
    _require_mapping(payload, "route_review_work")
    del context
    opinions = {_entity_id(row, "opinion_id"): _properties(row) for row in _rows(payload, "opinions")}
    matters = {_entity_id(row, "matter_id"): _properties(row) for row in _rows(payload, "matters")}
    matters_by_argument: dict[str, set[str]] = defaultdict(set)
    for edge in _rows(payload, "argument_matter_edges"):
        argument_id = _edge_from_id(edge, "argument_id")
        matter_id = _edge_to_id(edge, "matter_id")
        if argument_id and matter_id:
            matters_by_argument[argument_id].add(matter_id)
    arguments_by_opinion: dict[str, set[str]] = defaultdict(set)
    for edge in _rows(payload, "argument_citation_edges"):
        argument_id = _edge_from_id(edge, "argument_id")
        opinion_id = _edge_to_id(edge, "opinion_id")
        if argument_id and opinion_id:
            arguments_by_opinion[opinion_id].add(argument_id)
    rows: list[dict[str, Any]] = []
    for edge in _rows(payload, "matter_impact_edges"):
        opinion_id = _edge_from_id(edge, "opinion_id")
        matter_id = _edge_to_id(edge, "matter_id")
        if not opinion_id or not matter_id:
            continue
        props = _properties(edge)
        obligation_type = _obligation_type(props)
        priority = _priority_for_impact(props.get("impact_level"))
        opinion_name = opinions.get(opinion_id, {}).get("case_name") or opinion_id
        matter_name = matters.get(matter_id, {}).get("name") or matter_id
        rationale = _first_non_empty(props.get("rationale")) or (
            f"{opinion_name} has accepted impact on {matter_name}."
        )
        rows.append({
            "work_item_id": f"wi_{_slugify(opinion_id)}_{_slugify(matter_id)}_{obligation_type}",
            "title": f"Review {opinion_name} impact on {matter_name}",
            "summary": f"{opinion_name} may require {obligation_type.replace('_', ' ')}.",
            "description": rationale,
            "rationale": rationale,
            "work_item_type": "research",
            "type": "research",
            "status": "planned",
            "priority": priority,
            "target_date": None,
            "opinion_id": opinion_id,
            "matter_id": matter_id,
            "obligation_type": obligation_type,
            "verdict": "support",
        })

    for edge in _rows(payload, "treatment_edges"):
        opinion_id = _edge_from_id(edge, "source_opinion_id")
        cited_id = _edge_to_id(edge, "cited_opinion_id")
        if not cited_id or not opinion_id:
            continue
        props = _properties(edge)
        if props.get("treatment") not in NEGATIVE_TREATMENTS:
            continue
        affected_matter_ids: set[str] = set()
        direct_matter_id = _first_non_empty(edge.get("matter_id"))
        if direct_matter_id:
            affected_matter_ids.add(direct_matter_id)
        for argument_id in arguments_by_opinion.get(cited_id, set()):
            affected_matter_ids.update(matters_by_argument.get(argument_id, set()))
        for matter_id in sorted(affected_matter_ids):
            if matter_id not in matters:
                continue
            rows.append({
                "work_item_id": f"wi_{_slugify(opinion_id)}_{_slugify(matter_id)}_citation_check",
                "title": f"Check negative treatment for {matters.get(matter_id, {}).get('name') or matter_id}",
                "summary": "A cited authority has negative treatment.",
                "description": props.get("rationale"),
                "rationale": props.get("rationale") or "Negative treatment requires citation review.",
                "work_item_type": "research",
                "type": "research",
                "status": "planned",
                "priority": "high",
                "target_date": None,
                "opinion_id": opinion_id,
                "matter_id": matter_id,
                "obligation_type": "citation_check",
                "verdict": "support",
            })
    return {"items": _dedupe_sorted(rows, "work_item_id")}


_DEV_SEED_DIR = Path(__file__).resolve().parents[1] / "data" / "seed"


def _load_seed_json(filename: str, context: ProviderContext | None) -> dict[str, Any]:
    """SDK artifact loading; the dev-tree fallback serves direct calls in tests."""
    return load_artifact_json(context, filename, fallback_dir=_DEV_SEED_DIR)


def _corpus_payload(data: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field in CORPUS_FIELDS:
        value = data.get(field, [])
        if not isinstance(value, list):
            raise ValueError(f"corpus seed field '{field}' must be a list")
        payload[field] = [dict(row) for row in value]
    return payload


def _require_mapping(payload: dict[str, Any], provider_name: str) -> None:
    if not isinstance(payload, dict):
        raise ValueError(f"{provider_name} requires a payload object")


def _rows(payload: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"'{key}' must be a list of objects")
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise ValueError(f"'{key}' row {index} must be an object")
        rows.append(dict(row))
    return rows


def _properties(row: Mapping[str, Any]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    nested = row.get("properties")
    if isinstance(nested, Mapping):
        props.update(dict(nested))
    for key, value in row.items():
        if key not in {"properties", "metadata", "_query_result_index", "source"}:
            props.setdefault(key, value)
    return props


def _properties_with_id(row: Mapping[str, Any], id_key: str) -> dict[str, Any]:
    props = _properties(row)
    props[id_key] = _entity_id(row, id_key)
    return props


def _entity_id(row: Mapping[str, Any], id_key: str) -> str | None:
    props = _properties(row)
    return _first_non_empty(row.get(id_key), props.get(id_key), row.get("entity_id"))


def _edge_from_id(row: Mapping[str, Any], preferred_key: str) -> str | None:
    props = _properties(row)
    return _first_non_empty(row.get(preferred_key), props.get(preferred_key), row.get("from_id"))


def _edge_to_id(row: Mapping[str, Any], preferred_key: str) -> str | None:
    props = _properties(row)
    return _first_non_empty(row.get(preferred_key), props.get(preferred_key), row.get("to_id"))


def _mapping_value(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return None


def _first_non_empty(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _parse_date(value: Any) -> date | None:
    text = _first_non_empty(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _sorted_rows(rows: Iterable[Mapping[str, Any]], *keys: str) -> list[dict[str, Any]]:
    return sorted(
        [dict(row) for row in rows],
        key=lambda row: tuple(str(row.get(key, "")) for key in keys),
    )


def _dedupe_sorted(rows: Iterable[Mapping[str, Any]], *keys: str) -> list[dict[str, Any]]:
    deduped: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        key = tuple(str(item.get(name, "")) for name in keys)
        deduped.setdefault(key, item)
    return _sorted_rows(deduped.values(), *keys)


def _keyword_statute_matches(
    holding_props: Mapping[str, Any],
    statutes: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    text = _normalize_text(holding_props.get("summary"), holding_props.get("scope"))
    matches: list[str] = []
    for statute_id, statute in statutes.items():
        statute_text = _normalize_text(statute.get("title"), statute.get("section"), statute.get("topic"))
        if statute_id == "stat_chevron_doctrine" and ("chevron" in text or "deference" in text):
            matches.append(statute_id)
        elif any(token in text for token in _significant_tokens(statute_text)):
            matches.append(statute_id)
    return sorted(dict.fromkeys(matches))


def _keyword_issue_matches(
    holding_props: Mapping[str, Any],
    issues: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    text = _normalize_text(holding_props.get("summary"), holding_props.get("scope"))
    matches: set[str] = set()
    for issue_id, issue in issues.items():
        issue_text = _normalize_text(issue.get("name"), issue.get("description"), issue.get("practice_area"))
        if any(token in text for token in _significant_tokens(issue_text)):
            matches.add(issue_id)
    return matches


def _interpretation_type(props: Mapping[str, Any], curated: Mapping[str, Any]) -> str:
    text = _normalize_text(props.get("summary"), props.get("scope"), curated.get("summary"))
    if "overrule" in text or "independent judgment" in text:
        return "narrows"
    if "limit" in text or "force of law" in text:
        return "clarifies"
    if "exception" in text:
        return "creates_exception"
    return "applies"


def _classify_treatment(context_text: str) -> tuple[str, str, str]:
    text = _normalize_text(context_text)
    if "overrule" in text:
        return "overrules", "support", "states"
    if "abrogate" in text:
        return "abrogates", "support", "states"
    if "limit" in text or "narrow" in text:
        return "limits", "support", "states"
    if "distinguish" in text:
        return "distinguishes", "support", "states"
    if "critic" in text or "question" in text:
        return "criticizes", "support", "states"
    if (
        "follow" in text
        or "appl" in text
        or "rely" in text
        or "relies" in text
        or "relied" in text
        or "reaffirm" in text
    ):
        return "follows", "support", "states"
    return "follows", "unsure", "does not state"


def _load_match_hints(context: ProviderContext | None) -> dict[str, set[str]]:
    """Optional per-statute keyword hints from the seed bundle.

    Hints are curated bundle data (``statute_match_hints.json``), never
    provider logic: absent the file, matching falls back to plain
    significant-token overlap between statute text and matter text.
    """
    try:
        raw = _load_seed_json("statute_match_hints.json", context)
    except ValueError:
        return {}
    hints = raw.get("hints")
    if not isinstance(hints, Mapping):
        return {}
    return {
        str(statute_id): {str(token) for token in tokens}
        for statute_id, tokens in hints.items()
        if isinstance(tokens, list)
    }


def _matter_statute_match(
    matter_tokens: set[str],
    statute_text: str,
    hint_tokens: set[str],
) -> bool:
    if hint_tokens & matter_tokens:
        return True
    return bool(set(_significant_tokens(statute_text)) & matter_tokens)


def _store_matter_impact(
    rows_by_pair: dict[tuple[str, str], dict[str, Any]],
    opinion_id: str,
    matter_id: str,
    impact_level: str,
    impact_type: str,
    rationale: str,
    opinions: Mapping[str, Mapping[str, Any]],
    matters: Mapping[str, Mapping[str, Any]],
    opinion_courts: Mapping[str, set[str]],
    matter_courts: Mapping[str, set[str]],
) -> None:
    if opinion_id not in opinions or matter_id not in matters:
        return
    o_courts = opinion_courts.get(opinion_id, set())
    m_courts = matter_courts.get(matter_id, set())
    if "court_scotus" in o_courts:
        jurisdiction_verdict = "support"
        jurisdiction_rationale = "Supreme Court authority applies nationally."
    elif o_courts & m_courts:
        jurisdiction_verdict = "support"
        jurisdiction_rationale = "Opinion and matter share a court jurisdiction."
    else:
        jurisdiction_verdict = "unsure"
        jurisdiction_rationale = "No direct court overlap; legal issue overlap still warrants review."
    rows_by_pair.setdefault(
        (opinion_id, matter_id),
        {
            "opinion_id": opinion_id,
            "matter_id": matter_id,
            "impact_level": impact_level,
            "impact_type": impact_type,
            "rationale": rationale,
            "jurisdiction_verdict": jurisdiction_verdict,
            "jurisdiction_rationale": jurisdiction_rationale,
            "verdict": "support",
        },
    )


def _matched_matter_ids(
    filing: Mapping[str, Any],
    filing_props: Mapping[str, Any],
    matters: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    explicit = _first_non_empty(filing.get("matter_id"), filing_props.get("matter_id"))
    if explicit and explicit in matters:
        return [explicit]
    filing_text = _normalize_text(
        filing_props.get("title"),
        filing_props.get("docket_number"),
        filing_props.get("filing_type"),
    )
    matches = [
        matter_id
        for matter_id, matter in matters.items()
        if set(_significant_tokens(_normalize_text(matter.get("name")))) & set(filing_text.split())
    ]
    return sorted(matches)


def _matched_deadline_date(
    matter_id: str,
    filing_props: Mapping[str, Any],
    deadlines: list[Mapping[str, Any]],
    *,
    deadlines_by_matter: Mapping[str, set[str]] | None = None,
    deadlines_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> str | None:
    if deadlines_by_matter is not None and deadlines_by_id is not None:
        candidates = [
            due_date
            for deadline_id in deadlines_by_matter.get(matter_id, set())
            if (due_date := _first_non_empty(deadlines_by_id.get(deadline_id, {}).get("due_date")))
        ]
        return sorted(candidates)[0] if candidates else None
    filing_text = _normalize_text(filing_props.get("title"), filing_props.get("docket_number"))
    candidates: list[str] = []
    for deadline in deadlines:
        deadline_matter = _first_non_empty(deadline.get("matter_id"))
        deadline_text = _normalize_text(deadline.get("title"))
        if deadline_matter == matter_id or set(_significant_tokens(deadline_text)) & set(filing_text.split()):
            due_date = _first_non_empty(deadline.get("due_date"))
            if due_date:
                candidates.append(due_date)
    return sorted(candidates)[0] if candidates else None


def _response_type_and_verdict(filing_props: Mapping[str, Any]) -> tuple[str, str]:
    filing_type = _first_non_empty(filing_props.get("filing_type")) or ""
    title = _normalize_text(filing_props.get("title"))
    if filing_type == "complaint":
        return "answer", "support"
    if filing_type == "motion":
        return "opposition", "support"
    if "motion" in title:
        return "opposition", "unsure"
    if filing_type in {"order", "notice", "letter", "memo"}:
        return "client_update", "support"
    return "client_update", "unsure"


def _obligation_type(props: Mapping[str, Any]) -> str:
    impact_type = _first_non_empty(props.get("impact_type")) or ""
    if impact_type == "adverse_authority":
        return "strategy_review"
    if impact_type == "filing_deadline":
        return "deadline_check"
    if impact_type == "monitoring_only":
        return "citation_check"
    return "brief_update"


def _priority_for_impact(value: Any) -> str:
    level = _first_non_empty(value) or "medium"
    if level == "critical":
        return "critical"
    if level == "high":
        return "high"
    if level == "low":
        return "low"
    return "medium"


def _normalize_text(*values: Any) -> str:
    return " ".join(
        re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()
        for value in values
        if value is not None
    ).strip()


def _significant_tokens(text: str) -> list[str]:
    stop = {
        "and",
        "for",
        "inc",
        "law",
        "of",
        "or",
        "rule",
        "the",
        "to",
        "u",
        "s",
        "v",
    }
    return [token for token in text.split() if len(token) > 2 and token not in stop]


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "unknown"
