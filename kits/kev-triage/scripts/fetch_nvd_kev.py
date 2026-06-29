"""Fetch NVD CVE data for KEV-listed vulnerabilities with CPE match configurations.

Downloads all CVEs flagged in the CISA KEV catalog via the NVD 2.0 API's
hasKev filter, paginates through the full result set, and writes the raw
response to a local JSON file for use as a hashed Cruxible artifact.

Usage:
    uv run python kits/kev-triage/scripts/fetch_nvd_kev.py

Output:
    kits/kev-triage/data/nvd_kev_cves.json

Environment:
    NVD_API_KEY  Optional. Raises rate limit from 5 req/30s to 50 req/30s.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

NVD_CVE_API = "https://services.nvd.nist.gov/rest/json/cves/2.0"
RESULTS_PER_PAGE = 2000
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "nvd_kev_cves.json"

# NVD rate limits: 50 req/30s with key, 5 req/30s without.
DELAY_WITH_KEY = 0.6
DELAY_WITHOUT_KEY = 6.0


def fetch_all_kev_cves(api_key: str | None = None) -> list[dict]:
    """Paginate through NVD 2.0 CVE API with hasKev filter."""
    headers = {}
    if api_key:
        headers["apiKey"] = api_key
    delay = DELAY_WITH_KEY if api_key else DELAY_WITHOUT_KEY

    all_vulnerabilities: list[dict] = []
    start_index = 0

    with httpx.Client(timeout=60) as client:
        while True:
            params = {
                "hasKev": "",
                "resultsPerPage": RESULTS_PER_PAGE,
                "startIndex": start_index,
            }
            print(
                f"Fetching startIndex={start_index} "
                f"(collected {len(all_vulnerabilities)} so far)..."
            )

            response = client.get(
                NVD_CVE_API, params=params, headers=headers,
            )
            response.raise_for_status()
            data = response.json()

            vulnerabilities = data.get("vulnerabilities", [])
            all_vulnerabilities.extend(vulnerabilities)

            total_results = data.get("totalResults", 0)
            print(
                f"  Got {len(vulnerabilities)} results "
                f"({len(all_vulnerabilities)}/{total_results} total)"
            )

            if len(all_vulnerabilities) >= total_results:
                break

            start_index += RESULTS_PER_PAGE
            time.sleep(delay)

    return all_vulnerabilities


def main() -> None:
    api_key = os.environ.get("NVD_API_KEY")
    if api_key:
        print("Using NVD API key (50 req/30s rate limit)")
    else:
        print(
            "No NVD_API_KEY set — using public rate limit (5 req/30s). "
            "Set NVD_API_KEY for faster downloads."
        )

    vulnerabilities = fetch_all_kev_cves(api_key)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(vulnerabilities, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    print(f"\nWrote {len(vulnerabilities)} CVEs to {OUTPUT_PATH}")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPStatusError as exc:
        print(f"HTTP error: {exc.response.status_code} — {exc.response.text[:500]}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
