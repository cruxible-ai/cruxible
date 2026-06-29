"""Common providers for generic alias-based entity resolution."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from cruxible_core.provider.types import ProviderContext


def resolve_entities_by_alias(
    input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    """Resolve generic records to existing entities using aliases and scores."""
    records = _object_list(input_payload.get("records"), "records")
    entities = _object_list(input_payload.get("entities"), "entities")
    record_alias_fields = _string_list(
        input_payload.get("record_alias_fields"),
        "record_alias_fields",
    )
    entity_alias_fields = _string_list(
        input_payload.get("entity_alias_fields"),
        "entity_alias_fields",
    )
    record_id_field = str(input_payload.get("record_id_field", "id"))
    entity_id_field = str(input_payload.get("entity_id_field", "entity_id"))
    threshold = _float_value(input_payload.get("threshold", 0.82), "threshold")
    ambiguous_delta = _float_value(input_payload.get("ambiguous_delta", 0.03), "ambiguous_delta")

    entity_candidates = [
        {
            "entity": entity,
            "entity_id": _first_string(entity, [entity_id_field, "id", "entity_id"]),
            "aliases": _aliases(entity, entity_alias_fields),
        }
        for entity in entities
    ]

    matches: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []

    for record in records:
        record_id = _first_string(record, [record_id_field, "id", "_row_id"])
        record_aliases = _aliases(record, record_alias_fields)
        scored = [
            _score_entity(record_aliases, candidate)
            for candidate in entity_candidates
            if candidate["entity_id"]
        ]
        scored = [item for item in scored if item["score"] > 0]
        scored.sort(key=lambda item: (-float(item["score"]), str(item["entity_id"])))

        if not scored or float(scored[0]["score"]) < threshold:
            unmatched.append({"record_id": record_id, "record": record, "best": scored[:3]})
            continue

        best = scored[0]
        second = scored[1] if len(scored) > 1 else None
        if second is not None and float(best["score"]) - float(second["score"]) <= ambiguous_delta:
            ambiguous.append(
                {
                    "record_id": record_id,
                    "record": record,
                    "candidates": scored[:3],
                }
            )
            continue

        matches.append(
            {
                "record_id": record_id,
                "entity_id": best["entity_id"],
                "score": best["score"],
                "record_alias": best["record_alias"],
                "entity_alias": best["entity_alias"],
                "record": record,
            }
        )

    return {
        "matches": matches,
        "unmatched": unmatched,
        "ambiguous": ambiguous,
        "summary": {
            "records": len(records),
            "entities": len(entities),
            "matches": len(matches),
            "unmatched": len(unmatched),
            "ambiguous": len(ambiguous),
        },
    }


def _score_entity(
    record_aliases: list[str],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    best_score = 0.0
    best_pair = ("", "")
    for record_alias in record_aliases:
        for entity_alias in candidate["aliases"]:
            score = _alias_score(record_alias, entity_alias)
            if score > best_score:
                best_score = score
                best_pair = (record_alias, entity_alias)
    return {
        "entity_id": candidate["entity_id"],
        "score": round(best_score, 4),
        "record_alias": best_pair[0],
        "entity_alias": best_pair[1],
    }


def _alias_score(left: str, right: str) -> float:
    normalized_left = _normalize_alias(left)
    normalized_right = _normalize_alias(right)
    if not normalized_left or not normalized_right:
        return 0.0
    if normalized_left == normalized_right:
        return 1.0

    left_tokens = set(normalized_left.split())
    right_tokens = set(normalized_right.split())
    token_overlap = len(left_tokens & right_tokens) / max(len(left_tokens), len(right_tokens))
    sequence_score = SequenceMatcher(None, normalized_left, normalized_right).ratio()
    abbreviation_score = _abbreviation_score(left_tokens, right_tokens)
    if left_tokens <= right_tokens or right_tokens <= left_tokens:
        return max(token_overlap, sequence_score, abbreviation_score, 0.92)
    return max(token_overlap, sequence_score, abbreviation_score)


def _abbreviation_score(left_tokens: set[str], right_tokens: set[str]) -> float:
    for short_tokens, long_tokens in ((left_tokens, right_tokens), (right_tokens, left_tokens)):
        if not short_tokens or not long_tokens:
            continue
        matched = 0
        for token in short_tokens:
            if token in long_tokens:
                matched += 1
                continue
            if any(other.startswith(token) or token.startswith(other) for other in long_tokens):
                matched += 1
        if matched == len(short_tokens):
            return 0.86
    return 0.0


def _aliases(item: dict[str, Any], fields: list[str]) -> list[str]:
    aliases: list[str] = []
    for field in fields:
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            aliases.append(value)
        elif isinstance(value, list):
            aliases.extend(str(alias) for alias in value if str(alias).strip())
    return aliases


def _normalize_alias(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _first_string(item: dict[str, Any], fields: list[str]) -> str:
    for field in fields:
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _object_list(value: Any, field_name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{field_name} must be a list of objects")
    return value


def _string_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    return value


def _float_value(value: Any, field_name: str) -> float:
    if not isinstance(value, str | int | float):
        raise ValueError(f"{field_name} must be numeric")
    return float(value)
