"""Measure an unchanged disposable intent history; never target a live instance.

Run from the checkout being measured with its src and client src on PYTHONPATH.
--intent-json is a saved SDK intent response (no credentials). The copied exhaust
must contain that pending intent. No private signing custody is required. Every
run clears process-local history proofs, times cold and warm lookup separately,
and verifies the exact expected intent. Filesystem caches are not flushed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from pathlib import Path

from cruxible_core.playbill.authoring.store import (
    AuthoringIntentStore,
    _reset_authoring_history_memo,
)


def inventory(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.glob("AIT-*/events/*.json"))
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exhaust", type=Path, required=True)
    parser.add_argument("--intent-json", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    expected = json.loads(args.intent_json.read_bytes())["intent"]
    store = AuthoringIntentStore(args.exhaust, read_only=True)
    before = inventory(store.root)
    runs = []
    for _ in range(args.runs):
        _reset_authoring_history_memo()
        row = {}
        for phase in ("cold", "warm"):
            start = time.perf_counter()
            found = store._active_by_fingerprint(
                expected["create_fingerprint"], actor_id=expected["actor_id"]
            )
            row[phase] = time.perf_counter() - start
            assert found is not None
            assert found.model_dump(mode="json") == expected
        runs.append(row)
        print(json.dumps(row), flush=True)
    assert inventory(store.root) == before, "benchmark history changed"
    result = {
        "scope": "unprofiled copied-history lookup; process memos reset; OS cache not flushed",
        "streams": len(store._intent_directories()),
        "events": len(before),
        "history_inventory_digest": hashlib.sha256(
            json.dumps(before, sort_keys=True).encode()
        ).hexdigest(),
        "exact_intent_verified": True,
        "runs": runs,
        "median_seconds": {
            phase: statistics.median(row[phase] for row in runs) for phase in ("cold", "warm")
        },
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
