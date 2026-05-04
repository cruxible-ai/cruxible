"""Public KEV reference normalization providers."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, cast

from cruxible_core.provider.types import ProviderContext

from .common import (
    _first_non_empty,
    _humanize,
    _load_csv_rows,
    _parse_float,
    _parsed_table_rows,
    _require_artifact_root,
    _slugify,
)


def load_public_kev_rows(
    _input_payload: dict[str, Any],
    context: ProviderContext,
) -> dict[str, Any]:
    """Load and normalize public KEV reference rows from a hashed data bundle."""
    bundle_root = _require_artifact_root(context, "load_public_kev_rows")

    kev_rows = _load_csv_rows(bundle_root / "known_exploited_vulnerabilities.csv")
    enriched_by_cve = {
        row.get("CVE", "").strip(): row
        for row in _load_csv_rows(bundle_root / "epss_kev_nvd.csv")
        if row.get("CVE", "").strip()
    }
    nvd_cpe_by_cve = _load_nvd_cpe_data(bundle_root / "nvd_kev_cves.json")

    return _build_public_kev_rows(kev_rows, enriched_by_cve, nvd_cpe_by_cve)


def normalize_public_kev_reference(
    input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    """Normalize parsed public KEV tables into reference graph rows."""
    kev_rows = _parsed_table_rows(input_payload, "known_exploited_vulnerabilities")
    enriched_by_cve = {
        str(row.get("cve", "")).strip(): row
        for row in _parsed_table_rows(input_payload, "epss_kev_nvd")
        if str(row.get("cve", "")).strip()
    }
    nvd_cpe_by_cve = _parse_nvd_cpe_rows(_parsed_table_rows(input_payload, "nvd_kev_cves"))
    return _build_public_kev_rows(kev_rows, enriched_by_cve, nvd_cpe_by_cve)


def _build_public_kev_rows(
    kev_rows: list[dict[str, Any]],
    enriched_by_cve: dict[str, dict[str, Any]],
    nvd_cpe_by_cve: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for kev_row in kev_rows:
        cve_id = _first_non_empty(
            kev_row.get("cveID"),
            kev_row.get("cve_id"),
            kev_row.get("cveid"),
        )
        if not cve_id:
            continue
        enriched = enriched_by_cve.get(cve_id, {})
        cpe_products = nvd_cpe_by_cve.get(cve_id, [])

        vuln_base = {
            "cve_id": cve_id,
            "description": _first_non_empty(
                kev_row.get("shortDescription"),
                kev_row.get("short_description"),
                kev_row.get("shortdescription"),
                enriched.get("Description"),
                enriched.get("description"),
            ),
            "cvss_score": _parse_float(
                _first_non_empty(enriched.get("CVSS3"), enriched.get("cvss3"))
            ),
            "epss_score": _parse_float(
                _first_non_empty(enriched.get("EPSS"), enriched.get("epss"))
            ),
            "kev_due_date": _first_non_empty(
                kev_row.get("dueDate"),
                kev_row.get("due_date"),
                kev_row.get("duedate"),
            ),
            "known_ransomware_use": _first_non_empty(
                kev_row.get("knownRansomwareCampaignUse"),
                kev_row.get("known_ransomware_campaign_use"),
                kev_row.get("knownransomwarecampaignuse"),
            ),
        }

        if cpe_products:
            for product in cpe_products:
                items.append({
                    **vuln_base,
                    **product,
                })
            continue

        vendor_name = _first_non_empty(
            kev_row.get("vendorProject"),
            kev_row.get("vendor_project"),
            kev_row.get("vendorproject"),
            enriched.get("Vendor"),
            enriched.get("vendor"),
        )
        product_name = _first_non_empty(
            kev_row.get("product"),
            enriched.get("Product"),
            enriched.get("product"),
        )
        vendor_id = _slugify(vendor_name or "unknown-vendor")
        items.append({
            **vuln_base,
            "vendor_id": vendor_id,
            "vendor_name": vendor_name or "Unknown Vendor",
            "product_id": _slugify(
                f"{vendor_id}__{product_name or 'unknown-product'}",
            ),
            "product_name": product_name or "Unknown Product",
            "cpe_vendor": None,
            "cpe_product": None,
            "cpe_part": None,
            "affected_versions": [],
            "fixed_version": None,
        })

    return {"items": items}


def _load_nvd_cpe_data(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Parse NVD CVE JSON and extract CPE product + version data."""
    if not path.exists():
        return {}

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return {}
    return _parse_nvd_cpe_rows(raw)


def _parse_nvd_cpe_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}

    for entry in rows:
        cve = entry.get("cve", {})
        if not isinstance(cve, dict):
            continue
        cve_id = cve.get("id", "")
        if not cve_id:
            continue

        product_versions: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        configurations = entry.get("configurations")
        if not isinstance(configurations, list):
            configurations = cve.get("configurations", [])
        for config in configurations:
            if not isinstance(config, dict):
                continue
            for node in config.get("nodes", []):
                if not isinstance(node, dict):
                    continue
                for match in node.get("cpeMatch", []):
                    if not isinstance(match, dict):
                        continue
                    if not match.get("vulnerable", False):
                        continue
                    parsed = _parse_cpe_criteria(match.get("criteria", ""))
                    if parsed is None:
                        continue
                    cpe_part, cpe_vendor, cpe_product = parsed
                    version_range = _extract_version_range(match)
                    key = (cpe_part, cpe_vendor, cpe_product)
                    if key not in product_versions:
                        product_versions[key] = []
                    if version_range is not None:
                        product_versions[key].append(version_range)

        if not product_versions:
            continue

        products: list[dict[str, Any]] = []
        for (cpe_part, cpe_vendor, cpe_product), versions in product_versions.items():
            vendor_id = _slugify(cpe_vendor)
            product_id = _slugify(f"{cpe_vendor}__{cpe_product}")
            products.append({
                "vendor_id": vendor_id,
                "vendor_name": _humanize(cpe_vendor),
                "product_id": product_id,
                "product_name": _humanize(cpe_product),
                "cpe_vendor": cpe_vendor,
                "cpe_product": cpe_product,
                "cpe_part": cpe_part,
                "affected_versions": versions,
                "fixed_version": _pick_latest_fixed_version(versions),
            })

        result[cve_id] = products

    return result


def _parse_cpe_criteria(criteria: str) -> tuple[str, str, str] | None:
    parts = criteria.split(":")
    if len(parts) < 5 or parts[0] != "cpe" or parts[1] != "2.3":
        return None
    return parts[2], parts[3], parts[4]


def _extract_specific_version(criteria: str) -> str | None:
    parts = criteria.split(":")
    if len(parts) < 6:
        return None
    version = parts[5]
    if version in ("*", "-", ""):
        return None
    return version


def _extract_version_range(match: dict[str, Any]) -> dict[str, Any] | None:
    version_range: dict[str, Any] = {}
    for field in (
        "versionStartIncluding",
        "versionStartExcluding",
        "versionEndIncluding",
        "versionEndExcluding",
    ):
        value = match.get(field)
        if value is not None:
            version_range[re.sub(r"([A-Z])", r"_\1", field).lower()] = value

    if not version_range:
        specific = _extract_specific_version(match.get("criteria", ""))
        if specific:
            version_range["version_exact"] = specific
        else:
            return None

    end_excl = match.get("versionEndExcluding")
    if end_excl:
        version_range["fixed_version"] = end_excl
    return version_range


def _pick_latest_fixed_version(versions: list[dict[str, Any]]) -> str | None:
    fixed_versions = [value["fixed_version"] for value in versions if value.get("fixed_version")]
    if not fixed_versions:
        return None
    return cast(str, max(fixed_versions))
