#!/usr/bin/env python3
"""Acquisition tool: pull inventory positions from an internal inventory API.

This script lives OUTSIDE the kit's providers on purpose. Acquisition is not
workflow logic: fetching is nondeterministic, so it happens out here, and the
result crosses into Cruxible as reviewed rows — the output matches the
``InventoryFetchResults`` contract, so it pipes straight into the
``sync_inventory_positions`` apply path.

Usage:
    python scripts/fetch_inventory.py --base-url https://inventory.internal/positions \
        --item-id CMP-0142 --location-id LOC-SEA -o positions.json

Auth: whatever your inventory API needs — add headers/tokens here, in your
copy of this script. Provider code never carries API credentials.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import urlopen


def fetch_inventory(
    base_url: str,
    *,
    item_ids: list[str],
    item_types: list[str],
    location_ids: list[str],
    as_of: str | None,
    timeout: float,
) -> dict[str, Any]:
    parsed_base = urlsplit(base_url)
    if parsed_base.scheme not in {"http", "https"}:
        raise ValueError(f"Inventory base_url scheme must be http or https: {base_url}")
    query: dict[str, str] = {}
    for key, values in (
        ("item_ids", item_ids),
        ("item_types", item_types),
        ("location_ids", location_ids),
    ):
        if values:
            query[key] = ",".join(str(value) for value in values)
    if as_of:
        query["as_of"] = as_of
    url = _url_with_query(base_url, query)
    try:
        with urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except TimeoutError as exc:
        raise TimeoutError(f"Inventory GET timed out after {timeout:g}s: {base_url}") from exc
    except HTTPError as exc:
        raise RuntimeError(f"Inventory GET failed with HTTP {exc.code}: {base_url}") from exc
    except URLError as exc:
        raise RuntimeError(f"Inventory GET failed: {exc.reason}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Inventory API response must be a JSON object")
    return payload


def _url_with_query(base_url: str, query: dict[str, str]) -> str:
    if not query:
        return base_url
    parsed = urlsplit(base_url)
    existing = parsed.query
    rendered = urlencode(query)
    combined = f"{existing}&{rendered}" if existing else rendered
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, combined, parsed.fragment))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--item-id", action="append", default=[], dest="item_ids")
    parser.add_argument("--item-type", action="append", default=[], dest="item_types")
    parser.add_argument("--location-id", action="append", default=[], dest="location_ids")
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("-o", "--output", type=Path, default=None,
                        help="Write InventoryFetchResults JSON here (default: stdout)")
    args = parser.parse_args()
    payload = fetch_inventory(
        args.base_url,
        item_ids=args.item_ids,
        item_types=args.item_types,
        location_ids=args.location_ids,
        as_of=args.as_of,
        timeout=args.timeout,
    )
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        args.output.write_text(rendered)
        sys.stderr.write(
            f"wrote {args.output} ({len(payload.get('inventory_positions', []))} positions)\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
