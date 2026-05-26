"""Public KEV reference normalization providers."""

from __future__ import annotations

import json
import re
from ast import literal_eval
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
        nvd_vulnerability = cpe_products[0] if cpe_products else {}

        vuln_base = {
            "cve_id": cve_id,
            "vulnerability_name": _first_non_empty(
                kev_row.get("vulnerabilityName"),
                kev_row.get("vulnerability_name"),
                kev_row.get("vulnerabilityname"),
                nvd_vulnerability.get("vulnerability_name"),
            ),
            "description": _first_non_empty(
                kev_row.get("shortDescription"),
                kev_row.get("short_description"),
                kev_row.get("shortdescription"),
                enriched.get("Description"),
                enriched.get("description"),
                nvd_vulnerability.get("description"),
            ),
            "date_added_to_kev": _first_non_empty(
                kev_row.get("dateAdded"),
                kev_row.get("date_added"),
                kev_row.get("dateadded"),
            ),
            "cvss_score": _parse_float(
                _first_non_empty(
                    enriched.get("CVSS3"),
                    enriched.get("cvss3"),
                    nvd_vulnerability.get("cvss_score"),
                )
            ),
            "cvss_severity": _first_non_empty(
                enriched.get("CVSS Severity"),
                enriched.get("cvss_severity"),
                nvd_vulnerability.get("cvss_severity"),
            ),
            "epss_score": _parse_float(
                _first_non_empty(enriched.get("EPSS"), enriched.get("epss"))
            ),
            "epss_percentile": _parse_float(
                _first_non_empty(
                    enriched.get("EPSS Percentile"),
                    enriched.get("epss_percentile"),
                    enriched.get("epss percentile"),
                )
            ),
            "kev_due_date": _first_non_empty(
                kev_row.get("dueDate"),
                kev_row.get("due_date"),
                kev_row.get("duedate"),
            ),
            "required_action": _first_non_empty(
                kev_row.get("requiredAction"),
                kev_row.get("required_action"),
                kev_row.get("requiredaction"),
                nvd_vulnerability.get("required_action"),
            ),
            "known_ransomware_use": _first_non_empty(
                kev_row.get("knownRansomwareCampaignUse"),
                kev_row.get("known_ransomware_campaign_use"),
                kev_row.get("knownransomwarecampaignuse"),
            ),
            "cwes": _parse_cwes(
                _first_non_empty(
                    kev_row.get("cwes"),
                    kev_row.get("CWEs"),
                    kev_row.get("CWE"),
                ),
                nvd_vulnerability.get("cwes"),
            ),
        }

        if cpe_products:
            for product in cpe_products:
                merged = {**vuln_base, **product}
                for key, value in vuln_base.items():
                    if merged.get(key) in (None, "", []) and value not in (None, "", []):
                        merged[key] = value
                items.append(merged)
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
            "source": "cisa_kev",
            "source_record_id": cve_id,
            "default_status": None,
            "vulnerable": True,
            "version_logic": "fallback_product_match_from_cisa_kev",
            "source_last_modified_at": None,
            "evidence_refs": [
                {
                    "source": "cisa_kev",
                    "source_record_id": cve_id,
                }
            ],
            "rationale": (
                "CISA KEV catalog lists this vendor/product; "
                "no NVD CPE match data was available."
            ),
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
        cve = _object_value(entry.get("cve", {}))
        if not isinstance(cve, dict):
            continue
        cve_id = cve.get("id", "")
        if not cve_id:
            continue
        vulnerability_metadata = _extract_nvd_vulnerability_metadata(cve)

        product_versions: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        product_evidence: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        configurations = _object_value(entry.get("configurations"))
        if not isinstance(configurations, list):
            configurations = _object_value(cve.get("configurations", []))
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
                    product_evidence[key].append(_nvd_match_evidence(cve_id, match))

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
                "source": "nvd",
                "source_record_id": cve_id,
                "default_status": cve.get("vulnStatus"),
                "vulnerable": True,
                "version_logic": _version_logic_summary(versions),
                "source_last_modified_at": cve.get("lastModified"),
                "evidence_refs": product_evidence.get((cpe_part, cpe_vendor, cpe_product), []),
                "rationale": (
                    "NVD vulnerable CPE match data maps this KEV vulnerability "
                    f"to {cpe_vendor}/{cpe_product}."
                ),
                **vulnerability_metadata,
            })

        result[cve_id] = products

    return result


def _object_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return literal_eval(text)
    except (SyntaxError, ValueError):
        return value


def _extract_nvd_vulnerability_metadata(cve: dict[str, Any]) -> dict[str, Any]:
    metric = _best_cvss_metric(cve.get("metrics"))
    return {
        "vulnerability_name": _first_non_empty(cve.get("cisaVulnerabilityName")),
        "description": _english_description(cve.get("descriptions")),
        "required_action": _first_non_empty(cve.get("cisaRequiredAction")),
        "cvss_score": _parse_float(metric.get("baseScore")) if metric else None,
        "cvss_severity": _first_non_empty(metric.get("baseSeverity")) if metric else None,
        "cwes": _extract_cwes(cve.get("weaknesses")),
    }


def _best_cvss_metric(metrics: Any) -> dict[str, Any]:
    if not isinstance(metrics, dict):
        return {}
    for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        metric_rows = metrics.get(key)
        if isinstance(metric_rows, list) and metric_rows:
            first = metric_rows[0]
            if isinstance(first, dict):
                cvss_data = first.get("cvssData")
                if isinstance(cvss_data, dict):
                    return {
                        "baseScore": cvss_data.get("baseScore"),
                        "baseSeverity": first.get("baseSeverity")
                        or cvss_data.get("baseSeverity"),
                    }
    return {}


def _english_description(descriptions: Any) -> str | None:
    if not isinstance(descriptions, list):
        return None
    for row in descriptions:
        if isinstance(row, dict) and row.get("lang") == "en":
            return _first_non_empty(row.get("value"))
    return None


def _extract_cwes(weaknesses: Any) -> list[str]:
    if not isinstance(weaknesses, list):
        return []
    cwes: set[str] = set()
    for weakness in weaknesses:
        if not isinstance(weakness, dict):
            continue
        descriptions = weakness.get("description")
        if not isinstance(descriptions, list):
            continue
        for description in descriptions:
            if not isinstance(description, dict):
                continue
            value = _first_non_empty(description.get("value"))
            if value:
                cwes.add(value)
    return sorted(cwes)


def _parse_cwes(raw: str | None, fallback: Any) -> list[str]:
    if raw:
        values = [
            value.strip()
            for value in re.split(r"[,;|]", raw)
            if value.strip()
        ]
        if values:
            return sorted(dict.fromkeys(values))
    if isinstance(fallback, list):
        return [str(value) for value in fallback if str(value).strip()]
    return []


def _nvd_match_evidence(cve_id: str, match: dict[str, Any]) -> dict[str, Any]:
    evidence = {
        "source": "nvd_cpe_match",
        "source_record_id": cve_id,
        "criteria": match.get("criteria"),
    }
    match_criteria_id = _first_non_empty(match.get("matchCriteriaId"))
    if match_criteria_id:
        evidence["match_criteria_id"] = match_criteria_id
    return evidence


def _version_logic_summary(versions: list[dict[str, Any]]) -> str:
    if not versions:
        return "NVD CPE match applies to all listed product versions."
    return "NVD CPE match version ranges aggregated by product."


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
