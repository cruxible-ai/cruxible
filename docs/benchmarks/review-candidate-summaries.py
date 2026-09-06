"""Benchmark a complete review index on an isolated copy, with exact output parity.

Run with the repository source packages on PYTHONPATH and pass --instance-root
for a disposable instance copy. This measures index construction, excluding
instance recovery, note rendering, and publication. It needs no signing key,
does not recover accepted authority, and never updates Git refs or notes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from unittest.mock import patch

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.types import PlaybillDescriptor
from cruxible_core.playbill import candidate_review_summary as cache
from cruxible_core.playbill.git import GitLedger
from cruxible_core.playbill.proposal_evidence import ProposalEvidenceStore
from cruxible_core.playbill.proposal_note_projection import ProposalNoteIndex


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.instance_root.resolve(strict=True)
    descriptor = PlaybillDescriptor.model_validate_json((root / "instance.json").read_bytes())
    store = ProposalEvidenceStore(root / descriptor.storage.exhaust)
    ledger = GitLedger(
        root / descriptor.storage.ledger,
        signing_key_path=root / "unused-benchmark-signing-key",
        allowed_signers_path=root / "unused-benchmark-signers",
    )
    maximum = cache.MAX_ENTRIES
    baseline = None
    results = []
    try:
        for label in (
            "cache_disabled",
            "cold_cache",
            "warm_cache_1",
            "warm_cache_2",
            "warm_cache_3",
        ):
            if label in ("cache_disabled", "cold_cache"):
                cache._cache.clear()
            cache.MAX_ENTRIES = 0 if label == "cache_disabled" else maximum
            with patch.object(
                cache, "parse_candidate_evidence", wraps=cache.parse_candidate_evidence
            ) as parse:
                started = time.perf_counter()
                index = ProposalNoteIndex.build(store, ledger)
                elapsed = time.perf_counter() - started
            content = {
                "review_oids": index.review_oids,
                "groups": {oid: sorted(ids) for oid, ids in index.proposal_ids_by_oid.items()},
                "notes": {
                    oid: {
                        kind: hashlib.sha256(raw).hexdigest()
                        for kind, raw in index.note_bytes(oid).items()
                    }
                    for oid in index.proposal_ids_by_oid
                },
            }
            fingerprint = hashlib.sha256(canonical_bytes(content)).hexdigest()
            baseline = fingerprint if baseline is None else baseline
            if fingerprint != baseline:
                raise AssertionError("review OIDs, groups, or note bytes changed between modes")
            result = {
                "stage": label,
                "seconds": elapsed,
                "admissions": len(index.admissions),
                "groups": len(index.proposal_ids_by_oid),
                "candidate_decodes": parse.call_count,
                "retained_entries": len(cache._cache),
                "accounted_bytes": sum(entry.weight for entry in cache._cache.values()),
                "exact_projection_fingerprint": fingerprint,
            }
            results.append(result)
            print(json.dumps(result), flush=True)
    finally:
        cache.MAX_ENTRIES = maximum
        cache._cache.clear()
    args.output.write_text(
        json.dumps(
            {
                "scope": "complete ProposalNoteIndex.build; recovery and note rendering excluded",
                "fixture": "isolated instance copy",
                "candidate_file_bytes": sum(
                    path.stat().st_size for path in store.candidates.glob("*.json")
                ),
                "cache_max_entries": maximum,
                "cache_max_accounted_bytes": cache.MAX_RETAINED_BYTES,
                "results": results,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
