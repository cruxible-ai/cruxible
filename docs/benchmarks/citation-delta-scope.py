"""Compare citation maintenance against unrelated-world growth.

Runs each source checkout in a fresh interpreter. The measured phase begins
with immutable prior citation facts/index; no CAS or ledger work is simulated
inside the timer. One retired citation owner is removed from a fixed three-use
group. Other capture/source/version groups are unrelated. Parent construction,
full row export, counters and parity checks are outside latency samples.
Use write-loop-served.py separately for actual SDK/HTTP submit/accept latency.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch


def worker(args):
    repo = args.repo.resolve()
    sys.path[:0] = [str(repo / "src"), str(repo / "packages/cruxible-client/src"), str(repo)]
    from cruxible_client.contracts.canonical import canonical_bytes
    from cruxible_client.contracts.projection_extensions import ProjectionFact
    from cruxible_core.playbill import citation_relations as module

    def fixture(n):
        rows = []
        for i in range(n):
            group = 0 if i < 3 else i
            value = {
                "capture_contract_digest": {"$digest": "sha256:" + "a" * 64},
                "capture_digest": {"$digest": f"sha256:{group:064x}"},
                "citation_id": f"sha256:{i:064x}",
                "claim_artifact_digest": {"$digest": f"sha256:{i:064x}"},
                "claim_identity": f"Claim:CLM-{i:032x}",
                "claim_lifecycle": "retired" if i == 0 else "live",
                "claim_path": f"claims/CLM-{i:032x}.json",
                "commitment": {},
                "origin": "authored",
                "role": "support",
                "source": {
                    "tag": "playbill-external-source-reference-v1",
                    "kind": "external",
                    "source_identity": f"source-{group}",
                    "producer_binding_digest": "sha256:" + "c" * 64,
                    "coordinate_type": "foreign-source-snapshot-v1",
                    "coordinate": {"version": 1},
                    "selector_type": "foreign-source-span-v1",
                    "selector": {"start_byte": 0, "end_byte": 10},
                    "replayability": "exact",
                },
            }
            rows.append(
                ProjectionFact(
                    schema_id=module.RELATION_USE_SCHEMA,
                    schema_version=1,
                    subject_identity=module.capture_relation_subject(
                        value["capture_digest"]["$digest"]
                    ),
                    fact_key=module._fact_key("use", value["citation_id"], value["claim_identity"]),
                    value=value,
                )
            )
        return tuple(rows)

    def fingerprint(facts):
        values = sorted(
            (f.model_dump(mode="json") for f in facts),
            key=lambda f: (
                f["schema_id"],
                f["schema_version"],
                f["subject_identity"],
                f["fact_key"],
            ),
        )
        return hashlib.sha256(canonical_bytes(values)).hexdigest()

    results = []
    for n in (100, 1000, 10000):
        prior_uses = fixture(n)
        # Exact cold conflict rows for the fixed three-use capture group. These
        # are fixture inputs, outside measurement; the indexed arm also verifies
        # its reconstructed parent matches them byte for byte.
        conflicts = tuple(
            ProjectionFact(
                schema_id=module.RELATION_RETIRED_CONFLICT_SCHEMA,
                schema_version=1,
                subject_identity="claim-cites-retired",
                fact_key=module._fact_key(
                    "conflict", live.value["claim_identity"], "sha256:" + "0" * 64
                ),
                value={
                    "live_capture_digest": live.value["capture_digest"],
                    "live_citation_id": live.value["citation_id"],
                    "live_claim_artifact_digest": live.value["claim_artifact_digest"],
                    "live_claim_identity": live.value["claim_identity"],
                    "relation_key": "capture:sha256:" + "0" * 64,
                    "relation_kind": "capture",
                    "retired_citation_count": 1,
                    "retired_claim_count": 1,
                    "retired_citation_witnesses": [prior_uses[0].value["citation_id"]],
                    "retired_claim_witnesses": [prior_uses[0].value["claim_identity"]],
                },
            )
            for live in prior_uses[1:3]
        )
        prior = module.build_citation_relation_facts(
            {},
            bodies=None,
            previous_use_facts=prior_uses,
            previous_conflict_facts=conflicts,
            changed_claim_paths=frozenset(),
        )
        changed = frozenset((str(prior_uses[0].value["claim_path"]),))
        if args.mode == "after":
            from cruxible_core.playbill.citation_index import CitationIndex

            start = time.perf_counter()
            root = CitationIndex.rebuild(prior)
            bootstrap = time.perf_counter() - start
            assert fingerprint(root.facts()) == fingerprint(prior)

            def run():
                return root.advance({}, changed_paths=changed, bodies=None)
        else:
            bootstrap = None

            def run():
                return module.build_citation_relation_facts(
                    {},
                    bodies=None,
                    previous_use_facts=prior_uses,
                    previous_conflict_facts=conflicts,
                    changed_claim_paths=changed,
                )

        samples = []
        for _ in range(5):
            gc.collect()
            start = time.perf_counter()
            result = run()
            samples.append(time.perf_counter() - start)
        if args.mode == "after":
            actual = result[0].facts()
            updates = {"deletes": len(result[1].deletes), "inserts": len(result[1].inserts)}
        else:
            actual = result
            updates = {"deletes": len(prior), "inserts": len(actual)}
        oracle = module.build_citation_relation_facts(
            {}, bodies=None, previous_use_facts=prior_uses[1:], changed_claim_paths=frozenset()
        )
        assert fingerprint(actual) == fingerprint(oracle)
        calls = 0
        original = module._same_version_span_key

        def counted(value):
            nonlocal calls
            calls += 1
            return original(value)

        with patch.object(module, "_same_version_span_key", counted):
            run()
        results.append(
            {
                "population": n,
                "samples_s": samples,
                "median_s": statistics.median(samples),
                "bootstrap_s": bootstrap,
                "span_key_visits": calls,
                "sql_row_mutations": updates,
                "logical_rows_digest": fingerprint(actual),
                "parent_rows_digest": fingerprint(prior),
                "estimated_index_bytes": root.estimated_bytes if args.mode == "after" else None,
            }
        )
    paths = ["citation_relations.py", "citation_index.py", "projection_delta.py"]
    args.output.write_text(
        json.dumps(
            {
                "mode": args.mode,
                "repo": str(repo),
                "head": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=repo, text=True
                ).strip(),
                "source_sha256": {
                    p: hashlib.sha256(
                        (repo / "src/cruxible_core/playbill" / p).read_bytes()
                    ).hexdigest()
                    for p in paths
                    if (repo / "src/cruxible_core/playbill" / p).exists()
                },
                "rows": results,
            },
            indent=2,
        )
        + "\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--after", type=Path)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--mode", choices=["before", "after"])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.mode:
        worker(args)
        return
    reports = {}
    for mode, repo in [("before", args.baseline), ("after", args.after)]:
        output = args.output.with_name(f"{args.output.stem}-{mode}.json")
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--mode",
                mode,
                "--repo",
                str(repo),
                "--output",
                str(output),
            ],
            check=True,
        )
        reports[mode] = json.loads(output.read_text())
    for before, after in zip(reports["before"]["rows"], reports["after"]["rows"], strict=True):
        assert before["logical_rows_digest"] == after["logical_rows_digest"]
        assert before["parent_rows_digest"] == after["parent_rows_digest"]
    args.output.write_text(json.dumps(reports, indent=2) + "\n")
    print(args.output)


if __name__ == "__main__":
    main()
