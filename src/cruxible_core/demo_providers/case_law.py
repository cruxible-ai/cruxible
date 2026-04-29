"""Deterministic provider helpers used by the case-law monitoring demo configs."""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from cruxible_core.provider.types import ProviderContext

# ---------------------------------------------------------------------------
# Seed file mappings
# ---------------------------------------------------------------------------

_FORK_SEED_FILES = {
    "matters": "matters.csv",
    "clients": "clients.csv",
    "attorneys": "attorneys.csv",
    "practice_areas": "practice_areas.csv",
    "positions": "positions.csv",
    "deadlines": "deadlines.csv",
    "matter_client": "matter_client.csv",
    "matter_attorney": "matter_attorney.csv",
    "matter_practice_area": "matter_practice_area.csv",
    "matter_position": "matter_position.csv",
    "matter_deadline": "matter_deadline.csv",
}

# Court-to-circuit mapping for jurisdiction analysis
_COURT_CIRCUIT: dict[str, str] = {
    "scotus": "supreme",
    "ca9": "9",
    "ca2": "2",
    "ca5": "5",
    "cafc": "federal",
    "cacd": "9",
    "nysd": "2",
    "txed": "5",
}

# Filing types that require a response
_RESPONSE_FILING_TYPES = {"motion", "brief"}
_NO_RESPONSE_FILING_TYPES = {"order", "notice", "complaint"}

# Treatment-to-position-relationship mapping
_TREATMENT_TO_RELATIONSHIP: dict[str, str] = {
    "applied": "supports",
    "construed": "supports",
    "cited": "supports",
    "distinguished": "distinguishes",
    "overruled": "weakens",
}

# Response windows by filing type (days)
_RESPONSE_WINDOWS: dict[str, int] = {
    "motion": 21,
    "brief": 14,
}


# ---------------------------------------------------------------------------
# Provider 1: Reference layer loader
# ---------------------------------------------------------------------------


def load_public_courtlistener_rows(
    _input_payload: dict[str, Any],
    context: ProviderContext,
) -> dict[str, Any]:
    """Load normalized CourtListener reference data from the bundled artifact."""
    root = _require_artifact_root(context, "load_public_courtlistener_rows")

    courts = _load_csv_rows(root / "courts.csv")
    judges = _load_csv_rows(root / "judges.csv")
    for judge in judges:
        judge["active"] = _parse_bool(judge.get("active"))

    dockets = _load_csv_rows(root / "dockets.csv")
    opinions = _load_csv_rows(root / "opinions.csv")
    filings = _load_csv_rows(root / "filings.csv")
    for filing in filings:
        filing["entry_number"] = _parse_int(filing.get("entry_number"))

    statutes = _load_csv_rows(root / "statutes.csv")

    opinion_citation = _load_csv_rows(root / "opinion_citation.csv")
    for citation in opinion_citation:
        citation["depth"] = _parse_int(citation.get("depth"))

    return {
        "courts": courts,
        "judges": judges,
        "dockets": dockets,
        "opinions": opinions,
        "filings": filings,
        "statutes": statutes,
        "opinion_docket": _load_csv_rows(root / "opinion_docket.csv"),
        "opinion_judge": _load_csv_rows(root / "opinion_judge.csv"),
        "opinion_statute": _load_csv_rows(root / "opinion_statute.csv"),
        "opinion_citation": opinion_citation,
        "filing_docket": _load_csv_rows(root / "filing_docket.csv"),
        "docket_court": _load_csv_rows(root / "docket_court.csv"),
    }


# ---------------------------------------------------------------------------
# Provider 2: Fork seed loader
# ---------------------------------------------------------------------------


def load_firm_seed_data(
    _input_payload: dict[str, Any],
    context: ProviderContext,
) -> dict[str, Any]:
    """Load deterministic firm entity and relationship rows from the seed bundle."""
    root = _require_artifact_root(context, "load_firm_seed_data")
    return {
        key: _load_csv_rows(root / filename)
        for key, filename in _FORK_SEED_FILES.items()
    }


# ---------------------------------------------------------------------------
# Provider 3: Outcome loader
# ---------------------------------------------------------------------------


def load_case_outcomes(
    _input_payload: dict[str, Any],
    context: ProviderContext,
) -> dict[str, Any]:
    """Load resolved case outcomes and derive outcome-matter edges."""
    root = _require_artifact_root(context, "load_case_outcomes")

    outcomes = _load_csv_rows(root / "outcomes.csv")
    outcome_position = _load_csv_rows(root / "outcome_position.csv")
    for row in outcome_position:
        row["prevailed"] = _parse_bool(row.get("prevailed"))

    # Derive outcome_matter edges from outcomes.csv matter_id field
    outcome_matter = [
        {"outcome_id": row["outcome_id"], "matter_id": row["matter_id"]}
        for row in outcomes
        if row.get("outcome_id") and row.get("matter_id")
    ]

    return {
        "outcomes": outcomes,
        "outcome_matter": outcome_matter,
        "outcome_position": outcome_position,
    }


# ---------------------------------------------------------------------------
# Provider 4: Matter-statute extraction
# ---------------------------------------------------------------------------


def extract_matter_statutes(
    input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    """Match matters to statutes by keyword/citation overlap."""
    matters = _require_items(input_payload, "matters")
    statutes = _require_items(input_payload, "statutes")

    candidates: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []

    for matter in matters:
        matter_id = _entity_id(matter)
        props = _entity_properties(matter)
        matter_text = _normalize_text(props.get("title", ""))
        if not matter_id or not matter_text:
            continue

        for statute in statutes:
            statute_id = _entity_id(statute)
            statute_props = _entity_properties(statute)
            if not statute_id:
                continue

            score = _statute_match_score(matter_text, statute_props)
            if score <= 0:
                continue

            candidates.append({
                "matter_id": matter_id,
                "statute_id": statute_id,
            })
            signals.append({
                "matter_id": matter_id,
                "statute_id": statute_id,
                "signal": "support" if score >= 2 else "unsure",
            })

    return {"candidates": candidates, "signals": signals}


def _statute_match_score(matter_text: str, statute_props: dict[str, Any]) -> int:
    """Score how well a matter's title matches a statute. 0 = no match."""
    score = 0
    title = _normalize_text(statute_props.get("title", ""))
    citation = _normalize_text(statute_props.get("citation", ""))

    # Check for citation reference (e.g., "15 usc" in matter title)
    if citation:
        # Extract the USC-style reference (e.g., "42 usc")
        usc_match = re.search(r"(\d+)\s*usc", citation)
        if usc_match and usc_match.group(0) in matter_text:
            score += 3

    # Check for statute name keywords in matter title
    if title:
        title_words = set(title.split()) - {"act", "of", "the", "and", "for", "a"}
        matter_words = set(matter_text.split())
        overlap = title_words & matter_words
        if len(overlap) >= 2:
            score += 2
        elif len(overlap) >= 1 and len(title_words) <= 3:
            score += 1

    return score


# ---------------------------------------------------------------------------
# Provider 5: Opinion-matter impact assessment
# ---------------------------------------------------------------------------


def assess_opinion_matter_impact(
    input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    """Assess whether opinions materially affect tracked matters via shared statutes."""
    opinions = _require_items(input_payload, "opinions")
    matters = _require_items(input_payload, "matters")
    matter_statutes = _require_items(input_payload, "matter_statutes")
    opinion_statutes = _require_items(input_payload, "opinion_statutes")

    # Build indexes: which statutes does each matter turn on?
    statutes_by_matter: dict[str, set[str]] = defaultdict(set)
    for edge in matter_statutes:
        from_id = _edge_from_id(edge)
        to_id = _edge_to_id(edge)
        if from_id and to_id:
            statutes_by_matter[from_id].add(to_id)

    # Which statutes does each opinion interpret?
    statutes_by_opinion: dict[str, set[str]] = defaultdict(set)
    for edge in opinion_statutes:
        from_id = _edge_from_id(edge)
        to_id = _edge_to_id(edge)
        if from_id and to_id:
            statutes_by_opinion[from_id].add(to_id)

    # Build opinion court index
    opinion_court: dict[str, str] = {}
    for opinion in opinions:
        oid = _entity_id(opinion)
        props = _entity_properties(opinion)
        if oid:
            opinion_court[oid] = props.get("court_id", "")

    # Build matter court index
    matter_court: dict[str, str] = {}
    for matter in matters:
        mid = _entity_id(matter)
        props = _entity_properties(matter)
        if mid:
            matter_court[mid] = props.get("court_id", "")

    candidates: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []

    for opinion in opinions:
        opinion_id = _entity_id(opinion)
        if not opinion_id:
            continue
        opinion_statute_ids = statutes_by_opinion.get(opinion_id, set())
        if not opinion_statute_ids:
            continue

        o_court = opinion_court.get(opinion_id, "")
        o_circuit = _court_to_circuit(o_court)

        for matter in matters:
            matter_id = _entity_id(matter)
            if not matter_id:
                continue
            matter_statute_ids = statutes_by_matter.get(matter_id, set())
            shared = opinion_statute_ids & matter_statute_ids
            if not shared:
                continue

            m_court = matter_court.get(matter_id, "")
            m_circuit = _court_to_circuit(m_court)

            is_binding = _is_binding_authority(o_circuit, m_circuit)
            impact_level = "binding" if is_binding else "persuasive"
            urgency = "immediate" if is_binding else "routine"
            impact_signal = "support"
            jurisdiction_signal = "support" if is_binding else "unsure"

            candidates.append({
                "opinion_id": opinion_id,
                "matter_id": matter_id,
                "impact_level": impact_level,
                "urgency": urgency,
            })
            signals.append({
                "opinion_id": opinion_id,
                "matter_id": matter_id,
                "impact_signal": impact_signal,
                "jurisdiction_signal": jurisdiction_signal,
            })

    return {"candidates": candidates, "signals": signals}


# ---------------------------------------------------------------------------
# Provider 6: Position authority assessment
# ---------------------------------------------------------------------------


def assess_position_authority(
    input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    """Assess whether opinions support, distinguish, or weaken tracked positions."""
    opinions = _require_items(input_payload, "opinions")
    positions = _require_items(input_payload, "positions")
    matter_statutes = _require_items(input_payload, "matter_statutes")
    opinion_statutes = _require_items(input_payload, "opinion_statutes")

    # matter_id -> set of statute_ids
    statutes_by_matter: dict[str, set[str]] = defaultdict(set)
    for edge in matter_statutes:
        from_id = _edge_from_id(edge)
        to_id = _edge_to_id(edge)
        if from_id and to_id:
            statutes_by_matter[from_id].add(to_id)

    # opinion_id -> list of (statute_id, treatment)
    opinion_statute_treatments: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for edge in opinion_statutes:
        from_id = _edge_from_id(edge)
        to_id = _edge_to_id(edge)
        props = _edge_properties(edge)
        treatment = props.get("treatment", "cited")
        if from_id and to_id:
            opinion_statute_treatments[from_id].append((to_id, treatment))

    # position_id -> matter_id
    position_matter: dict[str, str] = {}
    for position in positions:
        pid = _entity_id(position)
        props = _entity_properties(position)
        if pid:
            position_matter[pid] = props.get("matter_id", "")

    # opinion_id -> court_id
    opinion_court: dict[str, str] = {}
    for opinion in opinions:
        oid = _entity_id(opinion)
        props = _entity_properties(opinion)
        if oid:
            opinion_court[oid] = props.get("court_id", "")

    # We need matter -> court for circuit comparison
    # Reconstruct from positions' matter_id — we don't have matters directly,
    # but we can infer from the matter_statutes edges (from_id is matter_id)
    # and the positions carry matter_id in properties.

    candidates: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []

    for opinion in opinions:
        opinion_id = _entity_id(opinion)
        if not opinion_id:
            continue
        treatments = opinion_statute_treatments.get(opinion_id, [])
        if not treatments:
            continue

        o_court = opinion_court.get(opinion_id, "")
        o_circuit = _court_to_circuit(o_court)

        for position in positions:
            position_id = _entity_id(position)
            if not position_id:
                continue

            matter_id = position_matter.get(position_id, "")
            if not matter_id:
                continue

            matter_statute_ids = statutes_by_matter.get(matter_id, set())
            if not matter_statute_ids:
                continue

            # Find shared statutes and the strongest treatment
            best_relationship = None
            for statute_id, treatment in treatments:
                if statute_id in matter_statute_ids:
                    relationship = _TREATMENT_TO_RELATIONSHIP.get(treatment, "supports")
                    is_stronger = (
                        best_relationship is None
                        or _relationship_priority(relationship)
                        > _relationship_priority(best_relationship)
                    )
                    if is_stronger:
                        best_relationship = relationship

            if best_relationship is None:
                continue

            # Determine authority weight from circuit comparison
            # We don't have matter court directly, but we can approximate:
            # matter's court is tracked in matter properties (court_id)
            # For now, use the matter_id to look up via position's matter_id
            # We'll need to pass this through — for the demo we use a simple heuristic
            authority_weight = "persuasive"
            # Check if opinion circuit matches position's matter circuit
            # We don't have matter court in this provider's input, but we can
            # check if the opinion is from the same court system
            if o_circuit and o_circuit == "supreme":
                authority_weight = "binding"

            signal = "support" if best_relationship == "supports" else (
                "unsure" if best_relationship == "distinguishes" else "contradict"
            )

            candidates.append({
                "opinion_id": opinion_id,
                "position_id": position_id,
                "relationship_to_position": best_relationship,
                "authority_weight": authority_weight,
            })
            signals.append({
                "opinion_id": opinion_id,
                "position_id": position_id,
                "signal": signal,
            })

    return {"candidates": candidates, "signals": signals}


def _relationship_priority(relationship: str) -> int:
    return {"weakens": 3, "distinguishes": 2, "supports": 1}.get(relationship, 0)


# ---------------------------------------------------------------------------
# Provider 7: Filing obligation classification
# ---------------------------------------------------------------------------


def classify_filing_obligation(
    input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    """Classify filings into response obligations by matching to matters via docket_id."""
    filings = _require_items(input_payload, "filings")
    matters = _require_items(input_payload, "matters")

    # Build matter index by docket_id
    matters_by_docket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for matter in matters:
        props = _entity_properties(matter)
        docket_id = props.get("docket_id", "")
        if docket_id:
            matters_by_docket[docket_id].append(matter)

    candidates: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []

    for filing in filings:
        filing_id = _entity_id(filing)
        filing_props = _entity_properties(filing)
        if not filing_id:
            continue

        filing_docket = filing_props.get("docket_id", "")
        filing_type = (filing_props.get("filing_type", "") or "").lower()
        date_filed = filing_props.get("date_filed", "")

        matched_matters = matters_by_docket.get(filing_docket, [])
        if not matched_matters:
            continue

        requires_response = filing_type in _RESPONSE_FILING_TYPES
        response_type = _classify_response_type(filing_type)
        deadline_date = _compute_deadline(date_filed, filing_type)

        for matter in matched_matters:
            matter_id = _entity_id(matter)
            if not matter_id:
                continue

            obligation_signal = "support" if requires_response else "contradict"
            match_signal = "support"  # docket_id matched

            candidates.append({
                "filing_id": filing_id,
                "matter_id": matter_id,
                "response_type": response_type,
                "deadline_date": deadline_date,
            })
            signals.append({
                "filing_id": filing_id,
                "matter_id": matter_id,
                "obligation_signal": obligation_signal,
                "match_signal": match_signal,
            })

    return {"candidates": candidates, "signals": signals}


def _classify_response_type(filing_type: str) -> str:
    mapping = {
        "motion": "opposition",
        "brief": "reply",
        "complaint": "answer",
        "order": "no_action",
        "notice": "no_action",
    }
    return mapping.get(filing_type, "no_action")


def _compute_deadline(date_filed: str, filing_type: str) -> str:
    window_days = _RESPONSE_WINDOWS.get(filing_type)
    if not window_days or not date_filed:
        return ""
    try:
        filed_date = datetime.strptime(date_filed, "%Y-%m-%d")
        deadline = filed_date + timedelta(days=window_days)
        return deadline.strftime("%Y-%m-%d")
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Jurisdiction helpers
# ---------------------------------------------------------------------------


def _court_to_circuit(court_id: str) -> str:
    return _COURT_CIRCUIT.get(court_id, "")


def _is_binding_authority(opinion_circuit: str, matter_circuit: str) -> bool:
    if not opinion_circuit or not matter_circuit:
        return False
    if opinion_circuit == "supreme":
        return True
    return opinion_circuit == matter_circuit


# ---------------------------------------------------------------------------
# Shared helpers (same patterns as kev_triage.py)
# ---------------------------------------------------------------------------


def _require_artifact_root(context: ProviderContext, provider_name: str) -> Path:
    if context.artifact is None or context.artifact.local_path is None:
        raise ValueError(f"{provider_name} requires a local artifact bundle")
    return Path(context.artifact.local_path)


def _require_items(input_payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = input_payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"Expected '{key}' to be a list of objects")
    return value


def _load_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _parse_bool(value: Any) -> bool | None:
    text = _first_non_empty(value)
    if text is None:
        return None
    lowered = text.lower()
    if lowered in {"true", "1", "yes"}:
        return True
    if lowered in {"false", "0", "no"}:
        return False
    return None


def _parse_int(value: Any) -> int | None:
    text = _first_non_empty(value)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _first_non_empty(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _normalize_text(value: Any) -> str:
    text = _first_non_empty(value)
    if text is None:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _entity_id(entity: dict[str, Any]) -> str:
    return _first_non_empty(entity.get("entity_id")) or ""


def _entity_properties(entity: dict[str, Any]) -> dict[str, Any]:
    properties = entity.get("properties")
    return properties if isinstance(properties, dict) else entity


def _edge_from_id(edge: dict[str, Any]) -> str:
    return _first_non_empty(edge.get("from_id")) or ""


def _edge_to_id(edge: dict[str, Any]) -> str:
    return _first_non_empty(edge.get("to_id")) or ""


def _edge_properties(edge: dict[str, Any]) -> dict[str, Any]:
    properties = edge.get("properties")
    return properties if isinstance(properties, dict) else {}
