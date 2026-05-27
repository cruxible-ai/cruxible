"""Software inventory to KEV product matching providers."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from cruxible_core.provider.payloads import JsonItems, evidence_ref
from cruxible_core.provider.types import ProviderContext

_GENERIC_TOKENS = {
    "corp",
    "corporation",
    "co",
    "company",
    "foundation",
    "group",
    "inc",
    "llc",
    "ltd",
    "project",
}


def match_software_to_products(
    input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    """Match software inventory rows to reference products deterministically."""
    inventory_items = JsonItems.from_payload(input_payload, key="inventory_items").items
    reference_products = [
        product
        for raw_product in JsonItems.from_payload(input_payload, key="reference_products").items
        if (product := _normalize_reference_product(raw_product)) is not None
    ]
    reference_products = sorted(reference_products, key=_reference_product_sort_key)

    best_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for item in inventory_items:
        best_product: dict[str, Any] | None = None
        best_score = 0.0
        for product in reference_products:
            score = _score_product_match(item, product)
            if score > best_score or (
                score == best_score
                and best_product is not None
                and _reference_product_sort_key(product)
                < _reference_product_sort_key(best_product)
            ):
                best_product = product
                best_score = score

        if best_product is None or best_score < 0.5:
            continue

        pair = (str(item.get("asset_id", "")), str(best_product.get("product_id", "")))
        if not all(pair):
            continue

        row = {
            "asset_id": pair[0],
            "product_id": pair[1],
            "observed_software_name": _first_non_empty(item.get("software_name")) or "",
            "observed_vendor": _first_non_empty(item.get("vendor")) or "",
            "installed_version": _first_non_empty(item.get("version")) or "",
            "inventory_source": _first_non_empty(item.get("evidence_source")) or "",
            "last_seen_at": _first_non_empty(item.get("last_seen")) or "",
            "evidence_source": _first_non_empty(item.get("evidence_source")) or "",
            "match_confidence": round(best_score, 4),
            "match_basis": _match_basis(item, best_product, best_score),
            "evidence_refs": [
                evidence_ref(
                    _first_non_empty(item.get("evidence_source")) or "software_inventory",
                    _inventory_source_record_id(item),
                    observed_at=_first_non_empty(item.get("last_seen")) or "",
                )
            ],
            "rationale": _match_basis(item, best_product, best_score),
            "verdict": _score_to_verdict(best_score),
            "_last_seen": _first_non_empty(item.get("last_seen")) or "",
        }

        current = best_by_pair.get(pair)
        if current is None or _match_row_sort_key(row) > _match_row_sort_key(current):
            best_by_pair[pair] = row

    items = []
    for pair in sorted(best_by_pair):
        row = dict(best_by_pair[pair])
        row.pop("_last_seen", None)
        items.append(row)
    return JsonItems(items=items).to_payload()


def _normalize_reference_product(product: dict[str, Any]) -> dict[str, Any] | None:
    if "properties" not in product:
        product_id = _first_non_empty(product.get("product_id"))
        if not product_id:
            return None
        return {
            "product_id": product_id,
            "product_name": _first_non_empty(product.get("product_name")) or "",
            "vendor_id": _first_non_empty(product.get("vendor_id")) or "",
            "vendor_name": _first_non_empty(product.get("vendor_name")) or "",
            "cpe_vendor": _first_non_empty(product.get("cpe_vendor")) or "",
            "cpe_product": _first_non_empty(product.get("cpe_product")) or "",
            "cpe_part": _first_non_empty(product.get("cpe_part")) or "",
        }

    properties = product.get("properties")
    if not isinstance(properties, dict):
        return None

    product_id = _first_non_empty(product.get("entity_id"), properties.get("product_id"))
    if not product_id:
        return None

    vendor_name = _first_non_empty(properties.get("vendor_name")) or ""
    cpe_vendor = _first_non_empty(properties.get("cpe_vendor")) or ""
    return {
        "product_id": product_id,
        "product_name": _first_non_empty(properties.get("product_name")) or "",
        "vendor_id": _first_non_empty(properties.get("vendor_id")) or "",
        "vendor_name": vendor_name,
        "cpe_vendor": cpe_vendor,
        "cpe_product": _first_non_empty(properties.get("cpe_product")) or "",
        "cpe_part": _first_non_empty(properties.get("cpe_part")) or "",
    }


def _score_product_match(inventory_row: dict[str, Any], product_row: dict[str, Any]) -> float:
    inventory_name = _normalize_name(inventory_row.get("software_name"))
    inventory_vendor = _normalize_vendor(inventory_row.get("vendor"))
    if not inventory_name:
        return 0.0

    vendor_candidates = [
        _normalize_vendor(product_row.get("vendor_name")),
        _normalize_vendor(product_row.get("cpe_vendor")),
    ]
    vendor_candidates = [candidate for candidate in vendor_candidates if candidate]
    vendor_strength = 0.0
    if inventory_vendor:
        vendor_strength = max(
            (_text_similarity(inventory_vendor, candidate) for candidate in vendor_candidates),
            default=0.0,
        )
        if vendor_strength < 0.4:
            return 0.0
    else:
        vendor_strength = 1.0

    reference_names = [
        _normalize_name(product_row.get("product_name")),
        _normalize_name(product_row.get("cpe_product")),
    ]
    name_strength = max(
        (_text_similarity(inventory_name, candidate) for candidate in reference_names if candidate),
        default=0.0,
    )
    if name_strength < 0.45:
        return 0.0

    score = 0.6 * name_strength + 0.4 * vendor_strength
    if any(
        _is_contained_name(inventory_name, candidate)
        for candidate in reference_names
        if candidate
    ):
        score = max(score, 0.85 if vendor_strength >= 0.8 else 0.75)
    if any(inventory_name == candidate for candidate in reference_names if candidate):
        score = max(score, 0.95)
    return min(score, 0.99)


def _normalize_vendor(value: Any) -> str:
    return _normalize_text(value, drop_generic=True)


def _normalize_name(value: Any) -> str:
    return _normalize_text(value, drop_generic=False)


def _first_non_empty(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _normalize_text(value: Any, *, drop_generic: bool) -> str:
    text = _first_non_empty(value)
    if text is None:
        return ""
    normalized = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    if not normalized:
        return ""
    tokens = [token for token in normalized.split() if token]
    if drop_generic:
        tokens = [token for token in tokens if token not in _GENERIC_TOKENS]
    return " ".join(tokens)


def _text_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0

    left_tokens = set(left.split())
    right_tokens = set(right.split())
    overlap = len(left_tokens & right_tokens) / max(len(left_tokens), len(right_tokens))
    sequence = SequenceMatcher(None, left, right).ratio()
    if left_tokens <= right_tokens or right_tokens <= left_tokens:
        return max(overlap, sequence, 0.92)
    return max(overlap, sequence)


def _is_contained_name(left: str, right: str) -> bool:
    return bool(left and right and (left in right or right in left))


def _score_to_verdict(score: float) -> str:
    if score >= 0.8:
        return "support"
    if score >= 0.5:
        return "unsure"
    return "contradict"


def _match_basis(
    inventory_row: dict[str, Any],
    product_row: dict[str, Any],
    score: float,
) -> str:
    software_name = _first_non_empty(inventory_row.get("software_name")) or "observed software"
    product_name = _first_non_empty(product_row.get("product_name")) or "reference product"
    vendor = _first_non_empty(inventory_row.get("vendor")) or "unknown vendor"
    return (
        f"{software_name} from {vendor} matched {product_name} "
        f"with confidence {score:.2f}"
    )


def _inventory_source_record_id(inventory_row: dict[str, Any]) -> str:
    asset_id = _first_non_empty(inventory_row.get("asset_id")) or "unknown-asset"
    software_name = _first_non_empty(inventory_row.get("software_name")) or "unknown-software"
    version = _first_non_empty(inventory_row.get("version")) or "unknown-version"
    return f"{asset_id}:{software_name}:{version}"


def _reference_product_sort_key(product: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(product.get("product_id", "")),
        str(product.get("vendor_id", "")),
        str(product.get("product_name", "")),
        str(product.get("cpe_product", "")),
    )


def _match_row_sort_key(row: dict[str, Any]) -> tuple[float, str, str]:
    return (
        float(row.get("match_confidence", 0.0)),
        str(row.get("_last_seen", "")),
        str(row.get("installed_version", "")),
    )
